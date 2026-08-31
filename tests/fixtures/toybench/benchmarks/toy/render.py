"""toy render: question -> input; expected -> expected_output."""


def render(case: dict) -> dict:
    return {
        "id": case["id"],
        "input": {"question": case["question"]},
        "expected_output": case["expected"],
        "metadata": {"status": case["status"], "tags": list(case.get("tags", []))},
    }
