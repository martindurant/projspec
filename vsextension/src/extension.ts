import * as vscode from 'vscode';
import { ProjspecPanel } from './panel';
import { SidebarViewProvider } from './sidebarView';
import { FileBrowserPanel } from './fileBrowserPanel';

export function activate(context: vscode.ExtensionContext): void {
    console.log('[projspec] activate called');
    const sidebarProvider = new SidebarViewProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('projspec.view', sidebarProvider)
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.showTree', () => {
            ProjspecPanel.createOrShow(context.extensionUri);
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.openFileBrowser', (initialUrl?: string) => {
            console.log('[projspec] openFileBrowser fired, FileBrowserPanel=', typeof FileBrowserPanel);
            try {
                FileBrowserPanel.createOrShow(context.extensionUri, initialUrl);
            } catch (e) {
                console.error('[projspec] createOrShow threw:', e);
                vscode.window.showErrorMessage('FileBrowser error: ' + String(e));
            }
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.openFileBrowserHere', () => {
            const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            console.log('[projspec] openFileBrowserHere fired, folder=', folder);
            try {
                FileBrowserPanel.createOrShow(context.extensionUri, folder);
            } catch (e) {
                console.error('[projspec] createOrShow threw:', e);
                vscode.window.showErrorMessage('FileBrowser error: ' + String(e));
            }
        })
    );
}

export function deactivate(): void {
    // nothing to clean up
}
