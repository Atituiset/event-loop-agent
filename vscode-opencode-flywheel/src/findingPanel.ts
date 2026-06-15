import * as vscode from 'vscode';
import { ApiClient, Finding } from './apiClient';

export class FindingPanel {
    public static currentPanel: FindingPanel | undefined;
    private readonly panel: vscode.WebviewPanel;
    private finding: Finding;
    private apiClient: ApiClient;
    private disposables: vscode.Disposable[] = [];

    private constructor(panel: vscode.WebviewPanel, finding: Finding) {
        this.panel = panel;
        this.finding = finding;
        this.apiClient = new ApiClient();

        this.panel.webview.html = this.getHtml();
        this.panel.title = `${finding.rule_id} @ ${finding.file_path}`;

        this.panel.webview.onDidReceiveMessage(
            async (message) => {
                switch (message.command) {
                    case 'label':
                        await this.handleLabel(message.label, message.reason);
                        break;
                    case 'openFile':
                        await this.openFileAtLine();
                        break;
                }
            },
            undefined,
            this.disposables,
        );

        this.panel.onDidDispose(() => this.dispose(), undefined, this.disposables);
    }

    public static show(finding: Finding): void {
        const column = vscode.window.activeTextEditor
            ? vscode.window.activeTextEditor.viewColumn
            : undefined;

        if (FindingPanel.currentPanel) {
            FindingPanel.currentPanel.panel.reveal(column);
            FindingPanel.currentPanel.update(finding);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            'opencodeFindingDetail',
            `${finding.rule_id} @ ${finding.file_path}`,
            column || vscode.ViewColumn.One,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
            },
        );

        FindingPanel.currentPanel = new FindingPanel(panel, finding);
    }

    private update(finding: Finding): void {
        this.finding = finding;
        this.panel.title = `${finding.rule_id} @ ${finding.file_path}`;
        this.panel.webview.html = this.getHtml();
    }

    private async handleLabel(label: 'true_positive' | 'false_positive', reason: string): Promise<void> {
        try {
            await this.apiClient.labelFinding(this.finding.finding_id, label, reason);
            this.finding.label = label;
            this.finding.label_reason = reason;
            this.panel.webview.html = this.getHtml();
            vscode.window.showInformationMessage(`Marked as ${label.replace('_', ' ')}`);
        } catch (err) {
            vscode.window.showErrorMessage(`Failed to label finding: ${err}`);
        }
    }

    private async openFileAtLine(): Promise<void> {
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!workspaceRoot) { return; }

        const filePath = this.finding.file_path.startsWith('/')
            ? this.finding.file_path
            : `${workspaceRoot}/${this.finding.file_path}`;

        const document = await vscode.workspace.openTextDocument(filePath);
        const editor = await vscode.window.showTextDocument(document);
        const line = Math.max(0, this.finding.line_number - 1);
        const range = new vscode.Range(line, 0, line, 0);
        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
        editor.selection = new vscode.Selection(range.start, range.start);
    }

    private escapeHtml(text: string): string {
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    private getHtml(): string {
        const f = this.finding;
        const labelBadge = f.label
            ? `&lt;span class="badge ${f.label}"&gt;${f.label.replace('_', ' ')}&lt;/span&gt;`
            : '&lt;span class="badge unlabeled"&gt;unlabeled&lt;/span&gt;';

        return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${this.escapeHtml(f.rule_id)}</title>
    <style>
        body { font-family: var(--vscode-font-family); background: var(--vscode-editor-background); color: var(--vscode-foreground); padding: 16px; }
        h1 { font-size: 18px; margin-bottom: 8px; }
        h2 { font-size: 14px; margin-top: 24px; margin-bottom: 8px; color: var(--vscode-textLink-foreground); }
        .meta { color: var(--vscode-descriptionForeground); font-size: 12px; margin-bottom: 16px; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-left: 8px; text-transform: uppercase; }
        .badge.true_positive { background: rgba(63,185,80,0.2); color: #3fb950; }
        .badge.false_positive { background: rgba(248,81,73,0.2); color: #f85149; }
        .badge.unlabeled { background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
        pre { background: var(--vscode-textCodeBlock-background); padding: 12px; border-radius: 6px; overflow-x: auto; }
        code { font-family: var(--vscode-editor-font-family); font-size: 13px; }
        .actions { margin-top: 24px; display: flex; gap: 8px; align-items: center; }
        button { padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-weight: 600; }
        button.tp { background: #238636; color: white; }
        button.fp { background: #da3633; color: white; }
        button.secondary { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
        input[type="text"] { flex: 1; padding: 6px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border); border-radius: 4px; }
        .severity { font-weight: 700; }
        .severity.CRITICAL { color: #f85149; }
        .severity.HIGH { color: #d29922; }
    </style>
</head>
<body>
    <h1>${this.escapeHtml(f.rule_id)} ${labelBadge}</h1>
    <div class="meta">
        <span class="severity ${f.severity}">${this.escapeHtml(f.severity)}</span> •
        ${this.escapeHtml(f.file_path)}:${f.line_number} •
        confidence ${Math.round(f.confidence * 100)}%
    </div>

    <h2>Description</h2>
    <p>${this.escapeHtml(f.description)}</p>

    <h2>Code Snippet</h2>
    <pre><code>${this.escapeHtml(f.code_snippet)}</code></pre>

    <h2>Suggestion</h2>
    <p>${this.escapeHtml(f.suggestion)}</p>

    <div class="actions">
        <input type="text" id="reason" placeholder="Reason (optional)" />
        <button class="tp" onclick="label('true_positive')">True Positive</button>
        <button class="fp" onclick="label('false_positive')">False Positive</button>
        <button class="secondary" onclick="openFile()">Open File</button>
    </div>

    <script>
        const vscode = acquireVsCodeApi();
        function label(value) {
            const reason = document.getElementById('reason').value;
            vscode.postMessage({ command: 'label', label: value, reason });
        }
        function openFile() {
            vscode.postMessage({ command: 'openFile' });
        }
    </script>
</body>
</html>`;
    }

    public dispose(): void {
        FindingPanel.currentPanel = undefined;
        this.panel.dispose();
        while (this.disposables.length) {
            const x = this.disposables.pop();
            if (x) { x.dispose(); }
        }
    }
}
