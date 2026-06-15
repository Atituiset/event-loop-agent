import * as vscode from 'vscode';

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
}

export interface FeedbackStats {
    total_findings: number;
    true_positives: number;
    false_positives: number;
    unlabeled: number;
}

export class ApiClient {
    private baseUrl: string;
    private apiKey: string;

    constructor() {
        const config = vscode.workspace.getConfiguration('opencode');
        this.baseUrl = config.get<string>('apiBaseUrl') || 'http://localhost:8080';
        this.apiKey = config.get<string>('apiKey') || '';
    }

    private async request<T>(path: string, options: RequestInit = {}): Promise<T> {
        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            ...((options.headers as Record<string, string>) || {}),
        };
        if (this.apiKey) {
            headers['X-API-Key'] = this.apiKey;
        }

        const url = `${this.baseUrl}${path}`;
        const response = await fetch(url, { ...options, headers });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(`API error ${response.status}: ${text}`);
        }
        return response.json() as Promise<T>;
    }

    async getFindings(filePath?: string, functionName?: string): Promise<Finding[]> {
        const params = new URLSearchParams();
        if (filePath) { params.set('file_path', filePath); }
        if (functionName) { params.set('function_name', functionName); }
        const query = params.toString() ? `?${params.toString()}` : '';
        return this.request<Finding[]>(`/api/findings${query}`);
    }

    async getFinding(findingId: string): Promise<Finding> {
        return this.request<Finding>(`/api/findings/${encodeURIComponent(findingId)}`);
    }

    async labelFinding(
        findingId: string,
        label: 'true_positive' | 'false_positive',
        reason?: string,
    ): Promise<void> {
        const user = await this.getUserIdentifier();
        await this.request(`/api/findings/${encodeURIComponent(findingId)}/label`, {
            method: 'POST',
            body: JSON.stringify({ label, reason: reason || '', labeled_by: user }),
        });
    }

    async getStats(): Promise<FeedbackStats> {
        return this.request<FeedbackStats>('/api/stats');
    }

    private async getUserIdentifier(): Promise<string> {
        try {
            // Best-effort user identity from Git config
            const { exec } = await import('child_process');
            const { promisify } = await import('util');
            const execAsync = promisify(exec);
            const { stdout } = await execAsync('git config user.email', { timeout: 2000 });
            return stdout.trim();
        } catch {
            return 'unknown';
        }
    }
}
