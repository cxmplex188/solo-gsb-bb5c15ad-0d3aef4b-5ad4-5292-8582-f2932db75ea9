"""Tests for RFC 6902 JSON Patch generation (app.merger.make_patch)."""

from __future__ import annotations

from app.merger import MISSING, escape_token, json_equal, make_patch, parse_pointer


# ----------------------------------------------------------- RFC 6902 applier


def apply_patch(document, ops):
    """Apply *ops* (as generated for the API) to *document*, RFC 6902 style.

    Only what the generator emits is exercised: whole-document replaces,
    recursive object add/remove/replace and whole-array replaces. Array
    member addressing is implemented generically anyway.
    """
    current = document
    for op in ops:
        tokens = parse_pointer(op["path"])
        current = _apply_one(current, tokens, op)
    return current


def _resolve_array_index(token: str, length: int) -> int:
    if token == "-":
        return length
    if token == "0" or (token and token[0] in "123456789" and token.isdigit()):
        index = int(token)
        if index <= length:
            return index
    raise ValueError(f"invalid array index token {token!r}")


def _apply_one(current, tokens, op):
    if not tokens:
        if op["op"] == "remove":
            raise ValueError("cannot remove the whole document")
        return op["value"]
    parent = current
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            parent = parent[token]
        else:
            parent = parent[_resolve_array_index(token, len(parent))]
    last = tokens[-1]
    kind = op["op"]
    if isinstance(parent, dict):
        if kind == "add":
            parent[last] = op["value"]  # add on an existing member replaces
        elif kind == "replace":
            if last not in parent:
                raise ValueError(f"replace targets missing member {last!r}")
            parent[last] = op["value"]
        else:
            del parent[last]
    else:
        index = _resolve_array_index(last, len(parent))
        if kind == "add":
            parent.insert(index, op["value"])
        elif kind == "replace":
            parent[index] = op["value"]
        else:
            del parent[index]
    return current


def assert_patch_roundtrips(base, target):
    ops = [_public_op(op) for op in make_patch(base, target)]
    assert json_equal(apply_patch(base, ops), target)
    return ops


def _public_op(op):
    entry = {"op": op.op, "path": op.path}
    if op.value is not MISSING:
        entry["value"] = op.value
    return entry


# ----------------------------------------------------------------- behaviour


def test_unchanged_documents_produce_an_empty_patch():
    assert make_patch({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []
    assert make_patch(1, 1) == []
    assert make_patch(None, None) == []


def test_whole_document_scalar_change_is_one_root_replace():
    ops = assert_patch_roundtrips(1, 2)
    assert ops == [{"op": "replace", "path": "", "value": 2}]


def test_whole_document_type_changes_are_root_replaces():
    assert_patch_roundtrips({"a": 1}, [1, 2])
    assert_patch_roundtrips([1, 2], {"a": 1})
    assert_patch_roundtrips({"a": 1}, "scalar")
    ops = assert_patch_roundtrips([1, 2, 3], {})
    assert ops == [{"op": "replace", "path": "", "value": {}}]


def test_object_diffs_recursively():
    base = {
        "same": 1,
        "changed": 2,
        "nested": {"keep": 1, "edit": 9, "gone": 0},
        "removed": "x",
    }
    target = {
        "same": 1,
        "changed": 3,
        "nested": {"keep": 1, "edit": 10, "added": "n"},
        "added": [7, 8],
    }
    ops = assert_patch_roundtrips(base, target)
    assert ops == [
        {"op": "add", "path": "/added", "value": [7, 8]},
        {"op": "replace", "path": "/changed", "value": 3},
        {"op": "add", "path": "/nested/added", "value": "n"},
        {"op": "replace", "path": "/nested/edit", "value": 10},
        {"op": "remove", "path": "/nested/gone"},
        {"op": "remove", "path": "/removed"},
    ]


def test_null_is_a_real_replace_value():
    ops = assert_patch_roundtrips({"a": 1, "b": None}, {"a": None, "b": 1})
    by_path = {op["path"]: op for op in ops}
    assert by_path["/a"] == {"op": "replace", "path": "/a", "value": None}
    assert by_path["/b"] == {"op": "replace", "path": "/b", "value": 1}


def test_remove_operation_has_no_value_member():
    ops = assert_patch_roundtrips({"a": 1}, {})
    assert ops == [{"op": "remove", "path": "/a"}]


def test_arrays_are_replaced_wholesale():
    base = {"a": [1, 2, 3], "same": [1, 2]}
    target = {"a": [1, 2, 3, 4], "same": [1, 2]}
    ops = assert_patch_roundtrips(base, target)
    assert ops == [{"op": "replace", "path": "/a", "value": [1, 2, 3, 4]}]

    # Also when the change is only an element's content or length shrinks.
    ops = assert_patch_roundtrips({"a": [1, 2]}, {"a": [1, 9]})
    assert ops == [{"op": "replace", "path": "/a", "value": [1, 9]}]
    ops = assert_patch_roundtrips([1, 2, 3], [1])
    assert ops == [{"op": "replace", "path": "", "value": [1]}]


def test_array_inside_a_replaced_object_member_is_not_diffed():
    base = {"o": {"n": [1]}}
    target = {"o": {"n": [2]}}
    # Both /o values are objects, so the recursion descends into them;
    # the array under /o/n is still one wholesale replace.
    ops = assert_patch_roundtrips(base, target)
    assert ops == [{"op": "replace", "path": "/o/n", "value": [2]}]


def test_json_pointer_tokens_are_escaped():
    base = {"a/b": 1, "c~d": {"e/f": 1}, "g": {"~h": 1}}
    target = {"a/b": 2, "c~d": {"e/f": 2}, "g": {"~h": 2, "new/key~": 0}}
    ops = assert_patch_roundtrips(base, target)
    paths = [op["path"] for op in ops]
    assert paths == [
        "/a~1b",
        "/c~0d/e~1f",
        "/g/new~1key~0",
        "/g/~0h",
    ]
    # Sanity: the emitted paths parse back to the raw object keys.
    assert parse_pointer("/c~0d/e~1f") == ("c~d", "e/f")
    assert escape_token("/") == "~1"
    assert escape_token("~") == "~0"


def test_added_and_removed_nested_objects_walk_no_deeper():
    base = {"o": {"x": {"deep": [1, 2]}}}
    target = {"o": {"y": {"deep": [3, 4]}}}
    ops = assert_patch_roundtrips(base, target)
    assert ops == [
        {"op": "remove", "path": "/o/x"},
        {"op": "add", "path": "/o/y", "value": {"deep": [3, 4]}},
    ]


def test_patch_is_deterministic_and_sorted():
    base = {"z": 1, "a": {"q": 1, "m": 1}, "d": 1}
    target = {"y": 1, "a": {"r": 1, "m": 2}}
    first = make_patch(base, target)
    second = make_patch(base, target)
    assert [op._asdict() for op in first] == [op._asdict() for op in second]
    paths = [op.path for op in first]
    assert paths == sorted(paths)
    assert_patch_roundtrips(base, target)
