"""设施编号位号修订台账接口。

提交修订（移位/换型/修复/存量对齐）与审批结论全部走事务：主表、空间索引、
事件投影原子提交；消息编号或内容指纹命中时幂等返回首次结果。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    PageResult,
    RevisionDecisionPayload,
    RevisionSubmitPayload,
)
from app.services.facility_revision import revision_service

router = APIRouter(prefix="/api/facility_revision", tags=["设施编号位号修订台账"])


@router.get("", response_model=PageResult[dict])
def list_revisions(
    facility_id: int | None = Query(default=None, description="按设施过滤"),
    status: str | None = Query(default=None, description="待审批、审批通过、已驳回"),
    revision_type: str | None = Query(default=None, description="移位、换型、修复、存量对齐"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """修订台账列表：同一设施的修订序号连续，按时间倒序展示。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = revision_service.list_revisions(
        facility_id=facility_id,
        status_filter=status,
        revision_type=revision_type,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{revision_id}", response_model=dict)
def get_revision(revision_id: int) -> dict:
    """修订明细：含责任组仲裁记录、影响病害/工程、材料计划、历史位置快照。"""
    detail = revision_service.get_revision(revision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"修订 {revision_id} 不存在")
    return detail


@router.post("", response_model=ActionResult)
def submit_revision(payload: RevisionSubmitPayload) -> ActionResult:
    """提交一次修订。重复消息（相同 message_id 或相同业务内容）幂等返回首次修订。"""
    revision, message = revision_service.submit(payload.model_dump())
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.post("/{revision_id}/decision", response_model=ActionResult)
def decide_revision(revision_id: int, payload: RevisionDecisionPayload) -> ActionResult:
    """审批结论：通过则事务内同步设施台账、空间索引、事件投影与材料计划；驳回只记事件。"""
    if payload.approved:
        revision, message = revision_service.decide(
            revision_id, True, payload.approver or "", payload.opinion or "", payload.message_id
        )
    else:
        revision, message = revision_service.reject(
            revision_id, payload.approver or "", payload.opinion or "", payload.message_id
        )
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.get("/facility/{facility_id}/track", response_model=dict)
def facility_track(facility_id: int) -> dict:
    """地图轨迹：历史位置施工快照、修订序列及轨迹旁受影响路面病害与养护工程。"""
    result = revision_service.track(facility_id)
    if result.get("facility") is None:
        raise HTTPException(status_code=404, detail=f"交安设施 {facility_id} 不存在或已归档")
    return result


@router.post("/legacy/align", response_model=ActionResult)
def align_legacy() -> ActionResult:
    """存量缺失位号设施迁移对齐里程：批量开立「存量对齐」修订，幂等不重复。"""
    result = revision_service.align_legacy()
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/material/plans")
def material_plans(road: str | None = Query(default=None, description="按路段编号过滤")) -> dict:
    """工程材料计划：全部条目均带 revision_id，与台账/路段清单读取同一修订。"""
    items = revision_service.material_plans(road=road)
    return {"total": len(items), "items": items}
