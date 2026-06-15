import * as vscode from 'vscode';
import { FindingsTreeProvider } from './treeProvider';

export class FindingsFileWatcher {
    private watchers: vscode.FileSystemWatcher[] = [];
    private treeProvider: FindingsTreeProvider;

    constructor(treeProvider: FindingsTreeProvider) {
        this.treeProvider = treeProvider;
    }

    start(): void {
        const config = vscode.workspace.getConfiguration('opencode');
        if (!config.get<boolean>('autoLoadFindings')) {
            return;
        }

        // Watch for findings.json files in reports/ or agent_review_report/ subdirectories
        const patterns = [
            '**/reports/**/*.findings.json',
            '**/agent_review_report/**/*.findings.json',
        ];
        this.watchers = patterns.map((pattern) =>
            vscode.workspace.createFileSystemWatcher(pattern)
        );

        for (const watcher of this.watchers) {
            watcher.onDidCreate((uri) => {
                this.onNewFindings(uri);
            });
            watcher.onDidChange((uri) => {
                this.onNewFindings(uri);
            });
        }

        // Initial scan for existing findings
        this.scanExistingFindings();
    }

    private async scanExistingFindings(): Promise<void> {
        try {
            const files = await vscode.workspace.findFiles(
                '{reports,agent_review_report}/**/*.findings.json',
                null,
                10,
            );
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
        for (const watcher of this.watchers) {
            watcher.dispose();
        }
        this.watchers = [];
    }
}
