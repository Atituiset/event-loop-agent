"""
OpenCode Orchestrator Web Debug Server

FastAPI application providing:
  - Frontend page with 3 terminal-style windows for real-time NGA output
  - SSE streams /sse/{slot_id} for each concurrent slot
  - HTTP POST APIs for the orchestrator to push logs and manage slots

Launched by orchestrator.py when --debug is enabled (via gunicorn).
"""

import asyncio
import json
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="OpenCode Orchestrator Debug")

# Number of concurrent NGA slots (matches orchestrator semaphore)
NUM_SLOTS: int = 3

# Each slot has an asyncio.Queue for SSE streaming
_slots: list[dict] = [
    {
        "queue": asyncio.Queue(),
        "task_id": None,
        "file_path": None,
        "status": "waiting",  # waiting | running | done | failed
    }
    for _ in range(NUM_SLOTS)
]


# =============================================================================
# API Endpoints for Orchestrator
# =============================================================================

@app.post("/api/slot/{slot_id}/acquire")
async def api_slot_acquire(slot_id: int, payload: dict):
    """Orchestrator calls this when a task claims a slot."""
    _slots[slot_id]["task_id"] = payload.get("task_id")
    _slots[slot_id]["file_path"] = payload.get("file_path")
    _slots[slot_id]["status"] = "running"
    await _slots[slot_id]["queue"].put({
        "type": "meta",
        "event": "acquire",
        "task_id": payload.get("task_id"),
        "file_path": payload.get("file_path"),
        "slot": slot_id,
    })
    return {"ok": True}


@app.post("/api/slot/{slot_id}/push")
async def api_slot_push(slot_id: int, payload: dict):
    """Orchestrator pushes a raw log chunk (may contain ANSI sequences)."""
    await _slots[slot_id]["queue"].put({
        "type": payload.get("log_type", "stdout"),
        "content": payload.get("content", ""),
        "slot": slot_id,
    })
    return {"ok": True}


@app.post("/api/slot/{slot_id}/status")
async def api_slot_status(slot_id: int, payload: dict):
    """Update slot status (done / failed)."""
    _slots[slot_id]["status"] = payload.get("status", "running")
    await _slots[slot_id]["queue"].put({
        "type": "meta",
        "event": "status",
        "status": payload.get("status"),
        "duration": payload.get("duration"),
        "slot": slot_id,
    })
    return {"ok": True}


@app.post("/api/slot/{slot_id}/release")
async def api_slot_release(slot_id: int):
    """Orchestrator calls this when a task finishes and the slot is freed."""
    _slots[slot_id]["task_id"] = None
    _slots[slot_id]["file_path"] = None
    _slots[slot_id]["status"] = "waiting"
    await _slots[slot_id]["queue"].put({
        "type": "meta",
        "event": "release",
        "slot": slot_id,
    })
    return {"ok": True}


# =============================================================================
# SSE Endpoints for Browser
# =============================================================================

@app.get("/sse/{slot_id}")
async def sse_stream(slot_id: int):
    """Server-Sent Events stream for a given slot."""
    async def event_generator():
        queue: asyncio.Queue = _slots[slot_id]["queue"]
        while True:
            msg = await queue.get()
            yield f"data: {json.dumps(msg)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# =============================================================================
# Finding Feedback API (Data Flywheel)
# =============================================================================

class LabelRequest(BaseModel):
    label: str  # "true_positive" | "false_positive"
    reason: str = ""
    labeled_by: str = ""


class FindingResponse(BaseModel):
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
    label: Optional[str] = None
    label_reason: Optional[str] = None


_finding_stores: dict[str, "FindingStore"] = {}  # cache by db_path
_default_db_path: Optional[str] = os.environ.get("OPENCODE_FINDINGS_DB")


def _get_finding_store(db_path: Optional[str] = None) -> Optional["FindingStore"]:
    """Get or create FindingStore for the given db path (or default env var)."""
    target = db_path or _default_db_path
    if not target:
        return None

    if target in _finding_stores:
        return _finding_stores[target]

    try:
        from finding_store import FindingStore

        store = FindingStore(target)
        _finding_stores[target] = store
        return store
    except Exception as e:
        import logging

        logging.getLogger("web_server").warning(f"Failed to load finding store {target}: {e}")
        return None


def _finding_to_response(finding: "Finding") -> dict[str, Any]:
    return finding.to_dict()


@app.get("/api/findings")
async def api_get_findings(
    db_path: Optional[str] = None,
    file_path: Optional[str] = None,
    function_name: Optional[str] = None,
    rule_id: Optional[str] = None,
):
    """Get findings, optionally filtered by file, function, or rule."""
    store = _get_finding_store(db_path)
    if store is None:
        return []

    if file_path:
        findings = store.get_findings_for_file(file_path, function_name or None)
    else:
        findings = store.get_all_findings()
        if function_name:
            findings = [f for f in findings if f.function_name == function_name]

    if rule_id:
        findings = [f for f in findings if f.rule_id == rule_id]
    return [_finding_to_response(f) for f in findings]


@app.get("/api/findings/{finding_id}")
async def api_get_finding(finding_id: str, db_path: Optional[str] = None):
    """Get a single finding by ID."""
    store = _get_finding_store(db_path)
    if store is None:
        raise HTTPException(status_code=503, detail="Finding store not configured")

    finding = store.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return _finding_to_response(finding)


@app.post("/api/findings/{finding_id}/label")
async def api_label_finding(
    finding_id: str,
    req: LabelRequest,
    db_path: Optional[str] = None,
):
    """Label a finding as true_positive or false_positive."""
    store = _get_finding_store(db_path)
    if store is None:
        raise HTTPException(status_code=503, detail="Finding store not configured")

    if req.label not in ("true_positive", "false_positive"):
        raise HTTPException(
            status_code=400,
            detail="label must be 'true_positive' or 'false_positive'",
        )

    finding = store.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    store.update_label(
        finding_id,
        label=req.label,
        labeled_by=req.labeled_by,
        label_reason=req.reason,
    )
    return {"ok": True, "finding_id": finding_id, "label": req.label}


@app.get("/api/stats")
async def api_get_stats(db_path: Optional[str] = None):
    """Return feedback statistics."""
    store = _get_finding_store(db_path)
    if store is None:
        return {"total_findings": 0, "true_positives": 0, "false_positives": 0, "unlabeled": 0}
    return store.get_feedback_stats()


# =============================================================================
# Frontend Page
# =============================================================================

_FRONTEND_HTML: str = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenCode Orchestrator Debug</title>
    <script src="https://cdn.jsdelivr.net/npm/ansi_up@5.1.0/ansi_up.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: #0d1117;
            color: #c9d1d9;
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, monospace;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .header {
            padding: 8px 16px;
            background: #161b22;
            border-bottom: 1px solid #30363d;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .header h1 {
            font-size: 14px;
            font-weight: 600;
            color: #58a6ff;
        }
        .header .status {
            font-size: 12px;
            color: #8b949e;
        }
        .terminals {
            display: flex;
            flex: 1;
            gap: 6px;
            padding: 6px;
            overflow: hidden;
        }
        .terminal {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: #010409;
            border: 1px solid #30363d;
            border-radius: 6px;
            overflow: hidden;
            min-width: 0;
        }
        .term-header {
            padding: 6px 10px;
            background: #21262d;
            border-bottom: 1px solid #30363d;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .term-header .slot-label {
            font-size: 11px;
            font-weight: 700;
            color: #8b949e;
            padding: 1px 6px;
            background: #30363d;
            border-radius: 3px;
        }
        .term-header .task-info {
            font-size: 11px;
            color: #c9d1d9;
            margin-left: 8px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            flex: 1;
        }
        .term-header .task-status {
            font-size: 10px;
            font-weight: 600;
            padding: 1px 6px;
            border-radius: 3px;
            text-transform: uppercase;
            flex-shrink: 0;
        }
        .status-waiting { color: #8b949e; }
        .status-running { color: #3fb950; background: rgba(63,185,80,0.15); }
        .status-done    { color: #58a6ff; background: rgba(88,166,255,0.15); }
        .status-failed  { color: #f85149; background: rgba(248,81,73,0.15); }

        .term-body {
            flex: 1;
            padding: 8px 10px;
            overflow-y: auto;
            font-size: 12px;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-all;
            color: #e6edf3;
        }
        .term-body::-webkit-scrollbar { width: 6px; }
        .term-body::-webkit-scrollbar-track { background: transparent; }
        .term-body::-webkit-scrollbar-thumb { background: #484f58; border-radius: 3px; }

        /* ANSI color overrides for dark terminal */
        .term-body .ansi-black { color: #484f58; }
        .term-body .ansi-red { color: #ff7b72; }
        .term-body .ansi-green { color: #3fb950; }
        .term-body .ansi-yellow { color: #d29922; }
        .term-body .ansi-blue { color: #58a6ff; }
        .term-body .ansi-magenta { color: #bc8cff; }
        .term-body .ansi-cyan { color: #39c5cf; }
        .term-body .ansi-white { color: #e6edf3; }

        .stats-bar {
            padding: 4px 16px;
            background: #161b22;
            border-top: 1px solid #30363d;
            font-size: 11px;
            color: #8b949e;
            display: flex;
            gap: 20px;
            flex-shrink: 0;
        }
        .stats-bar span { display: flex; align-items: center; gap: 4px; }
        .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
        .dot-green { background: #3fb950; }
        .dot-gray { background: #484f58; }
    </style>
</head>
<body>
    <div class="header">
        <h1>OpenCode Orchestrator Debug</h1>
        <span class="status">Real-time NGA stream via SSE</span>
    </div>
    <div class="terminals" id="terminals">
        <!-- 3 terminals generated by JS -->
    </div>
    <div class="stats-bar">
        <span><span class="dot dot-green"></span> Connected</span>
        <span id="stat-connections">3 SSE connections active</span>
        <span id="stat-uptime">Uptime: 0s</span>
    </div>

    <script>
        const NUM_SLOTS = 3;
        const ansiUp = new AnsiUp();
        ansiUp.use_classes = true;

        const terminals = [];
        const esList = [];

        // Create terminal elements
        const container = document.getElementById('terminals');
        for (let i = 0; i < NUM_SLOTS; i++) {
            const term = document.createElement('div');
            term.className = 'terminal';
            term.innerHTML = `
                <div class="term-header">
                    <span class="slot-label">SLOT #${i}</span>
                    <span class="task-info" id="task-info-${i}">Waiting for task...</span>
                    <span class="task-status status-waiting" id="task-status-${i}">waiting</span>
                </div>
                <div class="term-body" id="term-body-${i}"></div>
            `;
            container.appendChild(term);
            terminals.push({
                body: document.getElementById(`term-body-${i}`),
                info: document.getElementById(`task-info-${i}`),
                status: document.getElementById(`task-status-${i}`),
            });
        }

        // Connect SSE for each slot
        let activeConnections = 0;
        for (let slotId = 0; slotId < NUM_SLOTS; slotId++) {
            const es = new EventSource(`/sse/${slotId}`);
            esList.push(es);

            es.onopen = () => {
                activeConnections++;
                updateStats();
            };

            es.onerror = (e) => {
                console.error(`SSE error for slot ${slotId}:`, e);
            };

            es.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
                    handleMessage(slotId, msg);
                } catch (err) {
                    console.error('Failed to parse SSE message:', err);
                }
            };
        }

        function handleMessage(slotId, msg) {
            const t = terminals[slotId];

            if (msg.type === 'meta') {
                if (msg.event === 'acquire') {
                    t.info.textContent = `${msg.task_id}: ${msg.file_path}`;
                    t.info.title = msg.file_path;
                    t.status.textContent = 'running';
                    t.status.className = 'task-status status-running';
                } else if (msg.event === 'status') {
                    t.status.textContent = msg.status;
                    t.status.className = `task-status status-${msg.status}`;
                } else if (msg.event === 'release') {
                    t.info.textContent = 'Waiting for task...';
                    t.info.title = '';
                    t.status.textContent = 'waiting';
                    t.status.className = 'task-status status-waiting';
                }
                return;
            }

            // stdout / stderr content
            const raw = msg.content || '';
            if (!raw) return;

            const html = ansiUp.ansi_to_html(raw);
            const span = document.createElement('span');
            span.innerHTML = html;
            t.body.appendChild(span);

            // Auto-scroll
            t.body.scrollTop = t.body.scrollHeight;

            // Trim if too large (keep last 5000 lines)
            while (t.body.childElementCount > 5000) {
                t.body.removeChild(t.body.firstChild);
            }
        }

        function updateStats() {
            document.getElementById('stat-connections').textContent =
                `${activeConnections}/${NUM_SLOTS} SSE connections active`;
        }

        // Uptime counter
        let uptime = 0;
        setInterval(() => {
            uptime++;
            document.getElementById('stat-uptime').textContent =
                `Uptime: ${uptime}s`;
        }, 1000);
    </script>
</body>
</html>"""


@app.get("/")
async def index():
    """Serve the debug frontend page."""
    return HTMLResponse(_FRONTEND_HTML)
