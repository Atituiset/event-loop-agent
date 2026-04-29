#!/usr/bin/env python3
"""
OpenCode Agent 并行调度器 (Orchestrator)

功能: 为每个 C/C++ 文件启动独立的 nga 进程进行审查，
      并发控制为3，处理完一个文件立即关闭 nga session，接着处理下一个文件。

两种输入模式:
  1. Diff 模式: 自动提取从指定 commit 到 HEAD 的变更文件
     python orchestrator.py --diff abc123 --repo ./app

  2. 文件列表模式: 手动指定要扫描的文件
     python orchestrator.py --files file1.c file2.c file3.c

nga 交互方式:
  - 启动 nga 子进程 (stdin/stdout/stderr 均为 PIPE)
  - 发送: nga run 'review <file_path>'
  - 等待 scan_delay 秒（给 nga 时间审查）
  - 发送: nga run '/exit' 关闭进程
  - 收集 stdout/stderr，生成 Markdown 审查报告
  - 超时自动 kill 进程

输出:
  - 终端: 实时进度日志
  - reports/YYYYMMDD_HHMMSS/*.md: 每个文件的 Markdown 审查报告
  - reports/YYYYMMDD_HHMMSS/summary.md: 汇总报告
"""

import argparse
import asyncio
import logging
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================================
# 日志配置: 终端显示进度，日志文件保存详细运行日志
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

# 文件 handler (DEBUG 级别，详细日志) — 延迟到知道输出目录后再添加
_file_handler: Optional[logging.FileHandler] = None


def setup_file_logger(log_dir: Path):
    """设置文件日志 handler"""
    global _file_handler
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "orchestrator.log"
    _file_handler = logging.FileHandler(log_file, encoding="utf-8")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(_file_handler)
    logger.info(f"Log file: {log_file}")


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class ScanTask:
    """单个文件的扫描任务"""
    file_path: str
    task_id: str
    status: str = "pending"          # pending, running, done, failed
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    report_file: str = ""            # Markdown 报告路径
    stdout: str = ""                 # nga stdout
    stderr: str = ""                 # nga stderr
    error: str = ""                  # 错误信息
    returncode: Optional[int] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return round(self.end_time - self.start_time, 1)
        return 0.0


class ProgressTracker:
    """终端进度跟踪器

    在进度显示期间，临时将 logger 的终端输出级别提升到 WARNING，
    避免 INFO 日志打断进度行。
    """

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.running = 0
        self.failed = 0
        self.start_time = time.time()
        self._printed = False
        # 保存 logger 的原始级别，用于恢复
        self._original_console_level = console_handler.level

    def _quiet_logger(self):
        """降低终端日志级别，避免打断进度显示"""
        console_handler.setLevel(logging.WARNING)

    def _restore_logger(self):
        """恢复终端日志级别"""
        console_handler.setLevel(self._original_console_level)

    def start_task(self):
        self._quiet_logger()
        self.running += 1
        self._print()

    def complete_task(self, success: bool = True):
        self.running -= 1
        self.completed += 1
        if not success:
            self.failed += 1
        self._print()

    def finish(self):
        """结束进度显示，打印最终统计"""
        self._restore_logger()
        if self._printed:
            print()  # 换行，清理进度行
        elapsed = time.time() - self.start_time
        logger.info(
            f"Finished: {self.completed}/{self.total} files | "
            f"Success: {self.completed - self.failed} | Failed: {self.failed} | "
            f"Total time: {elapsed:.1f}s"
        )

    def _print(self):
        elapsed = time.time() - self.start_time
        pct = self.completed / self.total * 100 if self.total > 0 else 0
        line = (
            f"\rProgress: {self.completed}/{self.total} ({pct:.0f}%) | "
            f"Running: {self.running} | Failed: {self.failed} | "
            f"Elapsed: {elapsed:.0f}s"
        )
        print(line, end="", flush=True)
        self._printed = True


# ============================================================================
# Markdown 报告生成
# ============================================================================

def generate_report(task: ScanTask) -> str:
    """为单个任务生成 Markdown 审查报告"""
    lines = []
    lines.append(f"# 代码审查报告 - {Path(task.file_path).name}")
    lines.append("")
    lines.append("## 扫描信息")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 文件路径 | `{task.file_path}` |")
    lines.append(f"| 任务ID | `{task.task_id}` |")
    lines.append(f"| 扫描时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    lines.append(f"| 耗时 | {task.duration}s |")
    lines.append(f"| 状态 | {'完成' if task.status == 'done' else '失败'} |")
    lines.append(f"| 退出码 | {task.returncode} |")
    lines.append("")

    if task.error:
        lines.append("## 错误信息")
        lines.append("")
        lines.append(f"```")
        lines.append(task.error)
        lines.append(f"```")
        lines.append("")

    # STDOUT
    if task.stdout.strip():
        lines.append("## STDOUT (审查输出)")
        lines.append("")
        lines.append("```")
        lines.append(task.stdout)
        lines.append("```")
        lines.append("")
    else:
        lines.append("## STDOUT (审查输出)")
        lines.append("")
        lines.append("*无输出*")
        lines.append("")

    # STDERR
    if task.stderr.strip():
        lines.append("## STDERR")
        lines.append("")
        lines.append("```")
        lines.append(task.stderr)
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by OpenCode Orchestrator at {datetime.now().isoformat()}*")

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
    lines.append("| # | 文件 | 状态 | 耗时 | 报告 |")
    lines.append("|---|------|------|------|------|")

    for i, t in enumerate(tasks, 1):
        status_icon = "✅" if t.status == "done" else "❌"
        report_link = f"[{Path(t.report_file).name}]({Path(t.report_file).name})"
        lines.append(
            f"| {i} | `{t.file_path}` | {status_icon} {t.status} | {t.duration}s | {report_link} |"
        )

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by OpenCode Orchestrator*")

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
        session_timeout: int = 300,
        scan_delay: int = 10,
    ):
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        self.session_timeout = session_timeout
        self.scan_delay = scan_delay

        self.tasks: list[ScanTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._shutdown = False

        # 输出目录
        self.output_dir = Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 设置文件日志
        setup_file_logger(self.output_dir)
        logger.info(f"Output directory: {self.output_dir}")

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
                # 统一转换为相对路径
                rel_path = path.relative_to(cwd) if path.is_absolute() else path
                all_files.append(str(rel_path))
            elif path.is_dir():
                # 递归扫描目录下的 C/C++ 文件，统一用相对路径
                for ext in c_extensions:
                    for p in path.rglob(f"*{ext}"):
                        rel_path = p.relative_to(cwd) if p.is_absolute() else p
                        all_files.append(str(rel_path))
            else:
                logger.warning(f"Path not found: {fp}")

        # 去重并排序
        all_files = sorted(set(all_files))

        for i, fp in enumerate(all_files, 1):
            self.tasks.append(ScanTask(
                file_path=fp,
                task_id=f"task-{i:03d}",
                report_file=str(self.output_dir / f"report_{i:03d}_{Path(fp).name}.md"),
            ))
        logger.info(f"File mode: {len(self.tasks)} files")

    def setup_diff_mode(self, start_commit: str, repo_path: str = ".", cared_paths: Optional[list[str]] = None):
        """Diff 模式: 提取变更文件

        - 执行 git diff 获取从 start_commit 到 HEAD 的变更文件
        - 过滤 C/C++ 文件
        - 如果指定了 cared_paths，只保留在 cared_paths 中的文件
        """
        repo = Path(repo_path).resolve()
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
            self.tasks.append(ScanTask(
                file_path=fp,
                task_id=f"task-{i:03d}",
                report_file=str(self.output_dir / f"report_{i:03d}_{Path(fp).name}.md"),
            ))

    @staticmethod
    def _filter_by_cared_paths(file_paths: list[str], cared_paths: list[str]) -> list[str]:
        """过滤出路径前缀匹配 cared_paths 的文件

        使用精确匹配：文件路径必须等于 cared_path，或在 cared_path 的子目录下。
        避免误判（如 app/a_test.c 不会被匹配到 app/a）。
        """
        # 标准化 cared_paths（去掉尾部斜杠）
        normalized_cared = [cp.rstrip("/") for cp in cared_paths]
        filtered = []
        for fp in file_paths:
            for cp in normalized_cared:
                # 精确匹配：文件路径等于 cared_path，或以 cared_path/ 开头
                if fp == cp or fp.startswith(cp + "/"):
                    filtered.append(fp)
                    break
        return filtered

    def _get_changed_files(self, repo: Path, start_commit: str) -> list[str]:
        """执行 git diff 获取变更文件列表"""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "diff", "--diff-filter=AM", "--name-only", f"{start_commit}..HEAD"],
                capture_output=True,
                text=True,
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
            f"concurrency={self.concurrency}, timeout={self.session_timeout}s, "
            f"scan_delay={self.scan_delay}s ==="
        )

        tracker = ProgressTracker(len(self.tasks))

        # 创建并发任务
        coros = [self._scan_one(task, tracker) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        tracker.finish()

        # 生成汇总报告
        total_time = sum(t.duration for t in self.tasks)
        self._save_summary(total_time)

    async def _scan_one(self, task: ScanTask, tracker: ProgressTracker):
        """扫描单个文件"""
        async with self.semaphore:
            if self._shutdown:
                logger.warning(f"[{task.task_id}] Skipped (shutdown)")
                return

            task.status = "running"
            task.start_time = time.time()
            tracker.start_task()

            logger.info(f"[{task.task_id}] START {task.file_path}")

            try:
                # 1. 启动 nga 子进程
                proc = await asyncio.create_subprocess_exec(
                    self.nga_bin,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # 2. 发送扫描命令: nga run 'review <file>'
                #    用 shlex.quote 处理文件路径中的特殊字符
                quoted_file = shlex.quote(task.file_path)
                scan_cmd = f"nga run 'review {quoted_file}'"

                logger.debug(f"[{task.task_id}] Send: {scan_cmd}")
                proc.stdin.write(scan_cmd.encode("utf-8") + b"\n")
                await proc.stdin.drain()

                # 3. 等待 nga 审查（给 nga 时间处理）
                logger.debug(f"[{task.task_id}] Waiting {self.scan_delay}s for scan...")
                await asyncio.sleep(self.scan_delay)

                # 4. 发送退出命令: nga run '/exit'
                exit_cmd = "nga run '/exit'"
                logger.debug(f"[{task.task_id}] Send: {exit_cmd}")
                proc.stdin.write(exit_cmd.encode("utf-8") + b"\n")
                await proc.stdin.drain()

                # 关闭 stdin，告诉 nga 没有更多输入了
                proc.stdin.close()
                await proc.stdin.wait_closed()

                # 5. 等待进程结束，带超时
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.session_timeout
                    )
                    task.returncode = proc.returncode
                    logger.debug(f"[{task.task_id}] Process exited with code {proc.returncode}")
                except asyncio.TimeoutError:
                    logger.warning(f"[{task.task_id}] Timeout after {self.session_timeout}s, killing...")
                    proc.kill()
                    stdout, stderr = await proc.communicate()
                    task.returncode = -1
                    task.error = f"Timeout after {self.session_timeout}s"

                task.end_time = time.time()
                task.stdout = stdout.decode("utf-8", errors="replace")
                task.stderr = stderr.decode("utf-8", errors="replace")

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

                # 7. 生成 Markdown 报告
                report_md = generate_report(task)
                Path(task.report_file).write_text(report_md, encoding="utf-8")
                logger.debug(f"[{task.task_id}] Report saved: {task.report_file}")

                tracker.complete_task(success=(task.status == "done"))

            except Exception as e:
                task.status = "failed"
                task.end_time = time.time()
                task.error = str(e)
                logger.error(f"[{task.task_id}] EXCEPTION: {e}")
                tracker.complete_task(success=False)

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

  # 调整扫描等待时间和超时
  python orchestrator.py --diff abc123 --scan-delay 20 --timeout 600
        """,
    )

    # 输入模式（互斥）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="要扫描的文件路径列表",
    )
    group.add_argument(
        "--diff",
        metavar="COMMIT",
        help="起始 commit hash，自动提取从该 commit 到 HEAD 的变更文件",
    )

    parser.add_argument(
        "--paths",
        help="关注的相对目录，逗号分隔（如 app/a,app/b）。Diff 模式下只保留这些目录下的变更文件；文件列表模式下递归扫描这些目录",
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
        "--scan-delay",
        type=int,
        default=10,
        help="发送扫描命令后等待的秒数，给 nga 时间审查（默认: 10）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="单个 nga session 的总超时时间(秒)（默认: 300）",
    )

    args = parser.parse_args()

    # 创建调度器
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        session_timeout=args.timeout,
        scan_delay=args.scan_delay,
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
