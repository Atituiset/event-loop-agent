"""Explicit slot allocation for concurrent nga processes."""

from __future__ import annotations

import asyncio
from typing import Optional


class SlotManager:
    """
    为并发 nga 进程分配固定编号的槽位（slot）。
    每个 slot 对应 web 界面中的一个终端窗口。
    槽位数与 orchestrator 的 concurrency 一致（默认 3）。
    """

    def __init__(self, num_slots: int = 3):
        self.num_slots = num_slots
        self.slots: list[Optional[dict]] = [None] * num_slots
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._event.set()  # 初始有可用槽位

    async def acquire(self, task_id: str, file_path: str) -> int:
        """获取一个空闲槽位，返回 slot_id (0 ~ num_slots-1)。"""
        while True:
            async with self._lock:
                for i in range(self.num_slots):
                    if self.slots[i] is None:
                        self.slots[i] = {"task_id": task_id, "file_path": file_path}
                        if all(self.slots):
                            self._event.clear()
                        return i
            # 没有可用槽位，等待 release 唤醒
            await self._event.wait()

    async def release(self, slot_id: int):
        """释放指定槽位。"""
        async with self._lock:
            self.slots[slot_id] = None
            self._event.set()
