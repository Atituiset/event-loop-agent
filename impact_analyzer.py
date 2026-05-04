"""
Impact Analyzer - Cross-file impact surface analysis for code changes.

Analyzes diffs to determine which other files are affected by a change:
  - Header file changes → all source files that #include it
  - Function signature changes → caller files
  - Global variable changes → all files that read/write it
  - Struct/enum/macro changes → all files that reference the symbol

Phase 2: Lightweight grep-based analysis.
Future: clang-based precise symbol analysis.
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Impact")


# ============================================================================
# Change Classification
# ============================================================================

class ChangeType(Enum):
    UNKNOWN = "unknown"
    LOG_ONLY = "log_only"
    COMMENT = "comment"
    VARIABLE_RENAME = "var_rename"
    POINTER_ARITHMETIC = "ptr_arith"
    STRUCT_DEFINITION = "struct_def"
    ENUM_DEFINITION = "enum_def"
    MACRO_DEFINITION = "macro_def"
    FUNCTION_SIGNATURE = "func_sig"
    GLOBAL_VARIABLE = "global_var"
    LOCK_STRATEGY = "lock_strategy"
    PROTOCOL_VERSION = "proto_ver"


class ChangeClassifier:
    """Classify diff content to determine impact scope."""

    # Keywords for quick classification
    _PATTERNS = [
        (ChangeType.LOG_ONLY, re.compile(r'^[+-]\s*(LOG_\w+|DBG_\w+|TRACE_\w+)\s*\(')),
        (ChangeType.COMMENT, re.compile(r'^[+-]\s*(//|/\*|\*)')),
        (ChangeType.STRUCT_DEFINITION, re.compile(r'^[+-]\s*(struct\s+\w+|typedef\s+struct)')),
        (ChangeType.ENUM_DEFINITION, re.compile(r'^[+-]\s*(enum\s+\w+|typedef\s+enum)')),
        (ChangeType.MACRO_DEFINITION, re.compile(r'^[+-]\s*#define\s+\w+')),
        (ChangeType.FUNCTION_SIGNATURE, re.compile(r'^[+-]\s*(?:static\s+)?(?:inline\s+)?(?:const\s+)?\w+[\s\*]+\w+\s*\([^)]*\)\s*(?:\{|;)?\s*$')),
        (ChangeType.GLOBAL_VARIABLE, re.compile(r'^[+-]\s*(?:static\s+|extern\s+)?(?:const\s+)?\w+[\s\*]+\w+\s*[=;]')),
    ]

    def classify(self, diff_content: str) -> ChangeType:
        """Classify the dominant change type in a diff."""
        if not diff_content:
            return ChangeType.UNKNOWN

        type_counts: dict[ChangeType, int] = {}
        for line in diff_content.splitlines():
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            for ctype, pattern in self._PATTERNS:
                if pattern.search(line):
                    type_counts[ctype] = type_counts.get(ctype, 0) + 1
                    break

        if not type_counts:
            return ChangeType.UNKNOWN

        # Return the most frequent type
        return max(type_counts, key=type_counts.get)


# ============================================================================
# Symbol Extractor
# ============================================================================

class SymbolExtractor:
    """Extract changed symbols from diff content."""

    _FUNCTION_RE = re.compile(
        r'^[+-]\s*(?:static\s+|inline\s+|const\s+)?'
        r'(?:\w+[\s\*]+)+'
        r'(\w+)\s*\([^)]*\)\s*(?:\{|;)?\s*$'
    )
    _STRUCT_RE = re.compile(r'^[+-]\s*(?:typedef\s+)?struct\s+(\w+)')
    _ENUM_RE = re.compile(r'^[+-]\s*(?:typedef\s+)?enum\s+(\w+)')
    _MACRO_RE = re.compile(r'^[+-]\s*#define\s+(\w+)')
    _GLOBAL_VAR_RE = re.compile(
        r'^[+-]\s*(?:static\s+|extern\s+|const\s+)?'
        r'\w+[\s\*]+(\w+)\s*[=;]'
    )

    def extract(self, diff_content: str) -> dict[str, list[str]]:
        """
        Extract symbols from diff by category.
        Returns: {"function": [...], "struct": [...], "enum": [...], "macro": [...], "global_var": [...]}
        """
        result: dict[str, list[str]] = {
            "function": [],
            "struct": [],
            "enum": [],
            "macro": [],
            "global_var": [],
        }
        seen = set()

        for line in diff_content.splitlines():
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue

            for category, pattern in [
                ("function", self._FUNCTION_RE),
                ("struct", self._STRUCT_RE),
                ("enum", self._ENUM_RE),
                ("macro", self._MACRO_RE),
                ("global_var", self._GLOBAL_VAR_RE),
            ]:
                m = pattern.search(line)
                if m:
                    name = m.group(1)
                    key = f"{category}:{name}"
                    if key not in seen:
                        seen.add(key)
                        result[category].append(name)

        return result


# ============================================================================
# Impact Analyzer
# ============================================================================

@dataclass
class ImpactResult:
    """Result of impact analysis for a single changed file."""
    file_path: str
    change_type: ChangeType
    changed_symbols: dict[str, list[str]]  # category -> symbol names
    impacted_files: list[str] = field(default_factory=list)
    impact_reason: str = ""


class ImpactAnalyzer:
    """
    Analyze the impact surface of code changes.

    Usage:
        analyzer = ImpactAnalyzer(repo_path=Path("."))
        results = analyzer.analyze(["src/rr/pdu.h", "src/mac/scheduler.c"])
        for r in results:
            print(f"{r.file_path} impacts: {r.impacted_files}")
    """

    def __init__(self, repo_path: Path = Path("."), max_depth: int = 2):
        self.repo_path = repo_path.resolve()
        self.max_depth = max_depth
        self.classifier = ChangeClassifier()
        self.symbol_extractor = SymbolExtractor()

    def analyze(self, changed_files: list[str]) -> list[ImpactResult]:
        """Analyze impact for each changed file."""
        results: list[ImpactResult] = []
        for fp in changed_files:
            result = self._analyze_one(fp)
            results.append(result)
        return results

    def _analyze_one(self, file_path: str) -> ImpactResult:
        """Analyze impact for a single file."""
        path = Path(file_path)

        # Try to read diff content from saved diff file
        diff_content = ""
        diff_file = self.repo_path / "reports" / "diffs" / file_path
        if diff_file.with_suffix(".diff").exists():
            diff_content = diff_file.with_suffix(".diff").read_text(encoding="utf-8")

        change_type = self.classifier.classify(diff_content)
        symbols = self.symbol_extractor.extract(diff_content)

        impacted: list[str] = []
        reason = ""

        # Header file cascade
        if path.suffix in (".h", ".hpp"):
            impacted.extend(self._find_includers(path.name))
            reason = f"header file change: {len(impacted)} files include it"

        # Symbol-based impact
        if symbols["struct"]:
            refs = self._find_symbol_references(symbols["struct"])
            impacted.extend(refs)
            reason += f"; struct references: {len(refs)} files"

        if symbols["enum"]:
            refs = self._find_symbol_references(symbols["enum"])
            impacted.extend(refs)
            reason += f"; enum references: {len(refs)} files"

        if symbols["macro"]:
            refs = self._find_symbol_references(symbols["macro"])
            impacted.extend(refs)
            reason += f"; macro references: {len(refs)} files"

        if symbols["function"]:
            refs = self._find_symbol_references(symbols["function"])
            impacted.extend(refs)
            reason += f"; function references: {len(refs)} files"

        if symbols["global_var"]:
            refs = self._find_symbol_references(symbols["global_var"])
            impacted.extend(refs)
            reason += f"; global var references: {len(refs)} files"

        # Deduplicate and exclude self
        impacted = sorted(set(f for f in impacted if f != file_path))

        return ImpactResult(
            file_path=file_path,
            change_type=change_type,
            changed_symbols=symbols,
            impacted_files=impacted,
            impact_reason=reason.lstrip("; "),
        )

    def _find_includers(self, header_name: str) -> list[str]:
        """Find all source files that #include the given header."""
        try:
            # Search for both quoted and angle-bracket includes
            patterns = [
                f'#include.*"{re.escape(header_name)}"',
                f'#include.*<{re.escape(header_name)}>',
            ]
            files: set[str] = set()
            for pattern in patterns:
                result = subprocess.run(
                    ["grep", "-rl", "--include=*.c", "--include=*.cc",
                     "--include=*.cpp", "--include=*.h", "--include=*.hpp",
                     pattern, str(self.repo_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line:
                        # Convert absolute path to relative path
                        rel = Path(line).relative_to(self.repo_path)
                        files.add(str(rel))
            return sorted(files)
        except Exception as e:
            logger.debug(f"find_includers failed for {header_name}: {e}")
            return []

    def _find_symbol_references(self, symbols: list[str]) -> list[str]:
        """Find all files that reference any of the given symbols."""
        if not symbols:
            return []

        files: set[str] = set()
        for sym in symbols:
            try:
                result = subprocess.run(
                    ["grep", "-rl", "--include=*.c", "--include=*.cc",
                     "--include=*.cpp", "--include=*.h", "--include=*.hpp",
                     f"\\b{re.escape(sym)}\\b", str(self.repo_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line:
                        rel = Path(line).relative_to(self.repo_path)
                        files.add(str(rel))
            except Exception as e:
                logger.debug(f"find_symbol_references failed for {sym}: {e}")

        return sorted(files)

    def expand_scan_tasks(
        self,
        changed_files: list[str],
    ) -> tuple[list[str], dict[str, list[str]]]:
        """
        Expand the scan task list based on impact analysis.

        Returns:
            - primary_files: files that should be scanned directly
            - context_map: {primary_file -> [context_files]} for prompt injection
        """
        results = self.analyze(changed_files)

        primary = list(changed_files)
        context_map: dict[str, list[str]] = {}

        for r in results:
            if r.impacted_files:
                context_map[r.file_path] = r.impacted_files
                # Add impacted files to primary if they are source files
                for f in r.impacted_files:
                    if f.endswith((".c", ".cc", ".cpp")) and f not in primary:
                        primary.append(f)

        return primary, context_map


# ============================================================================
# Utility
# ============================================================================

def format_impact_summary(results: list[ImpactResult]) -> str:
    """Format impact analysis results as markdown summary."""
    lines = ["# 变更影响面分析\n"]
    for r in results:
        lines.append(f"## {r.file_path}")
        lines.append(f"- **变更类型**: {r.change_type.value}")
        lines.append(f"- **影响文件数**: {len(r.impacted_files)}")
        if r.impact_reason:
            lines.append(f"- **影响原因**: {r.impact_reason}")
        if r.changed_symbols:
            for cat, syms in r.changed_symbols.items():
                if syms:
                    lines.append(f"- **变更{cat}**: {', '.join(syms)}")
        if r.impacted_files:
            lines.append("- **影响文件列表**:")
            for f in r.impacted_files[:10]:
                lines.append(f"  - `{f}`")
            if len(r.impacted_files) > 10:
                lines.append(f"  - ... 等共 {len(r.impacted_files)} 个文件")
        lines.append("")
    return "\n".join(lines)
