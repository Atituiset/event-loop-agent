"""CLI entry point for the OpenCode Agent."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from opencode_agent.scanner.orchestrator import OpenCodeOrchestrator
from opencode_agent.skills.config_loader import ConfigLoader
from opencode_agent.skills.registry import SkillRegistry
from opencode_agent.utils.logging import get_logger

logger = get_logger("CLI")


def build_combined_prompt(registry: SkillRegistry, config: ConfigLoader) -> str:
    """Build combined prompt from enabled skills and rules."""
    available_skills = list(registry.skills.keys())
    enabled_skills = config.list_enabled_skills(available_skills)

    enabled_rules_by_skill: dict[str, list[str]] = {}
    for skill_id in enabled_skills:
        skill = registry.skills.get(skill_id)
        if skill:
            all_rules = [r.local_id for r in skill.rules]
            enabled_rules = config.get_enabled_rules(skill_id, all_rules)
            enabled_rules_by_skill[skill_id] = enabled_rules

    return registry.build_combined_prompt(enabled_skills, enabled_rules_by_skill)


def main() -> int:
    """Main CLI entry point."""
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

    # Setup config and skills
    project_dir = Path(args.repo) if args.repo else Path.cwd()
    config_loader = ConfigLoader(project_dir=project_dir)
    skill_registry = SkillRegistry(project_dir=project_dir)

    combined_prompt = build_combined_prompt(skill_registry, config_loader)
    if combined_prompt:
        logger.debug(f"Combined prompt length: {len(combined_prompt)} chars")

    # Create orchestrator
    orch = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga,
        session_timeout=args.timeout,
        debug=args.debug,
        web_port=args.web_port,
        workspace=args.workspace,
        output_json=args.output_json,
    )

    # Store combined prompt on orchestrator for use during scan
    orch.combined_prompt = combined_prompt

    # Parse cared_paths
    cared_paths = None
    if args.paths:
        cared_paths = [p.strip().rstrip("/") for p in args.paths.split(",")]
        logger.info(f"Cared paths: {cared_paths}")

    # Initialize tasks
    if args.diff:
        orch.setup_diff_mode(start_commit=args.diff, repo_path=args.repo, cared_paths=cared_paths)
    elif args.full:
        orch.setup_full_mode(file_paths=args.full, cared_paths=cared_paths)
    else:
        orch.setup_file_mode(file_paths=args.files, cared_paths=cared_paths)

    if not orch.tasks:
        logger.error("No files to scan. Exiting.")
        return 1

    # Signal handling
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(orch, "_shutdown", True))

    asyncio.run(orch.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
