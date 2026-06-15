import * as path from 'path';
import * as vscode from 'vscode';
import { ApiClient, Finding } from './apiClient';

export type FindingNodeType = 'root' | 'file' | 'function' | 'finding';

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
            this.tooltip = `${finding.rule_id}: ${finding.description}`;
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
        } else if (type === 'function') {
            this.iconPath = new vscode.ThemeIcon('symbol-method');
            this.contextValue = 'function';
        } else {
            this.iconPath = new vscode.ThemeIcon('search');
        }
    }

    private getSeverityIcon(
        severity: string,
        label?: string | null,
    ): vscode.ThemeIcon {
        // Overlay status via icon modifier is not directly supported, so we pick
        // a severity color and rely on the detail panel to show labeled state.
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

    constructor() {
        this.apiClient = new ApiClient();
    }

    refresh(): void {
        this._onDidChangeTreeData.fire();
    }

    async loadFindings(): Promise<void> {
        try {
            this.findings = await this.apiClient.getFindings();
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

        const rootNodes: FindingNode[] = [];
        for (const [filePath, funcMap] of fileMap.entries()) {
            const funcNodes: FindingNode[] = [];
            for (const [funcName, funcFindings] of funcMap.entries()) {
                const findingNodes = funcFindings.map(
                    (f) => new FindingNode('finding', `${f.rule_id}: ${this.truncate(f.description, 40)}`, f),
                );
                funcNodes.push(new FindingNode('function', funcName, undefined, findingNodes));
            }
            rootNodes.push(new FindingNode('file', path.basename(filePath), undefined, funcNodes));
        }

        return rootNodes;
    }

    private truncate(text: string, maxLen: number): string {
        return text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
    }

    async updateFindingLabel(findingId: string): Promise<void> {
        // Refresh single finding state from server
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
