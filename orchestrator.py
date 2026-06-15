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
import json
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


# ============================================================================
# Markdown 报告生成
# ============================================================================

def generate_report(task: ScanTask) -> str:
    """生成 Markdown 审查报告 — 只展示审查结果（nga stdout）"""
    lines = []

    if task.function_name:
        lines.append(f"# 代码审查报告 - {task.function_name}")
        lines.append("")
        lines.append(f"**文件**: `{task.file_path}`")
        lines.append(f"**函数**: `{task.function_name}`")
    else:
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


def generate_summary(tasks: list[ScanTask], total_time: float, output_dir: Path) -> str:
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
    lines.append(f"| 总任务数 | {len(tasks)} |")
    lines.append(f"| 成功 | {done} |")
    lines.append(f"| 失败 | {failed} |")
    lines.append(f"| 总耗时 | {total_time:.1f}s |")
    lines.append(f"| 生成时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append("")

    lines.append("## 详细结果")
    lines.append("")

    # 按文件分组
    from collections import OrderedDict
    file_groups: OrderedDict[str, list[ScanTask]] = OrderedDict()
    for t in tasks:
        file_groups.setdefault(t.file_path, []).append(t)

    for file_path, file_tasks in file_groups.items():
        lines.append(f"### `{file_path}`")
        lines.append("")
        lines.append("| # | 函数 | 状态 | 耗时 | 报告 | 日志 |")
        lines.append("|---|------|------|------|------|------|")

        for i, t in enumerate(file_tasks, 1):
            status_icon = "✅" if t.status == "done" else "❌"
            func_name = t.function_name or "(整文件)"
            report_name = Path(t.report_file).name
            log_name = Path(t.log_file).name
            # 链接相对于 summary.md 所在目录
            report_link = f"[{report_name}]({Path(t.report_file).relative_to(output_dir)})"
            log_link = f"[{log_name}]({Path(t.log_file).relative_to(output_dir)})"
            lines.append(
                f"| {i} | `{func_name}` | {status_icon} {t.status} | {t.duration}s | {report_link} | {log_link} |"
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
        workspace: str = "",
        output_json: bool = False,
    ):
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        self.session_timeout = session_timeout
        self.debug = debug
        self.web_port = web_port
        self.workspace = workspace
        self.output_json = output_json

        self.tasks: list[ScanTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._file_locks: dict[str, asyncio.Lock] = {}  # 文件级锁：同文件内函数串行执行
        self._function_cache: dict[str, list] = {}  # tree-sitter 解析结果缓存
        self._shutdown = False

        # 检查 ngaent 清理命令是否可用（用于清理 nga 残留的并发锁文件）
        self._cleanup_available = shutil.which("ngaent") is not None
        if self._cleanup_available:
            logger.debug("ngaent cleanup available")
        self.repo_path: Optional[Path] = None
        self.start_commit: Optional[str] = None

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
        if self.workspace:
            self.output_dir = Path(self.workspace) / "agent_review_report" / datetime.now().strftime("%Y%m%d_%H%M%S")
        else:
            self.output_dir = Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")

        # 结构化 finding 输出与存储（数据飞轮）
        self.repo_url: str = ""
        self.finding_store: Optional["FindingStore"] = None  # type: ignore
        if self.output_json:
            try:
                from finding_store import FindingStore

                db_path = self.output_dir / "findings.db"
                self.finding_store = FindingStore(str(db_path))
                logger.info(f"Finding store initialized: {db_path}")
            except Exception as e:
                logger.warning(f"Failed to initialize finding store: {e}")

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

        env = os.environ.copy()
        if self.finding_store is not None:
            env["OPENCODE_FINDINGS_DB"] = str(self.finding_store.db_path)

        self.web_proc = subprocess.Popen(
            web_cmd,
            cwd=str(script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
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

    def _get_output_paths(self, file_path: str, cared_paths: Optional[list[str]],
                          function_name: str = "") -> tuple[Path, Path]:
        """
        计算报告和日志的输出路径。

        规则：
        - 使用文件路径的完整相对路径作为目录结构，保留 cared_path 前缀
        - Full 模式：以源文件名（含扩展名）创建目录，函数名作为文件名
        - 这样不同 cared_path 的文件不会混在一起

        示例:
          普通模式: cared_path=src/rr, file=src/rr/abc/cde/efg/Hello.c
                    -> reports/20250429/src/rr/abc/cde/efg/Hello.md
          Full 模式: file=src/rr/abc/cde/efg/Hello.c, function=process_pdu
                    -> reports/20250429/src/rr/abc/cde/efg/Hello.c/process_pdu.md
        """
        path_obj = Path(file_path)

        # 处理绝对路径：转换为相对路径
        if path_obj.is_absolute():
            # 优先相对于 workspace
            if self.workspace:
                try:
                    path_obj = path_obj.relative_to(self.workspace)
                except ValueError:
                    pass  # 不在 workspace 下，继续下面的 fallback
            # 再尝试相对于当前目录
            if path_obj.is_absolute():
                try:
                    path_obj = path_obj.relative_to(Path.cwd())
                except ValueError:
                    # 最终 fallback：取尾部目录
                    parts = path_obj.parts
                    if len(parts) > 3:
                        path_obj = Path(*parts[3:])
                    else:
                        path_obj = Path(path_obj.name)

        sub_dir = path_obj.parent
        file_stem = path_obj.stem
        file_ext = path_obj.suffix

        if function_name:
            # Full 模式：文件目录 + 函数文件
            base_dir = self.output_dir / sub_dir / f"{file_stem}{file_ext}"
            base_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r'[^\w\-]', '_', function_name)
            report_file = base_dir / f"{safe_name}.md"
            log_file = base_dir / f"{safe_name}.log"
        else:
            base_dir = self.output_dir / sub_dir
            base_dir.mkdir(parents=True, exist_ok=True)
            report_file = base_dir / f"{file_stem}.md"
            log_file = base_dir / f"{file_stem}.log"

        return report_file, log_file

    # ------------------------------------------------------------------
    #  任务初始化
    # ------------------------------------------------------------------

    def _detect_repo_url(self, repo_path: Path | str = ".") -> str:
        """尝试通过 git remote 获取仓库 URL，用于生成稳定 finding ID。"""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def setup_file_mode(self, file_paths: list[str], cared_paths: Optional[list[str]] = None):
        """文件列表模式

        - 如果传入的是文件，直接加入任务队列
        - 如果传入的是目录，递归扫描目录下的 C/C++ 文件
        - 路径统一用相对路径（相对于当前工作目录）
        """
        self.repo_url = self._detect_repo_url(".")
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
        self.repo_url = self._detect_repo_url(repo)
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

    def setup_full_mode(self, file_paths: list[str], cared_paths: Optional[list[str]] = None):
        """Full 扫描模式：用 tree-sitter 按函数切分源文件

        - 收集 .c/.cc/.cpp 源文件（跳过头文件）
        - 用 tree-sitter 提取每个函数定义
        - 每个函数创建一个独立 ScanTask
        - tree-sitter 解析失败时降级为整文件扫描
        """
        self.repo_url = self._detect_repo_url(".")
        from function_splitter import extract_functions

        all_files: list[str] = []
        source_extensions = (".c", ".cc", ".cpp")
        cwd = Path.cwd()

        def _make_relative(p: Path) -> str:
            """将路径转换为相对路径，若不在 cwd 下则使用路径尾部"""
            if p.is_absolute():
                try:
                    return str(p.relative_to(cwd))
                except ValueError:
                    # 外部路径：使用从根目录起的相对路径
                    # 如 /home/atituiset/Projects/opencode-c-cpp-test/utils/logger.c
                    # -> opencode-c-cpp-test/utils/logger.c
                    parts = p.parts
                    # 跳过开头的 '/' 和 'home'/'Users' 等
                    if len(parts) > 3:
                        return str(Path(*parts[3:]))
                    return str(p.name)
            return str(p)

        for fp in file_paths:
            path = Path(fp)
            if path.is_file():
                if path.suffix.lower() in source_extensions:
                    # 保留原始绝对路径用于文件读取，同时存储相对路径用于报告
                    all_files.append(str(path))
                else:
                    logger.debug(f"Skipping non-source file: {fp}")
            elif path.is_dir():
                for ext in source_extensions:
                    for p in path.rglob(f"*{ext}"):
                        # 排除报告输出目录，避免递归扫描自己生成的报告
                        if "agent_review_report" in str(p):
                            continue
                        all_files.append(str(p))
            else:
                logger.warning(f"Path not found: {fp}")

        all_files = sorted(set(all_files))

        # 过滤 cared_paths
        if cared_paths:
            # 构建 (相对路径 -> 原始路径) 映射，用相对路径做前缀匹配过滤
            rel_map: dict[str, str] = {}
            for fp in all_files:
                p = Path(fp)
                rel = fp
                if p.is_absolute():
                    if self.workspace:
                        try:
                            rel = str(p.relative_to(self.workspace))
                        except ValueError:
                            pass
                    else:
                        try:
                            rel = str(p.relative_to(Path.cwd()))
                        except ValueError:
                            pass
                rel_map[rel] = fp
            filtered_rel = self._filter_by_cared_paths(list(rel_map.keys()), cared_paths)
            all_files = [rel_map[r] for r in filtered_rel]
            logger.info(f"After cared_paths filter: {len(all_files)} files")

        # 逐文件切分函数
        file_idx = 0
        func_idx = 0
        for fp in all_files:
            try:
                if fp in self._function_cache:
                    functions = self._function_cache[fp]
                else:
                    functions = extract_functions(fp)
                    self._function_cache[fp] = functions
            except Exception as e:
                logger.warning(f"[{fp}] tree-sitter parse failed: {e}, falling back to whole-file scan")
                file_idx += 1
                func_idx += 1
                report_file, log_file = self._get_output_paths(fp, cared_paths)
                self.tasks.append(ScanTask(
                    file_path=fp,
                    task_id=f"task-{file_idx:03d}-{func_idx:03d}",
                    report_file=str(report_file),
                    log_file=str(log_file),
                ))
                continue

            if not functions:
                logger.warning(f"[{fp}] No functions found, skipping")
                continue

            file_idx += 1
            for func in functions:
                func_idx += 1
                report_file, log_file = self._get_output_paths(
                    fp, cared_paths, function_name=func.name
                )
                self.tasks.append(ScanTask(
                    file_path=fp,
                    task_id=f"task-{file_idx:03d}-{func_idx:03d}",
                    report_file=str(report_file),
                    log_file=str(log_file),
                    function_name=func.name,
                ))

        logger.info(f"Full mode: {len(all_files)} files, {len(self.tasks)} function tasks")

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

        # 创建并发任务
        coros = [self._scan_one(task, tracker) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        tracker.finish()

        # 生成汇总报告
        total_time = sum(t.duration for t in self.tasks)
        self._save_summary(total_time)

        # 扫描结束后关闭 web server
        if self.debug:
            await self._stop_web_server()

    def _build_diff_scan_cmd(self, task: ScanTask) -> str:
        """Diff 模式下构造审查提示词，指引 nga 读取 diff 文件并审查"""
        message = (
            f"请审查文件 {task.file_path} 的代码变更。\n\n"
            f"该文件的 diff 内容已保存到：{task.diff_file}\n"
            f"请读取该 diff 文件，结合变更上下文进行审查。\n\n"
            f"审查要求：\n"
            f"1. 应用无线通信安全编码规则（RULE-001~RULE-010）对变更代码进行检查\n"
            f"2. 如果变更在函数内部，请同时审查该函数的完整实现，包括："
            f"函数内所有变量的定义和声明、该函数的调用者（caller）、该函数调用的其他函数（callee）\n"
            f"3. 如果变更涉及全局变量、结构体声明、枚举声明等不在函数体内的代码，"
            f"请找到该符号的所有使用点并一并审查\n"
            f"4. 对每个发现的问题提供：文件路径、行号、问题描述、代码片段、修复建议、置信度"
        )
        return message

    def _filter_known_false_positives(self, findings: list["Finding"]) -> list["Finding"]:
        """移除数据库中已被标记为 false_positive 的 finding。"""
        if not self.finding_store:
            return findings

        filtered = []
        for finding in findings:
            stored = self.finding_store.get_finding(finding.finding_id)
            if stored and stored.label == "false_positive":
                logger.info(
                    f"[memory] Suppressing known false positive {finding.finding_id} "
                    f"({finding.rule_id} @ {finding.file_path})"
                )
                continue
            filtered.append(finding)
        return filtered

    def _build_memory_section(self, file_path: str, function_name: str = "") -> str:
        """从历史反馈中构建 prompt 记忆段落，引导 nga 避免重复误报。"""
        if not self.finding_store:
            return ""

        try:
            labels = self.finding_store.get_historical_labels(file_path, function_name)
            if not labels:
                return ""

            false_positives: list[str] = []
            true_positives: list[str] = []

            for finding_id, label in labels.items():
                finding = self.finding_store.get_finding(finding_id)
                if not finding:
                    continue
                summary = f"- {finding.rule_id}: {finding.description[:80]}..."
                if label == "false_positive":
                    false_positives.append(summary)
                elif label == "true_positive":
                    true_positives.append(summary)

            lines = ["\n=== 历史审查记忆（开发者标注） ==="]
            if false_positives:
                lines.append("以下模式已被开发者标记为误报，请避免重复报告：")
                lines.extend(false_positives)
                lines.append("")
            if true_positives:
                lines.append("以下模式已被开发者确认为真实问题，请保持关注：")
                lines.extend(true_positives)
                lines.append("")
            lines.append("=====================================\n")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Failed to build memory section: {e}")
            return ""

    def _build_full_scan_cmd(self, task: ScanTask) -> str:
        """Full 模式下构造审查提示词，包含函数代码和 AST 元数据"""
        from function_splitter import extract_functions

        try:
            if task.file_path in self._function_cache:
                functions = self._function_cache[task.file_path]
            else:
                functions = extract_functions(task.file_path)
                self._function_cache[task.file_path] = functions
            func = next((f for f in functions if f.name == task.function_name), None)
            if func is None:
                logger.warning(f"[{task.task_id}] Function {task.function_name} not found, falling back to file review")
                return f"review {task.file_path}"
        except Exception as e:
            logger.warning(f"[{task.task_id}] Failed to extract function code: {e}")
            return f"review {task.file_path}"

        meta = func.metadata
        param_lines = "\n".join(
            f"  - {p['type']} {p['name']}" for p in meta.get("parameters", [])
        ) or "  - (无参数)"

        modifier_str = ", ".join(meta.get("modifiers", [])) or "无"

        mem_ops = meta.get("memory_ops", [])
        mem_str = ", ".join(mem_ops) if mem_ops else "无"

        branch = meta.get("branch_count", 0)
        branch_detail = meta.get("branch_breakdown", {})
        branch_parts = [f"{k} x{v}" for k, v in branch_detail.items() if v > 0]
        branch_str = f"{branch} ({', '.join(branch_parts)})" if branch_parts else "0"

        message = (
            f"请审查以下 C/C++ 函数，应用无线通信安全编码规则（RULE-001~RULE-010）进行全面检查：\n\n"
            f"文件: {task.file_path}\n"
            f"函数名: {task.function_name}\n"
            f"行号: {func.start_line}-{func.end_line}\n\n"
            f"=== 函数元数据 ===\n"
            f"返回类型: {meta.get('return_type', 'void')}\n"
            f"参数列表:\n{param_lines}\n"
            f"修饰符: {modifier_str}\n"
            f"内存操作: {mem_str}\n"
            f"分支复杂度: {branch_str}\n"
            f"==================\n\n"
            f"```c\n"
            f"{func.code_text}\n"
            f"```\n\n"
        )

        # 注入历史审查记忆
        if self.finding_store:
            message += self._build_memory_section(task.file_path, task.function_name)

        message += (
            f"审查要求：\n"
            f"1. 检查函数内所有变量的定义和声明，是否存在未初始化使用\n"
            f"2. 检查内存操作（malloc/free、memcpy 等）是否存在泄漏或越界\n"
            f"3. 检查指针操作和输入参数的边界校验\n"
            f"4. 检查 switch-case 是否有安全的 default 分支\n"
            f"5. 检查 TLV/ASN.1 解析是否有边界校验\n"
            f"6. 对每个发现的问题提供：问题描述、相关代码片段、修复建议、置信度等级\n\n"
            f"如果需要查看更多上下文（如调用者、被调用函数、结构体/宏定义），请使用工具搜索相关符号。"
        )
        return message

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
        """扫描单个文件

        并发控制策略：
        - 文件级锁：同一文件内的函数串行执行（避免 nga 数据库锁冲突）
        - 全局 Semaphore：不同文件之间可以并行（由 -c 参数控制）
        """
        file_lock = self._file_locks.setdefault(task.file_path, asyncio.Lock())
        async with file_lock:
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

                # debug 模式下分配槽位并通知 web server
                slot_id: Optional[int] = None
                if self.debug and self.slot_manager is not None:
                    slot_id = await self.slot_manager.acquire(task.task_id, task.file_path)
                    task.slot_id = slot_id
                    await self._web_acquire(slot_id, task.task_id, task.file_path)
                    logger.info(f"[{task.task_id}] Assigned to web slot #{slot_id}")

                try:
                    # 0. 计算动态超时
                    if task.function_name:
                        # Full 模式：按函数代码行数估算
                        try:
                            if task.file_path in self._function_cache:
                                functions = self._function_cache[task.file_path]
                            else:
                                from function_splitter import extract_functions
                                functions = extract_functions(task.file_path)
                                self._function_cache[task.file_path] = functions
                            func = next((f for f in functions if f.name == task.function_name), None)
                            func_lines = len(func.code_text.splitlines()) if func else 50
                        except Exception:
                            func_lines = 50
                        extra = (func_lines // 10) * 30
                        session_timeout = min(120 + extra, 300)
                        logger.info(
                            f"[{task.task_id}] {task.file_path}::{task.function_name} | "
                            f"Func lines: {func_lines}, session timeout: {session_timeout}s"
                        )
                    else:
                        # Diff 模式：按 diff 行数计算
                        diff_lines = len(task.diff_content.splitlines()) if task.diff_content else 0
                        extra = (diff_lines // 10) * 60
                        session_timeout = min(300 + extra, 900)
                        logger.info(
                            f"[{task.task_id}] {task.file_path} | Diff lines: {diff_lines}, "
                            f"session timeout: {session_timeout}s"
                        )

                    # 1. 构造命令参数
                    if task.function_name:
                        message = self._build_full_scan_cmd(task)
                    elif task.diff_content:
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
                    # 计算 --dir：优先用 workspace，否则用文件所在目录
                    dir_arg = self.workspace if self.workspace else str(Path(task.file_path).parent)
                    proc = await asyncio.create_subprocess_exec(
                        self.nga_bin,
                        "run",
                        "--dir", dir_arg,
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

                    # 7.5 生成结构化 finding 输出（数据飞轮）
                    if self.output_json and task.status == "done":
                        try:
                            from finding_parser import parse_findings_from_markdown

                            findings = parse_findings_from_markdown(
                                task.stdout,
                                repo_url=self.repo_url,
                                function_name=task.function_name,
                                task_id=task.task_id,
                                file_path=task.file_path,
                            )
                            findings = self._filter_known_false_positives(findings)
                            if findings:
                                findings_json_path = Path(task.report_file).with_suffix(".findings.json")
                                findings_json_path.parent.mkdir(parents=True, exist_ok=True)
                                findings_json_path.write_text(
                                    json.dumps([f.to_dict() for f in findings], indent=2, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                                logger.debug(f"[{task.task_id}] Findings JSON saved: {findings_json_path}")

                                if self.finding_store:
                                    self.finding_store.save_findings(findings)
                        except Exception as e:
                            logger.warning(f"[{task.task_id}] Failed to parse findings: {e}")

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
        summary_md = generate_summary(self.tasks, total_time, self.output_dir)
        summary_file = self.output_dir / "summary.md"
        summary_file.write_text(summary_md, encoding="utf-8")
        logger.info(f"Summary report: {summary_file}")


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="并行运行 nga 审查 C/C++ 文件（每个文件/函数一个 nga session）",
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

  # Full 扫描模式（按函数切分）
  python orchestrator.py --full app/ src/ -c 3

  # Full 扫描 + 路径过滤
  python orchestrator.py --full . --paths src/rr,src/mac -c 3

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
    group.add_argument(
        "--full",
        nargs="+",
        default=[],
        help="Full 扫描模式：按函数切分，每个函数一个 nga session",
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
    parser.add_argument(
        "--workspace",
        default="",
        help="nga 工作目录，传给 --dir 参数（默认使用被扫描文件所在目录）",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        help="同时输出结构化的 findings.json 并写入 findings.db（数据飞轮）",
    )

    args = parser.parse_args()

    # 创建调度器
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        session_timeout=args.timeout,
        debug=args.debug,
        web_port=args.web_port,
        workspace=args.workspace,
        output_json=args.output_json,
    )

    # 解析 cared_paths
    cared_paths = None
    if args.paths:
        cared_paths = [p.strip().rstrip("/") for p in args.paths.split(",")]
        logger.info(f"Cared paths: {cared_paths}")

    # 初始化任务
    if args.diff:
        orch.setup_diff_mode(start_commit=args.diff, repo_path=args.repo, cared_paths=cared_paths)
    elif args.full:
        orch.setup_full_mode(file_paths=args.full, cared_paths=cared_paths)
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
