import * as fs from 'fs';
import * as path from 'path';

export interface SessionInfo {
    sessionId: string;
    sessionPath: string;
    workspacePath: string;
    totalTasks: number;
    successTasks: number;
    failedTasks: number;
    durationSeconds: number;
    generatedAt: string;
}

export function readSessionInfo(dbPath: string | undefined): SessionInfo | null {
    if (!dbPath) {
        return null;
    }

    const sessionPath = path.dirname(dbPath);
    const sessionId = path.basename(sessionPath);
    const summaryPath = path.join(sessionPath, 'summary.md');

    let workspacePath = '';
    let totalTasks = 0;
    let successTasks = 0;
    let failedTasks = 0;
    let durationSeconds = 0;
    let generatedAt = '';

    if (fs.existsSync(summaryPath)) {
        const content = fs.readFileSync(summaryPath, 'utf-8');
        const rows = parseMarkdownTable(content);

        for (const row of rows) {
            const [key, value] = row;
            switch (key.trim()) {
                case '总任务数':
                    totalTasks = parseInt(value.trim(), 10) || 0;
                    break;
                case '成功':
                    successTasks = parseInt(value.trim(), 10) || 0;
                    break;
                case '失败':
                    failedTasks = parseInt(value.trim(), 10) || 0;
                    break;
                case '总耗时':
                    durationSeconds = parseFloat(value.trim()) || 0;
                    break;
                case '生成时间':
                    generatedAt = value.trim();
                    break;
            }
        }
    }

    // Try to infer workspace path from the session directory's parent chain.
    // The typical layout is <workspace>/agent_review_report/<sessionId>/findings.db
    const parentDir = path.dirname(sessionPath);
    const grandparentDir = path.dirname(parentDir);
    const parentName = path.basename(parentDir);
    if (parentName === 'agent_review_report' || parentName === 'reports') {
        workspacePath = grandparentDir;
    } else {
        workspacePath = parentDir;
    }

    return {
        sessionId,
        sessionPath,
        workspacePath,
        totalTasks,
        successTasks,
        failedTasks,
        durationSeconds,
        generatedAt,
    };
}

function parseMarkdownTable(content: string): string[][] {
    const lines = content.split(/\r?\n/);
    const rows: string[][] = [];

    for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('|') || trimmed.endsWith('---|')) {
            continue;
        }
        // Skip separator lines like |---|---|
        if (/^\|[-:\s|]+\|$/.test(trimmed)) {
            continue;
        }
        const cells = trimmed
            .split('|')
            .map((cell) => cell.trim())
            .filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);
        if (cells.length >= 2) {
            rows.push(cells);
        }
    }

    return rows;
}
