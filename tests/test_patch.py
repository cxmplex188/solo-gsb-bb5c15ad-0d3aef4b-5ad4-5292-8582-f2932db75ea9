"""Unit tests for RFC 6902 patch generation (app/patch.py).

A small in-test RFC 6902 applier applies the generated operations to the
base document; the result must equal the merge target in every test,
including randomized documents.
"""

from __future__ import annotations

import copy
import random

from app.merger import parse_pointer
from app.patch import build_json_patch


# --------------------------------------------------------- RFC 6902 applier


def apply_json_patch(document, operations):
    """Minimal RFC 6902 applier: add/remove/replace, mutating a deep copy."""
    doc = copy.deepcopy(document)
    for operation in operations:
        doc = _apply_one(doc, operation)
    return doc


def _resolve_parent(doc, tokens):
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict):
            assert token in node, f"missing object member {token!r}"
            node = node[token]
        elif isinstance(node, list):
            index = int(token)
            assert 0 <= index < len(node), f"array index {index} out of range"
            node = node[index]
        else:
            raise AssertionError(f"cannot descend into {type(node).__name__}")
    return node


def _apply_one(doc, operation):
    op = operation["op"]
    tokens = parse_pointer(operation["path"])
    if not tokens:
        if op in ("replace", "add"):
            return copy.deepcopy(operation["value"])
        raise AssertionError("remove on the whole document is invalid")
    parent = _resolve_parent(doc, tokens)
    token = tokens[-1]
    if isinstance(parent, list):
        index = len(parent) if token == "-" else int(token)
        assert 0 <= index <= len(parent), f"array index {index} out of range"
        if op == "add":
            parent.insert(index, copy.deepcopy(operation["value"]))
        elif op == "remove":
            assert index < len(parent)
            del parent[index]
        else:  # replace
            assert index < len(parent)
            parent[index] = copy.deepcopy(operation["value"])
    else:
        if op == "add":
            assert token not in parent, f"add target {token!r} already exists"
            parent[token] = copy.deepcopy(operation["value"])
        elif op == "remove":
            assert token in parent
            del parent[token]
        else:  # replace
            assert token in parent
            parent[token] = copy.deepcopy(operation["value"])
    return doc


def check_roundtrip(base, target):
    operations = build_json_patch(base, target)
    assert apply_json_patch(base, operations) == target
    return operations


# -------------------------------------------------------------- unit tests


def test_identical_documents_produce_empty_patch():
    doc = {"a": [1, 2], "b": {"c": 3}}
    assert build_json_patch(doc, copy.deepcopy(doc)) == []


def test_added_scalar_member():
    operations = check_roundtrip({"a": 1}, {"a": 1, "b": 2})
    assert operations == [{"op": "add", "path": "/b", "value": 2}]


def test_removed_member():
    operations = check_roundtrip({"a": 1, "b": 2}, {"a": 1})
    assert operations == [{"op": "remove", "path": "/b"}]


def test_replaced_leaf():
    operations = check_roundtrip({"a": 1}, {"a": 9})
    assert operations == [{"op": "replace", "path": "/a", "value": 9}]


def test_nested_object_members_recurse():
    base = {"o": {"x": 1, "y": 2, "z": 3}}
    target = {"o": {"x": 10, "y": 2, "w": 4}}
    operations = check_roundtrip(base, target)
    # Sorted by raw object key: w (added), x (replaced), z (removed); the
    # unchanged member y produces no operation.
    assert operations == [
        {"op": "add", "path": "/o/w", "value": 4},
        {"op": "replace", "path": "/o/x", "value": 10},
        {"op": "remove", "path": "/o/z"},
    ]
    assert all(op["path"] != "/o/y" for op in operations)


def test_added_nested_object_starts_with_empty_object():
    # An object subtree added wholesale: the object must exist before its
    # members can be added, and operations apply in their listed order.
    base = {"a": 1}
    target = {"a": 1, "o": {"x": 1, "y": {"z": 2}}}
    operations = check_roundtrip(base, target)
    assert operations == [
        {"op": "add", "path": "/o", "value": {}},
        {"op": "add", "path": "/o/x", "value": 1},
        {"op": "add", "path": "/o/y", "value": {}},
        {"op": "add", "path": "/o/y/z", "value": 2},
    ]


def test_removed_object_subtree_is_one_operation():
    base = {"o": {"x": {"y": [1, 2]}}}
    target = {}
    operations = check_roundtrip(base, target)
    assert operations == [{"op": "remove", "path": "/o"}]


def test_arrays_are_replaced_wholesale_when_changed():
    # Even element-level changes never produce per-element operations.
    base = {"a": [1, 2, 3]}
    target = {"a": [1, 2, 4]}
    operations = check_roundtrip(base, target)
    assert operations == [{"op": "replace", "path": "/a", "value": [1, 2, 4]}]


def test_identical_array_is_not_touched():
    doc = {"a": [1, 2, 3]}
    assert build_json_patch(doc, copy.deepcopy(doc)) == []


def test_array_added_and_removed_as_a_whole():
    operations = check_roundtrip({}, {"a": [1, 2]})
    assert operations == [{"op": "add", "path": "/a", "value": [1, 2]}]
    operations = check_roundtrip({"a": [1, 2]}, {})
    assert operations == [{"op": "remove", "path": "/a"}]


def test_object_to_array_is_a_replace():
    operations = check_roundtrip({"a": {"x": 1}}, {"a": [1, 2]})
    assert operations == [{"op": "replace", "path": "/a", "value": [1, 2]}]


def test_array_to_object_is_a_replace():
    operations = check_roundtrip({"a": [1]}, {"a": {"x": 1}})
    assert operations == [{"op": "replace", "path": "/a", "value": {"x": 1}}]


def test_nested_array_inside_added_object_is_added_wholesale():
    base = {}
    target = {"o": {"arr": [1, {"k": 2}]}}
    operations = check_roundtrip(base, target)
    assert operations == [
        {"op": "add", "path": "/o", "value": {}},
        {"op": "add", "path": "/o/arr", "value": [1, {"k": 2}]},
    ]


def test_root_scalar_replacement_uses_empty_path():
    operations = check_roundtrip(1, 5)
    assert operations == [{"op": "replace", "path": "", "value": 5}]


def test_root_array_replacement_uses_empty_path():
    operations = check_roundtrip([1, 2], [1, 2, 3])
    assert operations == [{"op": "replace", "path": "", "value": [1, 2, 3]}]


def test_root_object_to_scalar_uses_empty_path():
    operations = check_roundtrip({"a": 1}, None)
    assert operations == [{"op": "replace", "path": "", "value": None}]


def test_null_is_a_real_value():
    # null differs from an absent member: replacing with null is 'replace',
    # not 'remove'.
    operations = check_roundtrip({"a": 1}, {"a": None})
    assert operations == [{"op": "replace", "path": "/a", "value": None}]


def test_empty_objects_are_added_as_empty_objects():
    operations = check_roundtrip({"k": 1}, {"k": 1, "o": {}})
    assert operations == [{"op": "add", "path": "/o", "value": {}}]


def test_pointer_tokens_are_escaped():
    base = {"a/b": 1, "~x": 2, "only~tilde": 3, "q~1r": 4}
    target = dict(base)
    target["a/b"] = 10          # replace with an escaped slash
    target["~x"] = 20           # replace with an escaped tilde
    del target["only~tilde"]    # removed, escaped
    target["new/key~"] = 7      # added, both escapes
    del target["q~1r"]
    operations = check_roundtrip(base, target)
    # Sorted by the RAW object keys (ASCII), not by escaped path text.
    paths = [op["path"] for op in operations]
    assert paths == [
        "/a~1b",
        "/new~1key~0",
        "/only~0tilde",
        "/q~01r",
        "/~0x",
    ]
    # All operations are well-formed tokens parsing back to the raw keys.
    assert [parse_pointer(op["path"])[0] for op in operations] == [
        "a/b", "new/key~", "only~tilde", "q~1r", "~x",
    ]


def test_escaped_tokens_in_nested_added_object():
    base = {}
    target = {"a/b": {"c~d": [1]}}
    operations = check_roundtrip(base, target)
    assert operations == [
        {"op": "add", "path": "/a~1b", "value": {}},
        {"op": "add", "path": "/a~1b/c~0d", "value": [1]},
    ]


def test_operations_are_deterministic():
    base = {"z": 1, "a": 2, "m": 3}
    target = {"z": 9, "a": 2, "n": 4}
    first = build_json_patch(base, target)
    second = build_json_patch(base, target)
    assert first == second
    assert [op["path"] for op in first] == sorted(op["path"] for op in first)


# ------------------------------------------------------- randomized property


def _random_value(rng, depth):
    kind = rng.randrange(7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randrange(0, 100)
    if kind == 3:
        return rng.choice(["alpha", "a/b", "~x", "", "z"])
    if kind == 4 or depth <= 0:
        return [rng.randrange(0, 5) for _ in range(rng.randrange(0, 4))]
    return _random_object(rng, depth - 1)


def _random_object(rng, depth):
    keys = ["a", "b/c", "~t", "x", "n~m/p", rng.choice(["a", "z"])]
    obj = {}
    for key in keys:
        if rng.random() < 0.7:
            obj[key] = _random_value(rng, depth)
    return obj


def test_randomized_patches_always_rebuild_target():
    rng = random.Random(20260923)
    for _ in range(400):
        base = _random_object(rng, 3)
        target = _random_object(rng, 3)
        operations = build_json_patch(base, target)
        assert apply_json_patch(base, operations) == target
