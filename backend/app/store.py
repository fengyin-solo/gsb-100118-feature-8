"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

设施编号位号修订台账引入了三类必须同生共死的数据：

- 主表：``traffic_facility`` 等业务表，保存当前生效状态；
- 空间索引：``facility_spatial_index``，按里程把设施落到路段网格上供地图检索；
- 事件投影：``facility_event_log``，只增不改的修订事件流，设施台账、路段清单、
  工程材料计划三处都从同一条修订事件读取结论。

因此仓库提供 ``transaction()`` 工作单元：业务逻辑在事务里改动的是深拷贝快照，
只有 ``commit`` 成功才一次性替换全部表；中途抛错则整体回滚，杜绝“地图已经挪到新
桩号、台账却没记修订”这类半成品。``idempotent`` 负责重复消息去重：同一条消息
（消息编号或内容指纹）重放时直接返回首次结果，不会生成第二次修订。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 修订域内部使用的表：不通过 seed 预置行，启动引导时建空表，也不进运营概览统计。
REVISION_TABLES = [
    "facility_revision",          # 设施编号位号修订台账（主台账）
    "facility_spatial_index",     # 空间索引（路段 + 里程网格）
    "facility_event_log",         # 事件投影（只增不改）
    "facility_position_history",  # 历史位置（施工时快照）
    "facility_asset_acceptance",  # 资产验收单（责任组冲突时的权威来源）
    "facility_material_plan",     # 工程材料计划（修订结论投影）
    "facility_idempotency",       # 重复消息幂等记录
]


class TransactionAbort(Exception):
    """业务逻辑显式要求回滚事务（带可读原因）。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in REVISION_TABLES:
            self._tables.setdefault(name, [])
        self._lock = threading.RLock()
        self._booted = False

    # ------------------------------------------------------------------ 基础读取
    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in REVISION_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ------------------------------------------------------------------ 事务一致性
    @contextmanager
    def transaction(self) -> Iterator["Transaction"]:
        """开启工作单元。

        事务内通过 ``tx.rows`` 操作的是整库深拷贝，commit 时在同一把锁内原子替换，
        保证主表、空间索引、事件投影要么一起生效、要么一起回滚。
        """
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            tx = Transaction(snapshot)
            try:
                yield tx
            except Exception:
                # 出错即丢弃快照：任何一处写失败都不允许留下半套修订。
                tx.rolled_back = True
                raise
            else:
                if not tx.rolled_back:
                    self._tables = snapshot

    def idempotent(self, message_key: str | None, fingerprint: str) -> dict[str, Any] | None:
        """查询消息是否已处理过；命中则返回首次结果字典，未命中返回 None。"""
        key = (message_key or "").strip() or f"fingerprint:{fingerprint}"
        for row in self.rows("facility_idempotency"):
            if row.get("message_key") == key:
                return row.get("first_result")
        return None

    def remember(self, message_key: str | None, fingerprint: str, first_result: dict[str, Any]) -> str:
        """登记一条已处理消息，返回实际落库的幂等键。"""
        key = (message_key or "").strip() or f"fingerprint:{fingerprint}"
        self.rows("facility_idempotency").append({
            "id": self.next_id("facility_idempotency"),
            "message_key": key,
            "fingerprint": fingerprint,
            "first_result": first_result,
        })
        return key

    @staticmethod
    def fingerprint(payload: dict[str, Any]) -> str:
        """对业务载荷做稳定指纹（键排序后取 sha1），用于无消息编号时的内容去重。"""
        normalized = ",".join(f"{k}={payload.get(k)}" for k in sorted(payload))
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ 启动引导
    def ensure_bootstrapped(self) -> None:
        """幂等引导：建空间索引/历史位置初始快照并放入示例资产验收单。"""
        if self._booted:
            return
        with self._lock:
            if self._booted:
                return
            # 延迟导入避免 store ↔ service 循环依赖。
            from app.services.facility_revision import revision_service

            revision_service.bootstrap(self)
            self._booted = True


class Transaction:
    """事务工作单元：包装快照表，并把幂等登记做成事务的一部分。"""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables
        self.rolled_back = False

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def fail(self, message: str) -> None:
        self.rolled_back = True
        raise TransactionAbort(message)

    def idempotent(self, message_key: str | None, fingerprint: str) -> dict[str, Any] | None:
        key = (message_key or "").strip() or f"fingerprint:{fingerprint}"
        for row in self.rows("facility_idempotency"):
            if row.get("message_key") == key:
                return row.get("first_result")
        return None

    def remember(self, message_key: str | None, fingerprint: str, first_result: dict[str, Any]) -> str:
        key = (message_key or "").strip() or f"fingerprint:{fingerprint}"
        self.rows("facility_idempotency").append({
            "id": self.next_id("facility_idempotency"),
            "message_key": key,
            "fingerprint": fingerprint,
            "first_result": first_result,
        })
        return key


store = Store()
