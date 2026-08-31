"""toy evaluators (stdlib-only, never raise)."""

from benchkit import Evaluation


def answer_matches(*, input, output, expected_output, metadata, **kwargs):
    if not isinstance(output, dict) or "_error" in output:
        return Evaluation(name="answer_matches", value=False, data_type="BOOLEAN", comment="no structured output")
    want = str((expected_output or {}).get("answer", "")).strip().casefold()
    got = str(output.get("answer", "")).strip().casefold()
    return Evaluation(name="answer_matches", value=got == want, data_type="BOOLEAN")


def confident(*, input, output, expected_output, metadata, **kwargs):
    if not isinstance(output, dict) or "_error" in output:
        return Evaluation(name="confident", value=False, data_type="BOOLEAN", comment="no structured output")
    c = output.get("confidence")
    return Evaluation(name="confident", value=isinstance(c, (int, float)) and c >= 0.5, data_type="BOOLEAN")


EVALUATORS = [answer_matches, confident]


def raises(*, input, output, expected_output, metadata, **kwargs):
    """A deliberately broken evaluator: `replay` must report it, not die on it."""
    raise RuntimeError("evaluator is mid-edit")


BROKEN_EVALUATORS = [raises, answer_matches, confident]
