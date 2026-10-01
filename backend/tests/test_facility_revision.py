"""设施编号位号修订台账 回归测试（不依赖 pytest，直接 python tests/test_facility_revision.py）。

覆盖需求中的硬约束：
1. 每次移位/换型/修复生成设施范围内连续修订；
2. 审批结论事务一致：主表、空间索引、事件投影同生共死，失败整体回滚；
3. 设施台账、路段清单、工程材料计划三处读取同一修订；
4. 责任组冲突以资产验收单为准，历史位置按施工时快照保留；
5. 存量缺失位号设施迁移对齐里程；
6. 重复消息（message_id 与内容指纹）幂等；
7. 驳回只记事件、不更新地图。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.services.facility_revision import revision_service as rs  # noqa: E402
from app.store import store  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        failures.append(label)


def main() -> int:
    store.ensure_bootstrapped()

    # --- 引导数据
    check(len(store.rows("facility_spatial_index")) >= 4, "启动引导生成空间索引")
    check(any(h["revision_no"] == "初始建档" for h in store.rows("facility_position_history")),
          "历史位置保留初始建档快照")
    boot = [r for r in store.rows("facility_revision") if r["facility_code"] == "TRAF-0002"][0]
    check(boot["status"] == "审批通过", "引导预置一条已审批修复修订")

    # --- 连续修订号 + 幂等提交
    rev, _ = rs.submit({"facility_id": 1, "revision_type": "移位", "new_station": "K13+520",
                        "responsible_group": "外包施工三组", "message_id": "T-SHIFT-1"})
    expected_seq = max(r["revision_seq"] for r in store.rows("facility_revision")
                       if r["facility_id"] == 1)
    check(rev["revision_no"] == f"REV-TRAF-0001-{expected_seq:02d}", "修订号在设施范围内连续")
    dup, _ = rs.submit({"facility_id": 1, "revision_type": "移位", "new_station": "K13+520",
                        "responsible_group": "外包施工三组", "message_id": "T-SHIFT-1"})
    check(dup["id"] == rev["id"], "相同 message_id 重复提交幂等")

    # --- 非法修订整体回滚（无半截修订、无幂等垃圾）
    idem_before = len(store.rows("facility_idempotency"))
    bad, _ = rs.submit({"facility_id": 1, "revision_type": "移位", "new_station": "无法识别"})
    check(bad is None, "非法桩号修订被拒绝")
    check(len(store.rows("facility_idempotency")) == idem_before, "失败事务不留下幂等/半截记录")

    # --- 审批：事务一致 + 责任组仲裁
    rid = rev["id"]
    done, _ = rs.decide(rid, True, "测试审批", "通过", "T-DEC-1")
    check(done["group_conflict"] is True, "责任组与验收单冲突被识别")
    check(done["resolved_group"] == "城东交安验收组", "冲突时以资产验收单责任组为准")
    facility = store.find("traffic_facility", 1)
    check(facility["桩号位置"] == "K13+520" and facility["位号"] == "W-K13+520", "主表台账写入新桩号/位号")
    check(facility["最新修订号"] == rev["revision_no"], "设施台账记录最新修订号")
    idx = [r for r in store.rows("facility_spatial_index") if r["facility_id"] == 1][0]
    check(idx["station"] == "K13+520" and idx["revision_id"] == rid, "空间索引与台账同一事务更新")
    conclusions = [e for e in store.rows("facility_event_log")
                   if e["revision_id"] == rid and e["event_kind"] == "审批结论"]
    check(len(conclusions) == 1, "事件投影写入一条且仅一条审批结论")

    # --- 历史位置施工快照：旧点位仍在
    hist = [h for h in store.rows("facility_position_history") if h["facility_id"] == 1]
    check(hist[0]["after"]["桩号位置"] == "K13+250", "历史位置保留施工前旧桩号")
    check(hist[-1]["after"]["桩号位置"] == "K13+520", "历史位置记录施工后新桩号")

    # --- 地图轨迹旁受影响病害与工程
    track = rs.track(1)
    check(any(p["ref_code"] == "PAVE-0001" for p in track["affected_pavements"]), "轨迹旁标注受影响路面病害")
    check(any(p["ref_code"] == "PROJ-0001" for p in track["affected_projects"]), "轨迹旁标注受影响养护工程")

    # --- 三处同读同一修订
    from app.services.road_section import RoadSectionService
    g104 = [r for r in RoadSectionService().list_entries(page=1, size=100)[0]
            if r["路段编号"] == "ROAD-G104-CS01"][0]
    check(any(p["revision_id"] == rid for p in g104["设施修订投影"]), "路段清单读取同一修订投影")
    plan = next((p for p in rs.material_plans() if p["revision_id"] == rid), None)
    check(plan is not None and plan["items"], "工程材料计划读取同一修订并生成材料明细")

    # --- 重复审批幂等且不重复投影
    event_count = len(store.rows("facility_event_log"))
    again, msg = rs.decide(rid, True, "测试审批", "重复", "T-DEC-1")
    check(again is not None, "相同审批 message_id 幂等返回首次结论")
    again2, _ = rs.decide(rid, True, "测试审批", "无消息重复")
    check(again2 is None and len(store.rows("facility_event_log")) == event_count,
          "无消息编号的重复审批被拦截且不新增投影：" + str(msg))

    # --- 驳回不挪地图
    fid4_before = dict([r for r in store.rows("facility_spatial_index") if r["facility_id"] == 4][0])
    draft, _ = rs.submit({"facility_id": 4, "revision_type": "修复", "message_id": "T-REJ"})
    rs.reject(draft["id"], "测试审批", "资料不全", "T-REJ-DEC")
    fid4_after = [r for r in store.rows("facility_spatial_index") if r["facility_id"] == 4][0]
    check(dict(fid4_after) == fid4_before, "驳回修订不更新空间索引（地图不挪）")
    check(store.find("traffic_facility", 4).get("最新修订号") in (None, ""), "驳回修订不写设施台账")

    # --- 存量缺失位号对齐里程
    res = rs.align_legacy()
    check({r["facility_code"] for r in res["created"]} >= {"TRAF-0003", "TRAF-0005"},
          "存量空位号/占位位号设施生成对齐修订")
    check(len(rs.align_legacy()["created"]) == 0, "存量对齐重复执行不产生重复修订")
    a3 = next(r for r in res["created"] if r["facility_code"] == "TRAF-0003")
    rs.decide(a3["id"], True, "测试审批", "同意对齐", "T-ALIGN-3")
    check(store.find("traffic_facility", 3)["位号"] == "W-K5+640", "对齐审批后位号迁移为里程位号")

    # --- HTTP 层
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        check(client.get("/api/health").json()["ok"], "HTTP 健康检查")
        created = client.post("/api/facility_revision", json={
            "facility_id": 4, "revision_type": "换型",
            "new_facility_type": "轮廓标(升级款)", "message_id": "HTTP-T-1",
        }).json()
        check(created["ok"], "HTTP 提交修订成功")
        replay = client.post("/api/facility_revision", json={
            "facility_id": 4, "revision_type": "换型",
            "new_facility_type": "轮廓标(升级款)", "message_id": "HTTP-T-1",
        }).json()
        check(replay["entry"]["id"] == created["entry"]["id"], "HTTP 重复消息幂等")
        decision = client.post(f"/api/facility_revision/{created['entry']['id']}/decision",
                               json={"approved": True, "approver": "测试", "message_id": "HTTP-D-1"}).json()
        check(decision["ok"], "HTTP 审批通过")
        check(client.get("/api/traffic_facility/4").json()["设施类型"] == "轮廓标(升级款)",
              "HTTP 设施台账读取到换型结论")

    print(f"\n{'=' * 50}\n{'全部通过' if not failures else f'{len(failures)} 条失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
