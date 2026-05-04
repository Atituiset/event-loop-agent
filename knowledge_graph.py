"""
Knowledge Graph Manager - SQLite-based knowledge storage for code scanning.

Stores three types of knowledge nodes:
  - pattern: Reusable problem patterns (from wireless-radio.md, extracted from scans)
  - case: Concrete instances found in specific files/lines
  - file_profile: Risk profile for each scanned file

Phase 0: File-path exact matching for prompt injection.
Phase 3: Embedding-based fuzzy matching (future).
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class KnowledgeNode:
    """Base knowledge node"""
    id: str
    type: str           # "pattern" | "case" | "file_profile"
    content: str        # Natural language description
    source: str         # "manual" | "extracted_from_scan"
    confidence: float   # 0.0 ~ 1.0
    created_at: str
    updated_at: str
    metadata: str       # JSON string


@dataclass
class PatternNode:
    """Problem pattern that can apply across files"""
    id: str
    content: str
    rule_id: Optional[str] = None
    confidence: float = 0.8
    source: str = "manual"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CaseNode:
    """Concrete instance found during scanning"""
    id: str
    file_path: str
    line_number: int
    rule_id: str
    message: str
    code_snippet: str = ""
    confidence: float = 0.8
    fix_suggestion: str = ""
    status: str = "open"    # open | confirmed | false_positive | fixed
    scan_id: str = ""
    source: str = "extracted_from_scan"
    created_at: str = ""


@dataclass
class FileProfile:
    """Risk profile for a scanned file"""
    file_path: str
    total_issues: int = 0
    last_scan_at: str = ""
    risk_score: float = 0.0
    top_patterns: list[str] = field(default_factory=list)


# ============================================================================
# Knowledge Graph Manager
# ============================================================================

class KnowledgeGraph:
    """
    SQLite-backed knowledge graph for code scanning.

    Usage:
        kg = KnowledgeGraph(".claude/knowledge.db")
        kg.add_pattern("PATTERN-001", "TLV parsing lacks boundary check", "RULE-001")
        cases = kg.get_cases_by_file("src/rr/pdu.c")
        patterns = kg.find_relevant_patterns("src/rr/pdu.c", code_snippet="")
    """

    def __init__(self, db_path: str = ".claude/knowledge.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ------------------------------------------------------------------
    #  Phase 3: Lightweight embedding (keyword-based similarity)
    # ------------------------------------------------------------------

    _STOP_WORDS: set[str] = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "must", "shall", "can", "need", "dare",
        "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
        "from", "as", "into", "through", "during", "before", "after",
        "above", "below", "between", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all",
        "any", "both", "each", "few", "more", "most", "other", "some",
        "such", "no", "nor", "not", "only", "own", "same", "so", "than",
        "too", "very", "just", "and", "but", "if", "or", "because", "until",
        "while", "这", "那", "的", "了", "在", "是", "我", "有", "和",
        "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到",
        "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己",
        "这", "那", "为", "之", "与", "及", "等", "或", "但", "而",
        "因", "于", "则", "即", "乃", "若", "虽", "故", "既", "以",
    }

    def _extract_keywords(self, text: str) -> set[str]:
        """Extract keywords from text for similarity matching."""
        # Keep Chinese characters, English words, and technical terms
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|[一-鿿]{2,}", text.lower())
        return {w for w in words if w not in self._STOP_WORDS and len(w) > 2}

    def _text_similarity(self, text1: str, text2: str) -> float:
        """Compute Jaccard similarity between two texts (0~1)."""
        kw1 = self._extract_keywords(text1)
        kw2 = self._extract_keywords(text2)
        if not kw1 or not kw2:
            return 0.0
        intersection = kw1 & kw2
        union = kw1 | kw2
        return len(intersection) / len(union)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None or self._conn.total_changes < 0:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        """Create tables if not exist"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS patterns (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                rule_id TEXT,
                confidence REAL DEFAULT 0.8,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                line_number INTEGER,
                rule_id TEXT,
                message TEXT NOT NULL,
                code_snippet TEXT DEFAULT '',
                confidence REAL DEFAULT 0.8,
                fix_suggestion TEXT DEFAULT '',
                status TEXT DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'false_positive', 'fixed', 'suppressed')),
                scan_id TEXT DEFAULT '',
                source TEXT DEFAULT 'extracted_from_scan',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS file_profiles (
                file_path TEXT PRIMARY KEY,
                total_issues INTEGER DEFAULT 0,
                last_scan_at TEXT DEFAULT '',
                risk_score REAL DEFAULT 0.0,
                top_patterns TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT UNIQUE,
                commit_hash TEXT,
                branch TEXT,
                timestamp TEXT DEFAULT (datetime('now')),
                total_files INTEGER,
                issues_found INTEGER,
                duration REAL,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_cases_file_path ON cases(file_path);
            CREATE INDEX IF NOT EXISTS idx_cases_rule_id ON cases(rule_id);
            CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
            CREATE INDEX IF NOT EXISTS idx_patterns_rule_id ON patterns(rule_id);
        """)
        conn.commit()

    # ------------------------------------------------------------------
    #  Pattern Operations
    # ------------------------------------------------------------------

    def add_pattern(
        self,
        pattern_id: str,
        content: str,
        rule_id: Optional[str] = None,
        confidence: float = 0.8,
        source: str = "manual",
    ) -> str:
        """Add or update a pattern. Returns pattern_id."""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO patterns (id, content, rule_id, confidence, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                rule_id=excluded.rule_id,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
            """,
            (pattern_id, content, rule_id, confidence, source, now, now),
        )
        conn.commit()
        return pattern_id

    def get_pattern(self, pattern_id: str) -> Optional[PatternNode]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
        if not row:
            return None
        return PatternNode(
            id=row["id"],
            content=row["content"],
            rule_id=row["rule_id"],
            confidence=row["confidence"],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_all_patterns(self) -> list[PatternNode]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM patterns ORDER BY confidence DESC").fetchall()
        return [
            PatternNode(
                id=r["id"], content=r["content"], rule_id=r["rule_id"],
                confidence=r["confidence"], source=r["source"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def find_patterns_by_rule(self, rule_id: str) -> list[PatternNode]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM patterns WHERE rule_id = ? ORDER BY confidence DESC",
            (rule_id,),
        ).fetchall()
        return [
            PatternNode(
                id=r["id"], content=r["content"], rule_id=r["rule_id"],
                confidence=r["confidence"], source=r["source"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    #  Case Operations
    # ------------------------------------------------------------------

    def add_case(
        self,
        file_path: str,
        line_number: int,
        rule_id: str,
        message: str,
        code_snippet: str = "",
        confidence: float = 0.8,
        fix_suggestion: str = "",
        scan_id: str = "",
    ) -> int:
        """Add a case. Returns case id (auto-increment)."""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO cases
            (file_path, line_number, rule_id, message, code_snippet,
             confidence, fix_suggestion, scan_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_path, line_number, rule_id, message, code_snippet,
             confidence, fix_suggestion, scan_id, now),
        )
        conn.commit()
        return cursor.lastrowid

    def get_cases_by_file(
        self,
        file_path: str,
        status_filter: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[CaseNode]:
        """Get cases for a specific file."""
        conn = self._get_conn()
        sql = "SELECT * FROM cases WHERE file_path = ?"
        params: list = [file_path]
        if status_filter:
            placeholders = ",".join("?" for _ in status_filter)
            sql += f" AND status IN ({placeholders})"
            params.extend(status_filter)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [
            CaseNode(
                id=str(r["id"]), file_path=r["file_path"],
                line_number=r["line_number"] or 0, rule_id=r["rule_id"] or "",
                message=r["message"], code_snippet=r["code_snippet"] or "",
                confidence=r["confidence"], fix_suggestion=r["fix_suggestion"] or "",
                status=r["status"], scan_id=r["scan_id"] or "",
                source=r["source"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_open_cases_by_file(self, file_path: str, limit: int = 10) -> list[CaseNode]:
        """Get open (unresolved) cases for a file."""
        return self.get_cases_by_file(file_path, status_filter=["open"], limit=limit)

    def mark_false_positive(self, case_id: int, reason: str = "") -> bool:
        """Mark a case as false positive and lower pattern confidence."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE cases SET status = 'false_positive' WHERE id = ?",
            (case_id,),
        )
        conn.commit()
        return True

    def mark_confirmed(self, case_id: int) -> bool:
        """Mark a case as confirmed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE cases SET status = 'confirmed' WHERE id = ?",
            (case_id,),
        )
        conn.commit()
        return True

    # ------------------------------------------------------------------
    #  File Profile Operations
    # ------------------------------------------------------------------

    def update_file_profile(self, file_path: str, new_issues: int = 0) -> FileProfile:
        """Update file profile after a scan."""
        conn = self._get_conn()
        now = datetime.now().isoformat()

        # Get current profile
        row = conn.execute(
            "SELECT * FROM file_profiles WHERE file_path = ?", (file_path,)
        ).fetchone()

        if row:
            total = row["total_issues"] + new_issues
        else:
            total = new_issues

        # Get top patterns for this file
        top = conn.execute(
            """
            SELECT rule_id, COUNT(*) as cnt FROM cases
            WHERE file_path = ? AND status != 'false_positive'
            GROUP BY rule_id ORDER BY cnt DESC LIMIT 5
            """,
            (file_path,),
        ).fetchall()
        top_patterns = [r["rule_id"] for r in top if r["rule_id"]]

        # Simple risk score: total issues * 0.1, capped at 10
        risk_score = min(total * 0.1, 10.0)

        conn.execute(
            """
            INSERT INTO file_profiles (file_path, total_issues, last_scan_at, risk_score, top_patterns)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                total_issues=excluded.total_issues,
                last_scan_at=excluded.last_scan_at,
                risk_score=excluded.risk_score,
                top_patterns=excluded.top_patterns
            """,
            (file_path, total, now, risk_score, json.dumps(top_patterns)),
        )
        conn.commit()

        return FileProfile(
            file_path=file_path,
            total_issues=total,
            last_scan_at=now,
            risk_score=risk_score,
            top_patterns=top_patterns,
        )

    def get_file_profile(self, file_path: str) -> Optional[FileProfile]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM file_profiles WHERE file_path = ?", (file_path,)
        ).fetchone()
        if not row:
            return None
        return FileProfile(
            file_path=row["file_path"],
            total_issues=row["total_issues"],
            last_scan_at=row["last_scan_at"],
            risk_score=row["risk_score"],
            top_patterns=json.loads(row["top_patterns"] or "[]"),
        )

    # ------------------------------------------------------------------
    #  Scan Run Operations
    # ------------------------------------------------------------------

    def record_scan_run(
        self,
        run_id: str,
        total_files: int,
        issues_found: int,
        duration: float,
        commit_hash: str = "",
        branch: str = "",
        metadata: dict = None,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO scan_runs (run_id, commit_hash, branch, total_files, issues_found, duration, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, commit_hash, branch, total_files, issues_found, duration,
             json.dumps(metadata or {})),
        )
        conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    #  Prompt Injection Helpers (Phase 0)
    # ------------------------------------------------------------------

    def find_relevant_patterns(
        self,
        file_path: str,
        code_snippet: str = "",
        top_k: int = 5,
        min_confidence: float = 0.5,
    ) -> list[PatternNode]:
        """
        Find relevant patterns for a file.
        Phase 0: Exact file-path matching + rule_id frequency from file profile.
        Phase 3: Embedding-based fuzzy matching.
        """
        conn = self._get_conn()
        results: list[PatternNode] = []

        # Strategy 1: Get top patterns from file profile
        profile = self.get_file_profile(file_path)
        if profile and profile.top_patterns:
            for rule_id in profile.top_patterns[:top_k]:
                patterns = self.find_patterns_by_rule(rule_id)
                for p in patterns:
                    if p.confidence >= min_confidence and p not in results:
                        results.append(p)

        # Strategy 2: Get patterns associated with open cases for this file
        if len(results) < top_k:
            cases = self.get_open_cases_by_file(file_path, limit=top_k * 2)
            for case in cases:
                patterns = self.find_patterns_by_rule(case.rule_id)
                for p in patterns:
                    if p.confidence >= min_confidence and p not in results:
                        results.append(p)
                if len(results) >= top_k:
                    break

        # Strategy 3: Embedding-based fuzzy matching (Phase 3)
        if len(results) < top_k and code_snippet:
            all_patterns = self.get_all_patterns()
            scored = []
            for p in all_patterns:
                if p.confidence < min_confidence or p in results:
                    continue
                sim = self._text_similarity(code_snippet, p.content)
                if sim > 0.1:  # Minimum similarity threshold
                    scored.append((sim, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            for sim, p in scored[:top_k - len(results)]:
                results.append(p)

        # Strategy 4: Fallback to all high-confidence patterns
        if not results:
            rows = conn.execute(
                "SELECT * FROM patterns WHERE confidence >= ? ORDER BY confidence DESC LIMIT ?",
                (min_confidence, top_k),
            ).fetchall()
            results = [
                PatternNode(
                    id=r["id"], content=r["content"], rule_id=r["rule_id"],
                    confidence=r["confidence"], source=r["source"],
                    created_at=r["created_at"], updated_at=r["updated_at"],
                )
                for r in rows
            ]

        return results[:top_k]

    def get_last_scan_issues(
        self,
        file_path: str,
        limit: int = 5,
    ) -> list[CaseNode]:
        """Get open issues from the last scan of this file."""
        return self.get_open_cases_by_file(file_path, limit=limit)

    # ------------------------------------------------------------------
    #  Batch Import
    # ------------------------------------------------------------------

    def import_from_wireless_radio_md(self, md_path: str) -> int:
        """
        Parse wireless-radio.md and import as Pattern nodes.
        Returns number of patterns imported.
        """
        content = Path(md_path).read_text(encoding="utf-8")
        count = 0

        # Map section titles to rule IDs
        rule_map = {
            "TLV": ("RULE-001", "TLV parsing lacks boundary check (remaining_len validation before pointer offset)"),
            "结构体强转": ("RULE-002", "Struct cast memory safety (sizeof validation before memcpy/cast)"),
            "Switch-Case": ("RULE-003", "Switch-Case missing safe default branch for unhandled message types"),
            "ASN.1": ("RULE-004", "ASN.1 Optional field access without presence check or NULL check"),
        }

        for keyword, (rule_id, desc) in rule_map.items():
            pattern_id = f"PATTERN-{rule_id.split('-')[1]}"
            self.add_pattern(pattern_id, desc, rule_id=rule_id, confidence=0.9, source="manual")
            count += 1

        # Also import the general concepts as a meta-pattern
        self.add_pattern(
            "PATTERN-META-001",
            "High-low version message compatibility issues in wireless communication systems: "
            "TLV boundary checks, struct cast safety, switch-case defaults, ASN.1 optional fields",
            rule_id=None,
            confidence=0.85,
            source="manual",
        )
        count += 1

        return count

    # ------------------------------------------------------------------
    #  Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return database statistics."""
        conn = self._get_conn()
        patterns = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
        cases = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM file_profiles").fetchone()[0]
        scans = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        return {
            "patterns": patterns,
            "cases": cases,
            "file_profiles": files,
            "scan_runs": scans,
        }

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
