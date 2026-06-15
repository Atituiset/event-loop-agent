import * as vscode from 'vscode';
import { FeedbackStats, Finding } from './apiClient';

export class SummaryPanel {
    public static currentPanel: SummaryPanel | undefined;
    private readonly panel: vscode.WebviewPanel;
    private disposables: vscode.Disposable[] = [];

    private constructor(panel: vscode.WebviewPanel) {
        this.panel = panel;
        this.panel.webview.html = this.getHtml([], { total_findings: 0, true_positives: 0, false_positives: 0, unlabeled: 0 });
        this.panel.onDidDispose(() => this.dispose(), undefined, this.disposables);
    }

    public static show(findings: Finding[], stats: FeedbackStats): void {
        if (SummaryPanel.currentPanel) {
            SummaryPanel.currentPanel.panel.reveal();
            SummaryPanel.currentPanel.update(findings, stats);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            'opencodeSummary',
            'OpenCode Scan Summary',
            vscode.ViewColumn.One,
            { enableScripts: true },
        );

        SummaryPanel.currentPanel = new SummaryPanel(panel);
        SummaryPanel.currentPanel.update(findings, stats);
    }

    private update(findings: Finding[], stats: FeedbackStats): void {
        this.panel.title = `OpenCode Summary (${stats.total_findings})`;
        this.panel.webview.html = this.getHtml(findings, stats);
    }

    private escapeHtml(text: string): string {
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    private getHtml(findings: Finding[], stats: FeedbackStats): string {
        const ruleCounts = new Map<string, number>();
        const fileCounts = new Map<string, number>();
        for (const f of findings) {
            ruleCounts.set(f.rule_id, (ruleCounts.get(f.rule_id) || 0) + 1);
            fileCounts.set(f.file_path, (fileCounts.get(f.file_path) || 0) + 1);
        }

        const ruleRows = Array.from(ruleCounts.entries())
            .sort((a, b) => b[1] - a[1])
            .map(([rule, count]) => `<tr><td>${rule}</td><td>${count}</td></tr>`)
            .join('');

        const fileRows = Array.from(fileCounts.entries())
            .sort((a, b) => b[1] - a[1])
            .slice(0, 10)
            .map(([file, count]) => `<tr><td>${this.escapeHtml(file)}</td><td>${count}</td></tr>`)
            .join('');

        return `<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body { font-family: var(--vscode-font-family); background: var(--vscode-editor-background); color: var(--vscode-foreground); padding: 16px; }
        h1 { font-size: 18px; }
        h2 { font-size: 14px; margin-top: 24px; color: var(--vscode-textLink-foreground); }
        .stats { display: flex; gap: 16px; margin: 16px 0; }
        .stat-box { background: var(--vscode-textCodeBlock-background); padding: 12px 16px; border-radius: 6px; min-width: 80px; text-align: center; }
        .stat-box .number { font-size: 24px; font-weight: 700; }
        .stat-box .label { font-size: 11px; color: var(--vscode-descriptionForeground); }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th, td { text-align: left; padding: 6px; border-bottom: 1px solid var(--vscode-panel-border); }
        th { color: var(--vscode-descriptionForeground); font-size: 12px; }
        .note { color: var(--vscode-descriptionForeground); font-size: 12px; margin-top: 24px; }
    </style>
</head>
<body>
    <h1>OpenCode Scan Summary</h1>
    <div class="stats">
        <div class="stat-box"><div class="number">${stats.total_findings}</div><div class="label">Total</div></div>
        <div class="stat-box"><div class="number">${stats.unlabeled}</div><div class="label">Unlabeled</div></div>
        <div class="stat-box"><div class="number">${stats.true_positives}</div><div class="label">True Positives</div></div>
        <div class="stat-box"><div class="number">${stats.false_positives}</div><div class="label">False Positives</div></div>
    </div>

    <h2>Findings by Rule</h2>
    <table><thead><tr><th>Rule</th><th>Count</th></tr></thead><tbody>${ruleRows}</tbody></table>

    <h2>Top Files by Finding Count</h2>
    <table><thead><tr><th>File</th><th>Count</th></tr></thead><tbody>${fileRows}</tbody></table>

    <p class="note">
        Unlabeled findings are those that have not yet been marked as true or false positive.
        Click the 👍 / 👎 buttons on each finding to build the feedback memory.
    </p>
</body>
</html>`;
    }

    public dispose(): void {
        SummaryPanel.currentPanel = undefined;
        this.panel.dispose();
        while (this.disposables.length) {
            const x = this.disposables.pop();
            if (x) { x.dispose(); }
        }
    }
}
