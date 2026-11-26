"""Planner与工具共用的语义条件；程序只执行与现有数据字段对应的部分。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sentrix.domain.retrieval.query_contracts import resolve_time_expression
from sentrix.domain.retrieval.time_expression import parse_time_expression

ROLES = {
    "time": ("capture", "event", "document"),
    "place": ("capture", "mentioned", "venue", "origin", "destination"),
    "person": ("depicted", "photographer", "author", "narrator", "animal"),
    "media": ("asset",),
    "source": ("asset",),
    "quantity": ("asset", "item", "money", "event"),
    "event": ("anchor",),
}


@dataclass(frozen=True, slots=True)
class QueryCondition:
    dimension: str
    value: str
    role: str
    required: bool = True
    # anchor是关系起点或待取证事件；relation可为母亲/姐姐等多段路径。
    anchor: str = ""
    relation: str = "is"
    state: str = "resolved"

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def condition_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string", "enum": list(ROLES)},
                "value": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": sorted({role for roles in ROLES.values() for role in roles}),
                },
                "required": {"type": "boolean"},
                "anchor": {"type": "string"},
                "relation": {"type": "string"},
                "state": {"type": "string", "enum": ["resolved", "pending"]},
            },
            "required": ["dimension", "value", "role", "required", "anchor", "relation", "state"],
            "additionalProperties": False,
        },
    }


def parse_conditions(raw: Any) -> tuple[QueryCondition, ...]:
    if not isinstance(raw, list):
        raise ValueError("conditions_must_be_array")
    conditions = []
    keys = set(condition_schema()["items"]["properties"])
    for row in raw:
        if not isinstance(row, Mapping) or set(row) - keys:
            raise ValueError("invalid_condition")
        dimension, role = row.get("dimension"), row.get("role")
        if dimension not in ROLES or role not in ROLES[dimension]:
            raise ValueError("invalid_condition_role")
        if not isinstance(row.get("value"), str) or not row["value"].strip():
            raise ValueError("condition_value_required")
        if type(row.get("required", True)) is not bool or row.get("state", "resolved") not in {
            "resolved",
            "pending",
        }:
            raise ValueError("invalid_condition_state")
        if any(not isinstance(row.get(key, ""), str) for key in ("anchor", "relation")):
            raise ValueError("invalid_condition_reference")
        conditions.append(
            QueryCondition(
                dimension=dimension,
                value=row["value"].strip(),
                role=role,
                required=row.get("required", True),
                anchor=row.get("anchor", "").strip(),
                relation=row.get("relation", "is").strip(),
                state=row.get("state", "resolved"),
            )
        )
    return tuple(dict.fromkeys(conditions))


def legacy_conditions(scope: Mapping[str, str], *, face_identity: bool) -> tuple[QueryCondition, ...]:
    return tuple(
        QueryCondition(key, value, "depicted" if key == "person" else "capture")
        for key, value in scope.items()
        if value and (key in {"time", "place"} or key == "person" and face_identity)
    )


def scope_projection(conditions: Sequence[QueryCondition]) -> tuple[tuple[str, str], ...]:
    scope: dict[str, list[str]] = {}
    for item in conditions:
        key = item.dimension
        if key == "event":
            key = "event"
        elif (
            key == "person"
            and item.role != "depicted"
            or key in {"time", "place"}
            and item.role != "capture"
            or key not in {"time", "place", "person"}
        ):
            continue
        scope.setdefault(key, []).append(item.value)
    return tuple(sorted((key, "、".join(dict.fromkeys(values))) for key, values in scope.items()))


@dataclass(frozen=True, slots=True)
class AppliedConditions:
    filters: Mapping[str, str]
    applied: tuple[QueryCondition, ...]
    pending: tuple[tuple[QueryCondition, str], ...]

    def report(self, *, executed: bool = True) -> dict[str, Any]:
        return {
            "conditions_applied": [item.payload() for item in self.applied] if executed else [],
            "conditions_resolved": [item.payload() for item in self.applied],
            "conditions_not_applied": [{**item.payload(), "reason": reason} for item, reason in self.pending],
            "answer_eligible": not any(item.required for item, _ in self.pending),
        }


def executable_conditions(
    conditions: Sequence[QueryCondition], *, now: datetime, exact: bool
) -> AppliedConditions:
    filters: dict[str, str] = {}
    applied: list[QueryCondition] = []
    pending: list[tuple[QueryCondition, str]] = []
    for item in conditions:
        reason = ""
        value = item.value
        if not item.required:
            continue
        if item.state == "pending":
            reason = "evidence_anchor_required"
        elif item.dimension == "time":
            if item.role != "capture" or item.relation != "is":
                reason = "event_or_document_time_requires_evidence"
            else:
                value = resolve_time_expression(value, now=now) or ""
                if not value or parse_time_expression(value) is None:
                    reason = "unparsed_time"
        elif item.dimension == "place":
            if item.role != "capture" or item.relation != "is":
                reason = "place_role_requires_content_evidence"
        elif item.dimension == "person":
            if item.role == "depicted":
                # 人物定位由目录执行，保留anchor与relation。
                applied.append(item)
                continue
            if item.role == "narrator" and not item.required:
                continue
            reason = "identity_role_not_indexed"
        elif item.dimension == "media":
            if item.relation != "is" or value not in {"image", "video"}:
                reason = "unsupported_media"
        elif item.dimension == "source":
            if item.relation != "is" or value not in {"all", "email", "document"} or exact and value != "all":
                reason = "unsupported_source_for_operation"
        elif item.dimension == "quantity":
            if exact and (item.role != "asset" or value not in {"asset", "image", "video"}):
                reason = "unit_requires_content_evidence"
            else:
                continue
        else:
            reason = "evidence_anchor_required"
        if reason:
            pending.append((item, reason))
        elif item.dimension in filters and filters[item.dimension] != value:
            # 同需求内多个范围不能取首项或清空；交集仅对可确定的时间执行。
            if item.dimension == "time":
                first, second = parse_time_expression(filters["time"]), parse_time_expression(value)
                assert first is not None and second is not None
                if first[0].tzinfo != second[0].tzinfo or first[1].tzinfo != second[1].tzinfo:
                    pending.append((item, "incompatible_timezones"))
                    continue
                start, end = max(first[0], second[0]), min(first[1], second[1])
                if start >= end:
                    pending.append((item, "conflicting_time_ranges"))
                else:
                    zone = f"[{end.tzinfo}]" if end.tzinfo is not None else ""
                    filters["time"] = f"{start.date().isoformat()}/{end.date().isoformat()}{zone}"
                    applied.append(item)
            else:
                pending.append((item, "multiple_values_require_separate_query"))
        else:
            filters[item.dimension] = value
            applied.append(item)
    return AppliedConditions(filters, tuple(applied), tuple(pending))
