import * as vscode from 'vscode';
import { registerCommands } from './commands';
import { FindingsFileWatcher } from './fileWatcher';
import { FindingsTreeProvider } from './treeProvider';

export function activate(context: vscode.ExtensionContext): void {
    const treeProvider = new FindingsTreeProvider();

    const treeView = vscode.window.createTreeView('opencodeFindings', {
        treeDataProvider: treeProvider,
        showCollapseAll: true,
    });

    registerCommands(context, treeProvider);

    const fileWatcher = new FindingsFileWatcher(treeProvider);
    fileWatcher.start();

    context.subscriptions.push(treeView, fileWatcher);
}

export function deactivate(): void {
    // Cleanup is handled via context.subscriptions
}
