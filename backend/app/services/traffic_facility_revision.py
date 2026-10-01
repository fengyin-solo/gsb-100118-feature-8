"""交安设施编号位号修订台账：移位、换型、修复都生成连续修订，审批结论一处写、三处读。

一致性约定（对应台账落地的硬要求）：
- 主表（traffic_facility）、空间索引（traffic_facility_position）、事件投影
  （traffic_facility_event）以及路段清单、工程材料计划的写入，全部收敛在同一个
  store.transaction() 里：任一环节失败整体回滚，不允许只更新地图而漏掉台账。
- 每个修订携带调用方给的 message_id，重复消息幂等命中：不重复建单、不重复落投影。
- 历史位置不覆盖，按施工时快照保留在空间索引里，新位置另起一条"当前"。
- 设施责任组与资产验收单冲突时，一律以资产验收单为准。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "traffic_facility"
REVISION_MODULE = "traffic_facility_revision"
POSITION_MODULE = "traffic_facility_position"
EVENT_MODULE = "traffic_facility_event"
ACCEPTANCE_MODULE = "asset_acceptance"
SECTION_MODULE = "road_section"
PAVEMENT_MODULE = "pavement"
PROJECT_MODULE = "project"
MATERIAL_PLAN_MODULE = "material_plan"

REVISION_TYPES = ["移位", "换型", "修复"]
MIGRATION_TYPE = "位号迁移"
REVISION_STATUSES = ["待审批", "已批准", "已驳回"]

# 批准后回写设施台账的状态；移位只动位置，不改完好状态
APPROVED_FACILITY_STATUS = {"移位": None, "换型": "完好", "修复": "完好", "位号迁移": None}

# 批准后各修订类型生成的工程材料计划条目
MATERIAL_RULES = {
    "移位": [("移位重装基础件", 1, "批"), ("安装辅材", 1, "批")],
    "换型": [],  # 换型按新设施类型动态生成
    "修复": [("反光膜", 2, "㎡"), ("紧固件", 1, "套")],
    "位号迁移": [],
}

STAKE_RE = re.compile(r"^\s*[Kk](\d+)\+(\d+(?:\.\d+)?)\s*$")
STAKE_RANGE_RE = re.compile(
    r"^\s*[Kk](\d+)\+(\d+(?:\.\d+)?)\s*[~～-]\s*[Kk](\d+)\+(\d+(?:\.\d+)?)\s*$"
)

SNAPSHOT_FIELDS = ["设施编号", "设施类型", "所属路段", "桩号位置", "位号", "里程", "经度", "纬度", "责任组"]


def parse_stake(text: Any) -> float | None:
    """把 K2+300 形式的桩号解析成公里里程；解析不了返回 None。"""
    match = STAKE_RE.match(str(text or ""))
    if not match:
        return None
    return int(match.group(1)) + float(match.group(2)) / 1000


def parse_stake_range(text: Any) -> tuple[float, float] | None:
    """把 K2+000~K2+600 形式的区间桩号解析成 (起, 止) 公里里程。"""
    match = STAKE_RANGE_RE.match(str(text or ""))
    if not match:
        return None
    start = int(match.group(1)) + float(match.group(2)) / 1000
    end = int(match.group(3)) + float(match.group(4)) / 1000
    return (min(start, end), max(start, end))


def position_code(km: float) -> str:
    """位号由里程派生：W + 米数补齐 6 位，迁移重算结果稳定，天然幂等。"""
    return f"W{int(round(km * 1000)):06d}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _snapshot(facility: dict[str, Any]) -> dict[str, Any]:
    return {field: facility.get(field) for field in SNAPSHOT_FIELDS}


class TrafficFacilityRevisionService:
    # ---------- 查询 ----------

    def list_revisions(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        revision_type: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(reversed(store.rows(REVISION_MODULE)))
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("设施编号", "")) or keyword in str(row.get("修订编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if revision_type:
            rows = [row for row in rows if row.get("修订类型") == revision_type]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_revision(self, revision_id: int) -> dict[str, Any] | None:
        return store.find(REVISION_MODULE, revision_id)

    def trajectory(self, facility_id: int) -> dict[str, Any] | None:
        """地图轨迹：设施当前台账 + 位置快照链 + 轨迹旁受影响的路面病害与养护工程。"""
        facility = store.find(MODULE, facility_id)
        if facility is None:
            return None
        positions = [
            row for row in store.rows(POSITION_MODULE)
            if int(row.get("设施id", 0)) == facility_id
        ]
        positions.sort(key=lambda row: (str(row.get("快照时间", "")), int(row.get("id", 0))))
        mileage = _facility_mileage(facility)
        diseases, projects = self._affected(str(facility.get("所属路段", "")), mileage)
        return {"设施": facility, "轨迹": positions, "影响病害": diseases, "影响工程": projects}

    def projection(self, revision_id: int) -> dict[str, Any] | None:
        """三处读取同一修订：设施台账、路段清单、工程材料计划看到的同一条审批结论。"""
        revision = store.find(REVISION_MODULE, revision_id)
        if revision is None:
            return None
        facility = store.find(MODULE, int(revision.get("设施id", 0)))
        section = store.find_by(SECTION_MODULE, "路段名称", revision.get("修订后", {}).get("所属路段"))
        plans = [
            row for row in store.rows(MATERIAL_PLAN_MODULE)
            if int(row.get("来源修订id", 0)) == revision_id
        ]
        ledger = None
        if facility is not None:
            ledger = {
                "设施编号": facility.get("设施编号"),
                "设施类型": facility.get("设施类型"),
                "桩号位置": facility.get("桩号位置"),
                "位号": facility.get("位号"),
                "责任组": facility.get("责任组"),
                "设施状态": facility.get("设施状态"),
                "当前修订": facility.get("当前修订"),
            }
        section_view = None
        if section is not None:
            section_view = {
                "路段编号": section.get("路段编号"),
                "路段名称": section.get("路段名称"),
                "最近交安修订": section.get("最近交安修订"),
                "交安设施数": section.get("交安设施数"),
            }
        return {
            "修订": revision,
            "设施台账": ledger,
            "路段清单": section_view,
            "工程材料计划": plans,
        }

    # ---------- 修订申请 ----------

    def create_revision(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """登记一条移位/换型/修复修订。返回 (修订, 说明, 是否幂等命中)。"""
        try:
            facility_id = int(values.get("设施id") or 0)
        except (TypeError, ValueError):
            return None, "设施id 不是有效数字", False
        facility = store.find(MODULE, facility_id)
        if facility is None:
            return None, f"交安设施 {facility_id} 不存在或已归档", False

        revision_type = str(values.get("修订类型") or "").strip()
        if revision_type not in REVISION_TYPES:
            return None, f"修订类型「{revision_type}」不支持，仅受理：{'、'.join(REVISION_TYPES)}", False

        message_id = str(values.get("message_id") or "").strip() or f"AUTO-{uuid.uuid4()}"
        duplicated = store.find_by(REVISION_MODULE, "message_id", message_id)
        if duplicated is not None:
            return duplicated, "重复消息已幂等命中，返回原修订，不重复建单", True

        before = _snapshot(facility)
        after = dict(before)
        if revision_type == "移位":
            new_stake = str(values.get("新桩号位置") or "").strip()
            km = parse_stake(new_stake)
            if km is None:
                return None, f"新桩号位置「{new_stake}」无法解析，应为 K12+300 形式", False
            after["桩号位置"] = new_stake.upper()
            after["里程"] = km
            after["位号"] = str(values.get("新位号") or "").strip() or position_code(km)
            after["经度"] = _to_float(values.get("新经度"), before.get("经度"))
            after["纬度"] = _to_float(values.get("新纬度"), before.get("纬度"))
        elif revision_type == "换型":
            new_type = str(values.get("新设施类型") or "").strip()
            if not new_type:
                return None, "换型修订必须给出新设施类型", False
            after["设施类型"] = new_type
        else:  # 修复
            fix_note = str(values.get("修复说明") or "").strip()
            if not fix_note:
                return None, "修复修订必须填写修复说明", False
            after["修复说明"] = fix_note

        group, group_source, conflict_note = self._resolve_group(facility, values.get("责任组"))
        after["责任组"] = group
        construction_time = str(values.get("施工时间") or "").strip() or _now()[:10]
        mileage = _to_float(after.get("里程"), 0.0)
        diseases, projects = self._affected(str(after.get("所属路段", "")), mileage)

        with store.transaction():
            revision_id = store.next_id(REVISION_MODULE)
            seq = self._next_sequence(facility_id)
            revision = {
                "id": revision_id,
                "修订编号": f"REV-{revision_id:04d}",
                "message_id": message_id,
                "设施id": facility_id,
                "设施编号": facility.get("设施编号"),
                "修订序号": seq,
                "修订类型": revision_type,
                "修订前": before,
                "修订后": after,
                "责任组": group,
                "责任组来源": group_source,
                "冲突说明": conflict_note,
                "影响病害": diseases,
                "影响工程": projects,
                "施工时间": construction_time,
                "状态": "待审批",
                "审批结论": "",
                "申请人": str(values.get("申请人") or "").strip() or "未署名",
                "申请时间": _now(),
                "审批人": "",
                "审批时间": "",
            }
            store.rows(REVISION_MODULE).append(revision)
            self._append_event(revision, "修订申请", f"{facility.get('设施编号')} 第 {seq} 次修订（{revision_type}）进入待审批")
        return revision, f"修订 {revision['修订编号']} 已登记，待审批", False

    # ---------- 审批 ----------

    def approve(self, revision_id: int, conclusion: str, operator: str) -> tuple[dict[str, Any] | None, str]:
        revision = store.find(REVISION_MODULE, revision_id)
        if revision is None:
            return None, f"修订 {revision_id} 不存在"
        if revision.get("状态") == "已批准":
            return revision, "该修订已是已批准状态，重复审批已幂等忽略"
        if revision.get("状态") == "已驳回":
            return None, "已驳回的修订不能再批准，请重新发起修订"
        if not conclusion.strip():
            return None, "批准修订必须填写审批结论"

        try:
            with store.transaction():
                revision["状态"] = "已批准"
                revision["审批结论"] = conclusion.strip()
                revision["审批人"] = operator or "未署名"
                revision["审批时间"] = _now()
                # 主表 → 空间索引 → 路段清单 → 材料计划 → 事件投影，同一事务内落账
                self._apply_facility(revision)
                self._apply_spatial_index(revision)
                self._apply_section_projection(revision)
                self._apply_material_plan(revision)
                self._append_event(revision, "修订批准", f"审批结论：{revision['审批结论']}")
        except Exception as exc:  # 任一环节失败都必须整体回滚，给出可读说明
            return None, f"修订落账失败，已整体回滚：{exc}"
        return revision, f"修订 {revision['修订编号']} 已批准，结论已同步设施台账、路段清单与工程材料计划"

    def reject(self, revision_id: int, conclusion: str, operator: str) -> tuple[dict[str, Any] | None, str]:
        revision = store.find(REVISION_MODULE, revision_id)
        if revision is None:
            return None, f"修订 {revision_id} 不存在"
        if revision.get("状态") == "已驳回":
            return revision, "该修订已是已驳回状态，重复驳回已幂等忽略"
        if revision.get("状态") == "已批准":
            return None, "已批准的修订不能驳回"
        if not conclusion.strip():
            return None, "驳回修订必须填写审批结论"

        with store.transaction():
            revision["状态"] = "已驳回"
            revision["审批结论"] = conclusion.strip()
            revision["审批人"] = operator or "未署名"
            revision["审批时间"] = _now()
            self._append_event(revision, "修订驳回", f"审批结论：{revision['审批结论']}")
        return revision, f"修订 {revision['修订编号']} 已驳回，台账与地图均未改动"

    # ---------- 存量迁移 ----------

    def migrate_legacy(self) -> tuple[list[dict[str, Any]], str]:
        """存量缺失位号的设施按里程对齐补齐位号；迁移结果稳定，重复执行是空操作。"""
        migrated: list[dict[str, Any]] = []
        with store.transaction():
            legacy = [
                row for row in store.rows(MODULE)
                if not str(row.get("位号") or "").strip()
            ]
            legacy.sort(key=lambda row: (str(row.get("所属路段", "")), _facility_mileage(row)))
            for facility in legacy:
                message_id = f"MIG-{facility.get('设施编号')}"
                if store.find_by(REVISION_MODULE, "message_id", message_id) is not None:
                    continue  # 已迁移过，幂等跳过
                km = _facility_mileage(facility)
                code = position_code(km)
                before = _snapshot(facility)
                after = dict(before)
                after["位号"] = code
                revision_id = store.next_id(REVISION_MODULE)
                revision = {
                    "id": revision_id,
                    "修订编号": f"REV-{revision_id:04d}",
                    "message_id": message_id,
                    "设施id": int(facility.get("id", 0)),
                    "设施编号": facility.get("设施编号"),
                    "修订序号": self._next_sequence(int(facility.get("id", 0))),
                    "修订类型": MIGRATION_TYPE,
                    "修订前": before,
                    "修订后": after,
                    "责任组": facility.get("责任组"),
                    "责任组来源": "资产验收单" if self._acceptance(facility) else "修订申请",
                    "冲突说明": "",
                    "影响病害": [],
                    "影响工程": [],
                    "施工时间": _now()[:10],
                    "状态": "已批准",
                    "审批结论": "存量迁移：缺失位号按里程自动对齐",
                    "申请人": "系统迁移",
                    "申请时间": _now(),
                    "审批人": "系统迁移",
                    "审批时间": _now(),
                }
                store.rows(REVISION_MODULE).append(revision)
                self._apply_facility(revision)
                self._apply_spatial_index(revision)
                self._apply_section_projection(revision)
                self._append_event(revision, "位号迁移", f"{facility.get('设施编号')} 补齐位号 {code}（里程 K{int(km)}+{int(round((km - int(km)) * 1000)):03d}）")
                migrated.append({"设施编号": facility.get("设施编号"), "位号": code, "修订编号": revision["修订编号"]})
        if not migrated:
            return [], "没有待迁移的存量设施，位号已全部对齐里程"
        return migrated, f"迁移完成：{len(migrated)} 条设施已按里程补齐位号"

    # ---------- 事务内落账（只能在 store.transaction() 里调用） ----------

    def _apply_facility(self, revision: dict[str, Any]) -> None:
        facility = store.find(MODULE, int(revision.get("设施id", 0)))
        if facility is None:
            raise ValueError(f"交安设施 {revision.get('设施id')} 不存在，修订无法落账")
        after = revision.get("修订后", {})
        for field in ("设施类型", "桩号位置", "位号", "里程", "经度", "纬度", "责任组"):
            if after.get(field) not in (None, ""):
                facility[field] = after[field]
        target_status = APPROVED_FACILITY_STATUS.get(str(revision.get("修订类型", "")))
        if target_status:
            facility["status"] = target_status
            facility["设施状态"] = target_status
            facility["pending"] = False
            facility["abnormal"] = False
        facility["当前修订"] = revision.get("修订编号")

    def _apply_spatial_index(self, revision: dict[str, Any]) -> None:
        """位置类修订把旧位置翻成历史快照，新位置另起一条当前；历史位置永不覆盖。"""
        if str(revision.get("修订类型", "")) not in ("移位", MIGRATION_TYPE):
            return
        facility_id = int(revision.get("设施id", 0))
        after = revision.get("修订后", {})
        for row in store.rows(POSITION_MODULE):
            if int(row.get("设施id", 0)) == facility_id and row.get("状态") == "当前":
                row["状态"] = "历史"
        store.rows(POSITION_MODULE).append({
            "id": store.next_id(POSITION_MODULE),
            "设施id": facility_id,
            "修订id": revision.get("id"),
            "位号": after.get("位号"),
            "里程": after.get("里程"),
            "经度": after.get("经度"),
            "纬度": after.get("纬度"),
            "快照时间": revision.get("施工时间"),
            "状态": "当前",
        })

    def _apply_section_projection(self, revision: dict[str, Any]) -> None:
        section_name = str(revision.get("修订后", {}).get("所属路段", ""))
        section = store.find_by(SECTION_MODULE, "路段名称", section_name)
        if section is None:
            raise ValueError(f"所属路段「{section_name}」不在路段清单中，修订无法落账")
        section["最近交安修订"] = revision.get("修订编号")
        section["交安设施数"] = sum(
            1 for row in store.rows(MODULE) if row.get("所属路段") == section_name
        )

    def _apply_material_plan(self, revision: dict[str, Any]) -> None:
        revision_type = str(revision.get("修订类型", ""))
        if revision_type == "换型":
            items: list[tuple[str, float, str]] = [(f"{revision.get('修订后', {}).get('设施类型')}（更换）", 1, "套")]
        else:
            items = list(MATERIAL_RULES.get(revision_type, []))
        for name, quantity, unit in items:
            plan_id = store.next_id(MATERIAL_PLAN_MODULE)
            store.rows(MATERIAL_PLAN_MODULE).append({
                "id": plan_id,
                "计划编号": f"MP-{plan_id:04d}",
                "来源修订id": revision.get("id"),
                "来源修订编号": revision.get("修订编号"),
                "设施编号": revision.get("设施编号"),
                "材料名称": name,
                "数量": quantity,
                "单位": unit,
                "状态": "待采购",
                "生成时间": _now(),
            })

    def _append_event(self, revision: dict[str, Any], event_type: str, summary: str) -> None:
        store.rows(EVENT_MODULE).append({
            "id": store.next_id(EVENT_MODULE),
            "修订id": revision.get("id"),
            "修订编号": revision.get("修订编号"),
            "设施id": revision.get("设施id"),
            "设施编号": revision.get("设施编号"),
            "事件类型": event_type,
            "摘要": summary,
            "时间": _now(),
        })

    # ---------- 内部口径 ----------

    def _next_sequence(self, facility_id: int) -> int:
        """设施维度连续修订序号：第 1、2、3… 次修订，事务内取号不会跳号。"""
        existing = [
            int(row.get("修订序号", 0))
            for row in store.rows(REVISION_MODULE)
            if int(row.get("设施id", 0)) == facility_id
        ]
        return max(existing, default=0) + 1

    def _acceptance(self, facility: dict[str, Any]) -> dict[str, Any] | None:
        forms = [
            row for row in store.rows(ACCEPTANCE_MODULE)
            if row.get("设施编号") == facility.get("设施编号") and row.get("状态") == "已验收"
        ]
        if not forms:
            return None
        forms.sort(key=lambda row: str(row.get("验收日期", "")), reverse=True)
        return forms[0]

    def _resolve_group(self, facility: dict[str, Any], requested: Any) -> tuple[str, str, str]:
        """责任组冲突时以资产验收单为准；没有验收单才采用申请人口径。"""
        acceptance = self._acceptance(facility)
        requested_group = str(requested or "").strip()
        if acceptance is not None:
            accepted = str(acceptance.get("责任组", ""))
            if requested_group and requested_group != accepted:
                return accepted, "资产验收单", (
                    f"申请责任组「{requested_group}」与资产验收单 {acceptance.get('验收单号')}"
                    f"「{accepted}」冲突，以验收单为准"
                )
            return accepted, "资产验收单", ""
        if requested_group:
            return requested_group, "修订申请", ""
        return str(facility.get("责任组") or "未定责任组"), "修订申请", ""

    def _affected(self, section: str, mileage: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """轨迹旁受影响面：同路段且桩号区间覆盖设施里程的病害 + 同路段的养护工程。"""
        diseases = []
        for row in store.rows(PAVEMENT_MODULE):
            if row.get("所属路段") != section:
                continue
            stake_range = parse_stake_range(row.get("起止桩号"))
            if stake_range and stake_range[0] <= mileage <= stake_range[1]:
                diseases.append({
                    "病害编号": row.get("病害编号"),
                    "病害类型": row.get("病害类型"),
                    "严重程度": row.get("严重程度"),
                    "起止桩号": row.get("起止桩号"),
                    "病害状态": row.get("病害状态"),
                })
        projects = [
            {
                "工程编号": row.get("工程编号"),
                "工程名称": row.get("工程名称"),
                "工程类型": row.get("工程类型"),
                "工程状态": row.get("工程状态"),
            }
            for row in store.rows(PROJECT_MODULE)
            if row.get("施工路段") == section
        ]
        return diseases, projects


def _facility_mileage(facility: dict[str, Any]) -> float:
    return _to_float(facility.get("里程"), parse_stake(facility.get("桩号位置")) or 0.0)


def _to_float(value: Any, default: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default or 0.0)
