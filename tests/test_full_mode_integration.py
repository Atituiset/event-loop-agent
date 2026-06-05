"""Integration test: verify --full mode task creation with real codebase."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import OpenCodeOrchestrator

TEST_DIR = Path("/home/atituiset/Projects/opencode-c-cpp-test")


def test_full_mode_task_creation():
    """验证 Full 模式正确创建函数级任务"""
    orch = OpenCodeOrchestrator(concurrency=1)
    orch.setup_full_mode([str(TEST_DIR / "utils" / "logger.c")])

    assert len(orch.tasks) == 3, f"Expected 3 tasks, got {len(orch.tasks)}"

    names = [t.function_name for t in orch.tasks]
    assert "logger_init" in names
    assert "log_write" in names
    assert "logger_close" in names

    # 验证输出路径包含函数目录
    for t in orch.tasks:
        assert t.function_name in str(t.report_file)
        assert t.function_name in str(t.log_file)
        assert ".c/" in str(t.report_file)  # 文件扩展名作为目录的一部分

    print("✅ Full mode integration test passed")


def test_full_mode_with_directory():
    """验证 Full 模式对目录递归扫描"""
    orch = OpenCodeOrchestrator(concurrency=1)
    orch.setup_full_mode([str(TEST_DIR)])

    # Should find multiple files with functions
    assert len(orch.tasks) > 0

    # All tasks should have function names
    for t in orch.tasks:
        assert t.function_name

    # Verify path structure
    for t in orch.tasks:
        assert ".c/" in str(t.report_file) or ".cpp/" in str(t.report_file)

    print(f"✅ Full mode directory scan: {len(orch.tasks)} function tasks created")


def test_cli_help_shows_full():
    """验证 CLI help 包含 --full"""
    import subprocess
    result = subprocess.run(
        ["uv", "run", "python", "orchestrator.py", "--help"],
        capture_output=True, text=True
    )
    assert "--full" in result.stdout
    assert "Full 扫描模式" in result.stdout
    print("✅ CLI help contains --full flag")


if __name__ == "__main__":
    test_full_mode_task_creation()
    test_full_mode_with_directory()
    test_cli_help_shows_full()
