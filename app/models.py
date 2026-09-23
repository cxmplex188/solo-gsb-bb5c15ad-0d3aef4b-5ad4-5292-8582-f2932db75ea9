"""Request/response models for the merge API."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Any JSON document: object, array, string, number, boolean or null.
JsonValue = Optional[Any]

# Sides a conflict can be adjudicated to.
ResolutionSide = Literal["base", "left", "right"]

# Side a merged leaf ultimately came from.
ProvenanceSource = Literal["base", "left", "right", "merged"]


class ConflictResolution(BaseModel):
    """One adjudication: which side wins at a reported conflict."""

    model_config = ConfigDict(extra="forbid")

    pointer: str = Field(
        ...,
        description=(
            "JSON Pointer (RFC 6901) of a reported conflict, exactly as it "
            "appears in the response 'conflicts' (after identity-array "
            "indices are resolved)."
        ),
    )
    resolution: ResolutionSide = Field(
        ...,
        description=(
            "Side whose value is placed at the conflict location in the "
            "merged document. Choosing a side on which the location is "
            "deleted removes it; 'base' restores the default outcome."
        ),
    )


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    base: JsonValue = Field(
        ..., description="Common ancestor of the two revisions."
    )
    left: JsonValue = Field(..., description="The left-hand revision.")
    right: JsonValue = Field(..., description="The right-hand revision.")
    array_identity: dict[str, str] = Field(
        default_factory=dict,
        alias="arrayIdentity",
        description=(
            "Mapping from a JSON Pointer (RFC 6901) of an object array to the "
            "name of the item field that uniquely identifies an entry. The "
            "token '*' matches a single positional index, e.g. "
            "'/servers/*/disks' -> 'name'."
        ),
    )
    conflict_resolutions: list[ConflictResolution] = Field(
        default_factory=list,
        alias="conflictResolutions",
        description=(
            "Adjudications for reported conflicts. Each entry binds a "
            "conflict pointer to the winning side; conflicts not listed keep "
            "the base value. Malformed, duplicated, or non-conflict pointers "
            "are rejected with HTTP 400. The response 'conflicts' always "
            "report the original three-way values (unless masked via "
            "'redactPaths')."
        ),
    )
    protected_paths: list[str] = Field(
        default_factory=list,
        alias="protectedPaths",
        description=(
            "JSON Pointers (RFC 6901) of locations that must never be "
            "written automatically. A single '*' token matches one path "
            "segment. When either revision changes a protected location - "
            "even a one-sided change or an identical change on both sides - "
            "the base value is kept and a conflict with kind "
            "'protected_change' carrying the three-way values is reported. "
            "Fields of objects and identity-array items match their final "
            "reported pointer; deletions match their original path. An "
            "explicit conflictResolutions entry for the reported pointer "
            "overrides the protection; unresolved protected conflicts keep "
            "the base value. Malformed or duplicate pointers are rejected "
            "with HTTP 400."
        ),
    )
    redact_paths: list[str] = Field(
        default_factory=list,
        alias="redactPaths",
        description=(
            "JSON Pointers (RFC 6901) of locations whose values are "
            "sensitive and must be masked in the response 'conflicts'. A "
            "single '*' token matches one path segment. A conflict "
            "reported at or below a listed pointer has its "
            "base/left/right values replaced with the fixed string "
            "'[REDACTED]'; a pointer reaching below a reported conflict "
            "is applied recursively inside the three-way snapshots. "
            "Sides on which the location is absent stay null. Redaction "
            "only rewrites the reported conflicts: 'merged', conflict "
            "detection and 'conflictResolutions' always use the original "
            "values. Malformed or duplicate pointers are rejected with "
            "HTTP 400."
        ),
    )
    include_provenance: bool = Field(
        default=False,
        alias="includeProvenance",
        description=(
            "When true, the response includes a 'provenance' array "
            "describing, for every actual changed leaf or identity-array "
            "item in the final merged document, where its value came from "
            "('base', 'left', 'right' or 'merged'), whether it resulted "
            "from a conflict adjudication, and whether it was deleted. "
            "Records are sorted by their final JSON Pointer (RFC 6901); "
            "identity-array items use their final index and deleted items "
            "keep their original base pointer. When false (the default) "
            "the response shape is unchanged and no 'provenance' key is "
            "emitted."
        ),
    )
    include_patch: bool = Field(
        default=False,
        alias="includePatch",
        description=(
            "When true, the response additionally includes a 'patch' array "
            "of JSON Patch operations (RFC 6902) that transform 'base' "
            "into 'merged': applying the operations to base in order with "
            "an RFC 6902 processor yields exactly merged. Object "
            "differences are emitted recursively (add/remove/replace per "
            "member), arrays are always replaced wholesale, and JSON "
            "Pointer reference tokens are escaped per RFC 6901 ('~' -> "
            "'~0', '/' -> '~1'). When false (the default) the response "
            "shape is unchanged and no 'patch' key is emitted."
        ),
    )


class PatchOperation(BaseModel):
    """One RFC 6902 JSON Patch operation transforming base into merged."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "remove", "replace"] = Field(
        ...,
        description=(
            "RFC 6902 operation kind: 'add' (including new object members "
            "and a freshly added object subtree, added as {} first), "
            "'remove' (a dropped object member or subtree), 'replace' (a "
            "changed leaf value or an array replaced wholesale)."
        ),
    )
    path: str = Field(
        ...,
        description=(
            "JSON Pointer (RFC 6901) of the operation's target location. "
            "Reference tokens are escaped ('~' -> '~0', '/' -> '~1'); the "
            "empty string targets the whole document (root replacement)."
        ),
    )
    value: JsonValue = Field(
        default=None,
        description=(
            "The new value for 'add' and 'replace' operations. Omitted "
            "entirely from 'remove' operations."
        ),
    )


class Conflict(BaseModel):
    pointer: str = Field(..., description="JSON Pointer (RFC 6901) of the conflict.")
    kind: str = Field(
        ...,
        description=(
            "both_modified: both sides changed the value differently; "
            "delete_vs_modify: one side deleted what the other modified; "
            "both_added: both sides added different values at a path absent "
            "from base; protected_change: a protectedPaths location was "
            "changed by either revision (one-sided or identical changes "
            "included); the base value is kept until adjudicated."
        ),
    )
    base: JsonValue
    left: JsonValue
    right: JsonValue


class ProvenanceEntry(BaseModel):
    """Origin of one actual changed leaf or identity-array item."""

    model_config = ConfigDict(extra="forbid")

    pointer: str = Field(
        ...,
        description=(
            "JSON Pointer (RFC 6901) of the changed leaf or identity-array "
            "item in the final merged document. Identity-array items use "
            "their resolved final index. A deleted item keeps its original "
            "base pointer and is additionally marked with 'deleted'."
        ),
    )
    source: ProvenanceSource = Field(
        ...,
        description=(
            "Where the final value came from: 'left' or 'right' for a "
            "one-sided change or a conflict adjudicated to that side; "
            "'base' for an unresolved conflict or a protectedPaths freeze "
            "that kept the base value; 'merged' for an identical change on "
            "both sides."
        ),
    )
    adjudicated: bool = Field(
        ...,
        description=(
            "True when the final value (or deletion) was produced by an "
            "entry of conflictResolutions, including an adjudication that "
            "overrides protectedPaths; false for automatic merges and "
            "unresolved conflicts."
        ),
    )
    deleted: bool = Field(
        ...,
        description=(
            "True when the base carried a leaf/item at this pointer that "
            "the final document does not contain. The pointer is the "
            "deleted item's original base pointer."
        ),
    )


class MergeResponse(BaseModel):
    merged: JsonValue
    conflicts: list[Conflict]
    provenance: Optional[list[ProvenanceEntry]] = Field(
        default=None,
        description=(
            "Present only when the request set includeProvenance=true. "
            "One entry per actual changed leaf or identity-array item in "
            "the final document, sorted by 'pointer'. Absent entirely from "
            "the response when provenance was not requested."
        ),
    )
    patch: Optional[list[PatchOperation]] = Field(
        default=None,
        description=(
            "Present only when the request set includePatch=true. Ordered "
            "RFC 6902 JSON Patch operations that turn 'base' into "
            "'merged'. Objects differ field by field, arrays are replaced "
            "wholesale, and JSON Pointer tokens follow RFC 6901 escaping. "
            "Absent entirely from the response when the patch was not "
            "requested."
        ),
    )
