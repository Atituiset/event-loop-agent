import * as vscode from 'vscode';
import { registerCommands } from './commands';
import { FindingsFileWatcher } from './fileWatcher';
import { SessionManager } from './sessionManager';
import { FindingsTreeProvider } from './treeProvider';

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    const treeProvider = new FindingsTreeProvider();

    const treeView = vscode.window.createTreeView('opencodeFindings', {
        treeDataProvider: treeProvider,
        showCollapseAll: true,
    });

    registerCommands(context, treeProvider);

    const fileWatcher = new FindingsFileWatcher(treeProvider);
    fileWatcher.start();

    context.subscriptions.push(treeView, fileWatcher);

    // Auto-select the latest scan session on activation
    const sessionManager = new SessionManager();
    const sessions = await sessionManager.discoverSessions();
    const activeDbPath = treeProvider.getDbPath();

    if (!activeDbPath && sessions.length > 0) {
        treeProvider.setDbPath(sessions[0].dbPath);
    }

    // Initial load
    await treeProvider.loadFindings();
}

export function deactivate(): void {
    // Cleanup is handled via context.subscriptions
}
