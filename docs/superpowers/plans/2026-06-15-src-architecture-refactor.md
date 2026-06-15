# src/ Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate root-level Python modules into a structured `src/opencode_agent/` package with clear layer boundaries while preserving the existing CLI and behavior.

**Architecture:** Split monolithic files by responsibility (`core`, `scanner`, `skills`, `findings`, `web`, `utils`, `cli`), inject dependencies from `cli/main.py`, and keep a thin root `orchestrator.py` shim for backward compatibility.

**Tech Stack:** Python 3.12, pytest, PyYAML, tree-sitter, FastAPI, setuptools src-layout.

---

## File Migration Map

| Source (root) | Destination (src/opencode_agent/) | Notes |
|---|---|---|
| `finding_parser.py` | `findings/parser.py` | Dataclass + Markdown parsing |
| `finding_store.py` | `findings/store.py` | SQLite persistence |
| `function_splitter.py` | `scanner/splitter.py` | Tree-sitter function extraction |
| `orchestrator.py` | `scanner/orchestrator.py`, `scanner/reporter.py`, `scanner/slot.py`, `cli/main.py` | Split scheduling, reporting, slots, CLI |
| `skill_loader.py` | `skills/loader.py`, `skills/registry.py` | Parse skills + registry/prompt building |
| `config_loader.py` | `skills/config_loader.py` | Global/project config merge |
| `web_server.py` | `web/server.py` | FastAPI debug server |
| `benchmark_cache.py` | `scripts/benchmark_cache.py` | One-off benchmark script |
| `skills/*.yaml` | `skills/*.yaml` | Built-in skill YAML, use importlib.resources |
| `knowleage/wireless-radio.md` | merged into `skills/wireless-scan.yaml` | Delete `knowleage/` |

---

## Task 1: Create Package Skeleton

**Files:**
- Create: `src/opencode_agent/__init__.py`
- Create: `src/opencode_agent/__main__.py`
- Create: `src/opencode_agent/cli/__init__.py`
- Create: `src/opencode_agent/core/__init__.py`
- Create: `src/opencode_agent/findings/__init__.py`
- Create: `src/opencode_agent/scanner/__init__.py`
- Create: `src/opencode_agent/skills/__init__.py`
- Create: `src/opencode_agent/utils/__init__.py`
- Create: `src/opencode_agent/web/__init__.py`
- Create: `scripts/__init__.py` (optional, keeps scripts importable)

- [ ] **Step 1: Create directories and empty `__init__.py` files**

```bash
mkdir -p src/opencode_agent/{cli,core,findings,scanner,skills,utils,web} scripts
touch src/opencode_agent/__init__.py
touch src/opencode_agent/__main__.py
touch src/opencode_agent/{cli,core,findings,scanner,skills,utils,web}/__init__.py
```

- [ ] **Step 2: Add `__main__.py` shim**

```python
# src/opencode_agent/__main__.py
from opencode_agent.cli.main import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify directory structure**

Run: `find src/opencode_agent -type f | sort`
Expected output includes all `__init__.py` files above.

- [ ] **Step 4: Commit**

```bash
git add src/ scripts/
git commit -m "chore: create src/opencode_agent package skeleton

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Migrate Utility Modules

**Files:**
- Create: `src/opencode_agent/utils/ansi.py`
- Create: `src/opencode_agent/utils/logging.py`
- Modify: `src/opencode_agent/scanner/orchestrator.py` (later tasks will import from here)
- Test: `tests/test_utils.py` (new, small)

- [ ] **Step 1: Create `utils/ansi.py`**

```python
# src/opencode_agent/utils/ansi.py
"""ANSI escape sequence filtering utilities."""

import re

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_ESCAPE.sub("", text)
```

- [ ] **Step 2: Create `utils/logging.py`**

```python
# src/opencode_agent/utils/logging.py
"""Shared logging configuration for the agent."""

import logging
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the standard formatter."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(console_handler)

    return logger
```

- [ ] **Step 3: Write test for ANSI stripping**

```python
# tests/test_utils.py
from opencode_agent.utils.ansi import strip_ansi


def test_strip_ansi_removes_color_codes():
    text = "\x1b[31mhello\x1b[0m"
    assert strip_ansi(text) == "hello"
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_utils.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencode_agent/utils/ tests/test_utils.py
git commit -m "feat: migrate utility modules into utils/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Migrate Core Models and Config

**Files:**
- Create: `src/opencode_agent/core/models.py`
- Create: `src/opencode_agent/core/config.py`
- Test: `tests/test_core_models.py` (new)
- Test: `tests/test_config_loader.py` (update imports)

- [ ] **Step 1: Create `core/models.py`**

Move `ScanTask` and `ProgressTracker` dataclasses from `orchestrator.py`.

```python
# src/opencode_agent/core/models.py
"""Core domain models for the OpenCode agent."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class ScanTask:
    """Single file/function scanning task."""

    file_path: str
    task_id: str
    report_file: str
    log_file: str
    function_name: str = ""
    status: str = "pending"
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    returncode: Optional[int] = None
    diff_content: str = ""
    diff_file: str = ""
    slot_id: Optional[int] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return round(self.end_time - self.start_time, 1)
        return 0.0


class ProgressTracker:
    """Terminal progress tracker."""

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
        elapsed = time.time() - self.start_time
        print(
            f"Finished: {self.completed}/{self.total} files | "
            f"Success: {self.completed - self.failed} | Failed: {self.failed} | "
            f"Total time: {elapsed:.1f}s"
        )

    def _print_progress(self):
        elapsed = time.time() - self.start_time
        pct = self.completed / self.total * 100 if self.total > 0 else 0
        print(
            f"Progress: {self.completed}/{self.total} ({pct:.0f}%) | "
            f"Running: {self.running} | Failed: {self.failed} | "
            f"Elapsed: {elapsed:.0f}s"
        )
```

Note: In later tasks, `ProgressTracker` will use the shared logger instead of `print`. For now, keep it simple and independent.

- [ ] **Step 2: Create `core/config.py`**

Move dataclasses from `config_loader.py`.

```python
# src/opencode_agent/core/config.py
"""Configuration dataclasses for skills and agent behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SkillConfig:
    """Per-skill configuration."""

    enabled: bool = True
    rules: dict[str, bool] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Merged agent configuration."""

    version: str = "1.0"
    skills: dict[str, SkillConfig] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)
```

- [ ] **Step 3: Write test for ScanTask duration**

```python
# tests/test_core_models.py
from opencode_agent.core.models import ScanTask


def test_scan_task_duration():
    task = ScanTask(file_path="a.c", task_id="t1", report_file="a.md", log_file="a.log")
    task.start_time = 10.0
    task.end_time = 12.5
    assert task.duration == 2.5
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_core_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencode_agent/core/ tests/test_core_models.py
git commit -m "feat: migrate core models into core/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Migrate Findings Parser and Store

**Files:**
- Create: `src/opencode_agent/findings/parser.py`
- Create: `src/opencode_agent/findings/store.py`
- Modify: `tests/test_finding_parser.py` (update imports)
- Modify: `tests/test_finding_store.py` (update imports)
- Modify: `tests/test_flywheel_integration.py` (update imports)

- [ ] **Step 1: Copy `finding_parser.py` to `src/opencode_agent/findings/parser.py`**

Keep all functions exactly as they are, only update imports at the top:

```python
# src/opencode_agent/findings/parser.py
"""Finding extraction from nga Markdown review reports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
```

- [ ] **Step 2: Copy `finding_store.py` to `src/opencode_agent/findings/store.py`**

Update import:

```python
# src/opencode_agent/findings/store.py
"""SQLite persistence for findings and user feedback."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from opencode_agent.findings.parser import Finding
```

- [ ] **Step 3: Update test imports**

In `tests/test_finding_parser.py`:

```python
from opencode_agent.findings.parser import (
    Finding,
    generate_finding_id,
    normalize_snippet,
    parse_findings_from_markdown,
)
```

In `tests/test_finding_store.py`:

```python
from opencode_agent.findings.parser import Finding
from opencode_agent.findings.store import FindingStore
```

In `tests/test_flywheel_integration.py`:

```python
from opencode_agent.findings.parser import Finding, parse_findings_from_markdown
from opencode_agent.findings.store import FindingStore
```

- [ ] **Step 4: Run finding tests**

Run: `pytest tests/test_finding_parser.py tests/test_finding_store.py tests/test_flywheel_integration.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencode_agent/findings/ tests/test_finding_parser.py tests/test_finding_store.py tests/test_flywheel_integration.py
git commit -m "feat: migrate findings parser and store into findings/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Migrate Skills Loader, Registry, and Config Loader

**Files:**
- Create: `src/opencode_agent/skills/loader.py`
- Create: `src/opencode_agent/skills/registry.py`
- Create: `src/opencode_agent/skills/config_loader.py`
- Move: `skills/wireless-scan.yaml` → `src/opencode_agent/skills/wireless-scan.yaml`
- Modify: `tests/test_skill_loader.py` (update imports)
- Modify: `tests/test_config_loader.py` (update imports)

- [ ] **Step 1: Create `skills/loader.py`**

Extract YAML parsing dataclasses and `_load_skill` from `skill_loader.py`.

```python
# src/opencode_agent/skills/loader.py
"""YAML skill parsing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("SkillLoader")


@dataclass
class Rule:
    """A single rule within a skill."""

    local_id: str
    namespace: str
    global_id: str
    name: str
    severity: str
    description: str
    check_points: list[str] = field(default_factory=list)
    examples: dict[str, str] = field(default_factory=dict)


@dataclass
class Skill:
    """A code review skill definition."""

    id: str
    name: str
    description: str
    version: str
    skill_type: str
    language: str
    tags: list[str]
    agent_role: str
    agent_expertise: list[str]
    workflow: list[str]
    rules: list[Rule]
    output_format: dict
    exclusions: list[str]
    source_path: Path


def load_skill_from_path(path: Path) -> Skill:
    """Parse a single skill YAML file into a Skill dataclass."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    skill_data = data.get("skill", data) if isinstance(data, dict) else {}

    required = ["id", "name", "description", "version", "rules"]
    for field_name in required:
        if field_name not in skill_data:
            raise ValueError(f"Skill {path} missing required field: {field_name}")

    agent_data = skill_data.get("agent", {}) or {}
    output_format = skill_data.get("output_format", {}) or {}

    rules = []
    seen_local_ids: set[str] = set()
    for rule_data in skill_data.get("rules", []):
        local_id = rule_data["id"]
        if local_id in seen_local_ids:
            raise ValueError(f"Duplicate rule id '{local_id}' in skill {path}")
        seen_local_ids.add(local_id)

        namespace = skill_data["id"]
        global_id = f"{namespace}:{local_id}"
        examples = rule_data.get("examples", {}) or {}
        rules.append(
            Rule(
                local_id=local_id,
                namespace=namespace,
                global_id=global_id,
                name=rule_data.get("name", local_id),
                severity=rule_data.get("severity", "MEDIUM"),
                description=rule_data.get("description", ""),
                check_points=rule_data.get("check_points", []) or [],
                examples=examples if isinstance(examples, dict) else {},
            )
        )

    return Skill(
        id=skill_data["id"],
        name=skill_data["name"],
        description=skill_data["description"],
        version=str(skill_data["version"]),
        skill_type=skill_data.get("type", "security-audit"),
        language=skill_data.get("language", "zh-CN"),
        tags=skill_data.get("tags", []) or [],
        agent_role=agent_data.get("role", ""),
        agent_expertise=agent_data.get("expertise", []) or [],
        workflow=skill_data.get("workflow", []) or [],
        rules=rules,
        output_format=output_format,
        exclusions=skill_data.get("exclusions", []) or [],
        source_path=path,
    )
```

- [ ] **Step 2: Create `skills/registry.py`**

Extract `SkillRegistry` from `skill_loader.py`, using `importlib.resources` for built-in skills.

```python
# src/opencode_agent/skills/registry.py
"""Skill registry with built-in and project-specific skill discovery."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from importlib import resources

from opencode_agent.skills.loader import Rule, Skill, load_skill_from_path

logger = logging.getLogger("SkillRegistry")


class SkillRegistry:
    """Registry of all loaded skills with namespaced rule lookup."""

    def __init__(self, project_dir: Optional[Path] = None):
        self.skills: dict[str, Skill] = {}
        self.project_dir = project_dir
        self._load_all()

    def _load_all(self) -> None:
        # Built-in skills from package resources
        try:
            builtin_root = resources.files("opencode_agent.skills")
            for yaml_file in sorted(builtin_root.glob("*.yaml")):
                try:
                    skill = load_skill_from_path(Path(str(yaml_file)))
                    self.skills[skill.id] = skill
                    logger.debug(f"Loaded builtin skill: {skill.id}")
                except Exception as e:
                    logger.warning(f"Failed to load builtin skill {yaml_file}: {e}")
        except Exception as e:
            logger.warning(f"Failed to load builtin skills: {e}")

        # Project-specific skills
        if self.project_dir:
            project_skills_dir = self.project_dir / ".opencode" / "skills"
            if project_skills_dir.exists():
                for yaml_file in sorted(project_skills_dir.glob("*.yaml")):
                    try:
                        skill = load_skill_from_path(yaml_file)
                        self.skills[skill.id] = skill
                        logger.debug(f"Loaded project skill: {skill.id}")
                    except Exception as e:
                        logger.warning(f"Failed to load project skill {yaml_file}: {e}")

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        return self.skills.get(skill_id)

    def get_rule(self, global_id: str) -> Optional[Rule]:
        if ":" not in global_id:
            return None
        namespace, local_id = global_id.split(":", 1)
        skill = self.skills.get(namespace)
        if not skill:
            return None
        for rule in skill.rules:
            if rule.local_id == local_id:
                return rule
        return None

    def list_rules(self, skill_id: Optional[str] = None) -> list[Rule]:
        if skill_id:
            skill = self.skills.get(skill_id)
            return skill.rules if skill else []
        return [r for s in self.skills.values() for r in s.rules]

    def build_prompt_section(self, skill_id: str, enabled_rules: Optional[list[str]] = None) -> str:
        skill = self.skills.get(skill_id)
        if not skill:
            return ""

        rules_to_include = skill.rules
        if enabled_rules:
            enabled_set = set(enabled_rules)
            rules_to_include = [r for r in skill.rules if r.local_id in enabled_set]

        if not rules_to_include:
            return ""

        lines: list[str] = []
        lines.append(f"## {skill.name}")
        if skill.agent_role:
            lines.append(f"**角色**: {skill.agent_role}")

        if skill.agent_expertise:
            lines.append("")
            lines.append("**专长领域**:")
            for exp in skill.agent_expertise:
                lines.append(f"- {exp}")

        if skill.workflow:
            lines.append("")
            lines.append("**工作流程**:")
            for idx, step in enumerate(skill.workflow, 1):
                lines.append(f"{idx}. {step}")

        lines.append("")
        lines.append("### 扫描规则")
        for rule in rules_to_include:
            lines.append(f"#### {rule.global_id}: {rule.name} [{rule.severity}]")
            lines.append(rule.description)
            if rule.check_points:
                lines.append("")
                lines.append("**检查点**:")
                for cp in rule.check_points:
                    lines.append(f"- {cp}")
            if rule.examples.get("bad"):
                lines.append("")
                lines.append("**错误示例**:")
                lines.append("```c")
                lines.append(rule.examples["bad"].strip())
                lines.append("```")
            if rule.examples.get("good"):
                lines.append("")
                lines.append("**正确示例**:")
                lines.append("```c")
                lines.append(rule.examples["good"].strip())
                lines.append("```")
            lines.append("")

        return "\n".join(lines)

    def build_combined_prompt(
        self,
        skill_ids: list[str],
        enabled_rules_by_skill: Optional[dict[str, list[str]]] = None,
    ) -> str:
        enabled_rules_by_skill = enabled_rules_by_skill or {}
        sections: list[str] = []
        for skill_id in skill_ids:
            enabled_rules = enabled_rules_by_skill.get(skill_id)
            section = self.build_prompt_section(skill_id, enabled_rules)
            if section:
                sections.append(section)
        return "\n\n".join(sections)

    def list_enabled_global_ids(
        self,
        skill_ids: list[str],
        enabled_rules_by_skill: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        enabled_rules_by_skill = enabled_rules_by_skill or {}
        result: list[str] = []
        for skill_id in skill_ids:
            skill = self.skills.get(skill_id)
            if not skill:
                continue
            enabled_rules = enabled_rules_by_skill.get(skill_id)
            if enabled_rules:
                enabled_set = set(enabled_rules)
                result.extend(r.global_id for r in skill.rules if r.local_id in enabled_set)
            else:
                result.extend(r.global_id for r in skill.rules)
        return result
```

- [ ] **Step 3: Create `skills/config_loader.py`**

Copy `config_loader.py` content, update import to use `opencode_agent.core.config`.

```python
# src/opencode_agent/skills/config_loader.py
"""Global and project-specific YAML configuration loader."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from opencode_agent.core.config import AgentConfig, SkillConfig

logger = logging.getLogger("ConfigLoader")
```

Then paste the rest of the existing `ConfigLoader` class, keeping the same methods.

- [ ] **Step 4: Move built-in skill YAML**

```bash
mv skills/wireless-scan.yaml src/opencode_agent/skills/wireless-scan.yaml
```

Leave `skills/wireless-scan.claude.md` and `skills/wireless-scan.mcp.json` in root `skills/` for now, or move them if they are documentation/tool definitions. For this plan, only move the YAML skill definition.

- [ ] **Step 5: Update test imports**

In `tests/test_skill_loader.py`:

```python
from opencode_agent.skills.loader import Rule, Skill, load_skill_from_path
from opencode_agent.skills.registry import SkillRegistry
```

In `tests/test_config_loader.py`:

```python
from opencode_agent.core.config import AgentConfig, SkillConfig
from opencode_agent.skills.config_loader import ConfigLoader
```

- [ ] **Step 6: Run skill and config tests**

Run: `pytest tests/test_skill_loader.py tests/test_config_loader.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencode_agent/skills/ tests/test_skill_loader.py tests/test_config_loader.py
git rm skills/wireless-scan.yaml
git commit -m "feat: migrate skills loader, registry, and config into skills/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Migrate Scanner Components

**Files:**
- Create: `src/opencode_agent/scanner/splitter.py`
- Create: `src/opencode_agent/scanner/slot.py`
- Create: `src/opencode_agent/scanner/reporter.py`
- Create: `src/opencode_agent/scanner/orchestrator.py`
- Modify: `tests/test_function_splitter.py` (update imports)
- Modify: `tests/test_full_mode_integration.py` (update imports)

- [ ] **Step 1: Create `scanner/splitter.py`**

Copy `function_splitter.py` content, update imports at the top only.

```python
# src/opencode_agent/scanner/splitter.py
"""Tree-sitter based C/C++ function extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
```

Keep all existing functions (`FunctionInfo`, `extract_functions`, etc.).

- [ ] **Step 2: Create `scanner/slot.py`**

Extract `SlotManager` from `orchestrator.py`.

```python
# src/opencode_agent/scanner/slot.py
"""Explicit slot allocation for concurrent nga processes."""

from __future__ import annotations

import asyncio
from typing import Optional


class SlotManager:
    """Assigns fixed-numbered slots to concurrent nga tasks."""

    def __init__(self, num_slots: int = 3):
        self.num_slots = num_slots
        self.slots: list[Optional[dict]] = [None] * num_slots
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._event.set()

    async def acquire(self, task_id: str, file_path: str) -> int:
        while True:
            async with self._lock:
                for i in range(self.num_slots):
                    if self.slots[i] is None:
                        self.slots[i] = {"task_id": task_id, "file_path": file_path}
                        if all(self.slots):
                            self._event.clear()
                        return i
            await self._event.wait()

    async def release(self, slot_id: int):
        async with self._lock:
            self.slots[slot_id] = None
            self._event.set()
```

- [ ] **Step 3: Create `scanner/reporter.py`**

Extract report generation functions from `orchestrator.py`.

```python
# src/opencode_agent/scanner/reporter.py
"""Markdown report, log, and summary generation."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from opencode_agent.core.models import ScanTask


def generate_report(task: ScanTask) -> str:
    """Generate Markdown review report for a single task."""
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
        lines.append(task.stdout)
    else:
        lines.append("*无审查结果*")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by OpenCode Orchestrator*")

    return "\n".join(lines)


def generate_log(task: ScanTask) -> str:
    """Generate runtime log for a single task."""
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
    """Generate Markdown summary report."""
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
            report_link = f"[{report_name}]({Path(t.report_file).relative_to(output_dir)})"
            log_link = f"[{log_name}]({Path(t.log_file).relative_to(output_dir)})"
            lines.append(
                f"| {i} | `{func_name}` | {status_icon} {t.status} | {t.duration}s | {report_link} | {log_link} |"
            )

        lines.append("")

    lines.append("---")
    lines.append("*Generated by OpenCode Orchestrator*")

    return "\n".join(lines)
```

- [ ] **Step 4: Create `scanner/orchestrator.py`**

This is the largest migration. Copy `orchestrator.py` content and update imports:

```python
# src/opencode_agent/scanner/orchestrator.py
"""OpenCode Agent parallel orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from opencode_agent.core.models import ProgressTracker, ScanTask
from opencode_agent.findings.parser import parse_findings_from_markdown
from opencode_agent.findings.store import FindingStore
from opencode_agent.scanner.reporter import generate_log, generate_report, generate_summary
from opencode_agent.scanner.slot import SlotManager
from opencode_agent.scanner.splitter import extract_functions
from opencode_agent.utils.ansi import strip_ansi
from opencode_agent.utils.logging import get_logger

logger = get_logger("Orchestrator")
```

Remove `ScanTask`, `ProgressTracker`, `SlotManager`, `generate_report`, `generate_log`, `generate_summary`, `ANSI_ESCAPE`, and the inline logger setup from this file. They now live in other modules.

Replace `ANSI_ESCAPE.sub("", text)` calls with `strip_ansi(text)`.

Replace inline logger setup with `logger = get_logger("Orchestrator")`.

Note: The `argparse` section and `if __name__ == "__main__":` block will be removed in Task 8 and moved to `cli/main.py`. For now, keep them so existing tests still pass.

Also update the public orchestrator interface so `scan_diff` and `scan_files` accept optional `prompt` and `enabled_rules` parameters and forward them to the command builders:

```python
async def scan_diff(
    self,
    start_commit: str,
    repo_path: str,
    cared_paths: Optional[str] = None,
    prompt: str = "",
    enabled_rules: Optional[list[str]] = None,
):
    ...
    cmd = self._build_diff_scan_cmd(file_path, diff_content, prompt=prompt, enabled_rules=enabled_rules)
    ...

async def scan_files(
    self,
    paths: list[str],
    prompt: str = "",
    enabled_rules: Optional[list[str]] = None,
):
    ...
    cmd = self._build_full_scan_cmd(file_path, function_name, prompt=prompt, enabled_rules=enabled_rules)
    ...
```

Then modify `_build_full_scan_cmd` and `_build_diff_scan_cmd` to append the provided `prompt` to the message sent to `nga`, and include `enabled_rules` in the system instruction if given. The existing hard-coded RULE-001~RULE-010 block can remain as fallback when `prompt` is empty, but should be removed once skill-based prompt generation is verified end-to-end.

- [ ] **Step 5: Update test imports**

In `tests/test_function_splitter.py`:

```python
from opencode_agent.scanner.splitter import extract_functions
```

In `tests/test_full_mode_integration.py`:

```python
from opencode_agent.scanner.orchestrator import OpenCodeOrchestrator
```

- [ ] **Step 6: Run scanner tests**

Run: `pytest tests/test_function_splitter.py tests/test_full_mode_integration.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencode_agent/scanner/ tests/test_function_splitter.py tests/test_full_mode_integration.py
git commit -m "feat: migrate scanner components into scanner/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Migrate Web Server

**Files:**
- Create: `src/opencode_agent/web/server.py`
- Modify: `tests/test_web_server_api.py` (update imports)

- [ ] **Step 1: Create `web/server.py`**

Copy `web_server.py` content, update import:

```python
# src/opencode_agent/web/server.py
"""OpenCode Orchestrator Web Debug Server."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from opencode_agent.findings.parser import Finding
from opencode_agent.findings.store import FindingStore
```

Keep all existing endpoints and helper functions.

- [ ] **Step 2: Update test imports**

In `tests/test_web_server_api.py`:

```python
from opencode_agent.web.server import app, _get_finding_store
```

- [ ] **Step 3: Run web server tests**

Run: `pytest tests/test_web_server_api.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencode_agent/web/ tests/test_web_server_api.py
git commit -m "feat: migrate web debug server into web/

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Create CLI Entry Point

**Files:**
- Create: `src/opencode_agent/cli/main.py`
- Modify: `orchestrator.py` (replace with shim)
- Test: `tests/test_cli_entry.py` (new)

- [ ] **Step 1: Create `cli/main.py`**

Move argparse and main execution logic from `scanner/orchestrator.py`.

```python
# src/opencode_agent/cli/main.py
"""Command-line entry point for OpenCode Agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from opencode_agent.scanner.orchestrator import OpenCodeOrchestrator
from opencode_agent.skills.config_loader import ConfigLoader
from opencode_agent.skills.registry import SkillRegistry
from opencode_agent.utils.logging import get_logger

logger = get_logger("CLI")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenCode Agent")
    parser.add_argument("--diff", type=str, help="Diff mode: start commit hash")
    parser.add_argument("--files", nargs="+", help="File list mode")
    parser.add_argument("--repo", type=str, default=".", help="Repository path")
    parser.add_argument("--paths", type=str, help="Comma-separated paths to care about")
    parser.add_argument("-c", "--concurrency", type=int, default=3)
    parser.add_argument("--nga-bin", type=str, default="nga")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--workspace", type=str, default="")
    parser.add_argument("--output-json", action="store_true")
    parser.add_argument("--skills-config", type=str, help="Path to skills config YAML")
    parser.add_argument("--skills", type=str, help="Comma-separated skill IDs to enable")
    parser.add_argument("--rules", type=str, help="Comma-separated global rule IDs to enable")
    parser.add_argument("--list-skills", action="store_true", help="List all skills and rules")
    parser.add_argument("--list-active", action="store_true", help="List active skills and rules")
    return parser


def _enabled_skills_and_rules(registry: SkillRegistry, config: ConfigLoader) -> tuple[list[str], dict[str, list[str]]]:
    """Determine enabled skills and rules based on config."""
    available = sorted(registry.skills.keys())
    enabled_skill_ids = config.list_enabled_skills(available)
    enabled_rules_by_skill: dict[str, list[str]] = {}
    for skill_id in enabled_skill_ids:
        skill = registry.skills[skill_id]
        all_rule_ids = [r.local_id for r in skill.rules]
        enabled_rules_by_skill[skill_id] = config.get_enabled_rules(skill_id, all_rule_ids)
    return enabled_skill_ids, enabled_rules_by_skill


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    project_dir = Path(args.repo).resolve()
    config = ConfigLoader(project_dir=project_dir)
    registry = SkillRegistry(project_dir=project_dir)

    if args.list_skills:
        for skill_id in sorted(registry.skills.keys()):
            skill = registry.skills[skill_id]
            print(f"{skill_id}: {skill.name}")
            for rule in skill.rules:
                print(f"  {rule.global_id}: {rule.name}")
        return 0

    if args.list_active:
        enabled_skill_ids, enabled_rules_by_skill = _enabled_skills_and_rules(registry, config)
        for skill_id in enabled_skill_ids:
            print(f"{skill_id}: {registry.skills[skill_id].name}")
            for rid in enabled_rules_by_skill.get(skill_id, []):
                print(f"  {skill_id}:{rid}")
        return 0

    # CLI overrides for --skills and --rules
    if args.skills:
        enabled_skill_ids = [s.strip() for s in args.skills.split(",")]
        enabled_rules_by_skill = {sid: [r.local_id for r in registry.skills[sid].rules] for sid in enabled_skill_ids}
    else:
        enabled_skill_ids, enabled_rules_by_skill = _enabled_skills_and_rules(registry, config)

    if args.rules:
        explicit_rules = {s.strip() for s in args.rules.split(",")}
        # Narrow down to only skills/rules mentioned
        new_enabled: dict[str, list[str]] = {}
        for sid in enabled_skill_ids:
            skill = registry.skills[sid]
            selected = [r.local_id for r in skill.rules if f"{sid}:{r.local_id}" in explicit_rules]
            if selected:
                new_enabled[sid] = selected
        enabled_skill_ids = sorted(new_enabled.keys())
        enabled_rules_by_skill = new_enabled

    combined_prompt = registry.build_combined_prompt(enabled_skill_ids, enabled_rules_by_skill)
    enabled_global_rules = registry.list_enabled_global_ids(enabled_skill_ids, enabled_rules_by_skill)

    if not combined_prompt.strip():
        logger.warning("No enabled skills/rules found. Scan will run without skill guidance.")

    orchestrator = OpenCodeOrchestrator(
        concurrency=args.concurrency,
        nga_bin=args.nga_bin,
        session_timeout=args.timeout,
        debug=args.debug,
        web_port=args.web_port,
        workspace=args.workspace,
        output_json=args.output_json,
    )

    if args.diff:
        asyncio.run(
            orchestrator.scan_diff(
                args.diff, args.repo, args.paths,
                prompt=combined_prompt,
                enabled_rules=enabled_global_rules,
            )
        )
    elif args.files:
        asyncio.run(
            orchestrator.scan_files(
                args.files,
                prompt=combined_prompt,
                enabled_rules=enabled_global_rules,
            )
        )
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: `orchestrator.scan_diff` and `orchestrator.scan_files` method names may need to match the existing `orchestrator.py` methods. Adjust accordingly.

- [ ] **Step 2: Replace root `orchestrator.py` with shim**

```python
#!/usr/bin/env python3
"""Backward-compatible CLI shim for OpenCode Agent."""

from opencode_agent.cli.main import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write CLI entry test**

```python
# tests/test_cli_entry.py
import subprocess
import sys


def test_root_orchestrator_help():
    result = subprocess.run(
        [sys.executable, "orchestrator.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "OpenCode Agent" in result.stdout


def test_module_main_help():
    result = subprocess.run(
        [sys.executable, "-m", "opencode_agent", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "OpenCode Agent" in result.stdout
```

- [ ] **Step 4: Run CLI tests**

Run: `pytest tests/test_cli_entry.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencode_agent/cli/ orchestrator.py tests/test_cli_entry.py
git commit -m "feat: add cli entry point and root shim

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Migrate Benchmark Script and Merge Knowledge

**Files:**
- Move: `benchmark_cache.py` → `scripts/benchmark_cache.py`
- Modify: `src/opencode_agent/skills/wireless-scan.yaml`
- Delete: `knowleage/` directory

- [ ] **Step 1: Move benchmark script**

```bash
mv benchmark_cache.py scripts/benchmark_cache.py
```

Update import inside `scripts/benchmark_cache.py`:

```python
from opencode_agent.scanner.splitter import extract_functions
```

- [ ] **Step 2: Merge `knowleage/wireless-radio.md` into `wireless-scan.yaml`**

Append the expertise notes from `knowleage/wireless-radio.md` to the `agent.expertise` list in `src/opencode_agent/skills/wireless-scan.yaml`. For example, add:

```yaml
agent:
  role: "资深嵌入式通信协议栈安全审计专家"
  expertise:
    - "熟悉 4G/5G RRC/MAC/NAS 层协议消息兼容性"
    - "擅长 TLV/嵌套 TLV 解析边界检查"
    - "识别结构体强转与内存越界风险"
    - "排查 switch-case 消息分发缺少 default 分支的问题"
    - "审计 ASN.1 生成代码中 Optional 字段的空指针访问"
```

- [ ] **Step 3: Delete `knowleage/`**

```bash
rm -rf knowleage/
```

- [ ] **Step 4: Commit**

```bash
git add scripts/benchmark_cache.py src/opencode_agent/skills/wireless-scan.yaml
git rm -r knowleage/ benchmark_cache.py
git commit -m "chore: move benchmark script and merge knowledge into skill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Update `pyproject.toml` for src Layout

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add src-layout package discovery**

Ensure `pyproject.toml` contains:

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "opencode-agent"
version = "0.1.0"
description = "OpenCode Agent for C/C++ code review"
requires-python = ">=3.10"
dependencies = [
    "pyyaml>=6.0.0",
    "tree-sitter>=0.20.0",
    "tree-sitter-c>=0.20.0",
    "tree-sitter-cpp>=0.20.0",
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0",
    "gunicorn>=21.0.0",
    "httpx>=0.24.0",
    "starlette>=0.27.0",
    "pydantic>=2.0.0",
]

[project.scripts]
opencode-agent = "opencode_agent.cli.main:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
opencode_agent = ["skills/*.yaml"]
```

Adjust dependency versions to match `requirements.txt`.

- [ ] **Step 2: Verify package install in editable mode**

Run: `pip install -e .`
Expected: Package installs without errors.

- [ ] **Step 3: Verify import from installed package**

Run: `python -c "from opencode_agent.cli.main import main; from opencode_agent.skills.registry import SkillRegistry; print('ok')"`
Expected: prints `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: configure src-layout package and console script

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Update Remaining Test Imports

**Files:**
- Modify: all `tests/test_*.py` files not yet updated.

- [ ] **Step 1: Update any remaining imports**

Search for old root-level imports in `tests/`:

```bash
grep -rn "from \(finding_parser\|finding_store\|function_splitter\|orchestrator\|skill_loader\|config_loader\|web_server\)" tests/
```

Replace with package imports:

| Old | New |
|---|---|
| `from finding_parser import ...` | `from opencode_agent.findings.parser import ...` |
| `from finding_store import ...` | `from opencode_agent.findings.store import ...` |
| `from function_splitter import ...` | `from opencode_agent.scanner.splitter import ...` |
| `from orchestrator import ...` | `from opencode_agent.scanner.orchestrator import ...` |
| `from skill_loader import ...` | `from opencode_agent.skills.loader import ...` |
| `from config_loader import ...` | `from opencode_agent.skills.config_loader import ...` |
| `from web_server import ...` | `from opencode_agent.web.server import ...` |

- [ ] **Step 2: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "test: update all test imports to package paths

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 12: Clean Up Root Directory and Final Verification

**Files:**
- Delete: root-level `.py` files that have been migrated (after confirming no references remain)

- [ ] **Step 1: List root-level Python files**

Run: `ls *.py`
Expected: Only `orchestrator.py` remains (the shim).

- [ ] **Step 2: Delete migrated root files**

```bash
rm -f finding_parser.py finding_store.py function_splitter.py skill_loader.py config_loader.py web_server.py
```

- [ ] **Step 3: Verify no stale imports reference root modules**

Run: `grep -rn "from \(finding_parser\|finding_store\|function_splitter\|skill_loader\|config_loader\|web_server\) " src/ tests/ || true`
Expected: No matches.

- [ ] **Step 4: Run full test suite again**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Run CLI help via root shim**

Run: `python orchestrator.py --help`
Expected: Help text prints successfully.

- [ ] **Step 6: Commit**

```bash
git rm finding_parser.py finding_store.py function_splitter.py skill_loader.py config_loader.py web_server.py
git commit -m "refactor: remove migrated root-level modules

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

Before execution, verify:

- [ ] **Spec coverage**: Every design doc section (modules, data flow, error handling, testing, migration path) has at least one task.
- [ ] **No placeholders**: No "TBD", "TODO", or vague steps remain.
- [ ] **Type consistency**: `ScanTask`, `ProgressTracker`, `Skill`, `Rule`, `Finding`, `FindingStore` names match across modules.
- [ ] **Import paths**: All test imports are updated to `opencode_agent.*`.
- [ ] **Backward compatibility**: Root `orchestrator.py` shim preserves `python orchestrator.py ...` usage.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-15-src-architecture-refactor.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach would you like?
