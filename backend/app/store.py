"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
多表写入（主表、空间索引、事件投影）通过 transaction() 收敛到同一个原子边界：
任一环节失败就整体回滚，不允许出现"地图已更新、台账没跟上"这类半截状态。
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self.reset()

    def reset(self) -> None:
        """把全部表恢复成种子数据；测试与演示重跑时使用。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """多表写入的原子边界：进入时快照，任一环节抛错就整体回滚。"""
        with self._lock:
            backup = copy.deepcopy(self._tables)
            try:
                yield self
            except BaseException:
                self._tables = backup
                raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def next_id(self, module: str) -> int:
        """模块内下一个自增 id；调用方需处在 transaction() 里才不会并发重号。"""
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


store = Store()
