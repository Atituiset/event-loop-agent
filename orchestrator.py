#!/usr/bin/env python3
"""
OpenCode Agent 并行调度器 (Orchestrator)

功能: 为每个任务启动独立的 nga 交互式进程，通过 stdin pipe 发送扫描指令，
      并发控制为3，自动分配任务。

两种模式:
  1. MR 模式: 扫描指定 MR 链接
     python orchestrator.py https://github.com/.../pull/100 -c 3

  2. Diff 模式: 扫描从指定 commit 到 HEAD 的所有变更文件
     python orchestrator.py --diff abc123 --repo ./app -c 3

原理:
    1. 启动 nga 子进程 (stdin/stdout/stderr 均为 PIPE)
    2. 通过 proc.stdin.write() 发送扫描命令
    3. 等待 nga 执行完毕并返回结果
    4. Semaphore 控制同时最多3个进程在跑
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Orchestrator")


@dataclass
class ScanTask:
    """扫描任务状态"""
    task_id: str = ""
    target: str = ""          # MR链接 或 文件列表描述
    status: str = "pending"   # pending, running, done, failed
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    output_file: str = ""
    error: str = ""
    returncode: Optional[int] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return round(self.end_time - self.start_time, 1)
        return 0.0


class OpenCodeOrchestrator:
    """
    Event Loop 并发调度器
    - 维护一个扫描任务队列
    - 用 asyncio.Semaphore 控制同时最多 N 个 nga 进程
    - 每个 nga 进程通过 stdin pipe 接收扫描命令
    """

    def __init__(
        self,
        concurrency: int = 3,
        nga_bin: str = "nga",
        scan_command_template: str = "review {target}",
        session_timeout: int = 600,
    ):
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        # 命令模板: {target} 会被替换为实际扫描目标（MR链接或文件列表）
        self.scan_command_template = scan_command_template
        self.session_timeout = session_timeout

        self.tasks: list[ScanTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._shutdown = False

        # 输出目录
        self.output_dir = Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")

    # ------------------------------------------------------------------
    #  MR 模式
    # ------------------------------------------------------------------

    def setup_mr_mode(self, mr_links: list[str]):
        """初始化 MR 扫描任务"""
        for i, link in enumerate(mr_links, 1):
            self.tasks.append(ScanTask(
                task_id=f"mr-{i:02d}",
                target=link,
                output_file=str(self.output_dir / f"mr_{i:02d}.log"),
            ))
        logger.info(f"MR mode: {len(self.tasks)} tasks")

    # ------------------------------------------------------------------
    #  Diff 模式
    # ------------------------------------------------------------------

    def setup_diff_mode(
        self,
        start_commit: str,
        repo_path: str = ".",
        batch_size: int = 10,
        file_extensions: tuple[str, ...] = (".c", ".cc", ".cpp", ".h", ".hpp"),
    ):
        """
        初始化 Diff 扫描任务
        1. 执行 git diff 获取从 start_commit 到 HEAD 的变更文件
        2. 按 batch_size 分组成多个任务
        3. 每组一个 nga session 扫描
        """
        repo = Path(repo_path).resolve()
        logger.info(f"Diff mode: repo={repo}, start_commit={start_commit}, batch={batch_size}")

        # 1. 获取变更文件列表（新增/修改的）
        changed_files = self._get_changed_files(repo, start_commit)
        if not changed_files:
            logger.warning("No changed files found")
            return

        logger.info(f"Found {len(changed_files)} changed files")

        # 2. 过滤指定扩展名（可选）
        if file_extensions:
            changed_files = [f for f in changed_files if f.endswith(file_extensions)]
            logger.info(f"After filtering {file_extensions}: {len(changed_files)} files")

        if not changed_files:
            logger.warning("No matching files after filter")
            return

        # 3. 按 batch_size 分组，每组一个任务
        for batch_idx in range(0, len(changed_files), batch_size):
            batch = changed_files[batch_idx:batch_idx + batch_size]
            task_num = batch_idx // batch_size + 1

            # 文件路径用空格分隔，传给 nga
            # 使用 repo 内的相对路径
            file_list = " ".join(batch)

            self.tasks.append(ScanTask(
                task_id=f"diff-{task_num:02d}",
                target=file_list,
                output_file=str(self.output_dir / f"diff_{task_num:02d}.log"),
            ))

        logger.info(f"Diff mode: {len(self.tasks)} batches, ~{batch_size} files each")

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
        """主入口：调度所有任务"""
        if not self.tasks:
            logger.warning("No tasks to run")
            return

        logger.info(
            f"=== Starting scan: {len(self.tasks)} tasks, "
            f"concurrency={self.concurrency}, timeout={self.session_timeout}s ==="
        )
        logger.info(f"Command template: '{self.scan_command_template}'")

        # 创建并发任务
        coros = [self._scan_one(task) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        # 汇总
        self._summary()

    async def _scan_one(self, task: ScanTask):
        """扫描单个任务：获取信号量槽位 -> 启动 nga -> stdin 发命令 -> 等待完成"""
        async with self.semaphore:
            if self._shutdown:
                logger.warning(f"[{task.task_id}] Skipped (shutdown)")
                return

            task.status = "running"
            task.start_time = time.time()

            # 构造要发送给 nga 的命令
            # {target} 会被替换为 MR链接 或 文件列表
            command = self.scan_command_template.format(target=task.target)

            logger.info(f"[{task.task_id}] START")
            logger.info(f"[{task.task_id}] Command: {command}")

            try:
                # 启动 nga 子进程（交互式 session）
                proc = await asyncio.create_subprocess_exec(
                    self.nga_bin,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # 发送扫描命令到 stdin
                proc.stdin.write(command.encode("utf-8") + b"\n")
                await proc.stdin.drain()

                # 可选：发送退出命令（如果 nga 需要）
                # proc.stdin.write(b"/exit\n")
                # await proc.stdin.drain()
                # proc.stdin.close()

                # 等待 nga 完成，带超时
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.session_timeout
                    )
                    task.returncode = proc.returncode
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[{task.task_id}] Timeout after {self.session_timeout}s, killing..."
                    )
                    proc.kill()
                    stdout, stderr = await proc.communicate()
                    task.returncode = -1
                    task.error = f"Timeout after {self.session_timeout}s"

                task.end_time = time.time()

                # 保存输出
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                log_path = Path(task.output_file)
                with log_path.open("w", encoding="utf-8") as f:
                    f.write(f"=== Target: {task.target} ===\n")
                    f.write(f"=== Task: {task.task_id} ===\n")
                    f.write(f"=== Duration: {task.duration}s ===\n")
                    f.write(f"=== Command: {command} ===\n\n")
                    f.write("STDOUT:\n")
                    f.write(stdout_text)
                    f.write("\n\nSTDERR:\n")
                    f.write(stderr_text)

                # 判断结果
                if task.returncode == 0 and not task.error:
                    task.status = "done"
                    logger.info(
                        f"[{task.task_id}] DONE in {task.duration}s -> {task.output_file}"
                    )
                elif task.error:
                    task.status = "failed"
                    logger.error(
                        f"[{task.task_id}] FAILED: {task.error}"
                    )
                else:
                    task.status = "failed"
                    task.error = stderr_text[:200]
                    logger.error(
                        f"[{task.task_id}] FAILED (code={task.returncode})"
                    )

            except Exception as e:
                task.status = "failed"
                task.end_time = time.time()
                task.error = str(e)
                logger.error(f"[{task.task_id}] EXCEPTION: {e}")

    def _summary(self):
        """打印扫描摘要"""
        done = sum(1 for t in self.tasks if t.status == "done")
        failed = sum(1 for t in self.tasks if t.status == "failed")
        total_time = sum(t.duration for t in self.tasks)

        print("\n" + "=" * 70)
        print("SCAN SUMMARY")
        print("=" * 70)
        print(f"Total Tasks : {len(self.tasks)}")
        print(f"Success     : {done}")
        print(f"Failed      : {failed}")
        print(f"CPU Time    : {total_time:.1f}s")
        print(f"Output Dir  : {self.output_dir}")
        print("-" * 70)

        for t in self.tasks:
            icon = "✓" if t.status == "done" else "✗"
            target_preview = t.target[:50] + "..." if len(t.target) > 50 else t.target
            print(
                f"{icon} [{t.task_id}] [{t.status:6s}] {t.duration:5.1f}s  {target_preview}"
            )
            if t.error:
                print(f"       Error: {t.error[:80]}")

        print("=" * 70)

        # 保存 JSON 汇总
        summary = {
            "timestamp": datetime.now().isoformat(),
            "mode": "mr" if any(t.task_id.startswith("mr") for t in self.tasks) else "diff",
            "concurrency": self.concurrency,
            "total": len(self.tasks),
            "success": done,
            "failed": failed,
            "results": [
                {
                    "task_id": t.task_id,
                    "target": t.target,
                    "status": t.status,
                    "duration": t.duration,
                    "output": t.output_file,
                    "error": t.error,
                }
                for t in self.tasks
            ],
        }
        summary_file = self.output_dir / "summary.json"
        summary_file.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"Summary saved: {summary_file}")


def main():
    parser = argparse.ArgumentParser(
        description="并行运行 nga 交互式 session 扫描代码",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # MR 模式
  python orchestrator.py https://github.com/.../pull/100 -c 3

  # Diff 模式（扫描从 abc123 到 HEAD 的所有变更）
  python orchestrator.py --diff abc123 --repo ./app -c 3

  # Diff 模式 + 指定命令模板
  python orchestrator.py --diff abc123 --cmd "review --files {target}"
        """,
    )

    # 两种模式互斥
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "mrs",
        nargs="*",
        default=[],
        help="MR 链接列表（MR 模式）",
    )
    group.add_argument(
        "--diff",
        metavar="COMMIT",
        help="起始 commit hash，扫描从该 commit 到 HEAD 的所有变更（Diff 模式）",
    )

    parser.add_argument(
        "--repo",
        default=".",
        help="Git 仓库路径（Diff 模式用，默认当前目录）",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=10,
        help="Diff 模式下每批文件数（默认: 10）",
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=3,
        help="并发数（默认: 3）",
    )
    parser.add_argument(
        "--nga",
        default="nga",
        help="nga 可执行文件路径（默认: nga）",
    )
    parser.add_argument(
        "--cmd",
        default="review {target}",
        help="发给 nga 的命令模板，{target} 会被替换（默认: 'review {target}'）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="单个任务扫描超时(秒)（默认: 600）",
    )

    args = parser.parse_args()

    # 创建调度器
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        scan_command_template=args.cmd,
        session_timeout=args.timeout,
    )

    # 根据模式初始化任务
    if args.diff:
        # Diff 模式
        orch.setup_diff_mode(
            start_commit=args.diff,
            repo_path=args.repo,
            batch_size=args.batch,
        )
    else:
        # MR 模式
        if not args.mrs:
            parser.error("MR 模式需要提供至少一个 MR 链接")
        orch.setup_mr_mode(mr_links=args.mrs)

    # 信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(orch, "_shutdown", True))

    asyncio.run(orch.run())


if __name__ == "__main__":
    main()
