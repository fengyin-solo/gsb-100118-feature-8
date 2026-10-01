"""交安设施业务规则：状态流转、字段校验与筛选口径都收在这里。

设施明细附带“修订台账视图”：修订序列、当前生效修订号与历史位置都来自修订域，
主表上的位号、桩号、责任组只由审批通过的修订写入（见 facility_revision 服务）。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "traffic_facility"
REQUIRED_FIELDS = ["设施编号", "设施类型", "所属路段"]
STATUS_ORDER = ["完好", "污损", "缺失", "已更换"]
ACTION_RULES = {"登记污损": "污损", "登记缺失": "缺失", "更换设施": "已更换"}
NEGATIVE_ACTIONS = []


def _with_revision_view(row: dict[str, Any]) -> dict[str, Any]:
    from app.services.facility_revision import revision_service

    view = dict(row)
    revisions, total = revision_service.list_revisions(
        facility_id=int(row.get("id", 0)), page=1, size=100
    )
    approved = [r for r in revisions if r.get("status") == "审批通过"]
    view["修订总数"] = total
    view["最新修订号"] = row.get("最新修订号") or (approved[0]["revision_no"] if approved else "")
    view["修订台账"] = [
        {"revision_id": r["id"], "revision_no": r["revision_no"],
         "修订类型": r["revision_type"], "状态": r["status"],
         "新桩号": r.get("new_station"), "新位号": r.get("new_tag"),
         "责任组": r.get("resolved_group") or r.get("declared_group"),
         "施工日期": r.get("construction_date"),
         "责任组冲突": r.get("group_conflict"), "仲裁说明": r.get("conflict_note")}
        for r in sorted(revisions, key=lambda item: int(item["revision_seq"]))
    ]
    return view


class TrafficFacilityService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("设施编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return _with_revision_view(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"交安设施 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于交安设施可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"交安设施已{action}（位号/桩号变更请走修订台账）"
