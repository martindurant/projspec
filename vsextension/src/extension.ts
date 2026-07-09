import * as vscode from 'vscode';
import { CombinedPanel } from './combinedPanel';
import { SidebarViewProvider } from './sidebarView';

export function activate(context: vscode.ExtensionContext): void {
    console.log('[projspec] activate called');

    // Sidebar view (activity bar launcher)
    const sidebarProvider = new SidebarViewProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('projspec.view', sidebarProvider)
    );

    // Primary command: open the combined panel (defaults to Library tab)
    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.showTree', () => {
            CombinedPanel.createOrShow(context.extensionUri, 'library');
        })
    );

    // Open directly to the File Browser tab (optionally at a specific URL)
    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.openFileBrowser', (initialUrl?: string) => {
            console.log('[projspec] openFileBrowser fired, initialUrl=', initialUrl);
            try {
                CombinedPanel.createOrShow(context.extensionUri, 'filebrowser', initialUrl);
            } catch (e) {
                console.error('[projspec] createOrShow threw:', e);
                vscode.window.showErrorMessage('FileBrowser error: ' + String(e));
            }
        })
    );

    // Open File Browser at the current workspace folder
    context.subscriptions.push(
        vscode.commands.registerCommand('projspec.openFileBrowserHere', () => {
            const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            console.log('[projspec] openFileBrowserHere fired, folder=', folder);
            try {
                CombinedPanel.createOrShow(context.extensionUri, 'filebrowser', folder);
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
