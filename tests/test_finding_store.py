"""Tests for finding_store module."""

import tempfile
from pathlib import Path

import pytest

from finding_parser import Finding, generate_finding_id
from finding_store import FindingStore


def test_init_creates_database():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        store = FindingStore(str(db_path))
        assert db_path.exists()


def test_save_and_retrieve_finding():
    with tempfile.TemporaryDirectory() as tmp:
        store = FindingStore(str(Path(tmp) / "findings.db"))

        finding = Finding(
            finding_id="abc123",
            file_path="src/main.c",
            line_number=42,
            rule_id="RULE-001",
            severity="HIGH",
            description="Test finding",
            code_snippet="int x;",
            suggestion="Fix it",
            confidence=0.9,
            function_name="main",
            scan_timestamp="2026-06-15T10:00:00",
        )

        store.save_findings([finding])
        retrieved = store.get_finding("abc123")
        assert retrieved is not None
        assert retrieved.finding_id == "abc123"
        assert retrieved.description == "Test finding"


def test_update_label():
    with tempfile.TemporaryDirectory() as tmp:
        store = FindingStore(str(Path(tmp) / "findings.db"))

        finding = Finding(
            finding_id="abc123",
            file_path="src/main.c",
            line_number=42,
            rule_id="RULE-001",
            severity="HIGH",
            description="Test",
            code_snippet="int x;",
            suggestion="Fix",
            confidence=0.9,
        )
        store.save_findings([finding])

        store.update_label(
            "abc123",
            "false_positive",
            labeled_by="dev@example.com",
            label_reason="Not a real bug",
        )

        updated = store.get_finding("abc123")
        assert updated.label == "false_positive"
        assert updated.labeled_by == "dev@example.com"
        assert updated.label_reason == "Not a real bug"
        assert updated.labeled_at is not None


def test_get_historical_labels():
    with tempfile.TemporaryDirectory() as tmp:
        store = FindingStore(str(Path(tmp) / "findings.db"))

        fid1 = generate_finding_id("repo", "src/main.c", "main", "RULE-001", "int a;")
        fid2 = generate_finding_id("repo", "src/main.c", "main", "RULE-002", "int b;")

        store.save_findings([
            Finding(
                finding_id=fid1,
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="HIGH",
                description="A",
                code_snippet="int a;",
                suggestion="Fix A",
                confidence=0.9,
                function_name="main",
                label="false_positive",
            ),
            Finding(
                finding_id=fid2,
                file_path="src/main.c",
                line_number=20,
                rule_id="RULE-002",
                severity="MEDIUM",
                description="B",
                code_snippet="int b;",
                suggestion="Fix B",
                confidence=0.7,
                function_name="main",
                label="true_positive",
            ),
        ])

        labels = store.get_historical_labels("src/main.c", "main")
        assert labels[fid1] == "false_positive"
        assert labels[fid2] == "true_positive"


def test_feedback_stats():
    with tempfile.TemporaryDirectory() as tmp:
        store = FindingStore(str(Path(tmp) / "findings.db"))

        store.save_findings([
            Finding(
                finding_id="f1",
                file_path="a.c",
                line_number=1,
                rule_id="RULE-001",
                severity="HIGH",
                description="A",
                code_snippet="x",
                suggestion="fix",
                confidence=0.9,
                label="true_positive",
            ),
            Finding(
                finding_id="f2",
                file_path="a.c",
                line_number=2,
                rule_id="RULE-002",
                severity="MEDIUM",
                description="B",
                code_snippet="y",
                suggestion="fix",
                confidence=0.6,
                label="false_positive",
            ),
            Finding(
                finding_id="f3",
                file_path="a.c",
                line_number=3,
                rule_id="RULE-003",
                severity="LOW",
                description="C",
                code_snippet="z",
                suggestion="fix",
                confidence=0.5,
            ),
        ])

        stats = store.get_feedback_stats()
        assert stats["total_findings"] == 3
        assert stats["true_positives"] == 1
        assert stats["false_positives"] == 1
        assert stats["unlabeled"] == 1
