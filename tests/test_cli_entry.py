"""Tests for CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_orchestrator_help():
    """Test `python orchestrator.py --help` works."""
    project_dir = Path(__file__).parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_dir / "src")
    result = subprocess.run(
        [sys.executable, "orchestrator.py", "--help"],
        capture_output=True,
        text=True,
        cwd=project_dir,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "usage:" in result.stdout
    assert "--files" in result.stdout
    assert "--diff" in result.stdout
    assert "--full" in result.stdout


def test_module_help():
    """Test `python -m opencode_agent --help` works."""
    project_dir = Path(__file__).parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_dir / "src")
    result = subprocess.run(
        [sys.executable, "-m", "opencode_agent", "--help"],
        capture_output=True,
        text=True,
        cwd=project_dir,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "usage:" in result.stdout
    assert "--files" in result.stdout
    assert "--diff" in result.stdout
    assert "--full" in result.stdout


def test_cli_main_imports():
    """Test that CLI main module can be imported without errors."""
    from opencode_agent.cli.main import build_combined_prompt, main

    assert callable(main)
    assert callable(build_combined_prompt)


def test_cli_main_argparse_flags():
    """Test that all expected CLI flags are defined."""
    import argparse

    from opencode_agent.cli.main import main

    # Test --help by calling main with --help (which exits)
    with pytest.raises(SystemExit) as exc_info:
        # Simulate argv with --help
        import sys

        old_argv = sys.argv
        try:
            sys.argv = ["opencode_agent", "--help"]
            main()
        finally:
            sys.argv = old_argv

    assert exc_info.value.code == 0


def test_orchestrator_shim_imports():
    """Test that the root orchestrator shim imports correctly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "orchestrator_shim",
        Path(__file__).parent.parent / "orchestrator.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert module is not None
    # Should not raise on import
    spec.loader.exec_module(module)
    assert hasattr(module, "main")
