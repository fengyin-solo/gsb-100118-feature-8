"""设施编号位号修订台账：移位、换型、修复的连续修订与审批结论投影。

业务约束（与交安设施管养口径对齐）：

1. 同一设施每次移位/换型/修复/存量对齐都生成一条修订，修订序号在设施范围内
   从 01 起连续编号（REV-设施编号-序号）。
2. 审批通过是一个原子事务：设施主表、空间索引、事件投影、历史位置快照、
   路段清单投影、工程材料计划必须一起生效；任一步失败整体回滚，绝不允许
   “只更新地图、漏掉台账”。
3. 设施台账、路段清单、工程材料计划三处读取同一条修订事件（同一 revision_id
   与 revision_no），不存在各自抄一份导致的口径漂移。
4. 责任组冲突时以资产验收单为准：申报组与验收单记载不一致，按验收单落账并
   在修订上记录冲突仲裁过程。
5. 历史位置按施工时快照保留：每次移位的前/后桩号、坐标在审批时固化，之后
   主表怎么改都不回写历史。
6. 存量缺失位号的设施通过“存量对齐”类修订迁移对齐里程。
7. 重复消息幂等：同一 message_id（或同一业务内容指纹）重放只返回首次结果。
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from app.store import Store, Transaction, TransactionAbort, store

REVISION_TYPES = ["移位", "换型", "修复", "存量对齐"]
REV_STATUS_DRAFT = "待审批"
REV_STATUS_APPROVED = "审批通过"
REV_STATUS_REJECTED = "已驳回"

# 空间索引网格：每 500m 一格，地图按路段+网格取设施。
GRID_METERS = 500
# 影响面判定半径：修订位置前后 150m 内的路面病害视为受影响病害。
IMPACT_BUFFER_M = 150

# 示意图坐标原点：把里程映射成经纬度，仅用于前端轨迹绘制（非真实测绘坐标）。
ROAD_ORIGIN: dict[str, tuple[float, float]] = {
    "ROAD-G104-CS01": (120.180, 31.320),
    "ROAD-S205-KF02": (120.250, 31.280),
    "ROAD-X318-BL03": (120.110, 31.260),
}

_STATION_RE = re.compile(r"[Kk]\s*(\d+)\s*[+＋]\s*(\d{1,3})(?:\.\d+)?")


def parse_mileage(text: Any) -> float | None:
    """把 ``K13+250`` 这类桩号解析成米；无法识别返回 None。"""
    if text is None:
        return None
    match = _STATION_RE.search(str(text))
    if not match:
        return None
    return int(match.group(1)) * 1000 + int(match.group(2))


def station_key(station: str | None, mileage: float | None) -> str:
    """由里程反推标准桩号文本（K13+250）。"""
    if station and parse_mileage(station) is not None:
        m = _STATION_RE.search(station)
        return f"K{m.group(1)}+{int(m.group(2)):03d}"  # type: ignore[union-attr]
    if mileage is None:
        return ""
    return f"K{int(mileage // 1000)}+{int(round(mileage % 1000)):03d}"


def align_tag(station: str | None, mileage: float | None) -> str:
    """位号迁移对齐里程：位号统一取 ``W-标准桩号``。"""
    key = station_key(station, mileage)
    return f"W-{key}" if key else ""


def project_coords(road: str, mileage: float | None) -> tuple[float | None, float | None]:
    """里程 → 示意图经纬度（沿道路每公里 0.012°，加微小弯曲）。"""
    if mileage is None or road not in ROAD_ORIGIN:
        return None, None
    base_lng, base_lat = ROAD_ORIGIN[road]
    km = mileage / 1000.0
    lng = round(base_lng + km * 0.012, 6)
    lat = round(base_lat + math.sin(km * 1.7) * 0.0015, 6)
    return lng, lat


def _grid_cell(mileage: float) -> int:
    return int(mileage // GRID_METERS)


def _snapshot_position(facility: dict[str, Any]) -> dict[str, Any]:
    mileage = parse_mileage(facility.get("桩号位置"))
    lng, lat = project_coords(str(facility.get("所属路段") or ""), mileage)
    return {
        "桩号位置": facility.get("桩号位置"),
        "里程米": mileage,
        "位号": facility.get("位号"),
        "lng": lng,
        "lat": lat,
    }


class FacilityRevisionService:
    # ================================================================ 读取
    def list_revisions(
        self,
        *,
        facility_id: int | None = None,
        status_filter: str | None = None,
        revision_type: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows("facility_revision"))
        if facility_id is not None:
            rows = [r for r in rows if int(r.get("facility_id", 0)) == facility_id]
        if status_filter:
            rows = [r for r in rows if r.get("status") == status_filter]
        if revision_type:
            rows = [r for r in rows if r.get("revision_type") == revision_type]
        rows.sort(key=lambda r: int(r.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_revision(self, revision_id: int) -> dict[str, Any] | None:
        rev = store.find("facility_revision", revision_id)
        if rev is None:
            return None
        detail = dict(rev)
        detail["affected_pavements"] = [
            dict(row) for row in store.rows("facility_event_log")
            if int(row.get("revision_id", 0)) == revision_id and row.get("event_kind") == "影响病害"
        ]
        detail["affected_projects"] = [
            dict(row) for row in store.rows("facility_event_log")
            if int(row.get("revision_id", 0)) == revision_id and row.get("event_kind") == "影响工程"
        ]
        detail["material_plan"] = next(
            (dict(row) for row in store.rows("facility_material_plan")
             if int(row.get("revision_id", 0)) == revision_id),
            None,
        )
        detail["position_snapshots"] = [
            dict(row) for row in store.rows("facility_position_history")
            if int(row.get("facility_id", 0)) == int(rev.get("facility_id", 0))
        ]
        return detail

    def track(self, facility_id: int) -> dict[str, Any]:
        """地图轨迹：历史位置快照（施工时点）+ 受影响病害/工程 + 当前位置。"""
        facility = store.find("traffic_facility", facility_id)
        if facility is None:
            return {"facility": None, "track": [], "affected_pavements": [], "affected_projects": []}
        history = sorted(
            (dict(row) for row in store.rows("facility_position_history")
             if int(row.get("facility_id", 0)) == facility_id),
            key=lambda row: int(row.get("seq", 0)),
        )
        rev_rows = {int(r["id"]): dict(r) for r in store.rows("facility_revision")
                    if int(r.get("facility_id", 0)) == facility_id}
        pavements: dict[int, dict[str, Any]] = {}
        projects: dict[int, dict[str, Any]] = {}
        for event in store.rows("facility_event_log"):
            if event.get("event_kind") == "影响病害":
                pavements.setdefault(int(event["ref_id"]), dict(event))
            elif event.get("event_kind") == "影响工程":
                projects.setdefault(int(event["ref_id"]), dict(event))
        return {
            "facility": dict(facility),
            "track": history,
            "revisions": [rev_rows[k] for k in sorted(rev_rows)],
            "affected_pavements": list(pavements.values()),
            "affected_projects": list(projects.values()),
        }

    def material_plans(self, *, road: str | None = None) -> list[dict[str, Any]]:
        """工程材料计划：直接读修订投影，保证与台账、路段清单是同一修订。"""
        rows = [dict(row) for row in store.rows("facility_material_plan")]
        if road:
            rows = [row for row in rows if row.get("road_code") == road]
        rows.sort(key=lambda row: int(row.get("revision_seq", 0)), reverse=True)
        return rows

    def road_section_revisions(self, road_code: str) -> list[dict[str, Any]]:
        """路段清单读取：返回落在该路段上的已审批修订（读事件投影，不另存结论）。"""
        return [
            dict(row) for row in store.rows("facility_event_log")
            if row.get("event_kind") == "审批结论"
            and row.get("road_code") == road_code
            and row.get("status") == REV_STATUS_APPROVED
        ]

    # ================================================================ 提交修订
    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        facility_id = int(payload.get("facility_id") or 0)
        revision_type = str(payload.get("revision_type") or "").strip()
        new_station = (payload.get("new_station") or None)
        new_tag = (payload.get("new_tag") or None)
        message_id = (payload.get("message_id") or None)

        # 幂等指纹只取业务字段，remark/message_id 本身不参与。
        fingerprint_src = {
            "facility_id": facility_id,
            "revision_type": revision_type,
            "new_station": new_station,
            "new_tag": new_tag,
            "new_facility_type": payload.get("new_facility_type"),
            "responsible_group": payload.get("responsible_group"),
            "project_id": payload.get("project_id"),
        }
        fingerprint = Store.fingerprint(fingerprint_src)
        cached = store.idempotent(message_id, fingerprint)
        if cached is not None:
            return cached.get("revision"), str(cached.get("message", "重复消息，返回首次修订结果"))

        if revision_type not in REVISION_TYPES:
            return None, f"修订类型「{revision_type}」不支持，仅支持：{'、'.join(REVISION_TYPES)}"

        try:
            with store.transaction() as tx:
                facility = tx.find("traffic_facility", facility_id)
                if facility is None:
                    tx.fail(f"交安设施 {facility_id} 不存在，无法开立修订台账")
                assert facility is not None

                if revision_type == "移位":
                    target_mileage = parse_mileage(new_station)
                    if target_mileage is None:
                        tx.fail("移位修订必须给出可识别的新桩号，如 K13+500")
                elif revision_type == "存量对齐":
                    target_mileage = parse_mileage(new_station)
                    if target_mileage is None:
                        target_mileage = parse_mileage(facility.get("桩号位置"))
                    if target_mileage is None:
                        tx.fail("存量对齐缺少可对齐的里程，请补充新桩号")
                else:
                    target_mileage = parse_mileage(new_station)
                    if new_station and target_mileage is None:
                        tx.fail(f"新桩号「{new_station}」无法识别里程")
                    target_mileage = target_mileage if new_station else parse_mileage(facility.get("桩号位置"))

                target_tag = (new_tag or "").strip() or align_tag(new_station, target_mileage)

                seq = sum(1 for r in tx.rows("facility_revision")
                          if int(r.get("facility_id", 0)) == facility_id) + 1
                rev_no = f"REV-{facility.get('设施编号')}-{seq:02d}"
                target_lng, target_lat = project_coords(str(facility.get("所属路段") or ""), target_mileage)

                revision = {
                    "id": tx.next_id("facility_revision"),
                    "revision_no": rev_no,
                    "revision_seq": seq,
                    "facility_id": facility_id,
                    "facility_code": facility.get("设施编号"),
                    "facility_type": facility.get("设施类型"),
                    "road_code": facility.get("所属路段"),
                    "revision_type": revision_type,
                    "status": REV_STATUS_DRAFT,
                    "before_station": facility.get("桩号位置"),
                    "before_tag": facility.get("位号"),
                    "before_facility_type": facility.get("设施类型"),
                    "new_station": station_key(new_station, target_mileage) if target_mileage is not None else facility.get("桩号位置"),
                    "new_tag": target_tag,
                    "new_facility_type": (payload.get("new_facility_type") or facility.get("设施类型")) if revision_type == "换型" else facility.get("设施类型"),
                    "new_lng": target_lng,
                    "new_lat": target_lat,
                    "declared_group": (payload.get("responsible_group") or "").strip() or facility.get("责任组"),
                    "resolved_group": None,
                    "group_conflict": False,
                    "conflict_note": "",
                    "reason": payload.get("reason") or "",
                    "construction_date": payload.get("construction_date") or date.today().isoformat(),
                    "project_id": payload.get("project_id"),
                    "approver": "",
                    "approval_opinion": "",
                    "decided_at": "",
                    "remark": payload.get("remark") or "",
                }
                tx.rows("facility_revision").append(revision)

                result = {"revision": dict(revision), "message": f"{revision_type}修订已提交，编号 {rev_no}，待审批"}
                key = tx.remember(message_id, fingerprint, result)
                result["message"] += f"（幂等键 {key[:12]}…）" if not message_id else ""
                return dict(revision), result["message"]
        except TransactionAbort as exc:
            return None, str(exc)

    # ================================================================ 审批
    def decide(self, revision_id: int, approved: bool, approver: str, opinion: str,
               message_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        fp = Store.fingerprint({"decide": revision_id, "approved": approved, "approver": approver})
        cached = store.idempotent(message_id, fp)
        if cached is not None:
            return cached.get("revision"), str(cached.get("message", "重复审批消息，返回首次结论"))

        try:
            with store.transaction() as tx:
                rev = tx.find("facility_revision", revision_id)
                if rev is None:
                    tx.fail(f"修订 {revision_id} 不存在")
                assert rev is not None
                if rev["status"] != REV_STATUS_DRAFT:
                    tx.fail(f"修订 {rev['revision_no']} 当前状态为「{rev['status']}」，不能重复审批")

                facility = tx.find("traffic_facility", int(rev["facility_id"]))
                if facility is None:
                    tx.fail(f"修订对应的设施 {rev['facility_id']} 已不存在，审批终止")
                assert facility is not None

                rev["approver"] = approver or "值班审批人"
                rev["approval_opinion"] = opinion or ""
                rev["decided_at"] = date.today().isoformat()

                if not approved:
                    rev["status"] = REV_STATUS_REJECTED
                    self._append_event(tx, rev, event_kind="审批驳回", payload={
                        "road_code": rev["road_code"],
                        "status": REV_STATUS_REJECTED,
                        "opinion": opinion,
                    })
                    result = {"revision": dict(rev), "message": f"修订 {rev['revision_no']} 已驳回，台账与投影均未变更"}
                    tx.remember(message_id, fp, result)
                    return dict(rev), result["message"]

                # -------- 责任组仲裁：以资产验收单为准 --------
                acceptance = self._find_acceptance(tx, int(rev["facility_id"]))
                declared = rev.get("declared_group") or facility.get("责任组")
                accepted_group = acceptance.get("责任组") if acceptance else None
                if accepted_group and declared and accepted_group != declared:
                    rev["group_conflict"] = True
                    rev["conflict_note"] = (
                        f"申报责任组「{declared}」与资产验收单 {acceptance.get('验收单编号')} "
                        f"记载「{accepted_group}」冲突，按验收单裁定为「{accepted_group}」"
                    )
                resolved = accepted_group or declared or facility.get("责任组")
                rev["resolved_group"] = resolved
                rev["status"] = REV_STATUS_APPROVED

                new_mileage = parse_mileage(rev.get("new_station"))
                lng, lat = project_coords(str(rev["road_code"]), new_mileage)

                # -------- 1) 主表：设施台账落账 --------
                before_snapshot = _snapshot_position(facility)
                if rev["revision_type"] in ("移位", "存量对齐"):
                    facility["桩号位置"] = rev["new_station"]
                facility["位号"] = rev["new_tag"] or facility.get("位号")
                if rev["revision_type"] == "换型":
                    facility["设施类型"] = rev["new_facility_type"]
                if rev["revision_type"] == "修复":
                    facility["status"] = "完好"
                    facility["完好程度"] = "修复完好"
                    facility["设施状态"] = "完好"
                    facility["abnormal"] = False
                facility["责任组"] = resolved
                facility["最新修订号"] = rev["revision_no"]
                facility["最新修订序号"] = rev["revision_seq"]

                # -------- 2) 空间索引：先删旧格再写新格（同一事务） --------
                self._upsert_spatial_index(tx, facility, new_mileage, lng, lat, rev)

                # -------- 3) 历史位置：施工时快照，旧点位保留 --------
                after_snapshot = _snapshot_position(facility)
                history_seq = sum(1 for h in tx.rows("facility_position_history")
                                  if int(h.get("facility_id", 0)) == int(rev["facility_id"])) + 1
                tx.rows("facility_position_history").append({
                    "id": tx.next_id("facility_position_history"),
                    "facility_id": int(rev["facility_id"]),
                    "seq": history_seq,
                    "revision_id": rev["id"],
                    "revision_no": rev["revision_no"],
                    "revision_type": rev["revision_type"],
                    "construction_date": rev["construction_date"],
                    "road_code": rev["road_code"],
                    "before": before_snapshot,
                    "after": after_snapshot,
                })

                # -------- 4) 影响面：受影响路面病害与养护工程 --------
                affected_pavements = self._match_pavements(tx, rev, new_mileage)
                affected_projects = self._match_projects(tx, rev, new_mileage)
                for p in affected_pavements:
                    self._append_event(tx, rev, event_kind="影响病害", payload={
                        "ref_id": p["id"], "ref_code": p.get("病害编号"),
                        "road_code": p.get("所属路段"),
                        "病害类型": p.get("病害类型"), "起止桩号": p.get("起止桩号"),
                        "status": p.get("status"),
                    })
                for prj in affected_projects:
                    self._append_event(tx, rev, event_kind="影响工程", payload={
                        "ref_id": prj["id"], "ref_code": prj.get("工程编号"),
                        "road_code": prj.get("施工路段"),
                        "工程名称": prj.get("工程名称"),
                        "施工起桩号": prj.get("施工起桩号"), "施工止桩号": prj.get("施工止桩号"),
                        "status": prj.get("status"),
                    })

                # -------- 5) 事件投影：审批结论（设施台账/路段清单/材料计划三处同读） --------
                self._append_event(tx, rev, event_kind="审批结论", payload={
                    "road_code": rev["road_code"],
                    "facility_code": rev["facility_code"],
                    "revision_type": rev["revision_type"],
                    "status": REV_STATUS_APPROVED,
                    "resolved_group": resolved,
                    "new_station": facility.get("桩号位置"),
                    "new_tag": facility.get("位号"),
                    "new_facility_type": facility.get("设施类型"),
                    "affected_pavement_ids": [p["id"] for p in affected_pavements],
                    "affected_project_ids": [p["id"] for p in affected_projects],
                })

                # -------- 6) 工程材料计划：同一 revision_id 的投影 --------
                self._upsert_material_plan(tx, rev, facility, affected_projects)

                result = {
                    "revision": dict(rev),
                    "message": (
                        f"修订 {rev['revision_no']} 审批通过：设施台账、空间索引、事件投影已一致更新；"
                        f"受影响病害 {len(affected_pavements)} 处、养护工程 {len(affected_projects)} 项"
                        + (f"；{rev['conflict_note']}" if rev["group_conflict"] else "")
                    ),
                }
                tx.remember(message_id, fp, result)
                return dict(rev), result["message"]
        except TransactionAbort as exc:
            return None, str(exc)

    def reject(self, revision_id: int, approver: str, opinion: str,
               message_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
        return self.decide(revision_id, False, approver, opinion, message_id)

    # ================================================================ 存量对齐迁移
    def align_legacy(self) -> dict[str, Any]:
        """存量缺失位号的设施：逐座生成「存量对齐」修订（待审批），审批后才迁移里程。

        幂等：已有未决/已通过的存量对齐修订的设施不重复生成。
        """
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        for facility in list(store.rows("traffic_facility")):
            tag = str(facility.get("位号") or "").strip()
            mileage = parse_mileage(facility.get("桩号位置"))
            has_open_align = any(
                int(r.get("facility_id", 0)) == int(facility["id"])
                and r.get("revision_type") == "存量对齐"
                and r.get("status") in (REV_STATUS_DRAFT, REV_STATUS_APPROVED)
                for r in store.rows("facility_revision")
            )
            if has_open_align:
                skipped.append(str(facility.get("设施编号")))
                continue
            if tag and tag not in ("（缺失待迁移对齐）",) and mileage is not None and tag.startswith(("W-K", "B-K", "G-K")):
                continue
            if mileage is None:
                skipped.append(str(facility.get("设施编号")))
                continue
            revision, _ = self.submit({
                "facility_id": int(facility["id"]),
                "revision_type": "存量对齐",
                "new_station": facility.get("桩号位置"),
                "reason": "存量设施位号缺失，迁移对齐里程",
            })
            if revision:
                created.append(revision)
        return {"created": created, "skipped": skipped,
                "message": f"已生成 {len(created)} 条存量对齐修订，审批后迁移落账"}

    # ================================================================ 启动引导
    def bootstrap(self, owner: Store) -> None:
        """初始化空间索引与历史位置快照、资产验收单；只在进程启动时执行一次。"""
        tables = owner.rows
        if not tables("facility_spatial_index") and not tables("facility_position_history"):
            for facility in tables("traffic_facility"):
                mileage = parse_mileage(facility.get("桩号位置"))
                lng, lat = project_coords(str(facility.get("所属路段") or ""), mileage)
                if mileage is not None:
                    tables("facility_spatial_index").append({
                        "id": len(tables("facility_spatial_index")) + 1,
                        "facility_id": int(facility["id"]),
                        "facility_code": facility.get("设施编号"),
                        "road_code": facility.get("所属路段"),
                        "grid": _grid_cell(mileage),
                        "mileage": mileage,
                        "station": facility.get("桩号位置"),
                        "tag": facility.get("位号"),
                        "lng": lng,
                        "lat": lat,
                        "revision_id": None,
                    })
                tables("facility_position_history").append({
                    "id": len(tables("facility_position_history")) + 1,
                    "facility_id": int(facility["id"]),
                    "seq": 0,
                    "revision_id": None,
                    "revision_no": "初始建档",
                    "revision_type": "初始建档",
                    "construction_date": facility.get("设置日期"),
                    "road_code": facility.get("所属路段"),
                    "before": None,
                    "after": _snapshot_position(facility),
                })
        if not tables("facility_asset_acceptance"):
            tables("facility_asset_acceptance").extend([
                {"id": 1, "验收单编号": "ACC-2024-0312", "facility_id": 1,
                 "设施编号": "TRAF-0001", "责任组": "城东交安验收组",
                 "验收日期": "2024-05-20", "里程米": 13250},
                {"id": 2, "验收单编号": "ACC-2023-0806", "facility_id": 2,
                 "设施编号": "TRAF-0002", "责任组": "城东交安二班",
                 "验收日期": "2023-11-05", "里程米": 14080},
                {"id": 3, "验收单编号": "ACC-2025-0217", "facility_id": 3,
                 "设施编号": "TRAF-0003", "责任组": "开发区交安专班",
                 "验收日期": "2025-03-25", "里程米": 5640},
                {"id": 4, "验收单编号": "ACC-2025-0529", "facility_id": 4,
                 "设施编号": "TRAF-0004", "责任组": "滨河养护班组",
                 "验收日期": "2025-08-12", "里程米": 2900},
                # TRAF-0005 刻意无验收单：仲裁时回退申报责任组。
            ])

        # 预置一条已审批的修复修订：打开地图轨迹即可看到受影响病害与养护工程；
        # 再放一条待审批移位修订，让台账开箱即有“待办”。引导只在进程启动时执行一次。
        if not tables("facility_revision"):
            repair, _ = self.submit({
                "facility_id": 2,
                "revision_type": "修复",
                "responsible_group": "城东交安二班",
                "reason": "标志牌板面污损，更换反光膜并紧固",
                "construction_date": "2026-09-22",
                "project_id": 2,
                "message_id": "BOOTSTRAP-REPAIR-TRAF-0002",
            })
            if repair:
                self.decide(repair["id"], True, "值班长", "修复合格，同意验收",
                            "BOOTSTRAP-DECIDE-TRAF-0002")
            self.submit({
                "facility_id": 1,
                "revision_type": "移位",
                "new_station": "K13+520",
                "responsible_group": "城东交安一班",
                "reason": "避让新增出入口（示例待审批修订）",
                "message_id": "BOOTSTRAP-DRAFT-TRAF-0001",
            })

    # ================================================================ 事务内工具
    def _find_acceptance(self, tx: Transaction, facility_id: int) -> dict[str, Any] | None:
        rows = [r for r in tx.rows("facility_asset_acceptance")
                if int(r.get("facility_id", 0)) == facility_id]
        return rows[-1] if rows else None

    def _upsert_spatial_index(self, tx: Transaction, facility: dict[str, Any],
                              mileage: float | None, lng: float | None, lat: float | None,
                              rev: dict[str, Any]) -> None:
        idx_rows = tx.rows("facility_spatial_index")
        for row in idx_rows:
            if int(row.get("facility_id", 0)) == int(facility["id"]):
                if mileage is None:
                    tx.fail(f"设施 {facility.get('设施编号')} 新桩号缺少里程，空间索引拒绝写入")
                row.update({
                    "road_code": facility.get("所属路段"),
                    "grid": _grid_cell(mileage),
                    "mileage": mileage,
                    "station": facility.get("桩号位置"),
                    "tag": facility.get("位号"),
                    "lng": lng,
                    "lat": lat,
                    "revision_id": rev["id"],
                })
                return
        if mileage is None:
            tx.fail(f"设施 {facility.get('设施编号')} 新桩号缺少里程，空间索引拒绝写入")
        idx_rows.append({
            "id": tx.next_id("facility_spatial_index"),
            "facility_id": int(facility["id"]),
            "facility_code": facility.get("设施编号"),
            "road_code": facility.get("所属路段"),
            "grid": _grid_cell(mileage),
            "mileage": mileage,
            "station": facility.get("桩号位置"),
            "tag": facility.get("位号"),
            "lng": lng,
            "lat": lat,
            "revision_id": rev["id"],
        })

    def _append_event(self, tx: Transaction, rev: dict[str, Any], *,
                      event_kind: str, payload: dict[str, Any]) -> None:
        tx.rows("facility_event_log").append({
            "id": tx.next_id("facility_event_log"),
            "revision_id": rev["id"],
            "revision_no": rev["revision_no"],
            "facility_id": rev["facility_id"],
            "event_kind": event_kind,
            "road_code": rev["road_code"],
            "status": rev["status"],
            "payload": payload,
            **payload,
        })

    def _range_of(self, text: Any) -> tuple[float | None, float | None]:
        """从 ``K13+200~K13+360`` 取里程区间；单点桩号退化为零长度区间。"""
        parts = re.split(r"[~～-]", str(text))
        starts = [parse_mileage(part) for part in parts]
        vals = [v for v in starts if v is not None]
        if not vals:
            return None, None
        return min(vals), max(vals)

    def _match_pavements(self, tx: Transaction, rev: dict[str, Any],
                         mileage: float | None) -> list[dict[str, Any]]:
        if mileage is None:
            return []
        hits: list[dict[str, Any]] = []
        for p in tx.rows("pavement"):
            if p.get("所属路段") != rev["road_code"]:
                continue
            lo, hi = self._range_of(p.get("起止桩号"))
            if lo is None:
                continue
            if lo - IMPACT_BUFFER_M <= mileage <= hi + IMPACT_BUFFER_M:
                hits.append(p)
        return hits

    def _match_projects(self, tx: Transaction, rev: dict[str, Any],
                        mileage: float | None) -> list[dict[str, Any]]:
        if mileage is None:
            return []
        hits: list[dict[str, Any]] = []
        for prj in tx.rows("project"):
            if prj.get("施工路段") != rev["road_code"]:
                continue
            lo = parse_mileage(prj.get("施工起桩号"))
            hi = parse_mileage(prj.get("施工止桩号"))
            if lo is None or hi is None:
                continue
            lo, hi = min(lo, hi), max(lo, hi)
            if lo - IMPACT_BUFFER_M <= mileage <= hi + IMPACT_BUFFER_M:
                hits.append(prj)
        return hits

    def _upsert_material_plan(self, tx: Transaction, rev: dict[str, Any],
                              facility: dict[str, Any],
                              affected_projects: list[dict[str, Any]]) -> None:
        catalog = {
            "移位": [("材料-立柱", "镀锌钢管立柱", "Φ114×4.5×2100mm", 2, "根"),
                    ("材料-紧固件", "护栏连接螺栓", "M16×170", 8, "套")],
            "换型": [("材料-标志牌", rev.get("new_facility_type") or "交安标志牌", "Ⅳ类反光膜", 1, "块"),
                    ("材料-立柱", "镀锌钢管立柱", "Φ76×4×2500mm", 1, "根")],
            "修复": [("材料-反光膜", "工程级反光膜", "Ⅳ类", 1.5, "㎡"),
                    ("材料-紧固件", "护栏连接螺栓", "M16×170", 4, "套")],
            "存量对齐": [("材料-定位件", "桩号定位牌", "铝合金反光", 1, "块"),
                        ("材料-紧固件", "膨胀螺栓", "M12×100", 4, "套")],
        }
        items = catalog[rev["revision_type"]]
        # 多个工程同时覆盖该点时：在建工程优先，其次区间最短（贴合度最高）的工程。
        project = None
        if affected_projects:
            def _priority(prj: dict[str, Any]) -> tuple[int, float]:
                lo = parse_mileage(prj.get("施工起桩号")) or 0.0
                hi = parse_mileage(prj.get("施工止桩号")) or 0.0
                active = 0 if prj.get("status") == "施工中" else 1
                return active, abs(hi - lo)

            project = sorted(affected_projects, key=_priority)[0]
        plan = {
            "id": tx.next_id("facility_material_plan"),
            "计划编号": f"MAT-PLAN-{rev['revision_no']}",
            "revision_id": rev["id"],
            "revision_no": rev["revision_no"],
            "revision_seq": rev["revision_seq"],
            "facility_id": rev["facility_id"],
            "facility_code": rev["facility_code"],
            "road_code": rev["road_code"],
            "revision_type": rev["revision_type"],
            "project_code": project.get("工程编号") if project else "",
            "responsible_group": rev["resolved_group"],
            "construction_date": rev["construction_date"],
            "items": [{"材料编号": code, "材料名称": name, "规格型号": spec,
                       "数量": qty, "单位": unit} for code, name, spec, qty, unit in items],
            "status": "已入计划" if rev["status"] == REV_STATUS_APPROVED else "待审批",
        }
        tx.rows("facility_material_plan").append(plan)


revision_service = FacilityRevisionService()
