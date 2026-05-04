"""
Output Format Generators - SARIF 2.1.0, JSON, and enhanced Markdown.

Provides machine-readable output for CI/CD integration and trend tracking.

Phase 4: Structured output + metrics.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from sast_engine import SASTIssue


# ============================================================================
# SARIF 2.1.0
# ============================================================================

def generate_sarif(
    tasks: list,
    run_id: str,
    commit_hash: str = "",
    rules_metadata: dict = None,
) -> dict:
    """
    Generate SARIF 2.1.0 output from scan tasks.

    Args:
        tasks: List of ScanTask
        run_id: Unique run identifier
        commit_hash: Git commit hash
        rules_metadata: Dict of rule_id -> {name, description, severity}
    """
    rules_metadata = rules_metadata or {}

    # Collect all unique rules
    rule_ids: set[str] = set()
    for task in tasks:
        if task.sast_issues:
            for issue in task.sast_issues:
                rule_ids.add(issue.rule_id)
        # Also extract from nga stdout
        nga_rules = _extract_rule_ids_from_stdout(task.stdout or "")
        rule_ids.update(nga_rules)

    # Build tool driver rules
    driver_rules = []
    for rid in sorted(rule_ids):
        meta = rules_metadata.get(rid, {})
        driver_rules.append({
            "id": rid,
            "name": meta.get("name", rid),
            "shortDescription": {"text": meta.get("description", "")},
            "defaultConfiguration": {
                "level": _severity_to_sarif_level(meta.get("severity", "MEDIUM")),
            },
        })

    # Build results
    results = []
    for task in tasks:
        # SAST issues
        if task.sast_issues:
            for issue in task.sast_issues:
                results.append(_sast_issue_to_sarif_result(issue))

        # NGA findings (extracted from stdout)
        nga_findings = _extract_findings_from_stdout(task)
        for finding in nga_findings:
            results.append(_finding_to_sarif_result(finding))

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "OpenCode Agent",
                        "version": "2.0.0",
                        "informationUri": "https://github.com/Atituiset/event-loop-agent",
                        "rules": driver_rules,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": all(t.status == "done" for t in tasks),
                        "startTimeUtc": _format_iso_time(),
                    }
                ],
                "versionControlProvenance": [
                    {
                        "repositoryUri": "",
                        "revisionId": commit_hash,
                    }
                ] if commit_hash else [],
                "results": results,
            }
        ],
    }
    return sarif


def write_sarif_file(
    tasks: list,
    output_path: Path,
    run_id: str = "",
    commit_hash: str = "",
) -> Path:
    """Generate and write SARIF to file."""
    sarif = generate_sarif(tasks, run_id, commit_hash)
    output_path.write_text(json.dumps(sarif, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


# ============================================================================
# JSON Output (internal)
# ============================================================================

def generate_json(tasks: list, run_id: str = "", duration: float = 0.0) -> dict:
    """Generate JSON output for internal consumption and database ingestion."""
    return {
        "run_id": run_id,
        "timestamp": _format_iso_time(),
        "duration_seconds": duration,
        "total_files": len(tasks),
        "successful": sum(1 for t in tasks if t.status == "done"),
        "failed": sum(1 for t in tasks if t.status == "failed"),
        "files": [
            {
                "file_path": t.file_path,
                "task_id": t.task_id,
                "status": t.status,
                "duration": t.duration,
                "sast_issues": [
                    {
                        "rule_id": i.rule_id,
                        "severity": i.severity,
                        "line": i.line_number,
                        "message": i.message,
                        "confidence": i.confidence,
                    }
                    for i in (t.sast_issues or [])
                ],
                "extracted_findings": _extract_findings_from_stdout(t),
            }
            for t in tasks
        ],
    }


def write_json_file(tasks: list, output_path: Path, run_id: str = "", duration: float = 0.0) -> Path:
    """Generate and write JSON to file."""
    data = generate_json(tasks, run_id, duration)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


# ============================================================================
# Helpers
# ============================================================================

def _severity_to_sarif_level(severity: str) -> str:
    """Map our severity to SARIF level."""
    mapping = {
        "CRITICAL": "error",
        "HIGH": "error",
        "MEDIUM": "warning",
        "LOW": "note",
    }
    return mapping.get(severity.upper(), "warning")


def _sast_issue_to_sarif_result(issue: SASTIssue) -> dict:
    """Convert SASTIssue to SARIF result."""
    return {
        "ruleId": issue.rule_id,
        "level": _severity_to_sarif_level(issue.severity),
        "message": {"text": issue.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": issue.file_path},
                    "region": {
                        "startLine": issue.line_number,
                        "startColumn": issue.column,
                        "snippet": {"text": issue.code_snippet},
                    },
                }
            }
        ],
        "properties": {
            "confidence": issue.confidence,
            "tool": issue.tool,
        },
    }


@dataclass
class _NgaFinding:
    rule_id: str
    message: str
    file_path: str
    line_number: int = 0


def _extract_findings_from_stdout(task) -> list[_NgaFinding]:
    """Extract RULE-XXX findings from nga stdout."""
    if not task.stdout:
        return []

    findings: list[_NgaFinding] = []
    rule_pattern = re.compile(r'\[?RULE-(\d{3})\]?', re.IGNORECASE)

    paragraphs = task.stdout.split('\n\n')
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        m = rule_pattern.search(para)
        if m:
            rule_id = f"RULE-{m.group(1)}"
            summary = para.replace('\n', ' ').strip()
            if len(summary) > 300:
                sentence_end = summary.find('。')
                if sentence_end == -1:
                    sentence_end = summary.find('.')
                if sentence_end > 10:
                    summary = summary[:sentence_end + 1]
                else:
                    summary = summary[:300] + "..."

            # Try to extract line number
            line_match = re.search(r'[:：]\s*(\d+)', para)
            line_number = int(line_match.group(1)) if line_match else 0

            findings.append(_NgaFinding(
                rule_id=rule_id,
                message=summary,
                file_path=task.file_path,
                line_number=line_number,
            ))

    return findings


def _finding_to_sarif_result(finding: _NgaFinding) -> dict:
    """Convert extracted nga finding to SARIF result."""
    return {
        "ruleId": finding.rule_id,
        "level": "warning",
        "message": {"text": finding.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file_path},
                    "region": {
                        "startLine": finding.line_number,
                    },
                }
            }
        ],
        "properties": {
            "source": "nga",
            "confidence": 0.7,
        },
    }


def _extract_rule_ids_from_stdout(stdout: str) -> set[str]:
    """Extract all RULE-XXX IDs from stdout."""
    pattern = re.compile(r'\[?RULE-(\d{3})\]?', re.IGNORECASE)
    return {f"RULE-{m.group(1)}" for m in pattern.finditer(stdout)}


def _format_iso_time() -> str:
    """Return current time in ISO 8601 format."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Rule Metadata (for SARIF driver)
# ============================================================================

DEFAULT_RULES_METADATA: dict[str, dict] = {
    "RULE-001": {
        "name": "TLV Boundary Check",
        "description": "Pointer offset without remaining_len validation in TLV parsing",
        "severity": "CRITICAL",
    },
    "RULE-002": {
        "name": "Struct Cast Memory Safety",
        "description": "memcpy or cast without sizeof validation",
        "severity": "HIGH",
    },
    "RULE-003": {
        "name": "Switch-Case Default Branch",
        "description": "Switch statement missing safe default branch",
        "severity": "MEDIUM",
    },
    "RULE-004": {
        "name": "ASN.1 Optional Field",
        "description": "Optional field accessed without presence check",
        "severity": "HIGH",
    },
    "RULE-005": {
        "name": "Similar Variable Name Confusion",
        "description": "Similar variable names may be confused",
        "severity": "MEDIUM",
    },
    "RULE-006": {
        "name": "Redundant Code",
        "description": "Duplicated or redundant code blocks",
        "severity": "LOW",
    },
    "RULE-007": {
        "name": "Uninitialized Variable",
        "description": "Local variable or struct used before initialization",
        "severity": "HIGH",
    },
    "RULE-008": {
        "name": "Memory Leak",
        "description": "malloc without corresponding free",
        "severity": "HIGH",
    },
    "RULE-009": {
        "name": "Null Pointer Dereference",
        "description": "Pointer dereferenced without NULL check",
        "severity": "CRITICAL",
    },
    "RULE-010": {
        "name": "Array Bounds",
        "description": "Array index used without bounds check",
        "severity": "CRITICAL",
    },
    "RULE-024": {
        "name": "Unsafe Function Usage",
        "description": "Use of unsafe functions like strcpy, sprintf, gets",
        "severity": "HIGH",
    },
}
