import * as vscode from 'vscode';
import { FindingsTreeProvider } from './treeProvider';

export class FindingsFileWatcher {
    private watcher: vscode.FileSystemWatcher | undefined;
    private treeProvider: FindingsTreeProvider;

    constructor(treeProvider: FindingsTreeProvider) {
        this.treeProvider = treeProvider;
    }

    start(): void {
        const config = vscode.workspace.getConfiguration('opencode');
        if (!config.get<boolean>('autoLoadFindings')) {
            return;
        }

        // Watch for findings.json files in any reports/ subdirectory
        this.watcher = vscode.workspace.createFileSystemWatcher('**/reports/**/*.findings.json');

        this.watcher.onDidCreate((uri) => {
            this.onNewFindings(uri);
        });

        this.watcher.onDidChange((uri) => {
            this.onNewFindings(uri);
        });

        // Initial scan for existing findings
        this.scanExistingFindings();
    }

    private async scanExistingFindings(): Promise<void> {
        try {
            const files = await vscode.workspace.findFiles('**/reports/**/*.findings.json', null, 10);
            if (files.length > 0) {
                await this.treeProvider.loadFindings();
            }
        } catch (err) {
            console.error('Failed to scan existing findings:', err);
        }
    }

    private async onNewFindings(uri: vscode.Uri): Promise<void> {
        const config = vscode.workspace.getConfiguration('opencode');
        if (!config.get<boolean>('autoLoadFindings')) { return; }

        // Debounce: wait a moment for all files to be written
        await new Promise((resolve) => setTimeout(resolve, 500));
        await this.treeProvider.loadFindings();

        const stats = await this.treeProvider['apiClient'].getStats().catch(() => null);
        if (stats) {
            vscode.window.showInformationMessage(
                `OpenCode: ${stats.unlabeled} unlabeled findings available`,
                'View'
            ).then((selection) => {
                if (selection === 'View') {
                    vscode.commands.executeCommand('opencodeFindings.focus');
                }
            });
        }
    }

    dispose(): void {
        this.watcher?.dispose();
    }
}
