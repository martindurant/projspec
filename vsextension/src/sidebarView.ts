import * as vscode from 'vscode';

/**
 * Simple sidebar view that provides buttons to launch the main Project
 * Library webview panel and the File Browser.  Keeping this view lightweight
 * (just command triggers) leaves the full UIs to be rendered in roomy
 * editor-area WebviewPanels, as described in ACTIONS.md.
 */
export class SidebarViewProvider implements vscode.WebviewViewProvider {
    constructor(private readonly _extensionUri: vscode.Uri) {}

    resolveWebviewView(webviewView: vscode.WebviewView): void {
        webviewView.webview.options = { enableScripts: true };
        webviewView.webview.html = this.getHtml();
        webviewView.webview.onDidReceiveMessage((msg) => {
            if (msg.cmd === 'open') {
                vscode.commands.executeCommand('projspec.showTree');
            } else if (msg.cmd === 'openFileBrowser') {
                vscode.commands.executeCommand('projspec.openFileBrowserHere');
            }
        });
    }

    private getHtml(): string {
        return /* html */ `
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8" />
<style>
    body { font-family: var(--vscode-font-family); padding: 12px; }
    button {
        background: var(--vscode-button-background);
        color: var(--vscode-button-foreground);
        border: none;
        padding: 8px 14px;
        cursor: pointer;
        border-radius: 3px;
        font-size: 13px;
        width: 100%;
        margin-bottom: 8px;
    }
    button:hover { background: var(--vscode-button-hoverBackground); }
    button.secondary {
        background: var(--vscode-button-secondaryBackground, transparent);
        color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
        border: 1px solid var(--vscode-panel-border);
    }
    button.secondary:hover { background: var(--vscode-toolbar-hoverBackground); }
    p { color: var(--vscode-descriptionForeground); font-size: 12px; }
</style>
</head>
<body>
    <p>projspec manages a library of projects, scans directories for known
    project types, and can build/run their artifacts.</p>
    <button onclick="acquireVsCodeApi().postMessage({cmd:'open'})">Open Project Library</button>
    <button class="secondary" onclick="acquireVsCodeApi().postMessage({cmd:'openFileBrowser'})">&#128193; Open File Browser</button>
</body>
</html>`;
    }
}
