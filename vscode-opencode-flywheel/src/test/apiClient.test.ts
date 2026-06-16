import * as assert from 'assert';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import initSqlJs from 'sql.js';
import { ApiClient } from '../apiClient';

suite('ApiClient SQLite Tests', () => {
    let dbPath: string;
    let client: ApiClient;
    let SQL: Awaited<ReturnType<typeof initSqlJs>>;

    const schemaSql = `
        CREATE TABLE IF NOT EXISTS findings (
            finding_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            line_number INTEGER,
            rule_id TEXT,
            severity TEXT,
            description TEXT,
            code_snippet TEXT,
            suggestion TEXT,
            confidence REAL,
            function_name TEXT,
            scan_timestamp TEXT,
            mr_link TEXT,
            task_id TEXT,
            log_file TEXT,
            label TEXT,
            labeled_by TEXT,
            labeled_at TEXT,
            label_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file_path, function_name);
        CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule_id);
        CREATE INDEX IF NOT EXISTS idx_findings_label ON findings(label);
    `;

    const insertFinding = `
        INSERT INTO findings (
            finding_id, file_path, line_number, rule_id, severity,
            description, code_snippet, suggestion, confidence,
            function_name, scan_timestamp, log_file, label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `;

    suiteSetup(async () => {
        const wasmPath = path.join(__dirname, '..', '..', 'node_modules', 'sql.js', 'dist', 'sql-wasm.wasm');
        SQL = await initSqlJs({ locateFile: () => wasmPath });
    });

    setup(() => {
        dbPath = path.join(os.tmpdir(), `opencode-test-${Date.now()}.db`);
        const db = new SQL.Database();
        db.exec(schemaSql);
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        client = new ApiClient();
        client.setDbPath(dbPath);
    });

    teardown(() => {
        client.setDbPath(undefined);
        for (const ext of ['', '-shm', '-wal']) {
            try {
                fs.unlinkSync(dbPath + ext);
            } catch {
                // ignore files that do not exist
            }
        }
    });

    test('getFindings returns empty array for empty DB', async () => {
        const findings = await client.getFindings();
        assert.strictEqual(findings.length, 0);
    });

    test('getFindings returns all inserted findings', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        db.run(insertFinding, [
            'f1', '/src/main.c', 10, 'RULE-001', 'HIGH',
            'Buffer overflow risk', 'strcpy(buf, input);', 'Use strncpy', 0.9,
            'parse_input', '2024-01-01T00:00:00Z', '/logs/f1.log', null,
        ]);
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        const findings = await client.getFindings();
        assert.strictEqual(findings.length, 1);
        assert.strictEqual(findings[0].finding_id, 'f1');
        assert.strictEqual(findings[0].severity, 'HIGH');
        assert.strictEqual(findings[0].function_name, 'parse_input');
        assert.strictEqual(findings[0].label, null);
    });

    test('getFindings filters by file_path', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        const stmt = db.prepare(insertFinding);
        stmt.run(['f1', '/src/a.c', 1, 'RULE-001', 'HIGH', 'd', 'c', 's', 0.5, 'func_a', '', '', null]);
        stmt.run(['f2', '/src/b.c', 2, 'RULE-002', 'LOW', 'd', 'c', 's', 0.5, 'func_b', '', '', null]);
        stmt.free();
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        const findings = await client.getFindings('/src/a.c');
        assert.strictEqual(findings.length, 1);
        assert.strictEqual(findings[0].finding_id, 'f1');
    });

    test('getFindings filters by file_path and function_name', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        const stmt = db.prepare(insertFinding);
        stmt.run(['f1', '/src/a.c', 1, 'RULE-001', 'HIGH', 'd', 'c', 's', 0.5, 'func_a', '', '', null]);
        stmt.run(['f2', '/src/a.c', 2, 'RULE-002', 'LOW', 'd', 'c', 's', 0.5, 'func_b', '', '', null]);
        stmt.free();
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        const findings = await client.getFindings('/src/a.c', 'func_b');
        assert.strictEqual(findings.length, 1);
        assert.strictEqual(findings[0].finding_id, 'f2');
    });

    test('getFinding returns single finding', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        db.run(insertFinding, [
            'f1', '/src/main.c', 10, 'RULE-001', 'HIGH',
            'Buffer overflow risk', 'strcpy(buf, input);', 'Use strncpy', 0.9,
            'parse_input', '', '', null,
        ]);
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        const finding = await client.getFinding('f1');
        assert.strictEqual(finding.finding_id, 'f1');
        assert.strictEqual(finding.line_number, 10);
    });

    test('getFinding throws for missing id', async () => {
        await assert.rejects(
            async () => client.getFinding('nonexistent'),
            /Finding not found/,
        );
    });

    test('labelFinding updates row and stats', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        db.run(insertFinding, [
            'f1', '/src/main.c', 10, 'RULE-001', 'HIGH',
            'Buffer overflow risk', 'strcpy(buf, input);', 'Use strncpy', 0.9,
            'parse_input', '', '', null,
        ]);
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        await client.labelFinding('f1', 'true_positive', 'intentional pattern');

        const finding = await client.getFinding('f1');
        assert.strictEqual(finding.label, 'true_positive');
        assert.strictEqual(finding.label_reason, 'intentional pattern');
        assert.ok(finding.labeled_by);
        assert.ok(finding.labeled_at);

        const stats = await client.getStats();
        assert.strictEqual(stats.total_findings, 1);
        assert.strictEqual(stats.true_positives, 1);
        assert.strictEqual(stats.false_positives, 0);
        assert.strictEqual(stats.unlabeled, 0);
    });

    test('getStats counts correctly', async () => {
        const db = new SQL.Database();
        db.exec(schemaSql);
        const stmt = db.prepare(insertFinding);
        stmt.run(['f1', '/src/a.c', 1, 'RULE-001', 'HIGH', 'd', 'c', 's', 0.5, 'func', '', '', 'true_positive']);
        stmt.run(['f2', '/src/a.c', 2, 'RULE-002', 'LOW', 'd', 'c', 's', 0.5, 'func', '', '', 'false_positive']);
        stmt.run(['f3', '/src/b.c', 3, 'RULE-003', 'MEDIUM', 'd', 'c', 's', 0.5, 'func', '', '', null]);
        stmt.free();
        fs.writeFileSync(dbPath, Buffer.from(db.export()));
        db.close();

        const stats = await client.getStats();
        assert.strictEqual(stats.total_findings, 3);
        assert.strictEqual(stats.true_positives, 1);
        assert.strictEqual(stats.false_positives, 1);
        assert.strictEqual(stats.unlabeled, 1);
    });

    test('setDbPath switches databases', async () => {
        const dbPath2 = path.join(os.tmpdir(), `opencode-test-2-${Date.now()}.db`);
        const db2 = new SQL.Database();
        db2.exec(schemaSql);
        db2.run(insertFinding, [
            'f2', '/src/other.c', 5, 'RULE-005', 'MEDIUM',
            'Leak', 'malloc without free', 'Free memory', 0.7,
            'other_func', '', '', null,
        ]);
        fs.writeFileSync(dbPath2, Buffer.from(db2.export()));
        db2.close();

        client.setDbPath(dbPath2);
        const findings = await client.getFindings();
        assert.strictEqual(findings.length, 1);
        assert.strictEqual(findings[0].finding_id, 'f2');

        for (const ext of ['', '-shm', '-wal']) {
            try {
                fs.unlinkSync(dbPath2 + ext);
            } catch {
                // ignore
            }
        }
    });

    test('operations throw when no DB is configured', async () => {
        client.setDbPath(undefined);
        await assert.rejects(async () => client.getFindings(), /No database open/);
        await assert.rejects(async () => client.getStats(), /No database open/);
    });
});
