import * as path from 'path';
import * as vscode from 'vscode';
import { ApiClient } from './apiClient';
import { FindingPanel } from './findingPanel';
import { SessionManager } from './sessionManager';
import { SummaryPanel } from './summaryPanel';
import { FindingNode, FindingsTreeProvider } from './treeProvider';

export function registerCommands(
    context: vscode.ExtensionContext,
    treeProvider: FindingsTreeProvider,
): void {
    const sessionManager = new SessionManager();

    context.subscriptions.push(
        vscode.commands.registerCommand('opencode.refreshFindings', () => {
            treeProvider.loadFindings();
        }),

        vscode.commands.registerCommand('opencode.selectSession', async () => {
            const session = await sessionManager.selectSession();
            if (session) {
                treeProvider.setDbPath(session.dbPath);
                await treeProvider.loadFindings();
                vscode.window.showInformationMessage(`OpenCode: loaded ${session.label}`);
            }
        }),

        vscode.commands.registerCommand('opencode.openSummary', () => {
            SummaryPanel.show(treeProvider.getFindings(), treeProvider.getStats());
        }),

        vscode.commands.registerCommand('opencode.openLogFile', async () => {
            const dbPath = treeProvider.getDbPath();
            if (!dbPath) {
                vscode.window.showWarningMessage('No active OpenCode scan session');
                return;
            }
            const logPath = path.join(path.dirname(dbPath), 'orchestrator.log');
            if (!require('fs').existsSync(logPath)) {
                vscode.window.showWarningMessage(
                    `No orchestrator.log found for this session. It may be a demo/converted session without original runtime logs.`
                );
                return;
            }
            const logUri = vscode.Uri.file(logPath);
            try {
                const doc = await vscode.workspace.openTextDocument(logUri);
                await vscode.window.showTextDocument(doc);
            } catch {
                vscode.window.showErrorMessage(`Could not open log file: ${logPath}`);
            }
        }),

        vscode.commands.registerCommand('opencode.openFileLog', async (logPath: string) => {
            if (!logPath) {
                vscode.window.showWarningMessage('No log file available for this item');
                return;
            }
            const logUri = vscode.Uri.file(logPath);
            try {
                const doc = await vscode.workspace.openTextDocument(logUri);
                await vscode.window.showTextDocument(doc);
            } catch {
                vscode.window.showErrorMessage(`Could not open log file: ${logPath}`);
            }
        }),

        vscode.commands.registerCommand('opencode.openFindingDetail', (nodeOrFinding) => {
            const finding = nodeOrFinding instanceof FindingNode ? nodeOrFinding.finding : nodeOrFinding;
            if (finding) {
                FindingPanel.show(finding);
            }
        }),

        vscode.commands.registerCommand('opencode.labelTruePositive', async (node: FindingNode) => {
            await labelFinding(node, 'true_positive', treeProvider);
        }),

        vscode.commands.registerCommand('opencode.labelFalsePositive', async (node: FindingNode) => {
            await labelFinding(node, 'false_positive', treeProvider);
        }),
    );
}

async function labelFinding(
    node: FindingNode,
    label: 'true_positive' | 'false_positive',
    treeProvider: FindingsTreeProvider,
): Promise<void> {
    if (!node.finding) {
        vscode.window.showWarningMessage('No finding selected');
        return;
    }

    const reason = await vscode.window.showInputBox({
        prompt: `Reason for marking as ${label.replace('_', ' ')} (optional)`,
        placeHolder: 'e.g. this pattern is intentional here',
    });

    if (reason === undefined) { return; }

    const apiClient = new ApiClient();
    apiClient.setDbPath(treeProvider.getDbPath());
    try {
        await apiClient.labelFinding(node.finding.finding_id, label, reason);
        vscode.window.showInformationMessage(`Marked ${node.finding.rule_id} as ${label.replace('_', ' ')}`);
        await treeProvider.updateFindingLabel(node.finding.finding_id);
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to label finding: ${err}`);
    }
}
