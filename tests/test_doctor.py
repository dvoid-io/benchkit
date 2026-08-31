from types import SimpleNamespace as NS

from langfuse.api import Prompt_Text

from benchkit.doctor import run_doctor


def _prompt(name, content):
    return Prompt_Text(name=name, version=1, prompt=content, config={}, labels=[], tags=[], type="text")


class FakeClient:
    def __init__(self, prompts, auth=True):
        self._auth = auth
        self.api = NS(prompts=NS(get=lambda prompt_name, **kw: prompts[prompt_name]))

    def auth_check(self):
        return self._auth


def test_env_missing_exits_3_without_network(toy, clean_env):
    called = []

    def factory():
        called.append(1)
        raise AssertionError("must not be called")

    rep = run_doctor([toy], client_factory=factory)
    assert rep.exit_code == 3 and not called
    text = rep.format()
    assert "[FAIL] langfuse env" in text and "missing: LANGFUSE_BASE_URL" in text
    assert "[WARN] gateway env" in text
    assert "[ok ] toy: render — 3 item(s)" in text
    assert "pk-" not in text


def test_host_fallback_counts(toy, clean_env, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://x")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    rep = run_doctor([toy], client_factory=lambda: (_ for _ in ()).throw(ConnectionError("refused")))
    assert rep.exit_code == 3 and "[FAIL] langfuse reachable — ConnectionError" in rep.format()


def test_unreachable_and_auth_false(toy, langfuse_env):
    rep = run_doctor([toy], client_factory=lambda: FakeClient({}, auth=False))
    assert rep.exit_code == 3 and "auth_check() returned False" in rep.format()


def test_prompts_and_vars(toy, langfuse_env):
    prompts = {"toy-prompt": _prompt("toy-prompt", "Q: {{question}}"), "toy-prompt-structured": _prompt("toy-prompt-structured", "{{question}} {{transcript}}")}
    rep = run_doctor([toy], client_factory=lambda: FakeClient(prompts))
    text = rep.format()
    assert rep.exit_code == 1
    assert "[ok ] toy: prompt plain (toy-prompt) — v1 text, vars=['question']" in text
    assert "[FAIL] toy: prompt structured vars ⊆ input keys — missing from rendered input: transcript" in text
    prompts["toy-prompt-structured"] = _prompt("toy-prompt-structured", "{{question}}")
    rep = run_doctor([toy], client_factory=lambda: FakeClient(prompts))
    assert rep.exit_code == 0 and "FAIL" not in rep.format()
    rep = run_doctor([toy], client_factory=lambda: FakeClient({}))
    assert rep.exit_code == 1 and "[FAIL] toy: prompt plain (toy-prompt)" in rep.format()


def test_offline_mode(toy, langfuse_env):
    rep = run_doctor([toy], client_factory=lambda: FakeClient({}), network=False)
    assert rep.exit_code == 0 and "reachable" not in rep.format()
