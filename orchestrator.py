#!/usr/bin/env python3
"""
OpenCode Agent 并行调度器 (Orchestrator)

功能: 为每个 MR 启动独立的 nga 交互式进程，通过 stdin pipe 发送扫描指令，
      并发控制为3，自动分配任务。

用法:
    python orchestrator.py \
        https://github.corp/xx/yy/pull/100 \
        https://github.corp/xx/yy/pull/101 \
        https://github.corp/xx/yy/pull/102 \
        -c 3

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
import signal
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
class MRTask:
    """MR 扫描任务状态"""
    mr_link: str
    session_id: int = 0
    status: str = "pending"  # pending, running, done, failed
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
    - 维护一个 MR 任务队列
    - 用 asyncio.Semaphore 控制同时最多3个 nga 进程
    - 每个 nga 进程通过 stdin pipe 接收扫描命令
    """

    def __init__(
        self,
        mr_links: list[str],
        concurrency: int = 3,
        nga_bin: str = "nga",
        scan_command_template: str = "review {mr_link}",
        session_timeout: int = 600,
    ):
        self.mr_links = mr_links
        self.concurrency = concurrency
        self.nga_bin = nga_bin
        # 命令模板: {mr_link} 会被替换为实际 MR 链接
        self.scan_command_template = scan_command_template
        self.session_timeout = session_timeout

        self.tasks: list[MRTask] = []
        self.semaphore = asyncio.Semaphore(concurrency)
        self._shutdown = False

        # 输出目录
        self.output_dir = Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")

    async def run(self):
        """主入口：调度所有 MR 任务"""
        logger.info(
            f"=== Starting scan: {len(self.mr_links)} MRs, "
            f"concurrency={self.concurrency}, timeout={self.session_timeout}s ==="
        )
        logger.info(f"Command template: '{self.scan_command_template}'")

        # 初始化任务
        for i, link in enumerate(self.mr_links, 1):
            self.tasks.append(
                MRTask(
                    mr_link=link,
                    session_id=i,
                    output_file=str(self.output_dir / f"mr_{i:02d}.log"),
                )
            )

        # 创建并发任务
        coros = [self._scan_one(task) for task in self.tasks]
        await asyncio.gather(*coros, return_exceptions=True)

        # 汇总
        self._summary()

    async def _scan_one(self, task: MRTask):
        """扫描单个 MR：获取信号量槽位 -> 启动 nga -> stdin 发命令 -> 等待完成"""
        async with self.semaphore:
            if self._shutdown:
                logger.warning(f"[#{task.session_id}] Skipped (shutdown)")
                return

            task.status = "running"
            task.start_time = time.time()

            # 构造要发送给 nga 的命令
            command = self.scan_command_template.format(mr_link=task.mr_link)

            logger.info(f"[#{task.session_id}] START {task.mr_link}")
            logger.info(f"[#{task.session_id}] Command: {command}")

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
                        f"[#{task.session_id}] Timeout after {self.session_timeout}s, killing..."
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
                    f.write(f"=== MR: {task.mr_link} ===\n")
                    f.write(f"=== Session: #{task.session_id} ===\n")
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
                        f"[#{task.session_id}] DONE in {task.duration}s -> {task.output_file}"
                    )
                elif task.error:
                    task.status = "failed"
                    logger.error(
                        f"[#{task.session_id}] FAILED: {task.error} | {task.mr_link}"
                    )
                else:
                    task.status = "failed"
                    task.error = stderr_text[:200]
                    logger.error(
                        f"[#{task.session_id}] FAILED (code={task.returncode}) {task.mr_link}"
                    )

            except Exception as e:
                task.status = "failed"
                task.end_time = time.time()
                task.error = str(e)
                logger.error(f"[#{task.session_id}] EXCEPTION: {e}")

    def _summary(self):
        """打印扫描摘要"""
        done = sum(1 for t in self.tasks if t.status == "done")
        failed = sum(1 for t in self.tasks if t.status == "failed")
        total_time = sum(t.duration for t in self.tasks)

        print("\n" + "=" * 70)
        print("SCAN SUMMARY")
        print("=" * 70)
        print(f"Total MRs  : {len(self.tasks)}")
        print(f"Success    : {done}")
        print(f"Failed     : {failed}")
        print(f"CPU Time   : {total_time:.1f}s")
        print(f"Output Dir : {self.output_dir}")
        print("-" * 70)

        for t in self.tasks:
            icon = "✓" if t.status == "done" else "✗"
            print(
                f"{icon} #{t.session_id:02d} [{t.status:6s}] {t.duration:5.1f}s  {t.mr_link}"
            )
            if t.error:
                print(f"       Error: {t.error[:80]}")

        print("=" * 70)

        # 保存 JSON 汇总
        summary = {
            "timestamp": datetime.now().isoformat(),
            "concurrency": self.concurrency,
            "total": len(self.tasks),
            "success": done,
            "failed": failed,
            "results": [
                {
                    "mr": t.mr_link,
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
        description="并行运行 nga 交互式 session 扫描 MR"
    )
    parser.add_argument("mrs", nargs="+", help="MR 链接列表")
    parser.add_argument(
        "-c", "--concurrency", type=int, default=3, help="并发数 (默认: 3)"
    )
    parser.add_argument(
        "--nga", default="nga", help="nga 可执行文件路径 (默认: nga)"
    )
    parser.add_argument(
        "--cmd",
        default="review {mr_link}",
        help="发给 nga 的命令模板，{mr_link} 会被替换 (默认: 'review {mr_link}')",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="单个 MR 扫描超时(秒) (默认: 600)",
    )

    args = parser.parse_args()

    orch = OpenCodeOrchestrator(
        mr_links=args.mrs,
        concurrency=args.concurrency,
        nga_bin=args.nga,
        scan_command_template=args.cmd,
        session_timeout=args.timeout,
    )

    # 信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(orch, "_shutdown", True))

    asyncio.run(orch.run())


if __name__ == "__main__":
    main()
