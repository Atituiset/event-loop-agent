"""
Local SAST Engine - Semgrep + Cppcheck wrapper.

Provides fast, local static analysis as a pre-filter layer.
High-confidence findings are output directly; uncertain ones
are forwarded to LLM (nga) for deep analysis.

Phase 1: Semgrep custom rules + Cppcheck fallback.
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("SAST")


# ============================================================================
# Unified Issue Format
# ============================================================================

@dataclass
class SASTIssue:
    tool: str              # "semgrep" | "cppcheck" | "clang_sa"
    rule_id: str           # e.g. "RULE-002"
    severity: str          # "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    file_path: str
    line_number: int
    column: int = 0
    message: str = ""
    code_snippet: str = ""
    confidence: float = 0.8
    fix_suggestion: str = ""
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ============================================================================
# Route Decision
# ============================================================================

class RouteDecision:
    DIRECT_OUTPUT = "direct"    # High confidence → output directly
    LLM_ENHANCE = "enhance"     # Medium confidence → LLM adds context
    LLM_VERIFY = "verify"       # Low confidence → LLM validates


def route_issue(issue: SASTIssue) -> str:
    """
    Decide how to handle a SAST finding.

    Rules:
    - confidence >= 0.9 and explicit pattern match → DIRECT_OUTPUT
    - confidence >= 0.7 → LLM_ENHANCE
    - else → LLM_VERIFY
    """
    high_confidence_rules = {
        "RULE-002", "RULE-003", "RULE-024",  # Semgrep patterns with clear matches
    }
    if issue.confidence >= 0.9 and issue.rule_id in high_confidence_rules:
        return RouteDecision.DIRECT_OUTPUT
    elif issue.confidence >= 0.7:
        return RouteDecision.LLM_ENHANCE
    else:
        return RouteDecision.LLM_VERIFY


# ============================================================================
# Base Tool
# ============================================================================

class BaseSASTTool:
    name: str = "base"
    available: bool = False

    def scan(self, file_path: str) -> list[SASTIssue]:
        raise NotImplementedError

    def scan_batch(self, file_paths: list[str]) -> dict[str, list[SASTIssue]]:
        results: dict[str, list[SASTIssue]] = {}
        for fp in file_paths:
            results[fp] = self.scan(fp)
        return results


# ============================================================================
# Semgrep Tool
# ============================================================================

class SemgrepTool(BaseSASTTool):
    name = "semgrep"

    def __init__(self, rules_dir: Path = Path("skills/semgrep")):
        self.rules_dir = rules_dir
        self.available = shutil.which("semgrep") is not None
        if not self.available:
            logger.warning("semgrep not found in PATH. Install: pip install semgrep")
        self._config_file = rules_dir / "wireless-rules.yaml"

    def scan(self, file_path: str) -> list[SASTIssue]:
        if not self.available:
            return []
        if not self._config_file.exists():
            logger.warning(f"Semgrep config not found: {self._config_file}")
            return []

        try:
            result = subprocess.run(
                [
                    "semgrep",
                    "--config", str(self._config_file),
                    "--json",
                    "--quiet",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode not in (0, 1):
                # Semgrep returns 1 when findings exist
                logger.debug(f"semgrep exit code {result.returncode} for {file_path}")

            data = json.loads(result.stdout)
            return self._parse_results(data, file_path)

        except subprocess.TimeoutExpired:
            logger.warning(f"semgrep timeout for {file_path}")
            return []
        except Exception as e:
            logger.debug(f"semgrep failed for {file_path}: {e}")
            return []

    def _parse_results(self, data: dict, file_path: str) -> list[SASTIssue]:
        issues: list[SASTIssue] = []
        results = data.get("results", [])
        for r in results:
            meta = r.get("extra", {}).get("metadata", {})
            rule_id = meta.get("rule_id", r.get("check_id", "UNKNOWN"))
            severity = r.get("extra", {}).get("severity", "WARNING")
            # Map semgrep severity to our levels
            severity_map = {
                "ERROR": "HIGH",
                "WARNING": "MEDIUM",
                "INFO": "LOW",
            }
            confidence = meta.get("confidence", 0.8)
            if isinstance(confidence, str):
                confidence = {"high": 0.9, "medium": 0.7, "low": 0.5}.get(confidence, 0.7)

            issue = SASTIssue(
                tool="semgrep",
                rule_id=rule_id,
                severity=severity_map.get(severity, "MEDIUM"),
                file_path=r.get("path", file_path),
                line_number=r.get("start", {}).get("line", 0),
                column=r.get("start", {}).get("col", 0),
                message=r.get("extra", {}).get("message", ""),
                code_snippet=r.get("extra", {}).get("lines", "").strip(),
                confidence=confidence,
                metadata=meta,
            )
            issues.append(issue)
        return issues

    def scan_batch(self, file_paths: list[str]) -> dict[str, list[SASTIssue]]:
        if not self.available or not file_paths:
            return {}
        if not self._config_file.exists():
            return {}

        BATCH_SIZE = 100  # Avoid command-line length issues
        all_results: dict[str, list[SASTIssue]] = {fp: [] for fp in file_paths}

        for i in range(0, len(file_paths), BATCH_SIZE):
            batch = file_paths[i:i + BATCH_SIZE]
            try:
                result = subprocess.run(
                    [
                        "semgrep",
                        "--config", str(self._config_file),
                        "--json",
                        "--quiet",
                    ] + batch,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                data = json.loads(result.stdout)
                for r in data.get("results", []):
                    fp = r.get("path", "")
                    if fp not in all_results:
                        all_results[fp] = []
                    meta = r.get("extra", {}).get("metadata", {})
                    rule_id = meta.get("rule_id", r.get("check_id", "UNKNOWN"))
                    severity = r.get("extra", {}).get("severity", "WARNING")
                    severity_map = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}
                    confidence = meta.get("confidence", 0.8)
                    if isinstance(confidence, str):
                        confidence = {"high": 0.9, "medium": 0.7, "low": 0.5}.get(confidence, 0.7)

                    issue = SASTIssue(
                        tool="semgrep",
                        rule_id=rule_id,
                        severity=severity_map.get(severity, "MEDIUM"),
                        file_path=fp,
                        line_number=r.get("start", {}).get("line", 0),
                        column=r.get("start", {}).get("col", 0),
                        message=r.get("extra", {}).get("message", ""),
                        code_snippet=r.get("extra", {}).get("lines", "").strip(),
                        confidence=confidence,
                        metadata=meta,
                    )
                    all_results[fp].append(issue)
            except Exception as e:
                logger.warning(f"semgrep batch scan failed for batch {i//BATCH_SIZE + 1}: {e}")
                # Fallback to individual scans for this batch
                for fp in batch:
                    issues = self.scan(fp)
                    all_results[fp] = issues

        return all_results


# ============================================================================
# Cppcheck Tool (Optional Fallback)
# ============================================================================

class CppcheckTool(BaseSASTTool):
    name = "cppcheck"

    def __init__(self):
        self.available = shutil.which("cppcheck") is not None
        if not self.available:
            logger.debug("cppcheck not found in PATH")

    def scan(self, file_path: str) -> list[SASTIssue]:
        if not self.available:
            return []
        try:
            result = subprocess.run(
                [
                    "cppcheck",
                    "--enable=all",
                    "--error-exitcode=0",
                    "--xml",
                    "--xml-version=2",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            # Cppcheck XML goes to stderr
            return self._parse_xml(result.stderr, file_path)
        except Exception as e:
            logger.debug(f"cppcheck failed for {file_path}: {e}")
            return []

    def _parse_xml(self, xml_text: str, file_path: str) -> list[SASTIssue]:
        import xml.etree.ElementTree as ET
        issues: list[SASTIssue] = []
        try:
            root = ET.fromstring(xml_text)
            for error in root.findall(".//error"):
                severity = error.get("severity", "warning")
                severity_map = {
                    "error": "HIGH",
                    "warning": "MEDIUM",
                    "style": "LOW",
                    "information": "LOW",
                }
                msg = error.get("msg", "")
                loc = error.find("location")
                line = int(loc.get("line", 0)) if loc is not None else 0
                file_p = loc.get("file", file_path) if loc is not None else file_path

                issue = SASTIssue(
                    tool="cppcheck",
                    rule_id=error.get("id", "UNKNOWN"),
                    severity=severity_map.get(severity, "MEDIUM"),
                    file_path=file_p,
                    line_number=line,
                    message=msg,
                    confidence=0.7,
                )
                issues.append(issue)
        except Exception as e:
            logger.debug(f"cppcheck XML parse failed: {e}")
        return issues


# ============================================================================
# SAST Engine Aggregator
# ============================================================================

class SASTEngine:
    """
    Aggregates multiple SAST tools and provides unified output.

    Usage:
        engine = SASTEngine()
        results = engine.scan_batch(["file1.c", "file2.c"])
        for fp, issues in results.items():
            for issue in issues:
                if route_issue(issue) == RouteDecision.DIRECT_OUTPUT:
                    # Output directly without LLM
                    pass
    """

    def __init__(self, rules_dir: Path = Path("skills/semgrep")):
        self.tools: list[BaseSASTTool] = [
            SemgrepTool(rules_dir),
            CppcheckTool(),
        ]
        self._available_tools = [t for t in self.tools if t.available]
        if self._available_tools:
            logger.info(f"SAST engine ready: {[t.name for t in self._available_tools]}")
        else:
            logger.warning("No SAST tools available. All scanning will use LLM.")

    @property
    def has_tools(self) -> bool:
        return len(self._available_tools) > 0

    def scan(self, file_path: str) -> list[SASTIssue]:
        """Scan a single file with all available tools and merge results."""
        all_issues: list[SASTIssue] = []
        seen = set()
        for tool in self._available_tools:
            issues = tool.scan(file_path)
            for issue in issues:
                key = (issue.file_path, issue.line_number, issue.rule_id, issue.message[:50])
                if key not in seen:
                    seen.add(key)
                    all_issues.append(issue)
        return all_issues

    def scan_batch(self, file_paths: list[str]) -> dict[str, list[SASTIssue]]:
        """Scan multiple files, reusing tool processes where possible."""
        if not self._available_tools:
            return {fp: [] for fp in file_paths}

        # Try batch scan on first available tool that supports it
        for tool in self._available_tools:
            if hasattr(tool, "scan_batch"):
                try:
                    return tool.scan_batch(file_paths)
                except Exception as e:
                    logger.debug(f"{tool.name} batch scan failed: {e}")

        # Fallback to individual scans
        results: dict[str, list[SASTIssue]] = {}
        for fp in file_paths:
            results[fp] = self.scan(fp)
        return results

    def classify(self, issue: SASTIssue) -> str:
        """Return routing decision for an issue."""
        return route_issue(issue)

    def direct_output_issues(self, issues: list[SASTIssue]) -> list[SASTIssue]:
        """Filter issues that can be output directly (no LLM needed)."""
        return [i for i in issues if route_issue(i) == RouteDecision.DIRECT_OUTPUT]

    def llm_issues(self, issues: list[SASTIssue]) -> list[SASTIssue]:
        """Filter issues that need LLM verification/enhancement."""
        return [i for i in issues if route_issue(i) != RouteDecision.DIRECT_OUTPUT]


# ============================================================================
# Format helpers
# ============================================================================

def format_sast_issue_markdown(issue: SASTIssue) -> str:
    """Format a SAST issue as markdown (for direct output)."""
    lines = []
    lines.append(f"### [{issue.rule_id}] {issue.severity}")
    lines.append(f"**文件**: `{issue.file_path}:{issue.line_number}`")
    lines.append(f"**工具**: {issue.tool}")
    lines.append(f"**描述**: {issue.message}")
    if issue.code_snippet:
        lines.append(f"**代码片段**:")
        lines.append(f"```c")
        lines.append(issue.code_snippet)
        lines.append(f"```")
    if issue.fix_suggestion:
        lines.append(f"**修复建议**: {issue.fix_suggestion}")
    lines.append(f"**置信度**: {issue.confidence:.0%}")
    lines.append("")
    return "\n".join(lines)
