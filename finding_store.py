"""
Finding Store Module

SQLite-based persistence for scan findings and user feedback labels.
Designed for the data flywheel: labeled findings become memory for future scans.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from finding_parser import Finding


class FindingStore:
    """SQLite store for findings and user feedback."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables and indexes if they do not exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    line_number INTEGER,
                    rule_id TEXT,
                    severity TEXT,
                    description TEXT,
                    code_snippet TEXT,
                    suggestion TEXT,
                    confidence REAL,
                    function_name TEXT,
                    scan_timestamp TEXT,
                    mr_link TEXT,
                    task_id TEXT,
                    log_file TEXT,
                    label TEXT,
                    labeled_by TEXT,
                    labeled_at TEXT,
                    label_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file_path, function_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_findings_label ON findings(label)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_sessions (
                    session_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    repo_url TEXT,
                    mr_link TEXT,
                    total_files INTEGER,
                    total_findings INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _row_to_finding(self, row: sqlite3.Row) -> Finding:
        """Convert a SQLite row to a Finding dataclass."""
        data = dict(row)
        return Finding.from_dict(data)

    def save_findings(self, findings: list[Finding]):
        """Batch insert or replace findings."""
        if not findings:
            return

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for finding in findings:
                data = finding.to_dict()
                columns = ", ".join(data.keys())
                placeholders = ", ".join([f":{k}" for k in data.keys()])
                sql = f"""
                    INSERT INTO findings ({columns})
                    VALUES ({placeholders})
                    ON CONFLICT(finding_id) DO UPDATE SET
                        file_path=excluded.file_path,
                        line_number=excluded.line_number,
                        rule_id=excluded.rule_id,
                        severity=excluded.severity,
                        description=excluded.description,
                        code_snippet=excluded.code_snippet,
                        suggestion=excluded.suggestion,
                        confidence=excluded.confidence,
                        function_name=excluded.function_name,
                        scan_timestamp=excluded.scan_timestamp,
                        mr_link=excluded.mr_link,
                        task_id=excluded.task_id,
                        log_file=excluded.log_file
                """
                conn.execute(sql, data)

    def get_finding(self, finding_id: str) -> Optional[Finding]:
        """Retrieve a single finding by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
            return self._row_to_finding(row) if row else None

    def get_all_findings(self) -> list[Finding]:
        """Get all findings in the store."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM findings").fetchall()
            return [self._row_to_finding(r) for r in rows]

    def get_findings_for_file(
        self,
        file_path: str,
        function_name: Optional[str] = None,
    ) -> list[Finding]:
        """Get all findings for a specific file, optionally filtered by function."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if function_name:
                rows = conn.execute(
                    "SELECT * FROM findings WHERE file_path = ? AND function_name = ?",
                    (file_path, function_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM findings WHERE file_path = ?", (file_path,)
                ).fetchall()
            return [self._row_to_finding(r) for r in rows]

    def get_historical_labels(
        self,
        file_path: str,
        function_name: str = "",
    ) -> dict[str, str]:
        """
        Return historical labels for a file/function as {finding_id: label}.
        Used by the orchestrator to inject memory into prompts.
        """
        findings = self.get_findings_for_file(file_path, function_name or None)
        return {
            f.finding_id: f.label
            for f in findings
            if f.label in ("true_positive", "false_positive")
        }

    def update_label(
        self,
        finding_id: str,
        label: str,
        labeled_by: str = "",
        label_reason: str = "",
        labeled_at: Optional[str] = None,
    ):
        """Update the label of a finding."""
        from datetime import datetime, timezone

        if labeled_at is None:
            labeled_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE findings
                SET label = ?, labeled_by = ?, labeled_at = ?, label_reason = ?
                WHERE finding_id = ?
                """,
                (label, labeled_by, labeled_at, label_reason, finding_id),
            )

    def get_feedback_stats(self) -> dict[str, Any]:
        """Return feedback statistics for dashboard."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM findings"
            ).fetchone()[0]
            true_positives = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE label = ?", ("true_positive",)
            ).fetchone()[0]
            false_positives = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE label = ?", ("false_positive",)
            ).fetchone()[0]
            unlabeled = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE label IS NULL"
            ).fetchone()[0]

        return {
            "total_findings": total,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "unlabeled": unlabeled,
        }

    def save_session(
        self,
        session_id: str,
        timestamp: str,
        repo_url: str = "",
        mr_link: str = "",
        total_files: int = 0,
        total_findings: int = 0,
    ):
        """Record a scan session."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scan_sessions
                (session_id, timestamp, repo_url, mr_link, total_files, total_findings)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, timestamp, repo_url, mr_link, total_files, total_findings),
            )
