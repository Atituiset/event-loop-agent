#!/usr/bin/env python3
"""Backward-compatible shim: delegates to the package CLI entry point."""

from opencode_agent.cli.main import main

if __name__ == "__main__":
    main()
