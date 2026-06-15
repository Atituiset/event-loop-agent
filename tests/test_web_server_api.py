"""Tests for web_server feedback API."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencode_agent.findings.parser import Finding
from opencode_agent.findings.store import FindingStore

# Import web_server after setting env var if needed
from opencode_agent.web.server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_api_returns_empty_when_no_store(client):
    """If OPENCODE_FINDINGS_DB is not set, /api/findings returns empty list."""
    # Ensure env var is not set
    old_db = os.environ.pop("OPENCODE_FINDINGS_DB", None)
    try:
        # Reset store cache
        import opencode_agent.web.server as web_server

        web_server._finding_stores = {}
        web_server._default_db_path = None

        resp = client.get("/api/findings")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        if old_db:
            os.environ["OPENCODE_FINDINGS_DB"] = old_db


def test_api_get_findings_with_store(client):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        store = FindingStore(str(db_path))
        store.save_findings([
            Finding(
                finding_id="f1",
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="HIGH",
                description="Test",
                code_snippet="int x;",
                suggestion="fix",
                confidence=0.9,
                function_name="main",
            )
        ])

        # Reset store cache and set env var
        import opencode_agent.web.server as web_server

        web_server._finding_stores = {}
        web_server._default_db_path = str(db_path)

        try:
            resp = client.get("/api/findings?file_path=src/main.c")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["finding_id"] == "f1"
        finally:
            web_server._finding_stores = {}


def test_api_label_finding(client):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        store = FindingStore(str(db_path))
        store.save_findings([
            Finding(
                finding_id="f1",
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="HIGH",
                description="Test",
                code_snippet="int x;",
                suggestion="fix",
                confidence=0.9,
            )
        ])

        import opencode_agent.web.server as web_server

        web_server._finding_stores = {}
        web_server._default_db_path = str(db_path)

        try:
            resp = client.post(
                "/api/findings/f1/label",
                json={"label": "false_positive", "reason": "Not a bug", "labeled_by": "dev"},
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            finding = store.get_finding("f1")
            assert finding.label == "false_positive"
            assert finding.label_reason == "Not a bug"
            assert finding.labeled_by == "dev"
        finally:
            web_server._finding_stores = {}


def test_api_label_finding_invalid_label(client):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "findings.db"
        store = FindingStore(str(db_path))
        store.save_findings([
            Finding(
                finding_id="f1",
                file_path="src/main.c",
                line_number=10,
                rule_id="RULE-001",
                severity="HIGH",
                description="Test",
                code_snippet="int x;",
                suggestion="fix",
                confidence=0.9,
            )
        ])

        import opencode_agent.web.server as web_server

        web_server._finding_stores = {}
        web_server._default_db_path = str(db_path)

        try:
            resp = client.post(
                "/api/findings/f1/label",
                json={"label": "invalid"},
            )
            assert resp.status_code == 400
        finally:
            web_server._finding_stores = {}
