"""
Finding Parser Module

Extracts structured findings from nga Markdown review reports.
Supports multiple report formats produced by the orchestrator.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Finding:
    """A single code review finding."""

    finding_id: str
    file_path: str
    line_number: int
    rule_id: str
    severity: str
    description: str
    code_snippet: str
    suggestion: str
    confidence: float
    function_name: str = ""
    scan_timestamp: str = ""
    mr_link: str = ""
    task_id: str = ""
    log_file: str = ""

    # Feedback fields (populated later)
    label: Optional[str] = None  # "true_positive" | "false_positive"
    labeled_by: Optional[str] = None
    labeled_at: Optional[str] = None
    label_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        # Ignore extra fields to stay forward-compatible
        kwargs = {k: data.get(k) for k in cls.__dataclass_fields__}
        return cls(**kwargs)


def normalize_snippet(snippet: str) -> str:
    """
    Normalize a code snippet for stable hashing.

    - Collapse whitespace to single spaces
    - Strip C/C++ comments
    - Replace user identifiers with generic placeholders (simple heuristic)
      while preserving C/C++ keywords and literals for stability.
    """
    text = snippet or ""
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Strip C-style block comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Strip C++ line comments
    text = re.sub(r"//.*", "", text)

    # Preserve common C/C++ keywords; replace other identifiers with VAR
    C_KEYWORDS = {
        "auto", "break", "case", "char", "const", "continue", "default", "do",
        "double", "else", "enum", "extern", "float", "for", "goto", "if",
        "int", "long", "register", "return", "short", "signed", "sizeof",
        "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while", "class", "public", "private", "protected",
        "template", "typename", "namespace", "using", "new", "delete",
        "try", "catch", "throw", "const_cast", "dynamic_cast", "reinterpret_cast",
        "static_cast", "bool", "true", "false", "nullptr", "inline", "virtual",
        "explicit", "override", "final", "noexcept",
    }

    def replace_identifier(match: re.Match) -> str:
        word = match.group(0)
        return word if word in C_KEYWORDS else "VAR"

    text = re.sub(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", replace_identifier, text)
    return text.strip()


def generate_finding_id(
    repo_url: str,
    file_path: str,
    function_name: str,
    rule_id: str,
    snippet: str,
) -> str:
    """Generate a stable 16-character finding ID."""
    normalized = normalize_snippet(snippet)
    composite = f"{repo_url}:{file_path}:{function_name}:{rule_id}:{normalized}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]


def _extract_metadata(markdown: str) -> dict[str, str]:
    """Extract header metadata like file_path, function_name, task_id, scan_timestamp."""
    meta: dict[str, str] = {}

    file_match = re.search(r"\*\*文件\*\*:\s*`?([^`\n]+)`?", markdown)
    if file_match:
        meta["file_path"] = file_match.group(1).strip()

    func_match = re.search(r"\*\*函数\*\*:\s*`?([^`\n]+)`?", markdown)
    if func_match:
        meta["function_name"] = func_match.group(1).strip()

    task_match = re.search(r"\*\*任务ID\*\*:\s*`?([^`\n]+)`?", markdown)
    if task_match:
        meta["task_id"] = task_match.group(1).strip()

    time_match = re.search(r"\*\*扫描时间\*\*:\s*([^\n]+)", markdown)
    if time_match:
        meta["scan_timestamp"] = time_match.group(1).strip()

    return meta


def _parse_severity(text: str, confidence: float = 0.5) -> str:
    """Map severity keywords to normalized levels."""
    lowered = text.lower()
    if "高危" in text or "critical" in lowered or "严重" in text:
        return "CRITICAL"
    if "中危" in text or "high" in lowered or "高" in text:
        return "HIGH"
    if "低危" in text or "medium" in lowered or "中" in text:
        return "MEDIUM"
    if "信息" in text or "low" in lowered or "低" in text:
        return "LOW"

    # Fallback: infer from confidence if no explicit severity keyword
    if confidence >= 0.9:
        return "HIGH"
    if confidence >= 0.7:
        return "MEDIUM"
    return "LOW"


def _parse_confidence(text: str) -> float:
    """Parse confidence text like '高', '中', '低', 'HIGH', '★★★★★', '0.85'."""
    if not text:
        return 0.5

    text = text.strip().lower()

    # Numeric confidence
    num_match = re.search(r"(0?\.\d+|1\.0)", text)
    if num_match:
        return float(num_match.group(1))

    # Star rating
    stars = text.count("★")
    if stars:
        return stars / 5.0

    # Keywords (Chinese and English)
    if "高" in text or "确定" in text or "high" in text:
        return 0.9
    if "中" in text or "medium" in text:
        return 0.6
    if "低" in text or "low" in text:
        return 0.3

    return 0.5


def _extract_rule_id(block: str, title: str = "") -> str:
    """Extract RULE-XXX from an issue block or title."""
    # Table format: | **规则** | RULE-003 — description |
    match = re.search(r"\*\*规则\*\*[:：]?\s*\|\s*(RULE-\d{3})", block)
    if match:
        return match.group(1)
    # Inline in title
    match = re.search(r"(RULE-\d{3})", title)
    if match:
        return match.group(1)
    return _infer_rule_id(title + " " + block)


def _infer_rule_id(text: str) -> str:
    """Infer rule ID from description keywords when nga omits explicit rule."""
    lowered = text.lower()
    if any(k in lowered for k in ["buffer overflow", "off-by-one", "memcpy", "memset", "边界", "越界", "数组"]):
        return "RULE-002"
    if any(k in lowered for k in ["malloc", "free", "leak", "fd leak", "resource leak", "内存泄漏"]):
        return "RULE-006"
    if any(k in lowered for k in ["return value", "unchecked", "未检查", "返回值"]):
        return "RULE-001"
    if any(k in lowered for k in ["null pointer", "pointer", "空指针"]):
        return "RULE-002"
    if any(k in lowered for k in ["switch", "default", "case"]):
        return "RULE-003"
    if any(k in lowered for k in ["tlv", "asn.1", "边界校验", "length"]):
        return "RULE-001"
    if any(k in lowered for k in ["format string", "printf", "sprintf"]):
        return "RULE-004"
    return "RULE-000"


def _extract_line_number(block: str) -> int:
    """Extract the first line number reference from an issue block."""
    # Chinese format: 第19行 or 第19行、第30行 or 第23-24行
    match = re.search(r"第(\d{1,})(?:行|、|,|\-|\s)", block)
    if match:
        return int(match.group(1))
    # file.c:24 or file.c:24,30
    match = re.search(r":(\d{1,})(?:-\d+|,\d+)?", block)
    if match:
        return int(match.group(1))
    # line 24 / line 42-45
    match = re.search(r"line\s+(\d{1,})", block, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def _split_into_issue_blocks(markdown: str) -> list[str]:
    """
    Split report markdown into individual issue blocks.

    Supports formats:
    - '### [ISSUE-N] ...'
    - '### 问题 N — ...'
    - '### Bug: ...'
    - '### Missing ...'
    """
    text = markdown.replace("\r\n", "\n")

    # Match issue headers. Exclude summary sections.
    pattern = re.compile(
        r"(?:^|\n)(#{2,3}\s*(?:\[ISSUE-\d+\]|问题\s*\d+|Bug:|Missing\s|Dead\s+code:).*?)(?="
        r"\n#{2,3}\s*(?:\[ISSUE-\d+\]|问题\s*\d+|Bug:|Missing\s|Dead\s+code:|逐规则总体结论|总结|总结)|"
        r"\n---\s*\n|"
        r"\Z)",
        re.DOTALL,
    )
    blocks = pattern.findall(text)

    if blocks:
        return [b.strip() for b in blocks if b.strip()]

    # Fallback: split by '---' separators
    parts = re.split(r"\n---\s*\n", text)
    return [p.strip() for p in parts if re.search(r"\[ISSUE-\d+\]|问题\s*\d+|Bug:|Missing\s|Dead\s+code:", p)]


def _extract_first_code_block(block: str) -> str:
    """Extract the first fenced C/C++ code block from an issue block."""
    match = re.search(r"```c\s*\n(.*?)```", block, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Generic fenced block
    match = re.search(r"```\s*\n(.*?)```", block, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_description(block: str) -> str:
    """Extract description from an issue block."""
    # '**问题描述**：'
    match = re.search(
        r"\*\*问题描述\*\*[:：]\s*(.*?)(?=\n\s*\*\*(?:相关代码|修复建议|置信度|严重度)|\Z)",
        block,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    # '- **描述**: ...'
    match = re.search(
        r"\*\*描述\*\*[:：]\s*(.*?)(?=\n\s*-\s*\*\*|\n\s*\*\*修复|\n\s*\*\*置信|\Z)",
        block,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    # Fallback: first paragraph after the header line
    lines = block.split("\n")
    if len(lines) > 1:
        # Skip header line and empty lines, take next non-empty paragraph
        paragraphs = "\n".join(lines[1:]).split("\n\n")
        for p in paragraphs:
            stripped = p.strip()
            if stripped and not stripped.startswith("```"):
                return stripped
    return ""


def _extract_suggestion(block: str) -> str:
    """Extract fix suggestion from an issue block."""
    # '**修复建议**：'
    match = re.search(
        r"\*\*修复建议\*\*[:：]\s*(.*?)(?=\n\s*\*\*置信度|\n\s*\*\*严重度|\Z)",
        block,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    # '- **修复建议**: ...'
    match = re.search(
        r"\*\*修复建议\*\*[:：]\s*(.*?)(?=\n\s*-\s*\*\*|\n\s*\*\*置信|\Z)",
        block,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return ""


def _extract_confidence(block: str) -> float:
    """Extract confidence value from an issue block (table or bold field)."""
    # Table format: | **置信度** | HIGH |
    match = re.search(r"\*\*置信度\*\*[:：]?\s*\|\s*([^|\n]+)\|", block)
    if match:
        return _parse_confidence(match.group(1))
    # Bold field format
    match = re.search(r"\*\*置信度\*\*[:：]\s*(.*?)(?:\n|\Z)", block)
    if match:
        return _parse_confidence(match.group(1))
    return 0.5


def _extract_severity(block: str, confidence: float = 0.5) -> str:
    """Extract severity value from an issue block (table or keywords)."""
    # Table format: | **严重度** | 🔴 高 |
    match = re.search(r"\*\*严重度\*\*[:：]?\s*\|\s*([^|\n]+)\|", block)
    if match:
        return _parse_severity(match.group(1), confidence)
    return _parse_severity("", confidence)


def parse_findings_from_markdown(
    markdown: str,
    repo_url: str = "",
    function_name: str = "",
    task_id: str = "",
    file_path: str = "",
    scan_timestamp: str = "",
) -> list[Finding]:
    """
    Parse a markdown review report into a list of Finding objects.

    Args:
        markdown: The nga review output in Markdown.
        repo_url: Repository URL for stable ID generation.
        function_name: Optional override for function name.
        task_id: Optional override for task ID.
        file_path: Optional override for file path.
        scan_timestamp: Optional override for scan timestamp.

    Returns:
        List of Finding objects.
    """
    if not markdown:
        return []

    meta = _extract_metadata(markdown)
    file_path = file_path or meta.get("file_path", "")
    function_name = function_name or meta.get("function_name", "")
    task_id = task_id or meta.get("task_id", "")
    scan_timestamp = scan_timestamp or meta.get("scan_timestamp", "")

    findings: list[Finding] = []
    blocks = _split_into_issue_blocks(markdown)

    for block in blocks:
        first_line = block.split("\n")[0]
        # Support '[ISSUE-N] title', '问题 N — title', 'Bug: title', 'Missing ...', 'Dead code: ...'
        title_match = re.search(
            r"(?:\[ISSUE-\d+\]|问题\s*\d+[\s:—\-]+|Bug:|Missing\s|Dead\s+code:)(.*)",
            first_line,
        )
        title = title_match.group(1).strip() if title_match else first_line.lstrip("# ").strip()

        rule_id = _extract_rule_id(block, title)
        line_number = _extract_line_number(block)
        code_snippet = _extract_first_code_block(block)
        description = _extract_description(block)
        suggestion = _extract_suggestion(block)
        confidence = _extract_confidence(block)
        severity = _extract_severity(block, confidence)

        finding_id = generate_finding_id(
            repo_url=repo_url,
            file_path=file_path,
            function_name=function_name,
            rule_id=rule_id,
            snippet=code_snippet,
        )

        findings.append(
            Finding(
                finding_id=finding_id,
                file_path=file_path,
                line_number=line_number,
                rule_id=rule_id,
                severity=severity,
                description=description or title,
                code_snippet=code_snippet,
                suggestion=suggestion,
                confidence=confidence,
                function_name=function_name,
                scan_timestamp=scan_timestamp,
                task_id=task_id,
            )
        )

    return findings


def parse_findings_from_report_file(
    report_path: str | Path,
    repo_url: str = "",
) -> list[Finding]:
    """Convenience wrapper to parse findings directly from a report file."""
    path = Path(report_path)
    markdown = path.read_text(encoding="utf-8")
    return parse_findings_from_markdown(markdown, repo_url=repo_url)
