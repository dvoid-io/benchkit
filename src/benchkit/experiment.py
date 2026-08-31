"""`benchkit experiment`: prompt fetch -> gateway call -> parse/validate -> Langfuse run (contract §3.2, §6)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jsonschema
from langfuse import Evaluation

from . import __version__
from .errors import EnvError, UsageError
from .langfuse_client import gateway_env, get_client
from .manifest import Benchmark, PromptSpec

# ---------------------------------------------------------------------------
# output parsing


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """json.loads(text), else the first fenced ```json block, else the outermost {...}."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for block in _FENCE.findall(text):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", text, 0)


MAX_SCHEMA_ERRORS_IN_MESSAGE = 3


def parse_output(text: str, output_schema: dict | None) -> Any:
    """Contract §3.2:
    (a) unparseable          -> {"_raw": text, "_error": "parse: …"}
    (b) parsed, schema-invalid -> the parsed object + "_error": "schema: …" and
        "_schema_errors": ["<json path>: <message>", …] (evaluators still see the fields)
    (c) valid                -> the parsed object unchanged."""
    try:
        data = extract_json(text)
    except json.JSONDecodeError as e:
        return {"_raw": text, "_error": f"parse: invalid JSON: {e.msg}"}
    if output_schema is None:
        return data
    errors = sorted(
        jsonschema.Draft202012Validator(output_schema).iter_errors(data),
        key=lambda e: list(e.absolute_path),
    )
    if not errors:
        return data
    details = [
        ("/".join(str(p) for p in e.absolute_path) or "<root>") + f": {e.message}" for e in errors
    ]
    head = "; ".join(details[:MAX_SCHEMA_ERRORS_IN_MESSAGE])
    more = len(details) - MAX_SCHEMA_ERRORS_IN_MESSAGE
    message = "schema: " + head + (f" (+{more} more)" if more > 0 else "")
    if isinstance(data, dict):
        return {**data, "_error": message, "_schema_errors": details}
    # non-object JSON (list/scalar) cannot carry the annotation: wrap it
    return {"_raw": text, "_parsed": data, "_error": message, "_schema_errors": details}


def is_contract_valid(output: Any) -> bool:
    return isinstance(output, (dict, list)) and not (isinstance(output, dict) and "_error" in output)


# ---------------------------------------------------------------------------
# benchkit's own evaluations


def contract_valid(*, output, **kwargs) -> Evaluation:
    ok = is_contract_valid(output)
    comment = None if ok else (output.get("_error") if isinstance(output, dict) else "no structured output")
    return Evaluation(name="contract_valid", value=ok, data_type="BOOLEAN", comment=comment)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value >= 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "pass", "passed", "yes", "1")
    return False


def _critical_names(expected_output: Any) -> list[str] | None:
    if not isinstance(expected_output, dict):
        return None
    rubric = expected_output.get("rubric")
    if not isinstance(rubric, dict) or "critical" not in rubric:
        return None
    crit = rubric["critical"]
    if isinstance(crit, str):
        return [crit]
    if isinstance(crit, (list, tuple)):
        return [str(c) for c in crit]
    return None


def composite_evaluations(*, evaluations: list, expected_output: Any = None, **kwargs) -> list[Evaluation]:
    """protocol_pass (AND of rubric.critical evaluations, if declared) and
    diagnostic_score (passed / applicable over all BOOLEAN evaluations)."""
    out: list[Evaluation] = []
    by_name: dict[str, list] = {}
    for ev in evaluations or []:
        by_name.setdefault(ev.name, []).append(ev)
    critical = _critical_names(expected_output)
    if critical is not None:
        missing = [n for n in critical if n not in by_name]
        passed = not missing and all(_truthy(e.value) for n in critical for e in by_name[n])
        failed = [n for n in critical if n in by_name and not all(_truthy(e.value) for e in by_name[n])]
        comment_bits = []
        if missing:
            comment_bits.append("missing evaluations: " + ", ".join(missing))
        if failed:
            comment_bits.append("failed: " + ", ".join(failed))
        out.append(
            Evaluation(
                name="protocol_pass",
                value=bool(passed),
                data_type="BOOLEAN",
                comment="; ".join(comment_bits) or None,
                metadata={"critical": critical},
            )
        )
    booleans = [e for e in (evaluations or []) if (e.data_type == "BOOLEAN" or isinstance(e.value, bool))]
    applicable = len(booleans)
    passed_n = sum(1 for e in booleans if _truthy(e.value))
    out.append(
        Evaluation(
            name="diagnostic_score",
            value=(passed_n / applicable) if applicable else 0.0,
            data_type="NUMERIC",
            comment=f"{passed_n}/{applicable} boolean evaluations passed",
            metadata={"passed": passed_n, "applicable": applicable},
        )
    )
    return out


# ---------------------------------------------------------------------------
# prompt fetch + compile


@dataclass
class CompiledPrompt:
    name: str
    version: int | None
    type: str  # "text" | "chat"
    variables: list[str]
    client: Any  # TextPromptClient | ChatPromptClient


def fetch_prompt(client, spec: PromptSpec) -> CompiledPrompt:
    """Fetch the prompt once; learn its type from the API when the manifest does not say."""
    from langfuse.model import ChatPromptClient, TextPromptClient

    kwargs: dict[str, Any] = {}
    if spec.version is not None:
        kwargs["version"] = spec.version
    elif spec.label:
        kwargs["label"] = spec.label
    try:
        raw = client.api.prompts.get(prompt_name=spec.name, **kwargs)
    except Exception as e:
        raise EnvError(f"cannot fetch prompt {spec.name!r} ({kwargs or 'latest'}): {e}") from e
    ptype = spec.type or getattr(raw, "type", None) or "text"
    pc = ChatPromptClient(raw) if ptype == "chat" else TextPromptClient(raw)
    return CompiledPrompt(
        name=spec.name,
        version=getattr(raw, "version", None),
        type=ptype,
        variables=list(getattr(pc, "variables", []) or []),
        client=pc,
    )


def _compile_vars(item_input: dict) -> dict:
    out = {}
    for k, v in (item_input or {}).items():
        if isinstance(v, (str, list)):
            out[k] = v
        elif v is None:
            out[k] = ""
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out


def build_messages(prompt: CompiledPrompt, item_input: dict) -> list[dict]:
    """Contract §6: text prompt -> system message with {{vars}} substituted; if the input
    has `messages: [...]` they are appended as chat turns. Chat prompt -> compiled messages
    (placeholders resolved from input keys; unresolved placeholders dropped)."""
    variables = _compile_vars(item_input)
    if prompt.type == "chat":
        compiled = prompt.client.compile(**variables)
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in compiled
            if isinstance(m, dict) and m.get("type") != "placeholder" and "role" in m
        ]
        return messages
    system = prompt.client.compile(**variables)
    messages = [{"role": "system", "content": system}]
    extra = item_input.get("messages") if isinstance(item_input, dict) else None
    if isinstance(extra, list):
        for m in extra:
            if isinstance(m, dict) and "role" in m and "content" in m:
                messages.append({"role": m["role"], "content": m["content"]})
    return messages


# ---------------------------------------------------------------------------
# generation telemetry


class ChatReply(str):
    """Gateway reply text carrying generation telemetry for Langfuse (model, usage)."""

    model: str | None = None
    usage: dict[str, int] | None = None
    usage_raw: dict | None = None

    def __new__(
        cls,
        text: str,
        *,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        usage_raw: dict | None = None,
    ) -> ChatReply:
        reply = super().__new__(cls, text)
        reply.model, reply.usage, reply.usage_raw = model, usage, usage_raw
        return reply


def _usage_int(d: Any, *keys: str) -> int | None:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if not isinstance(v, bool) and isinstance(v, (int, float)) and v >= 0:
            return int(v)
    return None


def normalize_usage(raw: Any) -> dict[str, int] | None:
    """Provider usage → Langfuse `usage_details` (`input`/`output`/`total` + cache/reasoning).

    Handles the OpenAI shape (OpenAI, LiteLLM, and the Anthropic/Gemini OpenAI-compatible
    endpoints: `prompt_tokens`/`completion_tokens`/`total_tokens` + `*_tokens_details`),
    the native Anthropic shape (`input_tokens`/`output_tokens` + cache fields, as some
    gateways pass through), and the native Gemini shape (`usageMetadata`:
    `promptTokenCount`/`candidatesTokenCount`/`totalTokenCount`/`thoughtsTokenCount`).
    Zero/absent components are dropped; returns None when nothing usable is present.
    """
    if not isinstance(raw, dict):
        return None
    usage = {
        "input": _usage_int(raw, "prompt_tokens", "input_tokens", "promptTokenCount"),
        "output": _usage_int(raw, "completion_tokens", "output_tokens", "candidatesTokenCount"),
        "total": _usage_int(raw, "total_tokens", "totalTokenCount"),
        "cache_read_input_tokens": _usage_int(raw, "cache_read_input_tokens", "cachedContentTokenCount")
        or _usage_int(raw.get("prompt_tokens_details"), "cached_tokens"),
        "cache_creation_input_tokens": _usage_int(raw, "cache_creation_input_tokens"),
        "reasoning_tokens": _usage_int(raw, "thoughtsTokenCount")
        or _usage_int(raw.get("completion_tokens_details"), "reasoning_tokens"),
    }
    return {k: v for k, v in usage.items() if v} or None


# ---------------------------------------------------------------------------
# gateway


@dataclass
class Gateway:
    base_url: str
    api_key: str
    timeout: float = 180.0
    transport: Any = None  # httpx transport override (tests)

    @classmethod
    def from_env(cls) -> Gateway:
        base, key = gateway_env()
        if not base or not key:
            raise EnvError(
                "missing environment: OPENAI_BASE_URL (fallback LITELLM_BASE_URL) and/or "
                "OPENAI_API_KEY (fallback LITELLM_VIRTUAL_KEY)"
            )
        return cls(base_url=base, api_key=key)

    def chat(self, *, model: str, messages: list[dict], response_format: dict | None = None) -> ChatReply:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            body["response_format"] = response_format
        url = self.base_url.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
            resp = http.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            )
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"unexpected gateway response shape: {e}") from e
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage_raw = data.get("usage") or data.get("usageMetadata")
        return ChatReply(
            content or "",
            model=data.get("model") or model,
            usage=normalize_usage(usage_raw),
            usage_raw=usage_raw if isinstance(usage_raw, dict) else None,
        )


RESPONSE_FORMATS = ("json_schema", "none")
ENV_RESPONSE_FORMAT = "BENCHKIT_RESPONSE_FORMAT"
ENV_STRICT = "BENCHKIT_STRICT"


def response_format_for(output_schema: dict, *, strict: bool = False) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {"name": "output", "strict": bool(strict), "schema": output_schema},
    }


def _env_bool(value: str | None) -> bool | None:
    if value is None or value.strip() == "":
        return None
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"{ENV_STRICT} must be a boolean, got {value!r}")


def resolve_structured_options(
    spec: PromptSpec, *, response_format: str | None = None, strict: bool | None = None
) -> tuple[str, bool]:
    """(response_format, strict) with precedence CLI > env > manifest > default."""
    rf = response_format or os.environ.get(ENV_RESPONSE_FORMAT) or spec.response_format or "json_schema"
    rf = rf.strip().lower()
    if rf not in RESPONSE_FORMATS:
        raise UsageError(f"response_format must be one of {RESPONSE_FORMATS}, got {rf!r}")
    st = strict if strict is not None else _env_bool(os.environ.get(ENV_STRICT))
    if st is None:
        st = bool(spec.strict)
    return rf, st


def build_task(
    prompt: CompiledPrompt,
    *,
    model: str,
    output_schema: dict | None,
    chat: Callable[..., str],
    response_format: str = "json_schema",
    strict: bool = False,
    langfuse_client: Any = None,
) -> Callable:
    """The Langfuse TaskFunction: task(*, item, **kw) -> output.
    `response_format="none"` sends no response_format (parsing/validation unchanged).
    With a `langfuse_client`, each gateway call is logged as a `generation` observation
    (model, messages, output, normalized usage) so Langfuse can price the run."""
    rf = (
        response_format_for(output_schema, strict=strict)
        if output_schema is not None and response_format == "json_schema"
        else None
    )
    start_observation = getattr(langfuse_client, "start_observation", None)

    def task(*, item, **kwargs):
        item_input = item["input"] if isinstance(item, dict) else getattr(item, "input", None)
        item_input = item_input if isinstance(item_input, dict) else {}
        messages = build_messages(prompt, item_input)
        obs = (
            start_observation(
                name="chat",
                as_type="generation",
                model=model,
                input=messages,
                model_parameters={"response_format": "json_schema" if rf is not None else "none"},
            )
            if callable(start_observation)
            else None
        )
        try:
            text = chat(model=model, messages=messages, response_format=rf)
        except Exception as e:
            if obs is not None:
                obs.update(level="ERROR", status_message=str(e))
                obs.end()
            raise
        if obs is not None:
            obs.update(
                output=str(text),
                model=getattr(text, "model", None) or model,
                usage_details=getattr(text, "usage", None),
            )
            obs.end()
        if output_schema is None:
            return text
        return parse_output(text, output_schema)

    task.__name__ = f"benchkit_{prompt.name}"
    return task


# ---------------------------------------------------------------------------
# run


def spec_repo_sha(path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def default_run_name(prompt: CompiledPrompt, model: str, sha: str) -> str:
    return f"{prompt.name}@{prompt.version or 'latest'}-{model}-{sha}".replace("/", "_")


def run_experiment(
    bench: Benchmark,
    *,
    dataset_logical: str,
    prompt_logical: str,
    model: str | None = None,
    run_name: str | None = None,
    version: datetime | None = None,
    max_concurrency: int = 4,
    limit: int | None = None,
    response_format: str | None = None,
    strict: bool | None = None,
    client=None,
    gateway: Gateway | None = None,
):
    ds = bench.dataset(dataset_logical)
    pspec = bench.prompt(prompt_logical)
    rf_mode, strict_flag = resolve_structured_options(pspec, response_format=response_format, strict=strict)
    model_id = bench.resolve_model(model)
    if not model_id:
        raise UsageError("no model: pass --model, set benchmarks.<x>.models.default, or $BENCHKIT_MODEL")
    client = client or get_client()
    gateway = gateway or Gateway.from_env()
    prompt = fetch_prompt(client, pspec)
    output_schema = bench.load_output_schema() if pspec.output_schema else None

    dataset = client.get_dataset(ds.name, version=version)
    items = list(dataset.items)
    if limit is not None:
        items = items[:limit]
    if not items:
        raise UsageError(f"dataset {ds.name!r} has no items (sync first?)")

    sha = spec_repo_sha(bench.manifest_dir)
    run_name = run_name or default_run_name(prompt, model_id, sha)
    metadata = {
        "benchmark": bench.name,
        "dataset": ds.logical,
        "prompt": f"{prompt.name}@{prompt.version or 'latest'}",
        "model": model_id,
        "benchkit_version": __version__,
        "spec_repo_sha": sha,
        "response_format": rf_mode if output_schema is not None else "n/a",
        "strict": str(strict_flag).lower(),
    }
    evaluators = ([contract_valid] if output_schema is not None else []) + bench.evaluators()
    task = build_task(
        prompt,
        model=model_id,
        output_schema=output_schema,
        chat=gateway.chat,
        response_format=rf_mode,
        strict=strict_flag,
        langfuse_client=client,
    )
    result = client.run_experiment(
        name=f"{bench.name}/{prompt_logical}",
        run_name=run_name,
        description=f"benchkit experiment: {bench.name} dataset={ds.logical} prompt={prompt_logical} model={model_id}",
        data=items,
        task=task,
        evaluators=evaluators,
        composite_evaluator=composite_evaluations,
        max_concurrency=max_concurrency,
        metadata=metadata,
        _dataset_version=version,
    )
    flush = getattr(client, "flush", None)
    if callable(flush):
        flush()
    return result
