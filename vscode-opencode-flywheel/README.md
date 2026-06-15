# OpenCode Data Flywheel (VS Code Extension)

Visualize findings from the OpenCode Orchestrator and label them as true/false positives to build a feedback loop.

## Features

- **TreeView explorer** for scan findings grouped by file → function → rule
- **Severity icons** (CRITICAL/HIGH/MEDIUM/LOW)
- **Inline buttons** to mark findings as true positive or false positive
- **WebView detail panel** with code snippet, description, suggestion, and open-file action
- **Auto-discovery** of `reports/**/*.findings.json` files after each scan
- **Feedback API** integration with the Python FastAPI server

## Requirements

- VS Code 1.80+
- OpenCode Orchestrator running with `--debug --output-json`
- Feedback API server reachable at `http://localhost:8080` (configurable)

## Usage

1. Run a scan with the orchestrator:
   ```bash
   python orchestrator.py --full ./src --debug --output-json
   ```
2. Open the **OpenCode Findings** panel in the VS Code Explorer sidebar.
3. Click a finding to view details.
4. Click 👍 / 👎 to label it.

## Extension Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `opencode.apiBaseUrl` | `http://localhost:8080` | Base URL of the feedback API |
| `opencode.apiKey` | `''` | API key (if auth is enabled) |
| `opencode.autoLoadFindings` | `true` | Auto-load findings when scans complete |
| `opencode.showLabeledFindings` | `false` | Show already-labeled findings in the tree |

## Development

```bash
cd vscode-opencode-flywheel
npm install
npm run compile
```

Press `F5` in VS Code to launch the Extension Development Host.
