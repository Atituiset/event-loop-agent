import * as path from 'path';
import * as vscode from 'vscode';
import { ApiClient, FeedbackStats, Finding } from './apiClient';
import { SummaryPanel } from './summaryPanel';

export type FindingNodeType = 'root' | 'summary' | 'log' | 'file' | 'function' | 'finding';

export class FindingNode extends vscode.TreeItem {
    constructor(
        public readonly type: FindingNodeType,
        public readonly label: string,
        public readonly finding?: Finding,
        public readonly children: FindingNode[] = [],
    ) {
        super(
            label,
            type === 'finding' ? vscode.TreeItemCollapsibleState.None : vscode.TreeItemCollapsibleState.Collapsed,
        );

        if (type === 'finding' && finding) {
            this.contextValue = 'finding';
            this.tooltip = `${finding.file_path}:${finding.line_number}\n${finding.rule_id}: ${finding.description}`;
            this.description = `line ${finding.line_number}`;
            this.iconPath = this.getSeverityIcon(finding.severity, finding.label);
            this.command = {
                command: 'opencode.openFindingDetail',
                title: 'Open Finding Detail',
                arguments: [finding],
            };
        } else if (type === 'file') {
            this.iconPath = vscode.ThemeIcon.File;
            this.contextValue = 'file';
            this.tooltip = finding?.file_path || label;
        } else if (type === 'function') {
            this.iconPath = new vscode.ThemeIcon('symbol-method');
            this.contextValue = 'function';
        } else if (type === 'summary') {
            this.iconPath = new vscode.ThemeIcon('dashboard');
            this.contextValue = 'summary';
            this.command = {
                command: 'opencode.openSummary',
                title: 'Open Scan Summary',
            };
        } else if (type === 'log') {
            this.iconPath = new vscode.ThemeIcon('output');
            this.contextValue = 'log';
        } else {
            this.iconPath = new vscode.ThemeIcon('search');
        }
    }

    private getSeverityIcon(
        severity: string,
        label?: string | null,
    ): vscode.ThemeIcon {
        switch (severity.toUpperCase()) {
            case 'CRITICAL':
                return new vscode.ThemeIcon('error');
            case 'HIGH':
                return new vscode.ThemeIcon('warning');
            case 'MEDIUM':
                return new vscode.ThemeIcon('info');
            case 'LOW':
                return new vscode.ThemeIcon('question');
            default:
                return new vscode.ThemeIcon('info');
        }
    }
}

export class FindingsTreeProvider implements vscode.TreeDataProvider<FindingNode> {
    private _onDidChangeTreeData: vscode.EventEmitter<FindingNode | undefined | void> =
        new vscode.EventEmitter<FindingNode | undefined | void>();
    readonly onDidChangeTreeData: vscode.Event<FindingNode | undefined | void> =
        this._onDidChangeTreeData.event;

    private apiClient: ApiClient;
    private findings: Finding[] = [];
    private stats: FeedbackStats = { total_findings: 0, true_positives: 0, false_positives: 0, unlabeled: 0 };

    constructor() {
        this.apiClient = new ApiClient();
    }

    setDbPath(dbPath: string | undefined): void {
        this.apiClient.setDbPath(dbPath);
    }

    getDbPath(): string | undefined {
        return this.apiClient.getDbPath();
    }

    getFindings(): Finding[] {
        return this.findings;
    }

    getStats(): FeedbackStats {
        return this.stats;
    }

    refresh(): void {
        this._onDidChangeTreeData.fire();
    }

    async loadFindings(): Promise<void> {
        try {
            [this.findings, this.stats] = await Promise.all([
                this.apiClient.getFindings(),
                this.apiClient.getStats(),
            ]);
            vscode.commands.executeCommand('setContext', 'opencode.hasFindings', this.findings.length > 0);
            this.refresh();
        } catch (err) {
            vscode.window.showErrorMessage(`Failed to load OpenCode findings: ${err}`);
        }
    }

    getTreeItem(element: FindingNode): vscode.TreeItem {
        return element;
    }

    async getChildren(element?: FindingNode): Promise<FindingNode[]> {
        if (!element) {
            return this.buildTree();
        }
        return element.children;
    }

    private buildTree(): FindingNode[] {
        const showLabeled = vscode.workspace.getConfiguration('opencode').get<boolean>('showLabeledFindings') || false;
        const visible = showLabeled ? this.findings : this.findings.filter((f) => !f.label);

        // Summary node
        const summaryNode = new FindingNode(
            'summary',
            `Summary: ${this.stats.total_findings} total, ${this.stats.unlabeled} unlabeled`,
            undefined,
            [],
        );

        // Log node
        const logNode = new FindingNode(
            'log',
            'Open Scan Log',
            undefined,
            [],
        );
        logNode.iconPath = new vscode.ThemeIcon('output');
        logNode.contextValue = 'log';
        logNode.command = {
            command: 'opencode.openLogFile',
            title: 'Open Scan Log',
        };

        // Group by file_path -> function_name -> findings
        const fileMap = new Map<string, Map<string, Finding[]>>();
        for (const finding of visible) {
            const file = finding.file_path || 'unknown';
            const func = finding.function_name || '(global)';
            if (!fileMap.has(file)) {
                fileMap.set(file, new Map<string, Finding[]>());
            }
            const funcMap = fileMap.get(file)!;
            if (!funcMap.has(func)) {
                funcMap.set(func, []);
            }
            funcMap.get(func)!.push(finding);
        }

        const rootNodes: FindingNode[] = [summaryNode, logNode];
        for (const [filePath, funcMap] of fileMap.entries()) {
            const fileFindings = Array.from(funcMap.values()).flat();
            const fileCount = fileFindings.length;
            const funcNodes: FindingNode[] = [];

            for (const [funcName, funcFindings] of funcMap.entries()) {
                const lines = funcFindings.map((f) => f.line_number).filter((n) => n > 0);
                const lineRange = lines.length > 0
                    ? `${Math.min(...lines)}-${Math.max(...lines)}`
                    : '-';
                const funcLabel = `${funcName} @ ${path.basename(filePath)}:${lineRange} (${funcFindings.length})`;

                const findingNodes = funcFindings.map(
                    (f) => new FindingNode('finding', `${f.rule_id} @ L${f.line_number}: ${this.truncate(f.description, 35)}`, f),
                );
                funcNodes.push(new FindingNode('function', funcLabel, undefined, findingNodes));
            }

            const fileLabel = `${filePath} (${fileCount})`;
            rootNodes.push(new FindingNode('file', fileLabel, fileFindings[0], funcNodes));
        }

        return rootNodes;
    }

    private truncate(text: string, maxLen: number): string {
        return text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
    }

    async updateFindingLabel(findingId: string): Promise<void> {
        try {
            const updated = await this.apiClient.getFinding(findingId);
            const idx = this.findings.findIndex((f) => f.finding_id === findingId);
            if (idx >= 0) {
                this.findings[idx] = updated;
                this.refresh();
            }
        } catch (err) {
            vscode.window.showWarningMessage(`Failed to refresh finding: ${err}`);
        }
    }
}
