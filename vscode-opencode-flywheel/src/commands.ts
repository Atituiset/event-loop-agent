import * as vscode from 'vscode';
import { ApiClient } from './apiClient';
import { FindingPanel } from './findingPanel';
import { FindingNode, FindingsTreeProvider } from './treeProvider';

export function registerCommands(
    context: vscode.ExtensionContext,
    treeProvider: FindingsTreeProvider,
): void {
    context.subscriptions.push(
        vscode.commands.registerCommand('opencode.refreshFindings', () => {
            treeProvider.loadFindings();
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

    // User cancelled
    if (reason === undefined) { return; }

    const apiClient = new ApiClient();
    try {
        await apiClient.labelFinding(node.finding.finding_id, label, reason);
        vscode.window.showInformationMessage(`Marked ${node.finding.rule_id} as ${label.replace('_', ' ')}`);
        await treeProvider.updateFindingLabel(node.finding.finding_id);
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to label finding: ${err}`);
    }
}
