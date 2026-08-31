import json

import httpx
import pytest
from langfuse import Evaluation
from langfuse.api import ChatMessage, Prompt_Chat, Prompt_Text

from benchkit.errors import EnvError, UsageError
from benchkit.experiment import (
    ChatReply,
    Gateway,
    build_messages,
    build_task,
    composite_evaluations,
    contract_valid,
    default_run_name,
    extract_json,
    fetch_prompt,
    normalize_usage,
    parse_output,
    resolve_structured_options,
    response_format_for,
    run_experiment,
)
from benchkit.manifest import PromptSpec

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}, "additionalProperties": False}


# --- parsing ---------------------------------------------------------------


def test_parse_plain_json():
    assert parse_output('{"answer": "Paris"}', SCHEMA) == {"answer": "Paris"}


def test_parse_fenced_json():
    text = 'Sure! Here you go:\n```json\n{"answer": "Paris"}\n```\nthanks'
    assert parse_output(text, SCHEMA) == {"answer": "Paris"}
    assert extract_json("```\n{\"a\": 1}\n```") == {"a": 1}
    assert extract_json('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}


def test_parse_invalid_json():
    out = parse_output("I don't know", SCHEMA)
    assert out == {"_raw": "I don't know", "_error": out["_error"]} and out["_error"].startswith("parse: invalid JSON")
    out = parse_output("```json\n{broken\n```", SCHEMA)
    assert out["_error"].startswith("parse:") and "_schema_errors" not in out


def test_parse_schema_invalid_keeps_fields():
    out = parse_output('{"answer": 3}', SCHEMA)
    assert out["answer"] == 3  # parsed fields preserved
    assert out["_error"].startswith("schema: answer:") and "_raw" not in out
    assert out["_schema_errors"] == ["answer: 3 is not of type 'string'"]
    enum_schema = {"type": "object", "required": ["answer", "kind"], "properties": {"answer": {"type": "string"}, "kind": {"enum": ["a", "b"]}}}
    out = parse_output('{"answer": "Paris", "kind": "invented", "extra": 1}', enum_schema)
    assert out["answer"] == "Paris" and out["kind"] == "invented" and out["extra"] == 1
    assert out["_schema_errors"] == ["kind: 'invented' is not one of ['a', 'b']"]
    many = {"type": "object", "properties": {k: {"type": "string"} for k in "abcde"}}
    out = parse_output('{"a":1,"b":2,"c":3,"d":4,"e":5}', many)
    assert len(out["_schema_errors"]) == 5 and "(+2 more)" in out["_error"] and out["_error"].count(";") == 2
    # non-object JSON cannot be annotated in place: wrapped with _parsed
    out = parse_output("[1, 2]", SCHEMA)
    assert out["_parsed"] == [1, 2] and out["_raw"] == "[1, 2]" and out["_error"].startswith("schema: <root>")


def test_parse_without_schema_still_parses():
    assert parse_output('{"anything": 1}', None) == {"anything": 1}


# --- benchkit evaluations ----------------------------------------------------


def test_contract_valid():
    ok = contract_valid(output={"answer": "x"})
    assert ok.name == "contract_valid" and ok.value is True and ok.data_type == "BOOLEAN"
    bad = contract_valid(output={"_raw": "…", "_error": "invalid JSON: x"})
    assert bad.value is False and bad.comment == "invalid JSON: x"
    assert contract_valid(output="plain text").value is False
    assert contract_valid(output=None).value is False


def _ev(name, value, dt="BOOLEAN"):
    return Evaluation(name=name, value=value, data_type=dt)


def test_composite_protocol_pass_true():
    evs = [_ev("contract_valid", True), _ev("answer_matches", True), _ev("confident", False), _ev("len", 3.0, "NUMERIC")]
    out = {e.name: e for e in composite_evaluations(evaluations=evs, expected_output={"rubric": {"critical": ["answer_matches"]}})}
    assert out["protocol_pass"].value is True and out["protocol_pass"].data_type == "BOOLEAN"
    assert out["diagnostic_score"].value == pytest.approx(2 / 3)
    assert out["diagnostic_score"].data_type == "NUMERIC"
    assert out["diagnostic_score"].metadata == {"passed": 2, "applicable": 3}


def test_composite_protocol_pass_false_and_missing():
    evs = [_ev("contract_valid", False), _ev("answer_matches", False)]
    out = {e.name: e for e in composite_evaluations(evaluations=evs, expected_output={"rubric": {"critical": ["answer_matches", "confident"]}})}
    assert out["protocol_pass"].value is False
    assert "missing evaluations: confident" in out["protocol_pass"].comment
    assert "failed: answer_matches" in out["protocol_pass"].comment
    assert out["diagnostic_score"].value == 0.0


def test_composite_without_rubric():
    names = [e.name for e in composite_evaluations(evaluations=[_ev("a", True)], expected_output={"answer": "x"})]
    assert names == ["diagnostic_score"]
    names = [e.name for e in composite_evaluations(evaluations=[], expected_output=None)]
    assert names == ["diagnostic_score"]


# --- prompts -----------------------------------------------------------------


class FakePrompts:
    def __init__(self, prompts):
        self.prompts = prompts
        self.calls = []

    def get(self, prompt_name, **kw):
        self.calls.append((prompt_name, kw))
        if prompt_name not in self.prompts:
            raise RuntimeError("404 not found")
        return self.prompts[prompt_name]


class FakeApi:
    def __init__(self, prompts):
        self.prompts = FakePrompts(prompts)


class FakeClient:
    def __init__(self, prompts=None, items=None):
        self.api = FakeApi(prompts or {})
        self._items = items or []
        self.runs = []
        self.observations = []
        self.flushed = False

    def start_observation(self, **kw):
        rec = {"start": kw, "updates": [], "ended": False}
        self.observations.append(rec)

        class Obs:
            def update(self, **u):
                rec["updates"].append(u)
                return self

            def end(self):
                rec["ended"] = True

        return Obs()

    def get_dataset(self, name, version=None):
        client = self

        class DS:
            items = client._items

        return DS()

    def run_experiment(self, **kw):
        self.runs.append(kw)
        # drive the task + evaluators the way the SDK would, minimally
        results = []
        for item in kw["data"]:
            out = kw["task"](item=item)
            evs = []
            for ev in kw["evaluators"]:
                r = ev(input=item.input, output=out, expected_output=item.expected_output, metadata=item.metadata)
                evs.extend(r if isinstance(r, list) else [r])
            evs.extend(kw["composite_evaluator"](input=item.input, output=out, expected_output=item.expected_output, metadata=item.metadata, evaluations=evs))
            results.append((item, out, evs))
        return results

    def flush(self):
        self.flushed = True


def text_prompt(name="toy-prompt", content="You answer. Q: {{question}}", version=3):
    return Prompt_Text(name=name, version=version, prompt=content, config={}, labels=["production"], tags=[], type="text")


def chat_prompt(name="toy-prompt-structured", version=2):
    return Prompt_Chat(
        name=name,
        version=version,
        prompt=[ChatMessage(role="system", content="Answer {{question}} as JSON"), ChatMessage(role="user", content="{{question}}")],
        config={},
        labels=["production"],
        tags=[],
        type="chat",
    )


def test_fetch_prompt_text_and_chat():
    client = FakeClient({"toy-prompt": text_prompt(), "chat": chat_prompt("chat")})
    p = fetch_prompt(client, PromptSpec(logical="plain", name="toy-prompt", label="production"))
    assert p.type == "text" and p.version == 3 and p.variables == ["question"]
    assert client.api.prompts.calls[-1] == ("toy-prompt", {"label": "production"})
    c = fetch_prompt(client, PromptSpec(logical="c", name="chat", version=2))
    assert c.type == "chat" and client.api.prompts.calls[-1] == ("chat", {"version": 2})
    with pytest.raises(EnvError, match="cannot fetch prompt"):
        fetch_prompt(client, PromptSpec(logical="x", name="missing"))


def test_build_messages_text_and_chat():
    client = FakeClient({"t": text_prompt("t"), "c": chat_prompt("c")})
    t = fetch_prompt(client, PromptSpec(logical="t", name="t"))
    msgs = build_messages(t, {"question": "Why?", "extra": {"k": 1}})
    assert msgs == [{"role": "system", "content": "You answer. Q: Why?"}]
    msgs = build_messages(t, {"question": "Why?", "messages": [{"role": "user", "content": "hi"}, {"bad": 1}]})
    assert msgs[1] == {"role": "user", "content": "hi"} and len(msgs) == 2
    c = fetch_prompt(client, PromptSpec(logical="c", name="c"))
    assert build_messages(c, {"question": "Why?"}) == [
        {"role": "system", "content": "Answer Why? as JSON"},
        {"role": "user", "content": "Why?"},
    ]


def test_response_format():
    rf = response_format_for(SCHEMA)
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] is SCHEMA and rf["json_schema"]["strict"] is False
    assert list(rf["json_schema"]) == ["name", "strict", "schema"]
    assert response_format_for(SCHEMA, strict=True)["json_schema"]["strict"] is True


def test_resolve_structured_options_precedence(clean_env, monkeypatch):
    spec = PromptSpec(logical="s", name="s", output_schema=True)  # defaults
    assert resolve_structured_options(spec) == ("json_schema", False)
    manifest_spec = PromptSpec(logical="s", name="s", output_schema=True, response_format="none", strict=True)
    assert resolve_structured_options(manifest_spec) == ("none", True)
    monkeypatch.setenv("BENCHKIT_RESPONSE_FORMAT", "json_schema")
    monkeypatch.setenv("BENCHKIT_STRICT", "false")
    assert resolve_structured_options(manifest_spec) == ("json_schema", False)  # env > manifest
    assert resolve_structured_options(manifest_spec, response_format="none", strict=True) == ("none", True)  # CLI > env
    monkeypatch.setenv("BENCHKIT_STRICT", "maybe")
    with pytest.raises(UsageError, match="BENCHKIT_STRICT"):
        resolve_structured_options(spec)
    monkeypatch.setenv("BENCHKIT_STRICT", "1")
    monkeypatch.setenv("BENCHKIT_RESPONSE_FORMAT", "json_object")
    with pytest.raises(UsageError, match="response_format"):
        resolve_structured_options(spec)
    monkeypatch.delenv("BENCHKIT_RESPONSE_FORMAT")
    assert resolve_structured_options(spec) == ("json_schema", True)


def _capture_gateway(reply: str, bodies: list):
    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return Gateway(base_url="http://gw", api_key="k", transport=httpx.MockTransport(handler))


def test_build_task_response_format_modes():
    client = FakeClient({"t": text_prompt("t")})
    p = fetch_prompt(client, PromptSpec(logical="t", name="t"))
    bodies: list = []
    gw = _capture_gateway('```json\n{"answer": "Paris"}\n```', bodies)
    # json_schema, strict false (default)
    task = build_task(p, model="m", output_schema=SCHEMA, chat=gw.chat)
    assert task(item=Item("a", {"question": "q"})) == {"answer": "Paris"}
    assert bodies[-1]["response_format"] == {"type": "json_schema", "json_schema": {"name": "output", "strict": False, "schema": SCHEMA}}
    # json_schema, strict true (Anthropic's OpenAI-compatible endpoint)
    task = build_task(p, model="m", output_schema=SCHEMA, chat=gw.chat, strict=True)
    task(item=Item("a", {"question": "q"}))
    assert bodies[-1]["response_format"]["json_schema"]["strict"] is True
    # none: no response_format key at all; fenced JSON still parsed + validated
    task = build_task(p, model="m", output_schema=SCHEMA, chat=gw.chat, response_format="none", strict=True)
    assert task(item=Item("a", {"question": "q"})) == {"answer": "Paris"}
    assert "response_format" not in bodies[-1] and bodies[-1]["model"] == "m"
    bad = _capture_gateway("not json at all", [])
    task = build_task(p, model="m", output_schema=SCHEMA, chat=bad.chat, response_format="none")
    assert task(item=Item("a", {"question": "q"}))["_error"].startswith("parse: invalid JSON")
    # plain prompt (no output_schema): never a response_format
    task = build_task(p, model="m", output_schema=None, chat=gw.chat, strict=True)
    task(item=Item("a", {"question": "q"}))
    assert "response_format" not in bodies[-1]


def test_run_experiment_response_format_precedence(toy, clean_env, monkeypatch):
    items = [Item("toy_001", {"question": "q"}, {"answer": "Paris"}, {})]

    def go(**kw):
        client = FakeClient({"toy-prompt-structured": text_prompt("toy-prompt-structured")}, items)
        bodies: list = []
        run_experiment(toy, dataset_logical="gold", prompt_logical="structured", client=client, gateway=_capture_gateway('{"answer": "Paris"}', bodies), **kw)
        return bodies[0], client.runs[0]["metadata"]

    body, meta = go()
    assert body["response_format"]["json_schema"]["strict"] is False and meta["response_format"] == "json_schema" and meta["strict"] == "false"
    monkeypatch.setenv("BENCHKIT_STRICT", "true")
    body, meta = go()
    assert body["response_format"]["json_schema"]["strict"] is True and meta["strict"] == "true"
    body, meta = go(strict=False)  # CLI beats env
    assert body["response_format"]["json_schema"]["strict"] is False
    monkeypatch.setenv("BENCHKIT_RESPONSE_FORMAT", "none")
    body, meta = go()
    assert "response_format" not in body and meta["response_format"] == "none"
    body, meta = go(response_format="json_schema")  # CLI beats env
    assert "response_format" in body


# --- gateway -----------------------------------------------------------------


def _gateway(handler):
    return Gateway(base_url="http://gw.test/v1", api_key="key", transport=httpx.MockTransport(handler))


def test_gateway_chat():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "Paris"}'}}]})

    text = _gateway(handler).chat(model="m", messages=[{"role": "system", "content": "x"}], response_format=response_format_for(SCHEMA))
    assert text == '{"answer": "Paris"}'
    assert seen["url"] == "http://gw.test/v1/chat/completions" and seen["auth"] == "Bearer key"
    assert seen["body"]["model"] == "m" and seen["body"]["response_format"]["type"] == "json_schema"


def test_gateway_errors():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        _gateway(handler).chat(model="m", messages=[])

    def parts(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]})

    assert _gateway(parts).chat(model="m", messages=[]) == "ab"


def test_gateway_from_env(clean_env, monkeypatch):
    with pytest.raises(EnvError):
        Gateway.from_env()
    monkeypatch.setenv("LITELLM_BASE_URL", "http://llm.test")
    monkeypatch.setenv("LITELLM_VIRTUAL_KEY", "vk")
    g = Gateway.from_env()
    assert g.base_url == "http://llm.test" and g.api_key == "vk"


# --- task + run ----------------------------------------------------------------


class Item:
    def __init__(self, id, input, expected_output=None, metadata=None):
        self.id, self.input, self.expected_output, self.metadata = id, input, expected_output, metadata


def test_build_task_outputs():
    client = FakeClient({"t": text_prompt("t")})
    p = fetch_prompt(client, PromptSpec(logical="t", name="t"))
    replies = iter(['{"answer": "Paris"}', "```json\n{\"answer\": \"4\"}\n```", "nope", '{"answer": 1}'])
    calls = []

    def chat(*, model, messages, response_format):
        calls.append((model, messages, response_format))
        return next(replies)

    task = build_task(p, model="m", output_schema=SCHEMA, chat=chat)
    assert task(item=Item("a", {"question": "q1"})) == {"answer": "Paris"}
    assert task(item={"input": {"question": "q2"}}) == {"answer": "4"}
    assert task(item=Item("c", {"question": "q3"}))["_error"].startswith("parse: invalid JSON")
    out_d = task(item=Item("d", {"question": "q4"}))
    assert out_d["_error"].startswith("schema") and out_d["answer"] == 1
    assert calls[0][0] == "m" and calls[0][2]["type"] == "json_schema"
    assert calls[0][1][0]["content"] == "You answer. Q: q1"
    plain = build_task(p, model="m", output_schema=None, chat=lambda **kw: "free text")
    assert plain(item=Item("e", {"question": "q"})) == "free text"


def test_default_run_name():
    client = FakeClient({"t": text_prompt("t", version=7)})
    p = fetch_prompt(client, PromptSpec(logical="t", name="t"))
    assert default_run_name(p, "org/model", "abc1234") == "t@7-org_model-abc1234"


def test_run_experiment_end_to_end(toy, clean_env):
    items = [
        Item("toy_001", {"question": "capital?"}, {"answer": "Paris", "rubric": {"critical": ["answer_matches"]}}, {"status": "gold"}),
        Item("toy_002", {"question": "2+2?"}, {"answer": "4", "rubric": {"critical": ["answer_matches", "confident"]}}, {}),
    ]
    client = FakeClient({"toy-prompt-structured": text_prompt("toy-prompt-structured", "Q: {{question}}", version=5)}, items)
    replies = iter(['{"answer": "Paris", "confidence": 0.9}', "not json"])

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(replies)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            },
        )

    gw = Gateway(base_url="http://gw", api_key="k", transport=httpx.MockTransport(handler))
    result = run_experiment(toy, dataset_logical="gold", prompt_logical="structured", model=None, client=client, gateway=gw, max_concurrency=2)
    kw = client.runs[0]
    assert kw["run_name"].startswith("toy-prompt-structured@5-toy-model-1-")
    assert kw["metadata"]["model"] == "toy-model-1" and kw["metadata"]["benchmark"] == "toy" and kw["metadata"]["dataset"] == "gold"
    assert kw["name"] == "toy/structured" and kw["max_concurrency"] == 2 and kw["_dataset_version"] is None
    assert [e.__name__ for e in kw["evaluators"]] == ["contract_valid", "answer_matches", "confident"]
    (i1, out1, ev1), (i2, out2, ev2) = result
    assert out1 == {"answer": "Paris", "confidence": 0.9}
    e1 = {e.name: e.value for e in ev1}
    assert e1 == {"contract_valid": True, "answer_matches": True, "confident": True, "protocol_pass": True, "diagnostic_score": 1.0}
    assert out2["_error"].startswith("parse: invalid JSON")
    e2 = {e.name: e.value for e in ev2}
    assert e2["contract_valid"] is False and e2["protocol_pass"] is False and e2["diagnostic_score"] == 0.0
    assert client.flushed
    # each gateway call became a priced generation observation
    gen1, gen2 = client.observations
    assert gen1["start"]["as_type"] == "generation" and gen1["start"]["model"] == "toy-model-1"
    assert gen1["ended"] and gen2["ended"]
    assert gen1["updates"][0]["usage_details"] == {"input": 12, "output": 7, "total": 19}


def test_run_experiment_schema_invalid_output_keeps_evaluator_signal(toy, clean_env):
    """(b): parsed-but-invalid output reaches evaluators with its fields; contract_valid False."""
    items = [Item("toy_001", {"question": "q"}, {"answer": "Paris", "rubric": {"critical": ["answer_matches"]}}, {})]
    client = FakeClient({"toy-prompt-structured": text_prompt("toy-prompt-structured")}, items)
    seen = {}

    def spy(*, input, output, expected_output, metadata, **kw):
        seen["output"] = output
        return Evaluation(name="spy", value=True, data_type="BOOLEAN")

    toy_spy = type(toy)(**{**toy.__dict__, "evaluators_ref": toy.evaluators_ref})
    import benchkit.manifest as mf

    orig = mf.Benchmark.evaluators
    mf.Benchmark.evaluators = lambda self: orig(self) + [spy]
    try:
        gw = Gateway(base_url="http://gw", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "Paris", "confidence": 7}'}}]})))
        ((item, out, evs),) = run_experiment(toy_spy, dataset_logical="gold", prompt_logical="structured", client=client, gateway=gw)
    finally:
        mf.Benchmark.evaluators = orig
    assert seen["output"]["answer"] == "Paris" and seen["output"]["confidence"] == 7
    assert seen["output"]["_error"].startswith("schema: confidence:") and seen["output"]["_schema_errors"]
    e = {x.name: x.value for x in evs}
    assert e["contract_valid"] is False and e["spy"] is True
    # toy evaluators treat `_error` as contract broken (their choice); protocol_pass reflects that
    assert e["answer_matches"] is False and e["protocol_pass"] is False


def test_run_experiment_limit_and_errors(toy, clean_env, monkeypatch):
    client = FakeClient({"toy-prompt": text_prompt()}, [Item("a", {"question": "q"}), Item("b", {"question": "q"})])
    gw = Gateway(base_url="http://gw", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})))
    run_experiment(toy, dataset_logical="all", prompt_logical="plain", client=client, gateway=gw, limit=1, run_name="r1")
    kw = client.runs[0]
    assert len(kw["data"]) == 1 and kw["run_name"] == "r1" and kw["evaluators"][0].__name__ == "answer_matches"
    empty = FakeClient({"toy-prompt": text_prompt()}, [])
    with pytest.raises(UsageError, match="no items"):
        run_experiment(toy, dataset_logical="all", prompt_logical="plain", client=empty, gateway=gw)
    bare = type(toy)(**{**toy.__dict__, "models": {}})
    with pytest.raises(UsageError, match="no model"):
        run_experiment(bare, dataset_logical="all", prompt_logical="plain", client=client, gateway=gw)


# --- generation telemetry -----------------------------------------------------


def test_normalize_usage_shapes():
    # OpenAI shape (OpenAI, LiteLLM, Anthropic/Gemini OpenAI-compatible endpoints)
    assert normalize_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}) == {"input": 10, "output": 5, "total": 15}
    # OpenAI detail blocks
    u = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 60},
            "completion_tokens_details": {"reasoning_tokens": 7},
        }
    )
    assert u == {"input": 100, "output": 20, "total": 120, "cache_read_input_tokens": 60, "reasoning_tokens": 7}
    # LiteLLM passing through Anthropic cache fields alongside the OpenAI keys
    u = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 3})
    assert u == {"input": 10, "output": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}
    # Anthropic native shape (gateway passthrough)
    assert normalize_usage({"input_tokens": 11, "output_tokens": 3, "cache_read_input_tokens": 8}) == {"input": 11, "output": 3, "cache_read_input_tokens": 8}
    # Gemini native usageMetadata
    u = normalize_usage({"promptTokenCount": 9, "candidatesTokenCount": 4, "totalTokenCount": 13, "thoughtsTokenCount": 2, "cachedContentTokenCount": 5})
    assert u == {"input": 9, "output": 4, "total": 13, "reasoning_tokens": 2, "cache_read_input_tokens": 5}
    # junk / absent / zeros
    assert normalize_usage(None) is None
    assert normalize_usage("usage") is None
    assert normalize_usage({}) is None
    assert normalize_usage({"prompt_tokens": "many", "completion_tokens": True}) is None
    assert normalize_usage({"prompt_tokens": 0, "completion_tokens": 0}) is None


def test_gateway_chat_carries_usage():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5-20251001",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            },
        )

    reply = _gateway(handler).chat(model="claude-haiku-4-5", messages=[])
    assert reply == "hi" and isinstance(reply, ChatReply)
    assert reply.model == "claude-haiku-4-5-20251001"
    assert reply.usage == {"input": 12, "output": 3, "total": 15}
    assert reply.usage_raw == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}

    def bare(request):  # no usage, no model: requested model kept, usage None
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    reply = _gateway(bare).chat(model="m", messages=[])
    assert reply.model == "m" and reply.usage is None and reply.usage_raw is None

    def gemini(request):  # native usageMetadata passthrough
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}})

    assert _gateway(gemini).chat(model="m", messages=[]).usage == {"input": 7, "output": 2}


def test_build_task_logs_generation():
    client = FakeClient({"t": text_prompt("t")})
    p = fetch_prompt(client, PromptSpec(logical="t", name="t"))

    def chat(*, model, messages, response_format):
        return ChatReply('{"answer": "Paris"}', model="m-2024", usage={"input": 5, "output": 2})

    task = build_task(p, model="m", output_schema=SCHEMA, chat=chat, langfuse_client=client)
    assert task(item=Item("a", {"question": "q"})) == {"answer": "Paris"}
    (rec,) = client.observations
    assert rec["start"]["as_type"] == "generation" and rec["start"]["name"] == "chat"
    assert rec["start"]["model"] == "m" and rec["start"]["input"][0]["role"] == "system"
    assert rec["start"]["model_parameters"] == {"response_format": "json_schema"}
    assert rec["ended"]
    assert rec["updates"] == [{"output": '{"answer": "Paris"}', "model": "m-2024", "usage_details": {"input": 5, "output": 2}}]
    # no client (or a client without start_observation): behaviour unchanged, nothing logged
    task = build_task(p, model="m", output_schema=SCHEMA, chat=chat)
    assert task(item=Item("a", {"question": "q"})) == {"answer": "Paris"}
    assert len(client.observations) == 1
    # plain str reply (test fakes): output logged, model falls back, usage None
    task = build_task(p, model="m", output_schema=None, chat=lambda **kw: "free text", langfuse_client=client)
    assert task(item=Item("a", {"question": "q"})) == "free text"
    assert client.observations[-1]["updates"] == [{"output": "free text", "model": "m", "usage_details": None}]
    assert client.observations[-1]["start"]["model_parameters"] == {"response_format": "none"}

    # gateway error: observation marked ERROR and closed, exception propagates
    def boom(**kw):
        raise RuntimeError("gateway down")

    task = build_task(p, model="m", output_schema=SCHEMA, chat=boom, langfuse_client=client)
    with pytest.raises(RuntimeError, match="gateway down"):
        task(item=Item("a", {"question": "q"}))
    rec = client.observations[-1]
    assert rec["ended"] and rec["updates"] == [{"level": "ERROR", "status_message": "gateway down"}]
