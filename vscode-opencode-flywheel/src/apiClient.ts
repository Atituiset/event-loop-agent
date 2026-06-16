import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import initSqlJs from 'sql.js';
import type { Database, SqlJsStatic } from 'sql.js';

export interface Finding {
    finding_id: string;
    file_path: string;
    line_number: number;
    rule_id: string;
    severity: string;
    description: string;
    code_snippet: string;
    suggestion: string;
    confidence: number;
    function_name?: string;
    scan_timestamp?: string;
    label?: 'true_positive' | 'false_positive' | null;
    label_reason?: string;
    log_file?: string;
    labeled_by?: string;
    labeled_at?: string;
}

export interface FeedbackStats {
    total_findings: number;
    true_positives: number;
    false_positives: number;
    unlabeled: number;
}

export class ApiClient {
    private dbPath: string | undefined;
    private sqlPromise: Promise<SqlJsStatic> | null = null;

    constructor() {
        this.dbPath = vscode.workspace.getConfiguration('opencode').get<string>('activeDbPath') || undefined;
    }

    setDbPath(dbPath: string | undefined): void {
        this.dbPath = dbPath;
        vscode.workspace.getConfiguration('opencode').update('activeDbPath', dbPath, true);
    }

    getDbPath(): string | undefined {
        return this.dbPath;
    }

    private getSql(): Promise<SqlJsStatic> {
        if (!this.sqlPromise) {
            const wasmPath = path.join(__dirname, '..', 'node_modules', 'sql.js', 'dist', 'sql-wasm.wasm');
            this.sqlPromise = initSqlJs({
                locateFile: () => wasmPath,
            });
        }
        return this.sqlPromise;
    }

    private ensureDbPath(): string {
        if (!this.dbPath) {
            throw new Error('No database open. Select an OpenCode scan session first.');
        }
        return this.dbPath;
    }

    private async openDatabase(): Promise<Database> {
        const SQL = await this.getSql();
        const dbPath = this.ensureDbPath();

        if (!fs.existsSync(dbPath)) {
            throw new Error(`Database not found: ${dbPath}`);
        }

        const buffer = fs.readFileSync(dbPath);
        return new SQL.Database(buffer);
    }

    private rowToFinding(row: Record<string, unknown>): Finding {
        const rawLabel = row.label as string | null;
        const label: Finding['label'] =
            rawLabel === 'true_positive' || rawLabel === 'false_positive'
                ? rawLabel
                : null;

        return {
            finding_id: row.finding_id as string,
            file_path: row.file_path as string,
            line_number: row.line_number as number,
            rule_id: row.rule_id as string,
            severity: row.severity as string,
            description: row.description as string,
            code_snippet: row.code_snippet as string,
            suggestion: row.suggestion as string,
            confidence: row.confidence as number,
            function_name: (row.function_name as string) || undefined,
            scan_timestamp: (row.scan_timestamp as string) || undefined,
            label,
            label_reason: (row.label_reason as string) || undefined,
            log_file: (row.log_file as string) || undefined,
            labeled_by: (row.labeled_by as string) || undefined,
            labeled_at: (row.labeled_at as string) || undefined,
        };
    }

    async getFindings(filePath?: string, functionName?: string): Promise<Finding[]> {
        const db = await this.openDatabase();
        try {
            const conditions: string[] = [];
            const params: (string | number)[] = [];

            if (filePath) {
                conditions.push('file_path = ?');
                params.push(filePath);
            }
            if (functionName) {
                conditions.push('function_name = ?');
                params.push(functionName);
            }

            const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
            const result = db.exec(`SELECT * FROM findings ${whereClause}`, params);

            if (result.length === 0 || result[0].values.length === 0) {
                return [];
            }

            const columns = result[0].columns;
            return result[0].values.map((row) => {
                const record: Record<string, unknown> = {};
                columns.forEach((col, idx) => {
                    record[col] = row[idx];
                });
                return this.rowToFinding(record);
            });
        } finally {
            db.close();
        }
    }

    async getFinding(findingId: string): Promise<Finding> {
        const db = await this.openDatabase();
        try {
            const result = db.exec('SELECT * FROM findings WHERE finding_id = ?', [findingId]);
            if (result.length === 0 || result[0].values.length === 0) {
                throw new Error(`Finding not found: ${findingId}`);
            }

            const columns = result[0].columns;
            const row = result[0].values[0];
            const record: Record<string, unknown> = {};
            columns.forEach((col, idx) => {
                record[col] = row[idx];
            });
            return this.rowToFinding(record);
        } finally {
            db.close();
        }
    }

    async labelFinding(
        findingId: string,
        label: 'true_positive' | 'false_positive',
        reason?: string,
    ): Promise<void> {
        const db = await this.openDatabase();
        try {
            const user = this.getUserIdentifierSync();
            db.run(
                'UPDATE findings SET label = ?, labeled_by = ?, labeled_at = ?, label_reason = ? WHERE finding_id = ?',
                [label, user, new Date().toISOString(), reason || '', findingId],
            );

            if (db.getRowsModified() === 0) {
                throw new Error(`Finding not found: ${findingId}`);
            }

            const data = db.export();
            fs.writeFileSync(this.ensureDbPath(), Buffer.from(data));
        } finally {
            db.close();
        }
    }

    async getStats(): Promise<FeedbackStats> {
        const db = await this.openDatabase();
        try {
            const total = this.execCount(db, 'SELECT COUNT(*) FROM findings');
            const truePositives = this.execCount(db, "SELECT COUNT(*) FROM findings WHERE label = 'true_positive'");
            const falsePositives = this.execCount(db, "SELECT COUNT(*) FROM findings WHERE label = 'false_positive'");
            const unlabeled = this.execCount(db, 'SELECT COUNT(*) FROM findings WHERE label IS NULL');

            return {
                total_findings: total,
                true_positives: truePositives,
                false_positives: falsePositives,
                unlabeled: unlabeled,
            };
        } finally {
            db.close();
        }
    }

    private execCount(db: Database, sql: string): number {
        const result = db.exec(sql);
        if (result.length === 0 || result[0].values.length === 0) {
            return 0;
        }
        return result[0].values[0][0] as number;
    }

    private getUserIdentifierSync(): string {
        try {
            const { execSync } = require('child_process');
            return execSync('git config user.email', { encoding: 'utf-8', timeout: 2000 }).trim();
        } catch {
            return 'unknown';
        }
    }
}
