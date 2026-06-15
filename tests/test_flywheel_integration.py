"""Integration tests for the data flywheel feature."""

import tempfile
from pathlib import Path

from finding_parser import Finding, generate_finding_id
from finding_store import FindingStore


class FakeOrchestrator:
    """Minimal fake orchestrator to test flywheel integration patterns."""

    def __init__(self, db_path: Path):
        self.finding_store = FindingStore(str(db_path))
        self.repo_url = "https://github.com/test/repo"

    def _filter_known_false_positives(self, findings: list[Finding]) -> list[Finding]:
        if not self.finding_store:
            return findings

        filtered = []
        for finding in findings:
            stored = self.finding_store.get_finding(finding.finding_id)
            if stored and stored.label == "false_positive":
                continue
            filtered.append(finding)
        return filtered


def test_filter_known_false_positives():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        orch = FakeOrchestrator(db_path)

        snippet = "int x = 1;"
        fid = generate_finding_id("https://github.com/test/repo", "src/main.c", "main", "RULE-001", snippet)

        orch.finding_store.save_findings([
            Finding(
                finding_id=fid,
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="HIGH",
                description="A",
                code_snippet=snippet,
                suggestion="fix",
                confidence=0.9,
                function_name="main",
                label="false_positive",
            )
        ])

        new_findings = [
            Finding(
                finding_id=fid,
                file_path="src/main.c",
                line_number=12,  # line shifted
                rule_id="RULE-001",
                severity="HIGH",
                description="A again",
                code_snippet=snippet,
                suggestion="fix",
                confidence=0.9,
                function_name="main",
            )
        ]

        filtered = orch._filter_known_false_positives(new_findings)
        assert len(filtered) == 0


def test_memory_prompt_building():
    """Verify historical labels can be formatted into a memory section."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        store = FindingStore(str(db_path))

        store.save_findings([
            Finding(
                finding_id="fp1",
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="MEDIUM",
                description="This is a known false positive",
                code_snippet="x",
                suggestion="ignore",
                confidence=0.6,
                function_name="main",
                label="false_positive",
            ),
            Finding(
                finding_id="tp1",
                file_path="src/main.c",
                line_number=20,
                rule_id="RULE-002",
                severity="HIGH",
                description="This is a confirmed bug",
                code_snippet="y",
                suggestion="fix",
                confidence=0.9,
                function_name="main",
                label="true_positive",
            ),
        ])

        labels = store.get_historical_labels("src/main.c", "main")
        assert labels["fp1"] == "false_positive"
        assert labels["tp1"] == "true_positive"

        memory_lines = []
        false_positives = []
        true_positives = []
        for fid, label in labels.items():
            finding = store.get_finding(fid)
            assert finding is not None
            summary = f"- {finding.rule_id}: {finding.description[:80]}..."
            if label == "false_positive":
                false_positives.append(summary)
            else:
                true_positives.append(summary)

        assert any("RULE-001" in line for line in false_positives)
        assert any("RULE-002" in line for line in true_positives)
