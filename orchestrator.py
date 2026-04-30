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
    ):
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        self.session_timeout = session_timeout

        self.tasks: list[ScanTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._shutdown = False

        # 检查 ngaent 清理命令是否可用（用于清理 nga 残留的并发锁文件）
        self._cleanup_available = shutil.which("ngaent") is not None
        if self._cleanup_available:
            logger.debug("ngaent cleanup available")
        self.repo_path: Optional[Path] = None
        self.start_commit: Optional[str] = None

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

        logger.info(
            f"=== Starting scan: {len(self.tasks)} files, "
            f"concurrency={self.concurrency}, timeout={self.session_timeout}s ==="
        )

        tracker = ProgressTracker(len(self.tasks))

        # 创建并发任务
        coros = [self._scan_one(task, tracker) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        tracker.finish()

        # 生成汇总报告
        total_time = sum(t.duration for t in self.tasks)
        self._save_summary(total_time)

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

                # 2. 启动 nga 子进程（命令行参数方式，TERM=dumb 避免 ANSI 乱码）
                env = os.environ.copy()
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

                async def _read_stream(stream, chunks: list[str], label: str, fh):
                    """实时读取 nga 输出，过滤 ANSI 后写入 log 文件"""
                    while True:
                        data = await stream.read(4096)
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        text = ANSI_ESCAPE.sub("", text)
                        chunks.append(text)
                        fh.write(text)
                        fh.flush()
                        io_stats["last_output_time"] = time.time()
                        io_stats["total_bytes"] += len(text)
                        io_stats["last_label"] = label

                # 启动后台读取任务
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_chunks, "OUT", log_fh)
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_chunks, "ERR", log_fh)
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

    args = parser.parse_args()

    # 创建调度器
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        session_timeout=args.timeout,
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
