"""路段管理业务规则：状态流转、字段校验与筛选口径都收在这里。

路段清单额外挂载“设施修订投影”：直接从修订事件流读取落在本路段、审批通过的
修订结论，与设施台账、工程材料计划读到的是同一条 revision_id，不单独抄存。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "road_section"
REQUIRED_FIELDS = ["路段编号", "路段名称", "起止桩号"]
STATUS_ORDER = ["正常", "施工", "限行", "封闭"]
ACTION_RULES = {"设置施工": "施工", "设置限行": "限行", "恢复通行": "正常"}
NEGATIVE_ACTIONS = []


def _with_revision_projection(row: dict[str, Any]) -> dict[str, Any]:
    # 延迟导入：修订服务依赖 store，路段模块被其间接引用时避免循环。
    from app.services.facility_revision import revision_service

    view = dict(row)
    road_code = str(row.get("路段编号") or "")
    projections = revision_service.road_section_revisions(road_code)
    view["设施修订数"] = len(projections)
    view["最近设施修订"] = projections[-1]["revision_no"] if projections else ""
    view["设施修订投影"] = [
        {"revision_id": p["revision_id"], "revision_no": p["revision_no"],
         "设施编号": p.get("facility_code"), "修订类型": p.get("revision_type"),
         "新桩号": p.get("new_station"), "新位号": p.get("new_tag"),
         "责任组": p.get("resolved_group")}
        for p in projections
    ]
    return view


class RoadSectionService:
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
            rows = [row for row in rows if keyword in str(row.get("路段编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [_with_revision_projection(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return _with_revision_projection(row) if row else None

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
            return None, f"管养路段 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于路段管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"管养路段已{action}"
