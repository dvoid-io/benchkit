"""toy invariants."""


def question_ends_with_qmark(case: dict) -> list[str]:
    if not str(case.get("question", "")).rstrip().endswith("?"):
        return ["question must end with '?'"]
    return []


INVARIANTS = [question_ends_with_qmark]
