"""HTTP-level tests: /health, /merge, request-size enforcement."""

from __future__ import annotations

import json
import os

os.environ.setdefault("MAX_REQUEST_SIZE_BYTES", "10M")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import app
from app.middleware import RequestSizeLimitMiddleware
from tests.test_patch import apply_json_patch


def apply_patch(document, operations):
    """Apply an RFC 6902 patch from an API response (plain JSON values)."""
    return apply_json_patch(document, operations)


client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_merge_endpoint_full_envelope():
    payload = {
        "base": {"a": 1, "b": 2, "srv": [{"id": "x", "v": 1}]},
        "left": {"a": 10, "b": 2, "srv": [{"id": "x", "v": 1}, {"id": "y", "v": 2}]},
        "right": {"a": 1, "b": 20, "srv": [{"id": "x", "v": 3}]},
        "arrayIdentity": {"/srv": "id"},
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {
        "a": 10,
        "b": 20,
        "srv": [
            {"id": "x", "v": 3},  # right-only edit -> accepted
            {"id": "y", "v": 2},  # left-only add, sorted after x
        ],
    }
    assert body["conflicts"] == []


def test_merge_accepts_snake_case_alias_too():
    payload = {
        "base": {"srv": []},
        "left": {"srv": [{"id": "a"}]},
        "right": {"srv": []},
        "array_identity": {"/srv": "id"},
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"srv": [{"id": "a"}]}


def test_merge_defaults_array_identity_to_empty():
    response = client.post(
        "/merge",
        json={"base": [1, 2], "left": [1, 2, 3], "right": [1, 2]},
    )
    assert response.status_code == 200
    assert response.json()["merged"] == [1, 2, 3]
    assert response.json()["conflicts"] == []


def test_unknown_field_is_rejected():
    response = client.post(
        "/merge",
        json={"base": 1, "left": 1, "right": 1, "bogus": 1},
    )
    assert response.status_code == 422


def test_invalid_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"srv": []},
            "left": {"srv": []},
            "right": {"srv": []},
            "arrayIdentity": {"srv": "id"},  # missing leading slash
        },
    )
    assert response.status_code == 400
    assert "arrayIdentity" in response.json()["detail"]


def test_invalid_identity_field_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"srv": []},
            "left": {"srv": []},
            "right": {"srv": []},
            "arrayIdentity": {"/srv": ""},
        },
    )
    assert response.status_code == 400


# ------------------------------------------------------- conflict resolutions


def test_merge_applies_conflict_resolutions_to_nested_objects():
    payload = {
        "base": {"db": {"host": "a", "port": 5432}},
        "left": {"db": {"host": "b", "port": 5433}},
        "right": {"db": {"host": "c", "port": 5434}},
        "conflictResolutions": [{"pointer": "/db/host", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # /db/host adjudicated to the left; /db/port unlisted -> keeps base.
    assert body["merged"] == {"db": {"host": "b", "port": 5432}}
    # Conflicts still report the original three-way values for both.
    assert body["conflicts"] == [
        {"pointer": "/db/host", "kind": "both_modified",
         "base": "a", "left": "b", "right": "c"},
        {"pointer": "/db/port", "kind": "both_modified",
         "base": 5432, "left": 5433, "right": 5434},
    ]


def test_merge_applies_conflict_resolutions_to_identity_arrays():
    payload = {
        "base": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
        "left": {"servers": [{"id": "b", "port": 80}]},  # deletes a
        "right": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 81}]},
        "arrayIdentity": {"/servers": "id"},
        "conflictResolutions": [{"pointer": "/servers/0", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # The adjudicated item takes right's value; b's right-only edit is kept.
    assert body["merged"] == {
        "servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 81}]
    }
    assert body["conflicts"] == [
        {
            "pointer": "/servers/0",
            "kind": "delete_vs_modify",
            "base": {"id": "a", "port": 8080},
            "left": None,
            "right": {"id": "a", "port": 9090},
        }
    ]


def test_resolution_can_accept_a_deletion():
    payload = {
        "base": {"servers": [{"id": "a"}, {"id": "b"}]},
        "left": {"servers": [{"id": "b"}]},  # deletes a
        "right": {"servers": [{"id": "a", "x": 1}, {"id": "b"}]},  # modifies a
        "arrayIdentity": {"/servers": "id"},
        "conflictResolutions": [{"pointer": "/servers/0", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"servers": [{"id": "b"}]}


def test_identical_new_identity_item_with_numeric_spelling_appended_once():
    # Regression: an item absent from base, added with identical content
    # on both sides under JSON-equal numeric spellings (1 / 1.0), is
    # appended exactly once with no conflict.
    payload = {
        "base": {"servers": [{"id": "a"}]},
        "left": {"servers": [{"id": "a"}, {"id": 1, "v": 1}]},
        "right": {"servers": [{"id": "a"}, {"id": 1.0, "v": 1}]},
        "arrayIdentity": {"/servers": "id"},
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {
        "servers": [{"id": "a"}, {"id": 1, "v": 1}]
    }
    assert body["conflicts"] == []


def test_merge_accepts_snake_case_conflict_resolutions():
    payload = {
        "base": {"a": 1},
        "left": {"a": 2},
        "right": {"a": 3},
        "conflict_resolutions": [{"pointer": "/a", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["merged"] == {"a": 3}


def test_resolution_for_nonexistent_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "/nope", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_resolution_for_non_conflict_location_returns_400():
    # /a exists but merged cleanly (left-only edit).
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "conflictResolutions": [{"pointer": "/a", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "does not correspond to a reported conflict" in response.json()["detail"]


def test_duplicate_resolution_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [
                {"pointer": "/a", "resolution": "left"},
                {"pointer": "/a", "resolution": "right"},
            ],
        },
    )
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"]


def test_invalid_resolution_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "a", "resolution": "left"}],
        },
    )
    assert response.status_code == 400
    assert "conflictResolutions" in response.json()["detail"]


def test_invalid_resolution_side_returns_422():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [{"pointer": "/a", "resolution": "sideways"}],
        },
    )
    assert response.status_code == 422


def test_resolution_entry_with_unknown_field_returns_422():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "conflictResolutions": [
                {"pointer": "/a", "resolution": "left", "note": "bogus"}
            ],
        },
    )
    assert response.status_code == 422


# ------------------------------------------------------------- protectedPaths


def test_protected_paths_blocks_one_sided_change():
    payload = {
        "base": {"a": 1, "b": 2},
        "left": {"a": 99, "b": 2},
        "right": {"a": 1, "b": 20},
        "protectedPaths": ["/a"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # /a is protected: base kept despite a one-sided change; /b auto-merges.
    assert body["merged"] == {"a": 1, "b": 20}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 99, "right": 1}
    ]


def test_protected_paths_blocks_identical_change_on_both_sides():
    payload = {
        "base": {"a": 1},
        "left": {"a": 9},
        "right": {"a": 9},
        "protectedPaths": ["/a"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 9, "right": 9}
    ]


def test_protected_paths_accepts_snake_case_alias():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protected_paths": ["/a"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert body["conflicts"][0]["kind"] == "protected_change"


def test_protected_paths_wildcard_matches_one_segment():
    payload = {
        "base": {"o": {"x": 1, "y": 1}},
        "left": {"o": {"x": 9, "y": 1}},
        "right": {"o": {"x": 1, "y": 8}},
        "protectedPaths": ["/o/x", "/o/y"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"o": {"x": 1, "y": 1}}
    assert {c["pointer"] for c in body["conflicts"]} == {"/o/x", "/o/y"}
    assert all(c["kind"] == "protected_change" for c in body["conflicts"])


def test_protected_paths_identity_array_final_pointer_and_original_path():
    # Field edit matches the item's final pointer...
    payload = {
        "base": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 80},
        ]},
        "left": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 9090},
        ]},
        "right": {"servers": [
            {"id": "a", "port": 8080},
            {"id": "b", "port": 80},
        ]},
        "arrayIdentity": {"/servers": "id"},
        "protectedPaths": ["/servers/1/port"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"servers": [
        {"id": "a", "port": 8080},
        {"id": "b", "port": 80},
    ]}
    assert body["conflicts"] == [{
        "pointer": "/servers/1/port",
        "kind": "protected_change",
        "base": 80, "left": 9090, "right": 80,
    }]

    # ... while a deletion matches the item's original base index.
    payload["left"] = {"servers": [{"id": "b", "port": 80}]}  # deletes a
    payload["protectedPaths"] = ["/servers/0"]
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"servers": [
        {"id": "a", "port": 8080},
        {"id": "b", "port": 80},
    ]}
    assert body["conflicts"] == [{
        "pointer": "/servers/0",
        "kind": "protected_change",
        "base": {"id": "a", "port": 8080},
        "left": None,
        "right": {"id": "a", "port": 8080},
    }]


def test_protected_path_inside_one_sided_object_add_is_atomic():
    # The whole one-sided object block stays out of merged when it carries
    # a hit protected descendant; it is never assembled field by field.
    payload = {
        "base": {"k": 1},
        "left": {"k": 1, "o": {"secret": "rotated", "host": "h"}},
        "right": {"k": 1},
        "protectedPaths": ["/o/secret"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"k": 1}
    assert body["conflicts"] == [
        {"pointer": "/o/secret", "kind": "protected_change",
         "base": None, "left": "rotated", "right": None}
    ]

    # An explicit resolution writes the whole block.
    payload["conflictResolutions"] = [
        {"pointer": "/o/secret", "resolution": "left"}
    ]
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"k": 1, "o": {"secret": "rotated", "host": "h"}}
    # The reported conflict keeps the original three-way values.
    assert body["conflicts"] == [
        {"pointer": "/o/secret", "kind": "protected_change",
         "base": None, "left": "rotated", "right": None}
    ]


def test_protected_field_inside_one_sided_deleted_identity_item_keeps_item():
    payload = {
        "base": {"servers": [{"id": "a", "port": 8080, "tag": "x"},
                             {"id": "b", "port": 80}]},
        "left": {"servers": [{"id": "b", "port": 80}]},  # deletes a
        "right": {"servers": [{"id": "a", "port": 8080, "tag": "x"},
                              {"id": "b", "port": 80}]},
        "arrayIdentity": {"/servers": "id"},
        "protectedPaths": ["/servers/0/port"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # The deleted item is kept wholesale (including its sibling field).
    assert body["merged"] == payload["base"]
    assert body["conflicts"] == [{
        "pointer": "/servers/0/port",
        "kind": "protected_change",
        "base": 8080,
        "left": None,
        "right": 8080,
    }]


def test_conflict_resolution_overrides_protected_path():
    payload = {
        "base": {"a": 1},
        "left": {"a": 9},
        "right": {"a": 1},
        "protectedPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "left"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # Explicit adjudication overrides the protection...
    assert body["merged"] == {"a": 9}
    # ... but the conflict is still reported with the original three-way
    # values, keeping kind protected_change.
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "protected_change",
         "base": 1, "left": 9, "right": 1}
    ]


def test_unresolved_protected_conflict_keeps_base_with_other_resolution():
    payload = {
        "base": {"a": 1, "b": 1},
        "left": {"a": 2, "b": 2},
        "right": {"a": 3, "b": 3},
        "protectedPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/b", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1, "b": 3}
    assert {c["pointer"] for c in body["conflicts"]} == {"/a", "/b"}


def test_invalid_protected_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protectedPaths": ["a"],  # missing leading slash
        },
    )
    assert response.status_code == 400
    assert "protectedPaths" in response.json()["detail"]


def test_duplicate_protected_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 1, "b": 1},
            "protectedPaths": ["/a", "/a"],
        },
    )
    assert response.status_code == 400
    assert "duplicate protectedPaths" in response.json()["detail"]


def test_protected_paths_entry_must_be_a_string():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "protectedPaths": [1],
        },
    )
    assert response.status_code == 422


def test_protected_paths_appear_in_openapi_schema():
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MergeRequest"]["properties"]
    assert "protectedPaths" in properties
    conflict_props = schema["components"]["schemas"]["Conflict"]["properties"]
    assert "protected_change" in conflict_props["kind"]["description"]


# --------------------------------------------------------------- redactPaths


def test_redact_paths_masks_exact_conflict_values():
    payload = {
        "base": {"db": {"host": "a", "password": "old"}},
        "left": {"db": {"host": "b", "password": "new-left"}},
        "right": {"db": {"host": "b", "password": "new-right"}},
        "redactPaths": ["/db/password"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # merged keeps the original values; only the reported conflict is masked.
    assert body["merged"] == {"db": {"host": "b", "password": "old"}}
    assert body["conflicts"] == [
        {"pointer": "/db/password", "kind": "both_modified",
         "base": "[REDACTED]", "left": "[REDACTED]", "right": "[REDACTED]"}
    ]


def test_redact_paths_missing_branch_stays_null():
    payload = {
        "base": {"a": 1},
        "left": {},  # deletes a
        "right": {"a": 3},
        "redactPaths": ["/a"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "delete_vs_modify",
         "base": "[REDACTED]", "left": None, "right": "[REDACTED]"}
    ]


def test_redact_paths_recurses_into_ancestor_conflict():
    payload = {
        "base": {"db": {"host": "a", "password": "old"}},
        "left": {"db": {"host": "b", "password": "new-left"}},
        "right": {},  # deletes db
        "redactPaths": ["/db/password"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # The whole /db object conflicts; the rule reaches inside the snapshots.
    assert body["merged"] == {"db": {"host": "a", "password": "old"}}
    assert body["conflicts"] == [
        {
            "pointer": "/db",
            "kind": "delete_vs_modify",
            "base": {"host": "a", "password": "[REDACTED]"},
            "left": {"host": "b", "password": "[REDACTED]"},
            "right": None,
        }
    ]


def test_redact_paths_rule_above_conflict_masks_wholesale():
    payload = {
        "base": {"db": {"password": "old"}},
        "left": {"db": {"password": "new-left"}},
        "right": {"db": {"password": "new-right"}},
        "redactPaths": ["/db"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    assert response.json()["conflicts"] == [
        {"pointer": "/db/password", "kind": "both_modified",
         "base": "[REDACTED]", "left": "[REDACTED]", "right": "[REDACTED]"}
    ]


def test_redact_paths_wildcard_matches_identity_array_items():
    payload = {
        "base": {"servers": [{"id": "a", "password": "p0"}]},
        "left": {"servers": []},  # deletes a
        "right": {"servers": [{"id": "a", "password": "p1"}]},
        "arrayIdentity": {"/servers": "id"},
        "redactPaths": ["/servers/*/password"],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == payload["base"]
    assert body["conflicts"] == [
        {
            "pointer": "/servers/0",
            "kind": "delete_vs_modify",
            "base": {"id": "a", "password": "[REDACTED]"},
            "left": None,
            "right": {"id": "a", "password": "[REDACTED]"},
        }
    ]


def test_redact_paths_does_not_change_merged_or_resolutions():
    payload = {
        "base": {"a": 1},
        "left": {"a": 2},
        "right": {"a": 3},
        "redactPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
    }
    response = client.post("/merge", json=payload)
    assert response.status_code == 200
    body = response.json()
    # Adjudication still uses the original values and pointers.
    assert body["merged"] == {"a": 3}
    assert body["conflicts"] == [
        {"pointer": "/a", "kind": "both_modified",
         "base": "[REDACTED]", "left": "[REDACTED]", "right": "[REDACTED]"}
    ]


def test_redact_paths_accepts_snake_case_alias():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 3},
            "redact_paths": ["/a"],
        },
    )
    assert response.status_code == 200
    assert response.json()["conflicts"][0]["base"] == "[REDACTED]"


def test_redact_paths_without_conflicts_is_a_noop():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "redactPaths": ["/a"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["merged"] == {"a": 2}
    assert body["conflicts"] == []


def test_invalid_redact_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "redactPaths": ["a"],  # missing leading slash
        },
    )
    assert response.status_code == 400
    assert "redactPaths" in response.json()["detail"]


def test_duplicate_redact_pointer_returns_400():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "redactPaths": ["/a", "/a"],
        },
    )
    assert response.status_code == 400
    assert "duplicate redactPaths" in response.json()["detail"]

    # Escaped spellings decoding to the same tokens collide too.
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "redactPaths": ["/~~", "/~0~0"],
        },
    )
    assert response.status_code == 400
    assert "duplicate redactPaths" in response.json()["detail"]


def test_redact_paths_entry_must_be_a_string():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1},
            "left": {"a": 2},
            "right": {"a": 1},
            "redactPaths": [1],
        },
    )
    assert response.status_code == 422


def test_redact_paths_appear_in_openapi_schema():
    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["MergeRequest"]["properties"]
    assert "redactPaths" in properties
    assert "[REDACTED]" in properties["redactPaths"]["description"]


# ----------------------------------------------------------- includeProvenance


def test_provenance_absent_from_response_by_default():
    response = client.post(
        "/merge",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1}},
    )
    assert response.status_code == 200
    # The legacy envelope is unchanged: no provenance key at all.
    assert set(response.json().keys()) == {"merged", "conflicts"}


def test_provenance_absent_when_explicitly_false():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1},
            "includeProvenance": False,
        },
    )
    assert response.status_code == 200
    assert set(response.json().keys()) == {"merged", "conflicts"}


def test_provenance_accepts_snake_case_alias():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1},
            "include_provenance": True,
        },
    )
    assert response.status_code == 200
    assert "provenance" in response.json()


def test_provenance_empty_when_nothing_changed():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 1}, "right": {"a": 1},
            "includeProvenance": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["provenance"] == []


def test_provenance_records_basic_leaf_sources():
    payload = {
        "base": {"a": 1, "b": 2, "same": 9, "keep": 0},
        "left": {"a": 10, "b": 2, "same": 99, "keep": 0},
        "right": {"a": 1, "b": 20, "same": 99, "keep": 0},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["provenance"] == [
        {"pointer": "/a", "source": "left",
         "adjudicated": False, "deleted": False},
        {"pointer": "/b", "source": "right",
         "adjudicated": False, "deleted": False},
        # Identical change on both sides is sourced "merged".
        {"pointer": "/same", "source": "merged",
         "adjudicated": False, "deleted": False},
    ]
    # Unchanged leaves (/keep) are not recorded; order is by pointer.
    assert [e["pointer"] for e in body["provenance"]] == sorted(
        e["pointer"] for e in body["provenance"]
    )


def test_provenance_records_addition_and_deletion():
    payload = {
        "base": {"a": 1, "gone": 5},
        "left": {"a": 1, "gone": 5, "added_left": 7},
        "right": {"a": 1},  # deletes gone
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert {(e["pointer"], e["source"], e["deleted"]) for e in body["provenance"]} == {
        ("/added_left", "left", False),
        ("/gone", "right", True),
    }
    deleted = next(e for e in body["provenance"] if e["deleted"])
    assert deleted == {
        "pointer": "/gone", "source": "right",
        "adjudicated": False, "deleted": True,
    }


def test_provenance_deletion_on_both_sides_is_merged():
    payload = {
        "base": {"a": 1}, "left": {}, "right": {},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["provenance"] == [
        {"pointer": "/a", "source": "merged",
         "adjudicated": False, "deleted": True}
    ]


def test_provenance_unresolved_conflict_keeps_base():
    payload = {
        "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"a": 1}
    assert body["provenance"] == [
        {"pointer": "/a", "source": "base",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_marks_conflict_adjudications():
    payload = {
        "base": {"a": 1, "b": 1},
        "left": {"a": 2, "b": 2},
        "right": {"a": 3, "b": 3},
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"a": 3, "b": 1}
    assert body["provenance"] == [
        {"pointer": "/a", "source": "right",
         "adjudicated": True, "deleted": False},
        {"pointer": "/b", "source": "base",
         "adjudicated": False, "deleted": False},
    ]


def test_provenance_adjudication_to_base_is_adjudicated():
    payload = {
        "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
        "conflictResolutions": [{"pointer": "/a", "resolution": "base"}],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["provenance"] == [
        {"pointer": "/a", "source": "base",
         "adjudicated": True, "deleted": False}
    ]


def test_provenance_adjudicated_deletion_is_marked():
    payload = {
        "base": {"servers": [{"id": "a"}, {"id": "b"}]},
        "left": {"servers": [{"id": "b"}]},  # deletes a
        "right": {"servers": [{"id": "a", "x": 1}, {"id": "b"}]},  # edits a
        "arrayIdentity": {"/servers": "id"},
        "conflictResolutions": [{"pointer": "/servers/0", "resolution": "left"}],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"servers": [{"id": "b"}]}
    assert body["provenance"] == [
        {"pointer": "/servers/0", "source": "left",
         "adjudicated": True, "deleted": True}
    ]


def test_provenance_protected_path_keeps_base():
    payload = {
        "base": {"a": 1, "b": 2},
        "left": {"a": 99, "b": 2},
        "right": {"a": 1, "b": 20},
        "protectedPaths": ["/a"],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"a": 1, "b": 20}
    assert body["provenance"] == [
        {"pointer": "/a", "source": "base",
         "adjudicated": False, "deleted": False},
        {"pointer": "/b", "source": "right",
         "adjudicated": False, "deleted": False},
    ]


def test_provenance_protected_path_adjudicated():
    payload = {
        "base": {"a": 1}, "left": {"a": 9}, "right": {"a": 1},
        "protectedPaths": ["/a"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "left"}],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"a": 9}
    assert body["provenance"] == [
        {"pointer": "/a", "source": "left",
         "adjudicated": True, "deleted": False}
    ]


def test_provenance_withheld_protected_addition_records_nothing():
    # A one-sided object add carrying a hit protected descendant is
    # withheld wholesale: absent from both base and merged, so no final
    # pointer exists and provenance records nothing for it.
    payload = {
        "base": {"k": 1},
        "left": {"k": 1, "o": {"secret": "rotated", "host": "h"}},
        "right": {"k": 1},
        "protectedPaths": ["/o/secret"],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"k": 1}
    assert body["provenance"] == []


def test_provenance_identity_array_uses_final_indices():
    payload = {
        "base": {"servers": [
            {"id": "a", "v": 1},
            {"id": "b", "v": 1},
        ]},
        # left deletes a, edits nothing else; right edits b.
        "left": {"servers": [{"id": "b", "v": 1}]},
        "right": {"servers": [
            {"id": "a", "v": 1},
            {"id": "b", "v": 8},
            {"id": "c", "v": 3},
        ]},
        "arrayIdentity": {"/servers": "id"},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    # Deletion of a accepted (right touched a but left deleted it; right's
    # edit is on b only): a is a delete_vs_modify? left deletes a, right
    # leaves a untouched -> accepted deletion.
    assert body["merged"] == {"servers": [
        {"id": "b", "v": 8},
        {"id": "c", "v": 3},
    ]}
    entries = {(e["pointer"], e["source"], e["deleted"])
               for e in body["provenance"]}
    # The deleted item keeps its ORIGINAL base pointer /servers/0 ...
    assert ("/servers/0", "left", True) in entries
    # ... while surviving item b, now at final index 0, uses the new index.
    assert ("/servers/0", "right", False) in entries
    # New item c appended at final index 1.
    assert ("/servers/1", "right", False) in entries
    # Sorted by the final pointer strings.
    assert [e["pointer"] for e in body["provenance"]] == sorted(
        e["pointer"] for e in body["provenance"]
    )


def test_provenance_identity_array_new_identical_item_is_merged():
    payload = {
        "base": {"servers": [{"id": "a"}]},
        "left": {"servers": [{"id": "a"}, {"id": "z", "v": 1}]},
        "right": {"servers": [{"id": "a"}, {"id": "z", "v": 1}]},
        "arrayIdentity": {"/servers": "id"},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["provenance"] == [
        {"pointer": "/servers/1", "source": "merged",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_identity_array_field_assembly():
    payload = {
        "base": {"servers": [{"id": "a", "v": 1, "w": 1}]},
        "left": {"servers": [{"id": "a", "v": 2, "w": 1}]},
        "right": {"servers": [{"id": "a", "v": 1, "w": 9}]},
        "arrayIdentity": {"/servers": "id"},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"servers": [{"id": "a", "v": 2, "w": 9}]}
    # One record per item (object array unit), assembled from both sides.
    assert body["provenance"] == [
        {"pointer": "/servers/0", "source": "merged",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_plain_array_is_one_atomic_leaf():
    payload = {
        "base": {"a": [1, 2]}, "left": {"a": [1, 2, 3]},
        "right": {"a": [1, 2]}, "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["provenance"] == [
        {"pointer": "/a", "source": "left",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_root_level_change():
    payload = {
        "base": 1, "left": 1, "right": 5,
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == 5
    assert body["provenance"] == [
        {"pointer": "", "source": "right",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_added_empty_object_is_a_leaf():
    payload = {
        "base": {"k": 1},
        "left": {"k": 1, "o": {}},
        "right": {"k": 1},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["merged"] == {"k": 1, "o": {}}
    assert body["provenance"] == [
        {"pointer": "/o", "source": "left",
         "adjudicated": False, "deleted": False}
    ]


def test_provenance_redaction_does_not_change_provenance():
    payload = {
        "base": {"a": 1, "pw": "x"},
        "left": {"a": 2, "pw": "L"},
        "right": {"a": 3, "pw": "R"},
        "redactPaths": ["/pw"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    # Conflicts are masked...
    pw_conflict = next(c for c in body["conflicts"] if c["pointer"] == "/pw")
    assert pw_conflict["base"] == "[REDACTED]"
    # ...but provenance reflects the true merged document (pw kept base).
    assert body["merged"]["pw"] == "x"
    assert {(e["pointer"], e["source"]) for e in body["provenance"]} == {
        ("/a", "right"), ("/pw", "base"),
    }


def test_provenance_toggle_does_not_change_merged_or_conflicts():
    payload = {
        "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
    }
    off = client.post("/merge", json=payload).json()
    on = client.post("/merge", json={**payload, "includeProvenance": True}).json()
    assert on["merged"] == off["merged"]
    assert on["conflicts"] == off["conflicts"]


def test_provenance_pointer_is_stable_and_sorted():
    payload = {
        "base": {"z": 1, "m": {"d": 1, "a": 1}, "a": [1]},
        "left": {"z": 2, "m": {"d": 2, "a": 2}, "a": [1, 2]},
        "right": {"z": 1, "m": {"d": 1, "a": 1}, "a": [1]},
        "includeProvenance": True,
    }
    body = client.post("/merge", json=payload).json()
    pointers = [e["pointer"] for e in body["provenance"]]
    assert pointers == sorted(pointers)


def test_include_provenance_must_be_boolean():
    response = client.post(
        "/merge",
        json={
            "base": 1, "left": 1, "right": 1,
            "includeProvenance": ["please"],
        },
    )
    assert response.status_code == 422


def test_provenance_entry_shape_is_validated_in_schema():
    schema = client.get("/openapi.json").json()
    req_props = schema["components"]["schemas"]["MergeRequest"]["properties"]
    assert "includeProvenance" in req_props
    provenance_entry = schema["components"]["schemas"]["ProvenanceEntry"]
    assert set(provenance_entry["required"]) == {
        "pointer", "source", "adjudicated", "deleted"
    }
    assert set(provenance_entry["properties"]["source"]["enum"]) == {
        "base", "left", "right", "merged"
    }
    response_schema = schema["components"]["schemas"]["MergeResponse"]
    # provenance is optional (not required) and accepts an array or null.
    assert "provenance" not in response_schema["required"]
    response_props = response_schema["properties"]
    assert "provenance" in response_props
    any_of = response_props["provenance"]["anyOf"]
    assert {"type": "null"} in any_of
    assert any(spec.get("type") == "array" for spec in any_of)
    # Only merged and conflicts are required at the envelope level.
    assert response_schema["required"] == ["merged", "conflicts"]


# --------------------------------------------------------------- includePatch


def test_patch_absent_from_response_by_default():
    response = client.post(
        "/merge",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1}},
    )
    assert response.status_code == 200
    # The legacy envelope is unchanged: no patch key at all.
    assert set(response.json().keys()) == {"merged", "conflicts"}


def test_patch_absent_when_explicitly_false():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1},
            "includePatch": False,
        },
    )
    assert response.status_code == 200
    assert set(response.json().keys()) == {"merged", "conflicts"}


def test_patch_accepts_snake_case_alias():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1},
            "include_patch": True,
        },
    )
    assert response.status_code == 200
    assert "patch" in response.json()


def test_patch_is_empty_when_nothing_changed():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 1}, "right": {"a": 1},
            "includePatch": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["patch"] == []


def test_patch_rebuilds_merged_for_mixed_object_changes():
    payload = {
        "base": {
            "keep": 0,
            "scalar": 1,
            "gone": {"x": [1, 2]},
            "o": {"a": 1, "b": 2},
        },
        "left": {
            "keep": 0,
            "scalar": 10,
            "added": {"deep": [9]},
            "o": {"a": 11, "b": 2},
        },
        "right": {
            "keep": 0,
            "scalar": 1,
            "o": {"a": 1, "b": 20, "c": 3},
        },
        "includePatch": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["conflicts"] == []
    # Operations walk raw object keys in sorted order at each object level.
    assert body["patch"] == [
        {"op": "add", "path": "/added", "value": {}},
        {"op": "add", "path": "/added/deep", "value": [9]},
        {"op": "remove", "path": "/gone"},
        {"op": "replace", "path": "/o/a", "value": 11},
        {"op": "replace", "path": "/o/b", "value": 20},
        {"op": "add", "path": "/o/c", "value": 3},
        {"op": "replace", "path": "/scalar", "value": 10},
    ]
    assert apply_patch(payload["base"], body["patch"]) == body["merged"]


def test_patch_for_unresolved_conflict_keeps_base_and_is_empty():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
            "includePatch": True,
        },
    )
    body = response.json()
    assert body["merged"] == {"a": 1}
    assert len(body["conflicts"]) == 1
    # merged equals base, so the patch is an empty no-op sequence.
    assert body["patch"] == []


def test_patch_reflects_conflict_adjudication():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
            "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
            "includePatch": True,
        },
    )
    body = response.json()
    assert body["merged"] == {"a": 3}
    assert body["patch"] == [
        {"op": "replace", "path": "/a", "value": 3}
    ]
    assert apply_patch({"a": 1}, body["patch"]) == body["merged"]


def test_patch_replaces_plain_arrays_wholesale():
    payload = {
        "base": {"arr": [1, 2, 3]},
        "left": {"arr": [1, 2, 4, 5]},
        "right": {"arr": [1, 2, 3]},
        "includePatch": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["patch"] == [
        {"op": "replace", "path": "/arr", "value": [1, 2, 4, 5]}
    ]
    assert apply_patch(payload["base"], body["patch"]) == body["merged"]


def test_patch_replaces_identity_array_wholesale():
    # Identity merging itemizes the array internally, but the RFC 6902 patch
    # still treats arrays atomically: one 'replace' of the whole array.
    payload = {
        "base": {"servers": [{"id": "a", "v": 1}, {"id": "b", "v": 1}]},
        "left": {"servers": [{"id": "b", "v": 1}]},  # deletes a
        "right": {"servers": [
            {"id": "a", "v": 1},
            {"id": "b", "v": 8},
            {"id": "c", "v": 3},
        ]},
        "arrayIdentity": {"/servers": "id"},
        "includePatch": True,
    }
    body = client.post("/merge", json=payload).json()
    assert body["conflicts"] == []
    assert body["patch"] == [
        {
            "op": "replace",
            "path": "/servers",
            "value": [
                {"id": "b", "v": 8},
                {"id": "c", "v": 3},
            ],
        }
    ]
    assert apply_patch(payload["base"], body["patch"]) == body["merged"]


def test_patch_escapes_pointer_tokens():
    response = client.post(
        "/merge",
        json={
            "base": {"a/b": 1, "~t": 2},
            "left": {"a/b": 9, "~t": 2, "new~k": 5},
            "right": {"a/b": 1, "~t": 2},
            "includePatch": True,
        },
    )
    body = response.json()
    assert body["patch"] == [
        {"op": "replace", "path": "/a~1b", "value": 9},
        {"op": "add", "path": "/new~0k", "value": 5},
    ]
    assert apply_patch({"a/b": 1, "~t": 2}, body["patch"]) == body["merged"]


def test_patch_root_level_replacement():
    response = client.post(
        "/merge",
        json={"base": [1, 2], "left": [1, 2, 3], "right": [1, 2],
              "includePatch": True},
    )
    body = response.json()
    assert body["patch"] == [
        {"op": "replace", "path": "", "value": [1, 2, 3]}
    ]
    assert apply_patch([1, 2], body["patch"]) == body["merged"]


def test_patch_removed_object_subtree_is_one_operation():
    response = client.post(
        "/merge",
        json={
            "base": {"o": {"x": 1}}, "left": {}, "right": {},
            "includePatch": True,
        },
    )
    body = response.json()
    assert body["patch"] == [{"op": "remove", "path": "/o"}]
    assert apply_patch({"o": {"x": 1}}, body["patch"]) == body["merged"]


def test_patch_redaction_does_not_leak_or_change_patch():
    payload = {
        "base": {"a": 1, "pw": "x"},
        "left": {"a": 2, "pw": "L"},
        "right": {"a": 3, "pw": "R"},
        "redactPaths": ["/pw"],
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
        "includePatch": True,
    }
    body = client.post("/merge", json=payload).json()
    # Conflicts are masked...
    pw_conflict = next(c for c in body["conflicts"] if c["pointer"] == "/pw")
    assert pw_conflict["base"] == "[REDACTED]"
    # ...the patch is built from the true merged document (/a adjudicated,
    # /pw frozen at base) and carries no redaction marker.
    assert body["merged"] == {"a": 3, "pw": "x"}
    assert body["patch"] == [{"op": "replace", "path": "/a", "value": 3}]
    serialized = json.dumps(body["patch"])
    assert "[REDACTED]" not in serialized
    assert apply_patch(payload["base"], body["patch"]) == body["merged"]


def test_patch_and_provenance_can_be_requested_together():
    response = client.post(
        "/merge",
        json={
            "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 1},
            "includePatch": True, "includeProvenance": True,
        },
    )
    body = response.json()
    assert set(body.keys()) == {"merged", "conflicts", "provenance", "patch"}
    assert body["patch"] == [{"op": "replace", "path": "/a", "value": 2}]
    assert body["provenance"] == [
        {"pointer": "/a", "source": "left",
         "adjudicated": False, "deleted": False}
    ]


def test_patch_toggle_does_not_change_merged_or_conflicts():
    payload = {
        "base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3},
        "conflictResolutions": [{"pointer": "/a", "resolution": "right"}],
    }
    off = client.post("/merge", json=payload).json()
    on = client.post("/merge", json={**payload, "includePatch": True}).json()
    assert on["merged"] == off["merged"]
    assert on["conflicts"] == off["conflicts"]


def test_patch_roundtrip_over_all_engine_fixtures():
    # Property-style guarantee over a spread of merge scenarios: applying
    # the reported patch to the request base always yields reported merged.
    scenarios = [
        # one-sided edits, identical edits, deletions
        ({"a": 1, "b": 2}, {"a": 10}, {"b": 20}, {}),
        ({"a": 1}, {}, {}, {}),
        # unresolved conflict keeps base; adjudicated conflict takes right
        ({"a": 1, "b": 1}, {"a": 2, "b": 2}, {"a": 3, "b": 3},
         {"conflictResolutions": [{"pointer": "/a", "resolution": "right"}]}),
        # nested objects and added subtrees
        ({"o": {"x": 1}}, {"o": {"x": 1, "y": {"z": 2}}}, {"o": {"x": 1}}, {}),
        # null values and type changes
        ({"a": 1, "b": []}, {"a": None, "b": {}}, {"a": 1, "b": []}, {}),
        # plain array wholesale
        ({"a": [1, 2]}, {"a": [9]}, {"a": [1, 2]}, {}),
        # identity array
        ({"s": [{"id": "a"}, {"id": "b"}]},
         {"s": [{"id": "b"}, {"id": "c"}]},
         {"s": [{"id": "a", "v": 1}, {"id": "b"}]},
         {"arrayIdentity": {"/s": "id"}}),
        # escaped keys
        ({"a/b": {"~": 1}}, {"a/b": {"~": 2}}, {"a/b": {"~": 1}}, {}),
    ]
    for base, left, right, options in scenarios:
        payload = {"base": base, "left": left, "right": right,
                   "includePatch": True, **options}
        body = client.post("/merge", json=payload).json()
        assert apply_patch(base, body["patch"]) == body["merged"]


def test_include_patch_must_be_boolean():
    response = client.post(
        "/merge",
        json={
            "base": 1, "left": 1, "right": 1,
            "includePatch": ["please"],
        },
    )
    assert response.status_code == 422


def test_patch_operation_shape_is_validated_in_schema():
    schema = client.get("/openapi.json").json()
    req_props = schema["components"]["schemas"]["MergeRequest"]["properties"]
    assert "includePatch" in req_props
    operation = schema["components"]["schemas"]["PatchOperation"]
    assert operation["required"] == ["op", "path"]
    assert set(operation["properties"]["op"]["enum"]) == {
        "add", "remove", "replace"
    }
    response_schema = schema["components"]["schemas"]["MergeResponse"]
    # patch is optional (not required) and accepts an array or null.
    assert "patch" not in response_schema["required"]
    response_props = response_schema["properties"]
    assert "patch" in response_props
    any_of = response_props["patch"]["anyOf"]
    assert {"type": "null"} in any_of
    assert any(spec.get("type") == "array" for spec in any_of)
    assert response_schema["required"] == ["merged", "conflicts"]


# ------------------------------------------------------------- size limiting


@pytest.fixture
def limited_client():
    small = FastAPI()

    @small.post("/echo")
    async def echo(request: Request) -> dict:
        return {"size": len(await request.body())}

    small.add_middleware(RequestSizeLimitMiddleware, max_body_size=100)
    return TestClient(small)


def test_request_under_limit_passes(limited_client):
    response = limited_client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"size": 100}


def test_request_over_declared_content_length_rejected(limited_client):
    response = limited_client.post(
        "/echo",
        content=b"x" * 101,
        headers={"Content-Length": "101"},
    )
    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"]


def test_chunked_oversized_body_rejected_even_without_content_length():
    small = FastAPI()

    @small.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    small.add_middleware(RequestSizeLimitMiddleware, max_body_size=100)

    def chunks():
        yield b"x" * 60
        yield b"x" * 60  # total 120 > 100

    with TestClient(small) as tc:
        response = tc.post(
            "/echo",
            content=chunks(),
            headers={"Transfer-Encoding": "chunked"},
        )
    assert response.status_code == 413


def test_size_setting_parses_suffixes():
    assert _parse("10M") == 10 * 1024 * 1024
    assert _parse("256K") == 256 * 1024
    assert _parse("1G") == 1024**3
    assert _parse("0") == 0


def _parse(value: str) -> int:
    from app.config import _parse_size

    return _parse_size(value)
