from benchkit.align import (
    Matching,
    assignment_key,
    match_hypotheses,
    match_propositions,
    match_variables,
    norm_str,
    partition_from_assignment,
    partition_isomorphic,
    partition_signature,
    partitions_equal,
    proposition_equal,
    variable_id_map,
    variable_key,
)


def test_norm_str():
    assert norm_str("  Hello   World ") == "hello world"
    assert norm_str(None) == "" and norm_str(3) == "3"


def test_variable_key_ignores_order_case_and_ids():
    a = {"id": "v1", "dimension": "Speaker of turn 3", "candidates": ["Alice", "bob"]}
    b = {"id": "x9", "dimension": "speaker of turn 3", "candidates": ["Bob", "alice"]}
    assert variable_key(a) == variable_key(b)


def test_match_variables():
    model = [
        {"id": "m1", "dimension": "referent", "candidates": ["A", "B"]},
        {"id": "m2", "dimension": "time", "candidates": ["now", "later"]},
        {"id": "m3", "dimension": "extra", "candidates": ["x"]},
    ]
    oracle = [
        {"id": "o1", "dimension": "time", "candidates": ["later", "now"]},
        {"id": "o2", "dimension": "referent", "candidates": ["a", "b"]},
        {"id": "o3", "dimension": "missing", "candidates": ["y"]},
    ]
    m = match_variables(model, oracle)
    assert sorted(m.pairs) == [(0, 1), (1, 0)]
    assert m.unmatched_model == [2] and m.unmatched_oracle == [2]
    assert not m.complete
    assert m.precision == 2 / 3 and m.recall == 2 / 3 and abs(m.f1 - 2 / 3) < 1e-9
    assert variable_id_map(model, oracle, m) == {"m1": "o2", "m2": "o1"}
    # duplicates are matched one-to-one
    dup = match_variables([model[0], model[0]], [oracle[1]])
    assert dup.pairs == [(0, 0)] and dup.unmatched_model == [1]
    assert match_variables([], []).complete


def test_custom_keys():
    m = match_variables([{"dim": "d", "opts": ["1"]}], [{"dim": "D", "opts": ["1"]}], dim_key="dim", cand_key="opts")
    assert m.complete


def test_assignment_key_forms():
    var_map = {"m1": "o2", "m2": "o1"}
    h1 = {"assignments": {"m1": "A", "m2": "now"}}
    h2 = {"assignments": [{"variable": "o2", "value": "a"}, {"var": "o1", "candidate": "NOW"}]}
    assert assignment_key(h1, var_map=var_map) == assignment_key(h2)
    assert assignment_key({}) == frozenset()


def test_match_hypotheses():
    var_map = {"m1": "o1"}
    model = [{"assignments": {"m1": "x"}}, {"assignments": {"m1": "y"}}, {"assignments": {"m1": "z"}}]
    oracle = [{"assignments": {"o1": "y"}}, {"assignments": {"o1": "x"}}]
    m = match_hypotheses(model, oracle, var_map=var_map)
    assert sorted(m.pairs) == [(0, 1), (1, 0)] and m.unmatched_model == [2] and m.unmatched_oracle == []


def test_partitions():
    p1 = [["t1", "t2"], ["t3"]]
    p2 = [["T3"], ["t2", "t1"]]
    p3 = [["t1"], ["t2", "t3"]]
    assert partitions_equal(p1, p2) and not partitions_equal(p1, p3)
    assert partition_isomorphic(p1, p3) and partition_signature(p1) == (2, 1)
    assert not partition_isomorphic(p1, [["t1", "t2", "t3"]])
    assert partitions_equal(partition_from_assignment({"t1": "A", "t2": "A", "t3": "B"}), p1)


def test_propositions():
    a = {"pred": "Speaker", "args": ["turn3", "Alice"], "value": True}
    b = {"predicate": "speaker", "arguments": ["Turn3", "alice"]}
    c = {"pred": "speaker", "args": ["turn3", "alice"], "value": False}
    d = {"pred": "speaker", "args": ["alice", "turn3"]}
    assert proposition_equal(a, b)
    assert not proposition_equal(a, c)
    assert not proposition_equal(a, d)  # list args are order-sensitive
    assert proposition_equal({"pred": "p", "args": {"x": "1", "y": "2"}}, {"pred": "P", "args": {"y": "2", "x": "1"}})
    m = match_propositions([a, d], [b, c])
    assert m.pairs == [(0, 0)] and m.unmatched_model == [1] and m.unmatched_oracle == [1]


def test_matching_properties_empty():
    m = Matching()
    assert m.precision == 1.0 and m.recall == 1.0 and m.f1 == 1.0 and m.complete
