#!/usr/bin/env python3
"""
OpenCode Agent 并行调度器 (Orchestrator)

功能: 为每个 C/C++ 文件启动独立的 nga 进程进行审查，
      并发控制为3，处理完一个文件立即关闭 nga session，接着处理下一个文件。

两种输入模式:
  1. Diff 模式: 自动提取从指定 commit 到 HEAD 的变更文件
     python orchestrator.py --diff abc123 --repo ./app

  2. 文件列表模式: 手动指定要扫描的文件或目录
     python orchestrator.py --files file1.c file2.c dir1/ dir2/ -c 3

  3. 指定关注目录（两种模式都支持）:
     python orchestrator.py --diff abc123 --paths app/a,app/b --repo .

nga 交互方式:
  - 启动 nga 子进程: nga run '<message>'（命令行参数方式）
  - Diff 模式: message 为审查提示词，指引 nga 读取 diffs/ 下的 diff 文件
  - 文件模式: message 为 'review <file_path>'
  - 实时收集 stdout/stderr 到各自 .log 文件，过滤 ANSI 转义序列

超时策略（动态超时 + 软/硬两阶段）:
  - 超时按 diff 行数动态计算: 基础 300s + (diff_lines // 10) * 60s，封顶 900s (15min)
    | diff 行数 | 软超时 | 硬超时 |
    |-----------|--------|--------|
    | 0         | ~270s  | 300s   |
    | 50        | ~540s  | 600s   |
    | >=100     | ~870s  | 900s   |
  - 软超时: 先发送 SIGTERM，给 nga 机会 flush 已分析的部分结果
  - 硬超时: SIGTERM 后 30s 仍未退出，发送 SIGKILL 强制终止
  - 超时 kill 后，已收集到的部分 stdout 仍会保存到 .md 报告

输出:
  - 终端: START/DONE/进度摘要（每个 task 带文件路径前缀，方便追踪）
  - reports/YYYYMMDD_HHMMSS/<relative_path>/<file>.md: Markdown 审查报告
  - reports/YYYYMMDD_HHMMSS/<relative_path>/<file>.log: 运行日志（含 nga stdout/stderr）
  - reports/YYYYMMDD_HHMMSS/diffs/<relative_path>/<file>.diff: diff 内容（Diff 模式）
  - reports/YYYYMMDD_HHMMSS/summary.md: 汇总报告
  - reports/YYYYMMDD_HHMMSS/orchestrator.log: 全局执行日志

输出路径规则:
  - 报告和日志按文件的完整相对路径存放，保留 cared_path 前缀
  - diff 文件单独存放在 diffs/ 子目录下，同样保留目录结构
  - 示例: cared_path=src/rr, 文件=src/rr/abc/cde/efg/Hello.c
    -> reports/20250429/src/rr/abc/cde/efg/Hello.md
    -> reports/20250429/src/rr/abc/cde/efg/Hello.log
    -> reports/20250429/diffs/src/rr/abc/cde/efg/Hello.diff
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from knowledge_graph import KnowledgeGraph
from sast_engine import SASTEngine, RouteDecision, format_sast_issue_markdown

# Optional HTTP client for web debug interface (only used when --debug)
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

# ANSI 转义序列过滤（用于清理 nga 终端控制输出，作为 TERM=dumb 的兜底）
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# ============================================================================
# 日志配置: 终端显示进度
# ============================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 根 logger
logger = logging.getLogger("Orchestrator")
logger.setLevel(logging.DEBUG)

# 终端 handler (INFO 级别，显示进度)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(console_handler)


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ScanTask:
    """单个文件的扫描任务"""
    file_path: str
    task_id: str
    report_file: str        # Markdown 报告路径
    log_file: str           # 运行日志路径
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
    sast_issues: list = None  # Phase 1: SAST 预扫描发现的问题

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


# ============================================================================
# Markdown 报告生成
# ============================================================================

def generate_report(task: ScanTask) -> str:
    """生成 Markdown 审查报告 — 只展示审查结果（nga stdout）"""
    lines = []
    lines.append(f"# 代码审查报告 - {Path(task.file_path).name}")
    lines.append("")
    lines.append(f"**文件**: `{task.file_path}`")
    lines.append(f"**任务ID**: `{task.task_id}`")
    lines.append(f"**扫描时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**耗时**: {task.duration}s")
    lines.append(f"**状态**: {'完成' if task.status == 'done' else '失败'}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if task.stdout.strip():
        # 直接展示 nga 的审查结果，不加代码块包装
        lines.append(task.stdout)
    else:
        lines.append("*无审查结果*")

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by OpenCode Orchestrator*")

    return "\n".join(lines)


def generate_log(task: ScanTask) -> str:
    """生成运行日志 — 保存运行详情和 stderr"""
    lines = []
    lines.append(f"=== Task: {task.task_id} ===")
    lines.append(f"File: {task.file_path}")
    lines.append(f"Status: {task.status}")
    lines.append(f"Duration: {task.duration}s")
    lines.append(f"Return code: {task.returncode}")
    lines.append(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    if task.error:
        lines.append("=== Error ===")
        lines.append(task.error)
        lines.append("")

    lines.append("=== STDERR ===")
    if task.stderr.strip():
        lines.append(task.stderr)
    else:
        lines.append("*No stderr output*")

    return "\n".join(lines)


def generate_summary(tasks: list[ScanTask], total_time: float) -> str:
    """生成 Markdown 汇总报告"""
    done = sum(1 for t in tasks if t.status == "done")
    failed = sum(1 for t in tasks if t.status == "failed")

    lines = []
    lines.append("# 扫描汇总报告")
    lines.append("")
    lines.append("## 统计")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 总文件数 | {len(tasks)} |")
    lines.append(f"| 成功 | {done} |")
    lines.append(f"| 失败 | {failed} |")
    lines.append(f"| 总耗时 | {total_time:.1f}s |")
    lines.append(f"| 生成时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append("")

    lines.append("## 详细结果")
    lines.append("")
    lines.append("| # | 文件 | 状态 | 耗时 | 报告 | 日志 |")
    lines.append("|---|------|------|------|------|------|")

    for i, t in enumerate(tasks, 1):
        status_icon = "✅" if t.status == "done" else "❌"
        report_name = Path(t.report_file).name
        log_name = Path(t.log_file).name
        report_link = f"[{report_name}]({Path(t.report_file).relative_to(Path(t.report_file).parents[-3])})"
        log_link = f"[{log_name}]({Path(t.log_file).relative_to(Path(t.log_file).parents[-3])})"
        lines.append(
            f"| {i} | `{t.file_path}` | {status_icon} {t.status} | {t.duration}s | {report_link} | {log_link} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("*Generated by OpenCode Orchestrator*")

    return "\n".join(lines)


# ============================================================================
# 槽位管理器（将 Semaphore 并发槽位显式化，映射到 Web 终端窗口）
# ============================================================================

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


# ============================================================================
# Orchestrator 核心
# ============================================================================

class OpenCodeOrchestrator:
    """
    Event Loop 并发调度器
    - 每个文件一个独立的 nga session
    - 最多3个并行
    - 处理完一个文件立即关闭 nga，处理下一个
    """

    def __init__(
        self,
        concurrency: int = 3,
        nga_bin: str = "nga",
        session_timeout: int = 600,
        debug: bool = False,
        web_port: int = 8080,
    ):
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        self.session_timeout = session_timeout
        self.debug = debug
        self.web_port = web_port

        self.tasks: list[ScanTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._shutdown = False

        # 检查 ngaent 清理命令是否可用（用于清理 nga 残留的并发锁文件）
        self._cleanup_available = shutil.which("ngaent") is not None
        if self._cleanup_available:
            logger.debug("ngaent cleanup available")
        self.repo_path: Optional[Path] = None
        self.start_commit: Optional[str] = None

        # Knowledge graph (Phase 0)
        self.knowledge_graph = KnowledgeGraph(".claude/knowledge.db")
        logger.info(f"Knowledge graph loaded: {self.knowledge_graph.stats()}")

        # SAST engine (Phase 1)
        self.sast_engine = SASTEngine()
        if self.sast_engine.has_tools:
            logger.info("SAST engine active: high-confidence issues will bypass LLM")
        else:
            logger.info("SAST engine inactive (no tools installed): all scanning via LLM")

        # debug 模式下的槽位管理和 web 服务器状态
        self.slot_manager: Optional[SlotManager] = None
        self.web_proc: Optional[subprocess.Popen] = None
        self.web_client: Optional["httpx.AsyncClient"] = None  # type: ignore
        if self.debug:
            self.slot_manager = SlotManager(num_slots=concurrency)
            if httpx is not None:
                self.web_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            else:
                logger.warning("httpx not installed, web debug will not work. Run: pip install httpx")

        # 输出目录
        self.output_dir = Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")

        # diff 文件单独存放
        self.diff_dir = self.output_dir / "diffs"
        self.diff_dir.mkdir(parents=True, exist_ok=True)

        # 全局日志文件 (orchestrator.log)
        self.log_file = self.output_dir / "orchestrator.log"
        # 移除已有的 file handlers，避免重复
        for h in logger.handlers[:]:
            if isinstance(h, logging.FileHandler):
                logger.removeHandler(h)
                h.close()
        file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(file_handler)
        logger.debug(f"Global log file: {self.log_file}")

    # ------------------------------------------------------------------
    #  Web Debug 服务器管理
    # ------------------------------------------------------------------

    async def _start_web_server(self):
        """启动 gunicorn 运行 web_server.py（FastAPI）。"""
        if not self.debug or self.web_client is None:
            return

        script_dir = Path(__file__).parent.resolve()
        web_cmd = [
            sys.executable, "-m", "gunicorn",
            "web_server:app",
            "-k", "uvicorn.workers.UvicornWorker",
            "--bind", f"0.0.0.0:{self.web_port}",
            "--workers", "1",
            "--access-logfile", "-",
        ]
        logger.info(f"Starting web debug server: http://localhost:{self.web_port}")
        self.web_proc = subprocess.Popen(
            web_cmd,
            cwd=str(script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 等待 web server 就绪（轮询 / 最多 5 秒）
        for _ in range(50):
            try:
                resp = await self.web_client.get(f"http://localhost:{self.web_port}/")
                if resp.status_code == 200:
                    logger.info(f"Web debug interface ready: http://localhost:{self.web_port}")
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        logger.warning("Web server did not become ready within 5s")

    async def _stop_web_server(self):
        """停止 gunicorn web 服务器。"""
        if self.web_proc is not None:
            self.web_proc.terminate()
            try:
                self.web_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.web_proc.kill()
                self.web_proc.wait()
            self.web_proc = None
            logger.info("Web debug server stopped")
        if self.web_client is not None:
            await self.web_client.aclose()
            self.web_client = None

    # ------------------------------------------------------------------
    #  Web Debug HTTP API 调用
    # ------------------------------------------------------------------

    async def _web_acquire(self, slot_id: int, task_id: str, file_path: str):
        if self.web_client is None:
            return
        try:
            await self.web_client.post(
                f"http://localhost:{self.web_port}/api/slot/{slot_id}/acquire",
                json={"task_id": task_id, "file_path": file_path},
            )
        except Exception as e:
            logger.debug(f"Web acquire failed: {e}")

    async def _web_push(self, slot_id: int, log_type: str, content: str):
        if self.web_client is None:
            return
        try:
            await self.web_client.post(
                f"http://localhost:{self.web_port}/api/slot/{slot_id}/push",
                json={"log_type": log_type, "content": content},
            )
        except Exception as e:
            logger.debug(f"Web push failed: {e}")

    async def _web_status(self, slot_id: int, status: str, duration: float = 0.0):
        if self.web_client is None:
            return
        try:
            await self.web_client.post(
                f"http://localhost:{self.web_port}/api/slot/{slot_id}/status",
                json={"status": status, "duration": duration},
            )
        except Exception as e:
            logger.debug(f"Web status failed: {e}")

    async def _web_release(self, slot_id: int):
        if self.web_client is None:
            return
        try:
            await self.web_client.post(
                f"http://localhost:{self.web_port}/api/slot/{slot_id}/release",
            )
        except Exception as e:
            logger.debug(f"Web release failed: {e}")

    # ------------------------------------------------------------------
    #  路径计算
    # ------------------------------------------------------------------

    def _get_output_paths(self, file_path: str, cared_paths: Optional[list[str]]) -> tuple[Path, Path]:
        """
        计算报告和日志的输出路径。

        规则：
        - 使用文件路径的完整相对路径作为目录结构，保留 cared_path 前缀
        - 这样不同 cared_path 的文件不会混在一起

        示例:
          cared_path=src/rr, file=src/rr/abc/cde/efg/Hello.c
          -> sub_dir=src/rr/abc/cde/efg, stem=Hello
          -> reports/20250429/src/rr/abc/cde/efg/Hello.md
        """
        path_obj = Path(file_path)
        sub_dir = path_obj.parent
        file_stem = path_obj.stem

        base_dir = self.output_dir / sub_dir
        base_dir.mkdir(parents=True, exist_ok=True)

        report_file = base_dir / f"{file_stem}.md"
        log_file = base_dir / f"{file_stem}.log"

        return report_file, log_file

    # ------------------------------------------------------------------
    #  任务初始化
    # ------------------------------------------------------------------

    def setup_file_mode(self, file_paths: list[str], cared_paths: Optional[list[str]] = None):
        """文件列表模式

        - 如果传入的是文件，直接加入任务队列
        - 如果传入的是目录，递归扫描目录下的 C/C++ 文件
        - 路径统一用相对路径（相对于当前工作目录）
        """
        all_files: list[str] = []
        c_extensions = (".c", ".cc", ".cpp", ".h", ".hpp")
        cwd = Path.cwd()

        for fp in file_paths:
            path = Path(fp)
            if path.is_file():
                rel_path = path.relative_to(cwd) if path.is_absolute() else path
                all_files.append(str(rel_path))
            elif path.is_dir():
                for ext in c_extensions:
                    for p in path.rglob(f"*{ext}"):
                        rel_path = p.relative_to(cwd) if p.is_absolute() else p
                        all_files.append(str(rel_path))
            else:
                logger.warning(f"Path not found: {fp}")

        all_files = sorted(set(all_files))

        for i, fp in enumerate(all_files, 1):
            report_file, log_file = self._get_output_paths(fp, cared_paths)
            self.tasks.append(ScanTask(
                file_path=fp,
                task_id=f"task-{i:03d}",
                report_file=str(report_file),
                log_file=str(log_file),
            ))
        logger.info(f"File mode: {len(self.tasks)} files")

    def setup_diff_mode(self, start_commit: str, repo_path: str = ".", cared_paths: Optional[list[str]] = None):
        """Diff 模式: 提取变更文件及其 diff 内容"""
        repo = Path(repo_path).resolve()
        self.repo_path = repo
        self.start_commit = start_commit
        logger.info(f"Diff mode: repo={repo}, start_commit={start_commit}")

        changed_files = self._get_changed_files(repo, start_commit)
        if not changed_files:
            logger.warning("No changed files found")
            return

        # 过滤 C/C++ 文件
        c_extensions = (".c", ".cc", ".cpp", ".h", ".hpp")
        changed_files = [f for f in changed_files if f.endswith(c_extensions)]
        logger.info(f"C/C++ changed files: {len(changed_files)}")

        # 如果指定了 cared_paths，过滤
        if cared_paths:
            changed_files = self._filter_by_cared_paths(changed_files, cared_paths)
            logger.info(f"After cared_paths filter: {len(changed_files)} files")

        for i, fp in enumerate(changed_files, 1):
            report_file, log_file = self._get_output_paths(fp, cared_paths)

            # 提取该文件的 diff 内容
            diff_content = self._get_file_diff(repo, start_commit, fp)
            diff_file = ""
            if diff_content:
                diff_path = self.diff_dir / Path(fp).parent / f"{Path(fp).stem}.diff"
                diff_path.parent.mkdir(parents=True, exist_ok=True)
                diff_path.write_text(diff_content, encoding="utf-8")
                diff_file = str(diff_path)
                logger.debug(f"[{i:03d}] Diff saved: {diff_path}")

            self.tasks.append(ScanTask(
                file_path=fp,
                task_id=f"task-{i:03d}",
                report_file=str(report_file),
                log_file=str(log_file),
                diff_content=diff_content,
                diff_file=diff_file,
            ))

    def _get_changed_files(self, repo: Path, start_commit: str) -> list[str]:
        """执行 git diff 获取变更文件列表"""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "diff", "--diff-filter=AM", "--name-only", f"{start_commit}..HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
            files = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            logger.info(f"Git diff found {len(files)} changed files")
            return files
        except subprocess.CalledProcessError as e:
            logger.error(f"git diff failed: {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"Failed to get changed files: {e}")
            return []

    @staticmethod
    def _get_file_diff(repo: Path, start_commit: str, file_path: str) -> str:
        """执行 git diff 获取单个文件的 diff 内容"""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "diff", f"{start_commit}..HEAD", "--", file_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""
        except Exception as e:
            logger.warning(f"Failed to get diff for {file_path}: {e}")
            return ""

    @staticmethod
    def _filter_by_cared_paths(file_paths: list[str], cared_paths: list[str]) -> list[str]:
        """过滤出路径前缀匹配 cared_paths 的文件（精确匹配，避免误判）"""
        normalized_cared = [cp.rstrip("/") for cp in cared_paths]
        filtered = []
        for fp in file_paths:
            for cp in normalized_cared:
                if fp == cp or fp.startswith(cp + "/"):
                    filtered.append(fp)
                    break
        return filtered

    # ------------------------------------------------------------------
    #  主控循环
    # ------------------------------------------------------------------

    async def run(self):
        """主入口"""
        if not self.tasks:
            logger.warning("No tasks to run")
            return

        # debug 模式下启动 web server
        if self.debug:
            await self._start_web_server()

        logger.info(
            f"=== Starting scan: {len(self.tasks)} files, "
            f"concurrency={self.concurrency}, timeout={self.session_timeout}s "
            f"debug={self.debug} ==="
        )

        tracker = ProgressTracker(len(self.tasks))

        # Phase 1: SAST pre-scan (fast local analysis)
        sast_results: dict[str, list] = {}
        if self.sast_engine.has_tools:
            logger.info("Running SAST pre-scan...")
            file_paths = [t.file_path for t in self.tasks]
            sast_results = self.sast_engine.scan_batch(file_paths)
            direct_count = 0
            llm_count = 0
            for fp, issues in sast_results.items():
                direct = self.sast_engine.direct_output_issues(issues)
                llm_needed = self.sast_engine.llm_issues(issues)
                direct_count += len(direct)
                llm_count += len(llm_needed)
                # Attach to tasks
                for task in self.tasks:
                    if task.file_path == fp:
                        task.sast_issues = issues
                        break
            logger.info(
                f"SAST pre-scan: {direct_count} direct-output, "
                f"{llm_count} need LLM, "
                f"{len(self.tasks) - len([t for t in self.tasks if t.sast_issues])} no findings"
            )

        # 创建并发任务
        coros = [self._scan_one(task, tracker) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        tracker.finish()

        # 生成汇总报告
        total_time = sum(t.duration for t in self.tasks)
        self._save_summary(total_time)

        # Phase 0: 记录扫描运行
        issues_found = sum(
            1 for t in self.tasks
            if t.status == "done" and self.knowledge_graph.get_cases_by_file(t.file_path, limit=1)
        )
        self.knowledge_graph.record_scan_run(
            run_id=self.output_dir.name,
            total_files=len(self.tasks),
            issues_found=issues_found,
            duration=total_time,
            commit_hash=self.start_commit or "",
            branch="",
        )

        # 知识库统计
        stats = self.knowledge_graph.stats()
        logger.info(f"Knowledge graph stats: {stats}")

        # 关闭知识库连接
        self.knowledge_graph.close()

        # 扫描结束后关闭 web server
        if self.debug:
            await self._stop_web_server()

    def _build_diff_scan_cmd(self, task: ScanTask) -> str:
        """Diff 模式下构造审查提示词，注入知识上下文后指引 nga 审查"""
        parts = []
        parts.append(f"请审查文件 {task.file_path} 的代码变更。\n")

        # 1. 知识上下文：从 Knowledge Graph 匹配相关 Pattern
        relevant = self.knowledge_graph.find_relevant_patterns(
            task.file_path, task.diff_content, top_k=5
        )
        if relevant:
            parts.append("## 已知风险模式（请重点检查）\n")
            for p in relevant:
                rule_hint = f"[{p.rule_id}] " if p.rule_id else ""
                parts.append(f"- {rule_hint}{p.content}\n")
            parts.append("\n")

        # 2. 文件风险画像
        profile = self.knowledge_graph.get_file_profile(task.file_path)
        if profile and profile.total_issues > 0:
            parts.append("## 文件风险画像\n")
            parts.append(f"该文件历史发现 {profile.total_issues} 个问题，"
                        f"风险评分: {profile.risk_score:.1f}/10\n")
            if profile.top_patterns:
                parts.append(f"常见模式: {', '.join(profile.top_patterns[:3])}\n")
            parts.append("\n")

        # 3. 上次扫描遗留问题
        last_issues = self.knowledge_graph.get_last_scan_issues(task.file_path)
        if last_issues:
            parts.append("## 上次扫描遗留问题（请确认是否已修复）\n")
            for issue in last_issues:
                parts.append(f"- [{issue.rule_id}] {issue.message}")
                if issue.line_number > 0:
                    parts.append(f" (行 {issue.line_number})")
                parts.append("\n")
            parts.append("\n")

        # 3.5 SAST 预扫描发现的问题（Phase 1）
        if task.sast_issues:
            llm_issues = self.sast_engine.llm_issues(task.sast_issues)
            if llm_issues:
                parts.append("## SAST 预扫描发现的问题（请验证并补充上下文分析）\n")
                for issue in llm_issues:
                    parts.append(f"- [{issue.rule_id}] 行 {issue.line_number}: {issue.message}\n")
                parts.append("\n")

        # 4. Diff 内容指引
        if task.diff_file:
            parts.append(f"该文件的 diff 内容已保存到：{task.diff_file}\n")
            parts.append("请读取该 diff 文件，结合变更上下文进行审查。\n\n")

        # 5. 审查要求（现有）
        parts.append("## 审查要求\n")
        parts.append("1. 应用无线通信安全编码规则（RULE-001~RULE-010）对变更代码进行检查\n")
        parts.append("2. 如果变更在函数内部，请同时审查该函数的完整实现，包括：")
        parts.append("函数内所有变量的定义和声明、该函数的调用者（caller）、该函数调用的其他函数（callee）\n")
        parts.append("3. 如果变更涉及全局变量、结构体声明、枚举声明等不在函数体内的代码，")
        parts.append("请找到该符号的所有使用点并一并审查\n")
        parts.append("4. 对每个发现的问题提供：文件路径、行号、问题描述、代码片段、修复建议、置信度\n")
        parts.append("5. 如果上次扫描遗留问题仍未修复，请在报告中明确指出\n")
        parts.append("6. 输出格式要求：对每个问题使用 [RULE-XXX] 标记，方便后续自动提取\n")

        return "".join(parts)

    def _extract_findings(self, task: ScanTask) -> int:
        """从 nga stdout 中提取 RULE-XXX 标记的问题，存入知识图谱。"""
        if not task.stdout:
            return 0

        count = 0
        rule_pattern = re.compile(r'\[?RULE-(\d{3})\]?', re.IGNORECASE)

        # Simple extraction: find RULE-XXX mentions and the surrounding paragraph
        paragraphs = task.stdout.split('\n\n')
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            m = rule_pattern.search(para)
            if m:
                rule_id = f"RULE-{m.group(1)}"
                # Extract a one-line summary (first sentence or first 200 chars)
                summary = para.replace('\n', ' ').strip()
                if len(summary) > 300:
                    # Try to find first sentence
                    sentence_end = summary.find('。')
                    if sentence_end == -1:
                        sentence_end = summary.find('.')
                    if sentence_end > 10:
                        summary = summary[:sentence_end + 1]
                    else:
                        summary = summary[:300] + "..."

                self.knowledge_graph.add_case(
                    file_path=task.file_path,
                    line_number=0,
                    rule_id=rule_id,
                    message=summary,
                    code_snippet="",
                    confidence=0.7,
                    scan_id=task.task_id,
                )
                count += 1

        if count > 0:
            logger.info(f"[{task.task_id}] Extracted {count} findings into knowledge graph")
        return count

    def _format_sast_output(self, task: ScanTask, issues: list) -> str:
        """Format SAST direct-output issues as nga-compatible markdown."""
        lines = []
        lines.append("# SAST 本地扫描结果（高置信度，直接输出）")
        lines.append("")
        lines.append(f"**文件**: `{task.file_path}`")
        lines.append(f"**扫描工具**: SAST Engine (Semgrep/Cppcheck)")
        lines.append(f"**问题数**: {len(issues)}")
        lines.append("")
        for issue in issues:
            lines.append(format_sast_issue_markdown(issue))
        lines.append("---")
        lines.append("*Generated by SAST Engine (Phase 1)*")
        return "\n".join(lines)

    async def _cleanup_nga_locks(self, task_id: str):
        """执行 ngaent --cleanup-concurrency 清理残留锁"""
        if not self._cleanup_available:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "ngaent",
                "--cleanup-concurrency",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            logger.debug(f"[{task_id}] Cleanup done")
        except Exception as e:
            logger.debug(f"[{task_id}] Cleanup skipped: {e}")

    async def _cleanup_children(self, pid: int):
        """尝试清理指定进程的子进程（递归 kill，兼容无 pstree 的环境）"""
        try:
            # 使用 ps 递归获取所有后代进程
            list_proc = await asyncio.create_subprocess_exec(
                "sh", "-c",
                f"get_children() {{ ps -o pid= --ppid $1 2>/dev/null; }}; "
                f"for c1 in $(get_children {pid}); do "
                f"  echo $c1; "
                f"  for c2 in $(get_children $c1); do echo $c2; done; "
                f"done | sort -u",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(list_proc.communicate(), timeout=2)
            children = [p.strip() for p in stdout.decode().strip().split("\n") if p.strip()]
            if children:
                logger.debug(f"Killing children of pid={pid}: {children}")
                kill_proc = await asyncio.create_subprocess_exec(
                    "kill", "-9", *children,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_proc.wait(), timeout=2)
        except Exception:
            pass

    async def _wait_for_nga_slot(self, task_id: str):
        """通过 pgrep 检查系统中的 nga 进程数，超过限制则短暂等待

        这用于兜底：即使 Semaphore 释放了，如果 nga 进程（或其 daemon 子进程）
        还在系统中运行，我们等它消失后再启动新的，避免被 nga 的并发拦截。
        """
        try:
            for attempt in range(20):  # 最多等 10 秒
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-x", "nga",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
                count = len([p for p in stdout.decode().strip().split("\n") if p.strip()])
                if count < self.concurrency:
                    if attempt > 0:
                        logger.info(f"[{task_id}] NGA slot ready after wait (count={count})")
                    break
                logger.debug(
                    f"[{task_id}] NGA slot full (count={count}, max={self.concurrency}), "
                    f"waiting... ({attempt + 1}/20)"
                )
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"[{task_id}] NGA slot check skipped: {e}")

    async def _scan_one(self, task: ScanTask, tracker: ProgressTracker):
        """扫描单个文件"""
        async with self.semaphore:
            if self._shutdown:
                logger.warning(
                    f"[{task.task_id}] {task.file_path} | Skipped (shutdown)"
                )
                return

            # 启动前清理：处理上一个任务可能残留的锁/进程
            await self._cleanup_nga_locks(task.task_id)
            # 额外兜底：如果系统里还有 >=3 个 nga 进程，等它们退出
            await self._wait_for_nga_slot(task.task_id)

            task.status = "running"
            task.start_time = time.time()
            tracker.start_task()

            logger.info(f"[{task.task_id}] START {task.file_path}")

            # Phase 1: SAST fast path - bypass LLM for high-confidence issues
            if task.sast_issues:
                direct_issues = self.sast_engine.direct_output_issues(task.sast_issues)
                if direct_issues:
                    task.end_time = time.time()
                    task.status = "done"
                    task.returncode = 0
                    task.stdout = self._format_sast_output(task, direct_issues)
                    logger.info(
                        f"[{task.task_id}] SAST direct output: "
                        f"{len(direct_issues)} issues, bypassing LLM"
                    )
                    # Generate report
                    report_md = generate_report(task)
                    Path(task.report_file).write_text(report_md, encoding="utf-8")
                    # Update knowledge graph
                    self.knowledge_graph.update_file_profile(
                        task.file_path, new_issues=len(direct_issues)
                    )
                    tracker.complete_task(success=True)
                    return

                # Inject SAST findings as context into LLM prompt
                llm_issues = self.sast_engine.llm_issues(task.sast_issues)
                if llm_issues:
                    logger.info(
                        f"[{task.task_id}] SAST context: "
                        f"{len(llm_issues)} issues for LLM verification"
                    )

            # debug 模式下分配槽位并通知 web server
            slot_id: Optional[int] = None
            if self.debug and self.slot_manager is not None:
                slot_id = await self.slot_manager.acquire(task.task_id, task.file_path)
                task.slot_id = slot_id
                await self._web_acquire(slot_id, task.task_id, task.file_path)
                logger.info(f"[{task.task_id}] Assigned to web slot #{slot_id}")

            try:
                # 0. 按 diff 行数计算动态超时
                diff_lines = len(task.diff_content.splitlines()) if task.diff_content else 0
                extra = (diff_lines // 10) * 60
                session_timeout = min(300 + extra, 900)
                logger.info(
                    f"[{task.task_id}] {task.file_path} | Diff lines: {diff_lines}, "
                    f"session timeout: {session_timeout}s"
                )

                # 1. 构造命令参数
                if task.diff_content:
                    message = self._build_diff_scan_cmd(task)
                else:
                    message = f"review {task.file_path}"

                logger.debug(f"[{task.task_id}] Command: nga run '{message[:200]}...'")

                # 2. 启动 nga 子进程
                # debug 模式下使用 TERM=xterm-256color 保留 ANSI 输出（捕获思考过程）
                # 非 debug 模式下使用 TERM=dumb 过滤 ANSI
                env = os.environ.copy()
                if self.debug:
                    env["TERM"] = "xterm-256color"
                else:
                    env["TERM"] = "dumb"
                proc = await asyncio.create_subprocess_exec(
                    self.nga_bin,
                    "run",
                    message,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )

                stdout_chunks: list[str] = []
                stderr_chunks: list[str] = []

                # 打开 .log 文件，准备实时写入
                log_fh = Path(task.log_file).open("w", encoding="utf-8")
                log_fh.write(f"=== Task: {task.task_id} ===\n")
                log_fh.write(f"File: {task.file_path}\n")
                log_fh.write(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

                # 统计信息，用于超时诊断
                io_stats = {"last_output_time": time.time(), "total_bytes": 0, "last_label": ""}

                async def _read_stream(stream, chunks: list[str], label: str, fh, slot_id: Optional[int] = None):
                    """实时读取 nga 输出：
                    - 过滤 ANSI 后写入 log 文件（保留原有行为）
                    - 推送原始内容（含 ANSI）到 web debug 界面（debug 模式）
                    """
                    while True:
                        data = await stream.read(4096)
                        if not data:
                            break
                        raw_text = data.decode("utf-8", errors="replace")
                        # 推送原始内容到 web（保留 ANSI，让前端 ansi_up 渲染）
                        if slot_id is not None:
                            await self._web_push(slot_id, label, raw_text)
                        # 过滤 ANSI 后用于 log 文件和后续报告
                        clean_text = ANSI_ESCAPE.sub("", raw_text)
                        chunks.append(clean_text)
                        fh.write(clean_text)
                        fh.flush()
                        io_stats["last_output_time"] = time.time()
                        io_stats["total_bytes"] += len(clean_text)
                        io_stats["last_label"] = label

                # 启动后台读取任务（传入 slot_id 用于 web 推送）
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_chunks, "stdout", log_fh, slot_id)
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_chunks, "stderr", log_fh, slot_id)
                )

                # 3. 等待 nga 进程结束（软超时 SIGTERM + 硬超时 SIGKILL）
                soft_timeout = max(session_timeout - 30, int(session_timeout * 0.9))
                try:
                    task.returncode = await asyncio.wait_for(
                        proc.wait(), timeout=soft_timeout
                    )
                    logger.debug(
                        f"[{task.task_id}] Process exited with code {task.returncode}"
                    )
                except asyncio.TimeoutError:
                    # 软超时：优雅关闭，给 nga 机会 flush 部分结果
                    logger.warning(
                        f"[{task.task_id}] {task.file_path} | Soft timeout "
                        f"({soft_timeout}s), sending SIGTERM to let nga flush "
                        f"partial results..."
                    )
                    log_fh.write("\n=== Soft Timeout ===\n")
                    log_fh.write(
                        f"Sent SIGTERM at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    proc.send_signal(signal.SIGTERM)

                    try:
                        task.returncode = await asyncio.wait_for(proc.wait(), timeout=30)
                        logger.info(
                            f"[{task.task_id}] {task.file_path} | "
                            f"Graceful shutdown after SIGTERM"
                        )
                    except asyncio.TimeoutError:
                        # 硬超时：强制 kill
                        elapsed = time.time() - task.start_time
                        last_out_ago = time.time() - io_stats["last_output_time"]
                        diag = (
                            f"Hard timeout after {session_timeout}s | "
                            f"Last output: {last_out_ago:.1f}s ago | "
                            f"Total bytes: {io_stats['total_bytes']}"
                        )
                        logger.warning(
                            f"[{task.task_id}] {task.file_path} | {diag}"
                        )
                        log_fh.write("\n=== Hard Timeout ===\n")
                        log_fh.write(f"Total runtime: {elapsed:.1f}s\n")
                        log_fh.write(
                            f"Last output received: {last_out_ago:.1f}s ago\n"
                        )
                        log_fh.write(
                            f"Total bytes collected: {io_stats['total_bytes']}\n"
                        )
                        proc.kill()
                        await proc.wait()
                        # 清理可能残留的子进程，避免它们变成孤儿进程占用 nga 并发
                        await self._cleanup_children(proc.pid)
                        task.returncode = -1
                        task.error = diag

                # 等待读取任务完成（进程结束后 pipe 会 EOF，读取任务自然退出）
                await asyncio.gather(stdout_task, stderr_task)

                task.end_time = time.time()
                task.stdout = "".join(stdout_chunks)
                task.stderr = "".join(stderr_chunks)

                # 6. 判断结果
                if task.returncode == 0 and not task.error:
                    task.status = "done"
                    logger.info(
                        f"[{task.task_id}] DONE {task.duration}s | {task.file_path}"
                    )
                else:
                    task.status = "failed"
                    if not task.error:
                        task.error = task.stderr[:200] if task.stderr else "Unknown error"
                    logger.error(
                        f"[{task.task_id}] FAILED (code={task.returncode}) | {task.file_path} | {task.error}"
                    )

                # 7. 生成 Markdown 报告（只含审查结果）
                report_md = generate_report(task)
                Path(task.report_file).write_text(report_md, encoding="utf-8")
                logger.debug(f"[{task.task_id}] Report saved: {task.report_file}")

                # 追加尾部统计到 .log 文件并关闭
                log_fh.write("\n=== End ===\n")
                log_fh.write(f"Status: {task.status}\n")
                log_fh.write(f"Duration: {task.duration}s\n")
                log_fh.write(f"Return code: {task.returncode}\n")
                if task.error:
                    log_fh.write(f"Error: {task.error}\n")
                log_fh.close()
                logger.debug(f"[{task.task_id}] Log saved: {task.log_file}")

                tracker.complete_task(success=(task.status == "done"))

                # Phase 0: 知识提取与文件画像更新
                if task.status == "done":
                    extracted = self._extract_findings(task)
                    self.knowledge_graph.update_file_profile(
                        task.file_path, new_issues=extracted
                    )

                # 通知 web server 任务状态变更
                if slot_id is not None:
                    await self._web_status(slot_id, task.status, task.duration)

            except Exception as e:
                task.status = "failed"
                task.end_time = time.time()
                task.error = str(e)
                logger.error(
                    f"[{task.task_id}] {task.file_path} | EXCEPTION: {e}"
                )
                # 异常退出时，nga 子进程可能还在运行，必须强制终止
                if "proc" in locals() and proc is not None and proc.returncode is None:
                    logger.warning(
                        f"[{task.task_id}] Killing leaked nga process "
                        f"(pid={proc.pid}) due to exception"
                    )
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:
                        pass
                    # 尝试清理子进程
                    await self._cleanup_children(proc.pid)
                # 确保读取任务也被取消，避免挂起导致 Semaphore 不释放
                if "stdout_task" in locals() and stdout_task is not None:
                    stdout_task.cancel()
                if "stderr_task" in locals() and stderr_task is not None:
                    stderr_task.cancel()
                # 确保 log 文件被关闭，并追加异常信息
                try:
                    if "log_fh" in locals() and log_fh is not None and not log_fh.closed:
                        log_fh.write(f"\n=== Exception ===\n{e}\n")
                        log_fh.close()
                except Exception:
                    pass
                tracker.complete_task(success=False)

                # 通知 web server 异常状态
                if slot_id is not None:
                    await self._web_status(slot_id, "failed", 0.0)

            finally:
                # 释放 web 槽位（无论成功/失败/异常）
                if slot_id is not None:
                    await self._web_release(slot_id)
                    if self.slot_manager is not None:
                        await self.slot_manager.release(slot_id)
                    logger.info(f"[{task.task_id}] Released web slot #{slot_id}")

        # 任务完成后执行清理（兜底：清理本任务可能残留的锁）
        await self._cleanup_nga_locks(task.task_id)

    def _save_summary(self, total_time: float):
        """保存 Markdown 汇总报告"""
        summary_md = generate_summary(self.tasks, total_time)
        summary_file = self.output_dir / "summary.md"
        summary_file.write_text(summary_md, encoding="utf-8")
        logger.info(f"Summary report: {summary_file}")


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="并行运行 nga 审查 C/C++ 文件（每个文件一个 nga session）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Diff 模式（自动提取变更文件）
  python orchestrator.py --diff abc123 --repo ./app -c 3

  # 只扫描指定目录下的变更文件
  python orchestrator.py --diff abc123 --paths app/a,app/b --repo . -c 3

  # 文件列表模式
  python orchestrator.py --files file1.c file2.c file3.c -c 3

  # 递归扫描目录
  python orchestrator.py --files app/a app/b -c 3

  # 调整会话总超时
  python orchestrator.py --diff abc123 --timeout 600

  # 启动 Web 调试界面（实时显示 NGA 输出）
  python orchestrator.py --diff abc123 --repo . --debug --web-port 8080
        """,
    )

    # 输入模式（互斥）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="要扫描的文件或目录列表（目录会自动递归扫描 C/C++ 文件）",
    )
    group.add_argument(
        "--diff",
        metavar="COMMIT",
        help="起始 commit hash，自动提取从该 commit 到 HEAD 的变更文件",
    )

    parser.add_argument(
        "--paths",
        help="关注的相对目录，逗号分隔（如 app/a,app/b）。Diff 模式下只保留这些目录下的变更文件",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Git 仓库路径（Diff 模式用，默认当前目录）",
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=3,
        help="并发数，即同时运行的 nga 进程数（默认: 3）",
    )
    parser.add_argument(
        "--nga",
        default="nga",
        help="nga 可执行文件路径（默认: nga）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="单个 nga session 的总超时时间(秒)（默认: 600）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启动 Web 调试界面，实时显示 NGA 进程输出（默认关闭）",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web 调试界面端口（默认: 8080）",
    )

    args = parser.parse_args()

    # 创建调度器
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        session_timeout=args.timeout,
        debug=args.debug,
        web_port=args.web_port,
    )

    # 解析 cared_paths
    cared_paths = None
    if args.paths:
        cared_paths = [p.strip().rstrip("/") for p in args.paths.split(",")]
        logger.info(f"Cared paths: {cared_paths}")

    # 初始化任务
    if args.diff:
        orch.setup_diff_mode(start_commit=args.diff, repo_path=args.repo, cared_paths=cared_paths)
    else:
        orch.setup_file_mode(file_paths=args.files, cared_paths=cared_paths)

    if not orch.tasks:
        logger.error("No files to scan. Exiting.")
        sys.exit(1)

    # 信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(orch, "_shutdown", True))

    asyncio.run(orch.run())


if __name__ == "__main__":
    main()
