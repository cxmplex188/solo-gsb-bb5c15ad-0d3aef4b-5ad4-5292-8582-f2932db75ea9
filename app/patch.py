"""RFC 6902 JSON Patch generation for the ``/merge`` response.

When ``includePatch`` is requested, the response carries a ``patch`` array
that turns the request's ``base`` document into the response's ``merged``
document. The diff follows the same granularity the merge engine exposes:

* objects are compared recursively, so independent field changes become
  individual ``add`` / ``remove`` / ``replace`` operations;
* arrays are atomic values and are **always replaced wholesale** -- the
  patch never edits array elements in place (an identity-merged array is a
  brand-new array value as far as the patch is concerned);
* an object added as a whole subtree is first ``add``-ed as ``{}`` and then
  filled field by field, so every operation's target pointer exists when
  the operation is applied;
* JSON Pointer reference tokens are escaped per RFC 6901 (``~`` -> ``~0``,
  ``/`` -> ``~1``).

Applying the returned operations to ``base`` in order with any conformant
RFC 6902 processor yields exactly ``merged``.
"""

from __future__ import annotations

from typing import Any, Optional

# Reuse the engine's JSON comparison and RFC 6901 token escaping so the
# patch speaks the exact same pointer dialect as the rest of the service.
from .merger import escape_token, json_equal

# A document value may be any JSON value.
JsonValue = Optional[Any]


def build_json_patch(base: JsonValue, target: JsonValue) -> list[dict[str, Any]]:
    """Build the RFC 6902 operations transforming *base* into *target*.

    Objects recurse field by field; arrays and scalars are replaced as whole
    values. Operations are emitted in a deterministic pre-order walk
    (object keys sorted) and apply cleanly in that order.
    """
    operations: list[dict[str, Any]] = []
    if json_equal(base, target):
        return operations
    if isinstance(base, dict) and isinstance(target, dict):
        _diff_members(base, target, "", operations)
    else:
        # Root replacement: scalar/array values, or a root type change
        # (e.g. object -> array). An empty path names the whole document.
        operations.append({"op": "replace", "path": "", "value": target})
    return operations


def _diff_members(
    base: dict,
    target: dict,
    prefix: str,
    operations: list[dict[str, Any]],
) -> None:
    """Diff two objects that exist on both sides at *prefix*."""
    for key in sorted(set(base) | set(target)):
        path = _join(prefix, key)
        if key not in base:
            _add_subtree(target[key], path, operations)
        elif key not in target:
            # Removing an object/array member takes a single operation:
            # RFC 6902 'remove' drops the whole subtree.
            operations.append({"op": "remove", "path": path})
        else:
            base_value = base[key]
            target_value = target[key]
            if json_equal(base_value, target_value):
                continue
            if isinstance(base_value, dict) and isinstance(target_value, dict):
                _diff_members(base_value, target_value, path, operations)
            else:
                # Arrays are replaced wholesale, as are scalars and type
                # changes (object <-> array/scalar).
                operations.append(
                    {"op": "replace", "path": path, "value": target_value}
                )


def _add_subtree(
    value: Any, path: str, operations: list[dict[str, Any]]
) -> None:
    """Emit operations adding *value* at a path absent from the base.

    A nested object is added as ``{}`` first, then its members are added one
    by one; every later operation therefore targets an existing object.
    Arrays and scalars are added wholesale.
    """
    if isinstance(value, dict):
        operations.append({"op": "add", "path": path, "value": {}})
        for key in sorted(value):
            _add_subtree(value[key], _join(path, key), operations)
    else:
        operations.append({"op": "add", "path": path, "value": value})


def _join(prefix: str, token: str) -> str:
    """Append one escaped reference token to a JSON Pointer prefix."""
    return prefix + "/" + escape_token(token)
