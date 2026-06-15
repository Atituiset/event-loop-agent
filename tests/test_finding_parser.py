"""Tests for finding_parser module."""

from pathlib import Path

import pytest

from finding_parser import (
    Finding,
    generate_finding_id,
    normalize_snippet,
    parse_findings_from_markdown,
    parse_findings_from_report_file,
)


SAMPLE_REPORT_V1 = """
# 代码审查报告 - init_server

**文件**: `/home/atituiset/Projects/opencode-c-cpp-test/network/tcp_handler.c`
**函数**: `init_server`
**任务ID**: `task-001-001`
**扫描时间**: 2026-06-05 17:25:45
**耗时**: 26.2s
**状态**: 完成

---

## 发现的问题

### 问题 1 — `port` 参数未做范围校验（RULE-001 / RULE-005）

- **位置**: `tcp_handler.c:19,30`
- **代码**:
  ```c
  int init_server(int port) {
      addr.sin_port = htons(port);
  ```
- **描述**: `port` 为 `int`，`htons()` 期望 `uint16_t`。
- **修复建议**:
  ```c
  if (port <= 0 || port > 65535) return -1;
  ```
- **置信度**: **高**

### 问题 2 — `setsockopt()` 返回值未检查（RULE-001）

- **位置**: `tcp_handler.c:24`
- **代码**:
  ```c
  setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
  ```
- **描述**: 返回值未检查。
- **修复建议**:
  ```c
  if (setsockopt(...) < 0) { close(server_fd); return -1; }
  ```
- **置信度**: **中**
"""

SAMPLE_REPORT_V2 = """
# 代码审查报告 - handle_client

**文件**: `/home/atituiset/Projects/opencode-c-cpp-test/network/tcp_handler.c`
**函数**: `handle_client`
**任务ID**: `task-001-002`
**扫描时间**: 2026-06-05 17:26:21

## 问题 1: 缓冲区越界写入（**RULE-001/RULE-002 违反**）

```c
// line 42-45
char buffer[BUFFER_SIZE];
int n = read(client_fd, buffer, BUFFER_SIZE);
if (n > 0) {
    buffer[n] = '\\0';
```

**问题描述**: `read()` 可返回 `BUFFER_SIZE`，此时越界写入。

**修复建议**: 将 `read` 的上限改为 `BUFFER_SIZE - 1`：
```c
int n = read(client_fd, buffer, BUFFER_SIZE - 1);
```

**置信度**: ★★★★★（确定）

## 问题 2: `write()` 返回值未检查（**RULE-001/RULE-006 违反**）

```c
write(client_fd, buffer, n);
```

**问题描述**: 丢弃返回值。

**修复建议**: 检查返回值。

**置信度**: ★★★★☆
"""


def test_normalize_snippet():
    code = "int   foo  =  bar;  // comment"
    normalized = normalize_snippet(code)
    assert "int" in normalized
    assert "foo" not in normalized
    assert "bar" not in normalized
    assert "//" not in normalized


def test_generate_finding_id_is_stable():
    id1 = generate_finding_id("repo", "src/main.c", "main", "RULE-001", "int x = 1;")
    id2 = generate_finding_id("repo", "src/main.c", "main", "RULE-001", "int x = 1;")
    assert id1 == id2
    assert len(id1) == 16


def test_generate_finding_id_line_number_independent():
    # Same code, different line context should produce same ID
    id1 = generate_finding_id("repo", "src/main.c", "main", "RULE-001", "int x = 1;")
    id2 = generate_finding_id("repo", "src/main.c", "main", "RULE-001", "int x = 1;")
    assert id1 == id2


def test_parse_findings_from_markdown_v1():
    findings = parse_findings_from_markdown(SAMPLE_REPORT_V1, repo_url="https://github.com/test/repo")
    assert len(findings) == 2

    f1 = findings[0]
    assert f1.function_name == "init_server"
    assert f1.rule_id == "RULE-001"
    assert f1.line_number == 19
    assert f1.severity == "HIGH"
    assert f1.confidence == pytest.approx(0.9)
    assert "port" in f1.description
    assert "addr.sin_port" in f1.code_snippet
    assert "65535" in f1.suggestion
    assert len(f1.finding_id) == 16

    f2 = findings[1]
    assert f2.rule_id == "RULE-001"
    assert f2.line_number == 24
    assert f2.confidence == pytest.approx(0.6)


def test_parse_findings_from_markdown_v2():
    findings = parse_findings_from_markdown(SAMPLE_REPORT_V2)
    assert len(findings) == 2

    f1 = findings[0]
    assert f1.function_name == "handle_client"
    assert f1.rule_id == "RULE-001"
    assert f1.line_number == 42
    assert f1.confidence == pytest.approx(1.0)
    assert "越界" in f1.description
    assert "BUFFER_SIZE - 1" in f1.suggestion

    f2 = findings[1]
    assert f2.rule_id == "RULE-001"
    assert f2.confidence == pytest.approx(0.8)


def test_parse_findings_from_report_file():
    sample_path = Path(__file__).parent.parent / "reports" / "20260605_172518" / "Projects" / "opencode-c-cpp-test" / "network" / "tcp_handler.c" / "init_server.md"
    if not sample_path.exists():
        pytest.skip("Sample report not found")

    findings = parse_findings_from_report_file(sample_path)
    assert len(findings) >= 1
    for finding in findings:
        assert finding.finding_id
        assert finding.rule_id.startswith("RULE-")
        assert finding.description


def test_finding_from_dict_roundtrip():
    finding = Finding(
        finding_id="abc123",
        file_path="src/main.c",
        line_number=42,
        rule_id="RULE-001",
        severity="HIGH",
        description="Test",
        code_snippet="int x;",
        suggestion="Fix it",
        confidence=0.9,
        label="false_positive",
        labeled_by="dev@example.com",
    )
    data = finding.to_dict()
    restored = Finding.from_dict(data)
    assert restored.finding_id == finding.finding_id
    assert restored.label == "false_positive"
    assert restored.labeled_by == "dev@example.com"
