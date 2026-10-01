"""交安设施修订台账接口：移位/换型/修复登记连续修订，审批结论同步设施台账、路段清单与工程材料计划。

路由顺序约定：/export、/migrate、/trajectory/{id} 等静态或双段路径必须放在
/{revision_id} 之前，否则会被路径参数路由遮蔽。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.traffic_facility_revision import REVISION_STATUSES, REVISION_TYPES, TrafficFacilityRevisionService

router = APIRouter(prefix="/api/traffic_facility_revision", tags=["交安设施修订台账"])

service = TrafficFacilityRevisionService()


@router.get("", response_model=PageResult[dict])
def list_revisions(
    keyword: str | None = Query(default=None, description="按设施编号或修订编号检索"),
    status: str | None = Query(default=None, description="待审批、已批准、已驳回"),
    revision_type: str | None = Query(default=None, description="移位、换型、修复、位号迁移"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按条件翻页读取修订台账；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_revisions(
        keyword=keyword, status=status, revision_type=revision_type, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_revision(payload: EntryPayload) -> ActionResult:
    """登记一条移位/换型/修复修订；携带相同 message_id 的重复消息幂等命中，不重复建单。"""
    revision, message, duplicated = service.create_revision(payload.values)
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.get("/export")
def export_revisions() -> dict[str, Any]:
    """导出修订台账：返回当前全量修订记录。"""
    items, total = service.list_revisions(page=1, size=10000)
    return {"module": "traffic_facility_revision", "total": total, "items": items}


@router.post("/migrate", response_model=ActionResult)
def migrate_legacy() -> ActionResult:
    """存量缺失位号的设施按里程对齐补齐；迁移结果稳定，重复执行是空操作。"""
    migrated, message = service.migrate_legacy()
    return ActionResult(ok=True, message=message, entry={"迁移明细": migrated})


@router.get("/trajectory/{facility_id}", response_model=dict)
def get_trajectory(facility_id: int) -> dict:
    """地图轨迹：位置快照链 + 轨迹旁受影响的路面病害与养护工程。"""
    result = service.trajectory(facility_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"交安设施 {facility_id} 不存在或已归档")
    return result


@router.get("/meta")
def get_meta() -> dict[str, Any]:
    """修订类型与状态枚举，前端表单直接取用。"""
    return {"修订类型": REVISION_TYPES, "状态": REVISION_STATUSES}


@router.get("/{revision_id}", response_model=dict)
def get_revision(revision_id: int) -> dict:
    """读取单条修订明细；不存在时给出可读的错误说明。"""
    revision = service.get_revision(revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail=f"修订 {revision_id} 不存在")
    return revision


@router.post("/{revision_id}/approve", response_model=ActionResult)
def approve_revision(revision_id: int, payload: EntryPayload) -> ActionResult:
    """批准修订：主表、空间索引、事件投影、路段清单、材料计划同一事务落账；重复批准幂等忽略。"""
    revision, message = service.approve(
        revision_id,
        str(payload.values.get("审批结论") or ""),
        str(payload.values.get("审批人") or ""),
    )
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.post("/{revision_id}/reject", response_model=ActionResult)
def reject_revision(revision_id: int, payload: EntryPayload) -> ActionResult:
    """驳回修订：只改修订状态并写事件投影，台账与地图不动；重复驳回幂等忽略。"""
    revision, message = service.reject(
        revision_id,
        str(payload.values.get("审批结论") or ""),
        str(payload.values.get("审批人") or ""),
    )
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.get("/{revision_id}/projection", response_model=dict)
def get_projection(revision_id: int) -> dict:
    """三处读取同一修订：设施台账、路段清单、工程材料计划看到的同一条审批结论。"""
    result = service.projection(revision_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"修订 {revision_id} 不存在")
    return result
