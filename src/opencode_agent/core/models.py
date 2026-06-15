"""Core domain models for the OpenCode agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from opencode_agent.utils.logging import get_logger

logger = get_logger("Orchestrator")


@dataclass
class ScanTask:
    """单个文件的扫描任务"""

    file_path: str
    task_id: str
    report_file: str        # Markdown 报告路径
    log_file: str           # 运行日志路径
    function_name: str = ""  # Full 模式: 函数名
    status: str = "pending"  # pending, running, done, failed
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    stdout: str = ""         # nga stdout (审查结果)
    stderr: str = ""         # nga stderr
    error: str = ""          # 错误信息
    returncode: Optional[int] = None
    diff_content: str = ""   # diff 模式: 该文件的 diff 内容
    diff_file: str = ""      # diff 模式: diff 内容保存的文件路径
    slot_id: Optional[int] = None  # debug 模式下分配的 web 终端槽位

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return round(self.end_time - self.start_time, 1)
        return 0.0


class ProgressTracker:
    """终端进度跟踪器

    进度信息通过 logger.info 输出，不再使用 \r 刷新进度行，
    避免与 nga 实时输出冲突。
    """

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.running = 0
        self.failed = 0
        self.start_time = time.time()

    def start_task(self):
        self.running += 1

    def complete_task(self, success: bool = True):
        self.running -= 1
        self.completed += 1
        if not success:
            self.failed += 1
        self._print_progress()

    def finish(self):
        """结束进度显示，打印最终统计"""
        elapsed = time.time() - self.start_time
        logger.info(
            f"Finished: {self.completed}/{self.total} files | "
            f"Success: {self.completed - self.failed} | Failed: {self.failed} | "
            f"Total time: {elapsed:.1f}s"
        )

    def _print_progress(self):
        elapsed = time.time() - self.start_time
        pct = self.completed / self.total * 100 if self.total > 0 else 0
        logger.info(
            f"Progress: {self.completed}/{self.total} ({pct:.0f}%) | "
            f"Running: {self.running} | Failed: {self.failed} | "
            f"Elapsed: {elapsed:.0f}s"
        )
