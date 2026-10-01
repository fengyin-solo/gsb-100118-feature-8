"""交安设施修订台账的端到端用例：连续修订、幂等、三处同读、事务一致。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
from app.services.traffic_facility_revision import TrafficFacilityRevisionService  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _create_revision(client: TestClient, **overrides):
    values = {
        "设施id": 1,
        "修订类型": "移位",
        "新桩号位置": "K3+000",
        "message_id": "MSG-0001",
        "申请人": "张三",
        "施工时间": "2026-10-01",
    }
    values.update(overrides)
    return client.post("/api/traffic_facility_revision", json={"values": values})


def test_create_revision_generates_continuous_sequence(client: TestClient):
    first = _create_revision(client).json()
    assert first["ok"] is True
    assert first["entry"]["修订序号"] == 1
    assert first["entry"]["状态"] == "待审批"

    second = _create_revision(
        client, message_id="MSG-0002", 修订类型="修复", 修复说明="更换反光膜"
    ).json()
    assert second["entry"]["修订序号"] == 2, "同一设施的修订序号必须连续递增"

    other = _create_revision(
        client, message_id="MSG-0003", 设施id=2, 修订类型="修复", 修复说明="除锈"
    ).json()
    assert other["entry"]["修订序号"] == 1, "不同设施各自从 1 开始连续编号"


def test_duplicate_message_id_is_idempotent(client: TestClient):
    first = _create_revision(client).json()
    second = _create_revision(client).json()
    assert second["ok"] is True
    assert "幂等" in second["message"]
    assert second["entry"]["修订编号"] == first["entry"]["修订编号"]
    listing = client.get("/api/traffic_facility_revision").json()
    assert listing["total"] == 1, "重复消息不得重复建单"


def test_approve_propagates_one_revision_to_three_places(client: TestClient):
    created = _create_revision(client).json()["entry"]
    revision_id = created["id"]
    approved = client.post(
        f"/api/traffic_facility_revision/{revision_id}/approve",
        json={"values": {"审批结论": "同意移位", "审批人": "李工"}},
    ).json()
    assert approved["ok"] is True
    code = created["修订编号"]

    facility = client.get("/api/traffic_facility/1").json()
    assert facility["位号"] == "W003000"
    assert facility["桩号位置"] == "K3+000"
    assert facility["当前修订"] == code, "设施台账必须引用同一修订"

    section = client.get("/api/road_section/1").json()
    assert section["最近交安修订"] == code, "路段清单必须引用同一修订"
    assert section["交安设施数"] == 2

    projection = client.get(f"/api/traffic_facility_revision/{revision_id}/projection").json()
    assert projection["修订"]["审批结论"] == "同意移位"
    assert projection["设施台账"]["当前修订"] == code
    assert projection["路段清单"]["最近交安修订"] == code
    plans = projection["工程材料计划"]
    assert len(plans) == 2, "移位应生成基础件与辅材两条材料计划"
    assert all(plan["来源修订编号"] == code for plan in plans), "材料计划必须引用同一修订"


def test_responsibility_group_conflict_uses_acceptance_form(client: TestClient):
    created = _create_revision(
        client, message_id="MSG-0101", 修订类型="换型", 新设施类型="限速标志牌", 责任组="施工队自报组"
    ).json()["entry"]
    assert created["责任组"] == "交安资产A组", "责任组冲突时以资产验收单为准"
    assert created["责任组来源"] == "资产验收单"
    assert "冲突" in created["冲突说明"]

    no_form = _create_revision(
        client, message_id="MSG-0102", 设施id=3, 修订类型="修复", 修复说明="补装", 责任组="交安九组"
    ).json()["entry"]
    assert no_form["责任组"] == "交安九组", "没有验收单时采用申请人口径"
    assert no_form["责任组来源"] == "修订申请"


def test_history_positions_kept_as_construction_snapshots(client: TestClient):
    created = _create_revision(client).json()["entry"]
    client.post(
        f"/api/traffic_facility_revision/{created['id']}/approve",
        json={"values": {"审批结论": "同意"}},
    )

    trajectory = client.get("/api/traffic_facility_revision/trajectory/1").json()
    positions = trajectory["轨迹"]
    assert len(positions) == 2
    old, new = positions
    assert old["状态"] == "历史"
    assert old["快照时间"] == "2026-09-01", "历史位置必须保留施工时快照，不得覆盖"
    assert old["位号"] == "W002300"
    assert new["状态"] == "当前"
    assert new["快照时间"] == "2026-10-01"
    assert new["位号"] == "W003000"


def test_trajectory_lists_affected_diseases_and_projects(client: TestClient):
    trajectory = client.get("/api/traffic_facility_revision/trajectory/1").json()
    disease_ids = [row["病害编号"] for row in trajectory["影响病害"]]
    assert disease_ids == ["PAVE-0001"], "K2+300 只落在 PAVE-0001 的桩号区间内"
    project_ids = [row["工程编号"] for row in trajectory["影响工程"]]
    assert project_ids == ["PROJ-0001"], "轨迹旁只显示同路段的养护工程"


def test_migrate_aligns_missing_position_codes_idempotently(client: TestClient):
    first = client.post("/api/traffic_facility_revision/migrate").json()
    assert first["ok"] is True
    migrated = {row["设施编号"]: row["位号"] for row in first["entry"]["迁移明细"]}
    assert migrated == {"TRAF-0003": "W009600", "TRAF-0004": "W018200"}

    facility = client.get("/api/traffic_facility/3").json()
    assert facility["位号"] == "W009600"

    second = client.post("/api/traffic_facility_revision/migrate").json()
    assert second["entry"]["迁移明细"] == [], "重复迁移必须是空操作"
    listing = client.get("/api/traffic_facility_revision", params={"revision_type": "位号迁移"}).json()
    assert listing["total"] == 2, "迁移只生成一次修订，不重复建单"


def test_double_approve_is_idempotent(client: TestClient):
    created = _create_revision(client).json()["entry"]
    url = f"/api/traffic_facility_revision/{created['id']}/approve"
    client.post(url, json={"values": {"审批结论": "同意"}})
    again = client.post(url, json={"values": {"审批结论": "同意"}}).json()
    assert again["ok"] is True
    assert "幂等" in again["message"]
    plans = [
        row for row in store.rows("material_plan") if row.get("来源修订编号") == created["修订编号"]
    ]
    assert len(plans) == 2, "重复批准不得重复生成材料计划"


def test_failed_approval_rolls_back_everything(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """落账中途失败：主表、空间索引、事件投影、路段清单、材料计划全部回滚。"""
    created = _create_revision(client).json()["entry"]

    def boom(self, revision):
        raise RuntimeError("模拟材料计划写入失败")

    monkeypatch.setattr(TrafficFacilityRevisionService, "_apply_material_plan", boom)
    failed = client.post(
        f"/api/traffic_facility_revision/{created['id']}/approve",
        json={"values": {"审批结论": "同意"}},
    ).json()
    assert failed["ok"] is False
    assert "回滚" in failed["message"]

    revision = client.get(f"/api/traffic_facility_revision/{created['id']}").json()
    assert revision["状态"] == "待审批", "失败后修订状态必须保持待审批"
    facility = client.get("/api/traffic_facility/1").json()
    assert facility["位号"] == "W002300", "主表不得留下半截更新"
    positions = [row for row in store.rows("traffic_facility_position") if row["设施id"] == 1]
    assert len(positions) == 1 and positions[0]["状态"] == "当前", "空间索引不得只更新地图"
    events = store.rows("traffic_facility_event")
    assert [row["事件类型"] for row in events] == ["修订申请"], "事件投影不得留下批准孤儿事件"
    assert store.rows("material_plan") == []
    section = client.get("/api/road_section/1").json()
    assert section["最近交安修订"] == ""


def test_approve_fails_when_section_missing_and_rolls_back(client: TestClient):
    """路段清单缺失时整个修订落账失败，不允许只更新设施台账。"""
    created = client.post(
        "/api/traffic_facility",
        json={"values": {"设施编号": "TRAF-0099", "设施类型": "轮廓标", "所属路段": "幽灵路段"}},
    ).json()["entry"]
    revision = _create_revision(
        client, message_id="MSG-0201", 设施id=created["id"]
    ).json()["entry"]
    failed = client.post(
        f"/api/traffic_facility_revision/{revision['id']}/approve",
        json={"values": {"审批结论": "同意"}},
    ).json()
    assert failed["ok"] is False
    assert "路段清单" in failed["message"]
    facility = client.get(f"/api/traffic_facility/{created['id']}").json()
    assert facility.get("当前修订") in ("", None), "失败时主表不得先落账"


def test_reject_keeps_ledger_and_map_untouched(client: TestClient):
    created = _create_revision(client).json()["entry"]
    rejected = client.post(
        f"/api/traffic_facility_revision/{created['id']}/reject",
        json={"values": {"审批结论": "资料不全"}},
    ).json()
    assert rejected["ok"] is True
    facility = client.get("/api/traffic_facility/1").json()
    assert facility["位号"] == "W002300"
    again = client.post(
        f"/api/traffic_facility_revision/{created['id']}/reject",
        json={"values": {"审批结论": "资料不全"}},
    ).json()
    assert "幂等" in again["message"]


def test_export_route_not_shadowed_by_entry_id(client: TestClient):
    response = client.get("/api/traffic_facility/export")
    assert response.status_code == 200
    assert response.json()["module"] == "traffic_facility"
