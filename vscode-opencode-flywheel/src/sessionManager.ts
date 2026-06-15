import * as path from 'path';
import * as vscode from 'vscode';
import { ApiClient, FeedbackStats } from './apiClient';

export interface ScanSession {
    label: string;
    dbPath: string;
    dirName: string;
    workspacePath: string;
}

export class SessionManager {
    private apiClient: ApiClient;

    constructor() {
        this.apiClient = new ApiClient();
    }

    async discoverSessions(): Promise<ScanSession[]> {
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!workspaceRoot) {
            return [];
        }

        const patterns = [
            new vscode.RelativePattern(workspaceRoot, '**/agent_review_report/**/findings.db'),
            new vscode.RelativePattern(workspaceRoot, '**/reports/**/findings.db'),
        ];

        const sessions: ScanSession[] = [];
        for (const pattern of patterns) {
            const files = await vscode.workspace.findFiles(pattern, null, 100);
            for (const uri of files) {
                const dbPath = uri.fsPath;
                const dirName = path.basename(path.dirname(dbPath));
                const relDir = path.relative(workspaceRoot, path.dirname(dbPath));
                sessions.push({
                    label: `${dirName} (${relDir})`,
                    dbPath,
                    dirName,
                    workspacePath: workspaceRoot,
                });
            }
        }

        // Sort by directory name descending (newest timestamp first)
        return sessions.sort((a, b) => b.dirName.localeCompare(a.dirName));
    }

    async selectSession(): Promise<ScanSession | undefined> {
        const sessions = await this.discoverSessions();
        if (sessions.length === 0) {
            vscode.window.showWarningMessage('No OpenCode scan sessions found in this workspace');
            return undefined;
        }

        const currentDbPath = this.apiClient.getDbPath();
        const items = sessions.map((s) => ({
            label: s.label,
            description: s.dbPath === currentDbPath ? 'active' : '',
            session: s,
        }));

        const picked = await vscode.window.showQuickPick(items, {
            placeHolder: 'Select an OpenCode scan session',
        });

        return picked?.session;
    }

    async getSessionStats(dbPath: string): Promise<FeedbackStats | null> {
        try {
            const tempClient = new ApiClient();
            tempClient.setDbPath(dbPath);
            return await tempClient.getStats();
        } catch {
            return null;
        }
    }
}
