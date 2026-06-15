import * as assert from 'assert';
import * as vscode from 'vscode';

suite('OpenCode Flywheel Extension Test Suite', () => {
    vscode.window.showInformationMessage('Start all tests.');

    test('Extension is activated', async () => {
        const ext = vscode.extensions.getExtension('undefined_publisher.opencode-flywheel');
        assert.ok(ext, 'Extension should be present');
        if (!ext.isActive) {
            await ext.activate();
        }
        assert.strictEqual(ext.isActive, true, 'Extension should be active');
    });

    test('TreeView is registered', async () => {
        const treeView = vscode.window.createTreeView('opencodeFindings', {
            treeDataProvider: {
                getTreeItem: (e) => e,
                getChildren: () => Promise.resolve([]),
            },
        });
        assert.ok(treeView, 'TreeView should be created');
        treeView.dispose();
    });

    test('Commands are registered', async () => {
        const commands = await vscode.commands.getCommands(true);
        const expected = [
            'opencode.refreshFindings',
            'opencode.openFindingDetail',
            'opencode.labelTruePositive',
            'opencode.labelFalsePositive',
        ];
        for (const cmd of expected) {
            assert.ok(commands.includes(cmd), `Command ${cmd} should be registered`);
        }
    });
});
