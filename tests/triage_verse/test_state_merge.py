"""Union-merge property tests for the triage-state bus."""

from triage_verse import state


def test_union_merge_adds_only_new_lines():
    existing = '{"id": "a"}\n{"id": "b"}\n'
    incoming = '{"id": "b"}\n{"id": "c"}\n'
    merged = state.union_merge_lines(existing, incoming)
    assert merged == '{"id": "a"}\n{"id": "b"}\n{"id": "c"}\n'


def test_union_merge_is_idempotent():
    a = '{"id": "x"}\n{"id": "y"}\n'
    assert state.union_merge_lines(a, a) == a


def test_union_merge_handles_empty_sides():
    assert state.union_merge_lines("", '{"id": "a"}\n') == '{"id": "a"}\n'
    assert state.union_merge_lines('{"id": "a"}\n', "") == '{"id": "a"}\n'
    assert state.union_merge_lines("", "") == ""


def test_union_merge_tolerates_missing_final_newline():
    merged = state.union_merge_lines('{"id": "a"}', '{"id": "b"}')
    assert merged == '{"id": "a"}\n{"id": "b"}\n'


def test_union_merge_preserves_existing_order_and_dedups_incoming():
    existing = "l1\nl2\n"
    incoming = "l3\nl2\nl3\nl4\n"
    assert state.union_merge_lines(existing, incoming) == "l1\nl2\nl3\nl4\n"
