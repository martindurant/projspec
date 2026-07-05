/**
 * FileBrowserPanel — VS Code webview panel for browsing local and remote
 * filesystems via fsspec / projspec filebrowser subcommands.
 *
 * Features
 * --------
 * - Browse any fsspec-supported URL (local, S3, GCS, HTTP, FTP, …)
 * - Bookmarks / favourites with custom labels
 * - Double-click a directory to descend; "Up" button or breadcrumb to ascend
 * - Info panel alongside the file tree showing file metadata, text preview
 *   or intake summary for the selected file
 * - Normal filesystem operations: create file, rename/move, delete
 * - Open any text-type file in VS Code for editing (up to configurable limit)
 * - Add any directory to the projspec project library
 * - Storage-options (credentials) per protocol / session
 */

import * as vscode from 'vscode';
import * as childProcess from 'child_process';
import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';
import { parseJsonOutput } from './projspec';

// ---------------------------------------------------------------------------
// Output channel for host-side logging (visible in Output > projspec-filebrowser)
// ---------------------------------------------------------------------------
let _log: vscode.OutputChannel | undefined;
function log(msg: string): void {
    if (!_log) { _log = vscode.window.createOutputChannel('projspec-filebrowser'); }
    _log.appendLine('[' + new Date().toISOString() + '] ' + msg);
}

// ---------------------------------------------------------------------------
// Python runner — calls projspec.filebrowser functions directly via python3
// without depending on the `projspec` binary being installed or up-to-date.
// ---------------------------------------------------------------------------

/** Path to python3 (override with PROJSPEC_PYTHON env var). */
function python3(): string {
    return process.env['PROJSPEC_PYTHON'] || 'python3';
}

/**
 * Run a projspec.filebrowser function by spawning python3 -c.
 * The script is self-contained: it adds the projspec src path, imports the
 * module, calls fn(**kwargs), then prints JSON.
 */
function runFbPython(fn: string, kwargs: Record<string, unknown>): Promise<{ data: unknown; stderr: string; code: number | null }> {
    // Load filebrowser.py directly via importlib so we don't trigger
    // projspec/__init__.py (which pulls in yaml, toml, etc. that may not be
    // installed in every environment).
    const fbPath = path.resolve(__dirname, '..', '..', 'src', 'projspec', 'filebrowser.py');
    const script = [
        'import sys, json, importlib.util',
        `spec = importlib.util.spec_from_file_location("projspec_filebrowser", ${JSON.stringify(fbPath)})`,
        'mod = importlib.util.module_from_spec(spec)',
        'spec.loader.exec_module(mod)',
        'kwargs = json.loads(sys.argv[1])',
        `result = getattr(mod, ${JSON.stringify(fn)})(**kwargs)`,
        'print(json.dumps(result))',
    ].join('\n');
    const kwargsStr = JSON.stringify(kwargs);
    log(`runFbPython ${fn} ${kwargsStr.slice(0, 100)}`);
    return new Promise((resolve) => {
        const proc = childProcess.spawn(python3(), ['-c', script, kwargsStr], { env: process.env });
        let stdout = '';
        let stderr = '';
        proc.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
        proc.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
        proc.on('error', (err: Error) => {
            log(`spawn error: ${err}`);
            resolve({ data: null, stderr: String(err), code: -1 });
        });
        proc.on('close', (code: number | null) => {
            log(`exit ${code} stdout=${stdout.slice(0, 200)} stderr=${stderr.slice(0, 200)}`);
            let data: unknown = null;
            try { data = parseJsonOutput(stdout); } catch { /* ok */ }
            resolve({ data, stderr, code });
        });
    });
}

/** Convenience: call fn and return parsed data, or throw with stderr on failure. */
async function fbCall(fn: string, kwargs: Record<string, unknown>): Promise<unknown> {
    const res = await runFbPython(fn, kwargs);
    if (res.code !== 0 || res.data === null) {
        throw new Error(`${fn} failed (exit ${res.code}): ${res.stderr.trim() || 'no output'}`);
    }
    return res.data;
}

// ---------------------------------------------------------------------------
// FileBrowserPanel
// ---------------------------------------------------------------------------

export class FileBrowserPanel {
    public static current: FileBrowserPanel | undefined;
    private readonly panel: vscode.WebviewPanel;
    private readonly extensionUri: vscode.Uri;
    private disposables: vscode.Disposable[] = [];
    private busyCount = 0;
    /** Pending initial data to send once the webview posts 'ready'. */
    private pendingInit: { bookmarks: unknown[]; protocols: string[]; startUrl: string } | null = null;

    // ---------------------------------------------------------------------------
    // Factory
    // ---------------------------------------------------------------------------
    public static createOrShow(extensionUri: vscode.Uri, initialUrl?: string): void {
        const col = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
        if (FileBrowserPanel.current) {
            FileBrowserPanel.current.panel.reveal(col);
            if (initialUrl) {
                FileBrowserPanel.current.navigateTo(initialUrl);
            }
            return;
        }
        const panel = vscode.window.createWebviewPanel(
            'projspec.filebrowser',
            'File Browser',
            col,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
            },
        );
        FileBrowserPanel.current = new FileBrowserPanel(panel, extensionUri, initialUrl);
    }

    // ---------------------------------------------------------------------------
    // Constructor
    // ---------------------------------------------------------------------------
    private constructor(
        panel: vscode.WebviewPanel,
        extensionUri: vscode.Uri,
        initialUrl?: string,
    ) {
        this.panel = panel;
        this.extensionUri = extensionUri;

        this.panel.webview.html = this.getHtml();
        this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
        this.panel.webview.onDidReceiveMessage(
            (msg) => void this.onMessage(msg),
            null,
            this.disposables,
        );

        // Pre-fetch bookmarks + protocols in the background so we are ready
        // when the webview fires 'ready'.  We do NOT post any messages until
        // then – the webview's message listener isn't registered yet.
        void this.prefetchInit(initialUrl);
    }

    // ---------------------------------------------------------------------------
    // Busy counter
    // ---------------------------------------------------------------------------
    private withBusy<T>(fn: () => Promise<T>): Promise<T> {
        this.busyCount += 1;
        if (this.busyCount === 1) {
            this.panel.webview.postMessage({ type: 'loading', loading: true });
        }
        return fn().finally(() => {
            this.busyCount -= 1;
            if (this.busyCount === 0) {
                this.panel.webview.postMessage({ type: 'loading', loading: false });
            }
        });
    }

    // ---------------------------------------------------------------------------
    // Initialisation
    // ---------------------------------------------------------------------------

    /** Run the cheap subprocess calls while the webview is still rendering. */
    private async prefetchInit(startUrl?: string): Promise<void> {
        log('prefetchInit start');
        let bookmarks: unknown[] = [];
        let protocols: string[] = [];
        try {
            const bmsRes = await runFbPython('bookmarks_list', {});
            if (Array.isArray(bmsRes.data)) { bookmarks = bmsRes.data; }
        } catch (e) { log('bookmarks_list error: ' + e); }
        try {
            const protoRes = await runFbPython('supported_protocols', {});
            if (Array.isArray(protoRes.data)) { protocols = protoRes.data as string[]; }
        } catch (e) { log('supported_protocols error: ' + e); }
        log(`prefetchInit done: ${bookmarks.length} bookmarks, ${protocols.length} protocols`);
        this.pendingInit = { bookmarks, protocols, startUrl: startUrl || os.homedir() };
        if (this.readyReceived) {
            void this.sendInitialData();
        }
    }

    private readyReceived = false;

    private async sendInitialData(): Promise<void> {
        const init = this.pendingInit;
        if (!init) { return; }
        this.pendingInit = null;
        log('sendInitialData: posting init + browse ' + init.startUrl);
        await this.withBusy(async () => {
            this.panel.webview.postMessage({
                type: 'init',
                bookmarks: init.bookmarks,
                protocols: init.protocols,
            });
            await this.browse(init.startUrl, undefined, false);
        });
    }

    private navigateTo(url: string): void {
        void this.withBusy(() => this.browse(url, undefined, false));
    }

    // ---------------------------------------------------------------------------
    // Message handling
    // ---------------------------------------------------------------------------
    private async onMessage(msg: Record<string, unknown>): Promise<void> {
        const cmd = msg.cmd as string;
        log('onMessage cmd=' + cmd + (msg.url ? ' url=' + msg.url : ''));
        try {
            switch (cmd) {
                case 'ready':
                    // The webview's message listener is now live. Send initial data.
                    log('ready received, readyReceived was ' + this.readyReceived + ', pendingInit=' + !!this.pendingInit);
                    this.readyReceived = true;
                    if (this.pendingInit) {
                        void this.sendInitialData();
                    }
                    break;

                case 'browse':
                    // msg.push === false means a refresh (no history entry);
                    // anything else (including undefined) pushes history.
                    await this.withBusy(() =>
                        this.browse(
                            msg.url as string,
                            msg.storageOptions as string | undefined,
                            msg.push !== false,
                        ),
                    );
                    break;

                case 'inspect':
                    await this.withBusy(() =>
                        this.inspect(msg.url as string, msg.storageOptions as string | undefined),
                    );
                    break;

                case 'openFile':
                    await this.openFileInEditor(
                        msg.url as string,
                        msg.storageOptions as string | undefined,
                        msg.maxBytes as number | undefined,
                    );
                    break;

                case 'writeFile':
                    await this.writeFile(
                        msg.url as string,
                        msg.content as string,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'createFile':
                    await this.createFile(
                        msg.parentUrl as string,
                        msg.name as string,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'deleteEntry':
                    await this.deleteEntry(
                        msg.url as string,
                        msg.isDir as boolean,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'renameEntry':
                    await this.renameEntry(
                        msg.url as string,
                        msg.newName as string,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'mkdir':
                    await this.mkdirEntry(
                        msg.parentUrl as string,
                        msg.name as string,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'addBookmark':
                    await this.bookmarkAdd(
                        msg.url as string,
                        msg.label as string | undefined,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'removeBookmark':
                    await this.bookmarkRemove(msg.url as string);
                    break;

                case 'addToLibrary':
                    await this.addToLibrary(
                        msg.url as string,
                        msg.storageOptions as string | undefined,
                    );
                    break;

                case 'scanDir':
                    await this.withBusy(() =>
                        this.scanDir(msg.url as string, msg.storageOptions as string | undefined),
                    );
                    break;

                case 'goToUrl':
                    await this.withBusy(() =>
                        this.goToUrl(msg.url as string, msg.storageOptions as string | undefined),
                    );
                    break;

                default:
                    log('unknown cmd: ' + cmd);
                    console.warn('[FileBrowser] Unknown cmd:', cmd);
            }
        } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            log('onMessage error: ' + message);
            vscode.window.showErrorMessage(`File Browser: ${message}`);
            this.panel.webview.postMessage({ type: 'error', message });
        }
    }

    // ---------------------------------------------------------------------------
    // Backend operations
    // ---------------------------------------------------------------------------

    private async browse(
        url: string,
        storageOptions: string | undefined,
        pushHistory: boolean,
    ): Promise<void> {
        log('browse ' + url);
        const so = storageOptions ? JSON.parse(storageOptions) : null;
        const res = await runFbPython('browse', so ? { url, storage_options: so } : { url });
        const data = (res.data as Record<string, unknown>) || {
            url, entries: [], parent: null, protocol: '',
            error: res.stderr || `exit ${res.code}`,
        };
        log('browse result: ' + (data.error || `${(data.entries as unknown[])?.length} entries`));
        this.panel.webview.postMessage({
            type: 'browseResult',
            pushHistory,
            storageOptions: storageOptions || '',
            ...data,
        });
    }

    private async inspect(url: string, storageOptions: string | undefined): Promise<void> {
        log('inspect ' + url);
        const so = storageOptions ? JSON.parse(storageOptions) : null;
        const res = await runFbPython('inspect_file', so ? { url, storage_options: so } : { url });
        const data = (res.data as Record<string, unknown>) || { url, error: res.stderr || `exit ${res.code}` };
        log('inspect result: ' + (data.error || data.mime_type));
        this.panel.webview.postMessage({ type: 'inspectResult', ...data });
    }

    private async openFileInEditor(
        url: string,
        storageOptions: string | undefined,
        maxBytes?: number,
    ): Promise<void> {
        await this.withBusy(async () => {
            // For local file:// URLs, just open directly
            if (url.startsWith('file://') || !url.includes('://')) {
                const localPath = url.startsWith('file://') ? url.slice('file://'.length) : url;
                const doc = await vscode.workspace.openTextDocument(localPath);
                await vscode.window.showTextDocument(doc);
                return;
            }
            // For remote files, fetch via Python then open a temp file
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url };
            if (so) { kwargs['storage_options'] = so; }
            if (maxBytes) { kwargs['max_bytes'] = maxBytes; }
            const res = await runFbPython('read_file', kwargs);
            const data = res.data as Record<string, unknown>;
            if (!data || data['error']) { throw new Error((data?.['error'] as string) || res.stderr || 'Failed to read file'); }
            const ext = path.extname(url) || '.txt';
            const tmpFile = path.join(os.tmpdir(), `projspec_fb_${Date.now()}${ext}`);
            fs.writeFileSync(tmpFile, (data['content'] as string) || '', 'utf-8');
            const doc = await vscode.workspace.openTextDocument(tmpFile);
            await vscode.window.showTextDocument(doc);
            this._openedRemoteFiles.set(tmpFile, { url, storageOptions: storageOptions || '' });
            const sub = vscode.workspace.onDidSaveTextDocument(async (saved) => {
                if (saved.fileName === tmpFile) {
                    await this.pushRemoteFile(tmpFile, url, storageOptions);
                }
            });
            this.disposables.push(sub);
        });
    }

    private _openedRemoteFiles = new Map<string, { url: string; storageOptions: string }>();

    private async pushRemoteFile(tmpFile: string, remoteUrl: string, storageOptions: string | undefined): Promise<void> {
        const content = fs.readFileSync(tmpFile, 'utf-8');
        const so = storageOptions ? JSON.parse(storageOptions) : null;
        const kwargs: Record<string, unknown> = { url: remoteUrl, content };
        if (so) { kwargs['storage_options'] = so; }
        const res = await runFbPython('write_file', kwargs);
        const data = res.data as Record<string, unknown>;
        if (data?.['error']) { vscode.window.showErrorMessage(`Save failed: ${data['error']}`); }
        else { vscode.window.showInformationMessage(`Saved to ${remoteUrl}`); }
    }

    private async writeFile(url: string, content: string, storageOptions: string | undefined): Promise<void> {
        await this.withBusy(async () => {
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url, content };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('write_file', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) { throw new Error(data['error'] as string); }
            const parent = url.replace(/\/?[^/]+$/, '') || '/';
            await this.browse(parent, storageOptions, false);
        });
    }

    private async createFile(parentUrl: string, name: string, storageOptions: string | undefined): Promise<void> {
        await this.withBusy(async () => {
            const newUrl = parentUrl.replace(/\/$/, '') + '/' + name;
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url: newUrl, content: '' };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('write_file', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) { throw new Error(data['error'] as string); }
            await this.browse(parentUrl, storageOptions, false);
        });
    }

    private async deleteEntry(url: string, isDir: boolean, storageOptions: string | undefined): Promise<void> {
        const label = url.split('/').pop() || url;
        const confirm = await vscode.window.showWarningMessage(`Delete "${label}"?`, { modal: true }, 'Delete');
        if (confirm !== 'Delete') { return; }
        await this.withBusy(async () => {
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url, recursive: isDir };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('delete', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) { throw new Error(data['error'] as string); }
            const parent = url.replace(/\/?[^/]+$/, '') || '/';
            await this.browse(parent, storageOptions, false);
        });
    }

    private async renameEntry(url: string, newName: string, storageOptions: string | undefined): Promise<void> {
        await this.withBusy(async () => {
            const parent = url.replace(/\/?[^/]+$/, '') || '/';
            const dst = parent.replace(/\/$/, '') + '/' + newName;
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { src: url, dst };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('move', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) { throw new Error(data['error'] as string); }
            await this.browse(parent, storageOptions, false);
        });
    }

    private async mkdirEntry(parentUrl: string, name: string, storageOptions: string | undefined): Promise<void> {
        await this.withBusy(async () => {
            const newUrl = parentUrl.replace(/\/$/, '') + '/' + name;
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url: newUrl };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('mkdir', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) { throw new Error(data['error'] as string); }
            await this.browse(parentUrl, storageOptions, false);
        });
    }

    private async bookmarkAdd(url: string, label?: string, storageOptions?: string): Promise<void> {
        const kwargs: Record<string, unknown> = { url };
        if (label) { kwargs['label'] = label; }
        if (storageOptions) {
            try { kwargs['storage_options'] = JSON.parse(storageOptions); } catch { /* ignore invalid SO */ }
        }
        const res = await runFbPython('bookmark_add', kwargs);
        const bms = Array.isArray(res.data) ? res.data : [];
        this.panel.webview.postMessage({ type: 'bookmarksUpdated', bookmarks: bms });
    }

    private async bookmarkRemove(url: string): Promise<void> {
        const res = await runFbPython('bookmark_remove', { url });
        const bms = Array.isArray(res.data) ? res.data : [];
        this.panel.webview.postMessage({ type: 'bookmarksUpdated', bookmarks: bms });
    }

    private async addToLibrary(url: string, storageOptions: string | undefined): Promise<void> {
        await this.withBusy(async () => {
            const so = storageOptions ? JSON.parse(storageOptions) : null;
            const kwargs: Record<string, unknown> = { url };
            if (so) { kwargs['storage_options'] = so; }
            const res = await runFbPython('add_to_projspec_library', kwargs);
            const data = res.data as Record<string, unknown>;
            if (data?.['error']) {
                vscode.window.showWarningMessage(`Add to library: ${data['error']}`);
            } else {
                vscode.window.showInformationMessage(`Added to projspec library: ${url}`);
            }
        });
    }

    private async goToUrl(url: string, storageOptions: string | undefined): Promise<void> {
        await this.browse(url, storageOptions, true);
    }

    private async scanDir(url: string, storageOptions: string | undefined): Promise<void> {
        log('scanDir ' + url);
        const so = storageOptions ? JSON.parse(storageOptions) : null;
        const srcPath = path.resolve(__dirname, '..', '..', 'src');
        const soArg = so ? JSON.stringify(so) : '';
        // Try projspec CLI first (fastest when projspec is installed in the env).
        // Fall back to importing projspec.filebrowser.scan_directory directly.
        const result = await new Promise<{data: unknown; stderr: string; code: number|null}>((resolve) => {
            const script = [
                'import sys, json, subprocess',
                `sys.path.insert(0, ${JSON.stringify(srcPath)})`,
                `url = ${JSON.stringify(url)}`,
                `so_str = ${JSON.stringify(soArg)}`,
                'args = ["projspec", "scan", "--json-out", url]',
                'if so_str: args = ["projspec", "scan", "--json-out", "--storage_options", so_str, url]',
                'r = subprocess.run(args, capture_output=True, text=True)',
                'if r.returncode == 0 and r.stdout.strip():',
                '    raw = r.stdout.strip()',
                '    idx = raw.find("{")',
                '    if idx >= 0: raw = raw[idx:]',
                '    proj = json.loads(raw)',
                '    print(json.dumps({"url": url, "project": proj, "error": None}))',
                '    sys.exit(0)',
                // Fall back to direct import
                'try:',
                '    from projspec.filebrowser import scan_directory',
                '    so = json.loads(so_str) if so_str else None',
                '    result = scan_directory(url, storage_options=so)',
                'except Exception as e:',
                '    result = {"url": url, "project": None, "error": str(e)}',
                'print(json.dumps(result))',
            ].join('\n');
            const proc = childProcess.spawn(python3(), ['-c', script], { env: process.env });
            let stdout = '', stderr = '';
            proc.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
            proc.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
            proc.on('error', (err: Error) => resolve({ data: null, stderr: String(err), code: -1 }));
            proc.on('close', (code: number | null) => {
                log(`scanDir exit=${code} out=${stdout.slice(0,200)} err=${stderr.slice(0,200)}`);
                let data: unknown = null;
                try { data = JSON.parse(stdout.trim()); } catch { /* ok */ }
                resolve({ data, stderr, code });
            });
        });
        const data = (result.data as Record<string, unknown>) || { url, error: result.stderr || `exit ${result.code}` };
        this.panel.webview.postMessage({ type: 'projectScanned', ...data });
    }

    // ---------------------------------------------------------------------------
    // HTML
    // ---------------------------------------------------------------------------
    private getHtml(): string {
        const webview = this.panel.webview;
        const nonce = getNonce();
        const csp = [
            `default-src 'none'`,
            `style-src ${webview.cspSource} 'unsafe-inline'`,
            `img-src ${webview.cspSource} data:`,
            `script-src 'nonce-${nonce}'`,
        ].join('; ');

        const css = getFileBrowserCss();
        const js  = getFileBrowserJs();

        // Read shared panel assets from the projspec webui package on disk.
        // Done synchronously here so the assets are in the original HTML
        // response — all three <script nonce> blocks below carry the page
        // nonce and are therefore allowed by the webview CSP.
        const webuiDir = path.resolve(__dirname, '..', '..', 'src', 'projspec', 'webui');
        let panelCss = '', panelJs = '', panelBodyHtml = '';
        try {
            panelJs  = fs.readFileSync(path.join(webuiDir, 'panel.js'),   'utf-8');
            panelCss = fs.readFileSync(path.join(webuiDir, 'panel.css'),  'utf-8');
            const rawHtml = fs.readFileSync(path.join(webuiDir, 'panel.html'), 'utf-8');
            const icons   = JSON.parse(fs.readFileSync(path.join(webuiDir, 'chrome.json'), 'utf-8'));
            // Substitute icon placeholders, inline CSS, remove the <script> block
            // (we re-emit it as a separate nonce-tagged block below)
            let html = rawHtml;
            for (const [key, glyph] of Object.entries(icons) as [string, string][]) {
                html = html.split(`<!--ICON:${key}-->`).join(glyph);
            }
            html = html.replace('/*__CSS__*/', panelCss);
            html = html.replace('<script>/*__JS__*/</script>', '');
            html = html.replace('<!--BOOTSTRAP-->', '');
            const bodyStart = html.indexOf('<body>') + '<body>'.length;
            const bodyEnd   = html.lastIndexOf('</body>');
            panelBodyHtml = html.slice(bodyStart, bodyEnd).trim();
            log(`panel assets: css=${panelCss.length} js=${panelJs.length} html=${panelBodyHtml.length}`);
        } catch (e) {
            log(`panel asset read error: ${e}`);
        }

        // Transport bootstrap: runs BEFORE panel.js so projspecTransport is
        // set when panel.js starts. Written as a plain string so it does not
        // contain any template-literal backticks that could confuse editors.
        const bootstrap = '(function(){'
            + 'var root=document.getElementById("fb-scan-panel-root");'
            + 'if(!root)return;'
            + 'var pending=[];'
            + 'window.__fbPanelDeliver=function(msg){'
            + 'if(window.__fbPanelDispatch){window.__fbPanelDispatch(msg);}'
            + 'else{pending.push(msg);}};'
            + 'window.projspecRoot=root;'
            + 'window.projspecTransport={'
            + 'send:function(){},'
            + 'onReady:function(d){'
            + 'window.__fbPanelDispatch=d;'
            + 'pending.forEach(function(m){d(m);});pending=[];'
            + 'delete window.projspecRoot;delete window.projspecTransport;'
            + '}};'
            + '})()';

        const pageBody = FB_HTML_BODY.replace(
            '<div id="fb-scan-panel-root"></div>',
            `<div id="fb-scan-panel-root">${panelBodyHtml}</div>`
        );

        return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="${csp}" />
<style>${css}
${panelCss}</style>
<title>File Browser</title>
</head>
<body>
${pageBody}
<script nonce="${nonce}">${bootstrap}</script>
<script nonce="${nonce}">${panelJs}</script>
<script nonce="${nonce}">${js}</script>
</body>
</html>`;
    }

    // ---------------------------------------------------------------------------
    // Dispose
    // ---------------------------------------------------------------------------
    private dispose(): void {
        FileBrowserPanel.current = undefined;
        this.panel.dispose();
        while (this.disposables.length) {
            const d = this.disposables.pop();
            if (d) { d.dispose(); }
        }
    }
}

// ---------------------------------------------------------------------------
// Nonce helper
// ---------------------------------------------------------------------------
function getNonce(): string {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    return Array.from({ length: 32 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

// ---------------------------------------------------------------------------
// HTML body
// ---------------------------------------------------------------------------
const FB_HTML_BODY = `
<div id="fb-app">

  <!-- Left: tree pane -->
  <div id="fb-tree-pane">
    <div id="fb-toolbar">
      <button id="btn-back"    class="fb-icon-btn" title="Back">&#9668;</button>
      <button id="btn-up"      class="fb-icon-btn" title="Up one level">&#8679;</button>
      <button id="btn-refresh" class="fb-icon-btn" title="Refresh">&#8635;</button>
      <button id="btn-bm-dropdown" class="fb-icon-btn" title="Bookmarks">&#9733;</button>
      <button id="btn-so"      class="fb-icon-btn" title="Storage options">&#128273;</button>
      <div class="fb-spacer"></div>
      <button id="btn-new-file" class="fb-icon-btn" title="New file">+F</button>
      <button id="btn-new-dir"  class="fb-icon-btn" title="New folder">+D</button>
    </div>

    <div id="fb-url-bar">
      <input id="fb-url-input" type="text" spellcheck="false" autocomplete="off" placeholder="Enter URL or path" />
      <button id="btn-go" class="fb-go-btn">Go</button>
    </div>

    <div id="fb-breadcrumb"></div>

    <!-- fb-empty and fb-error are SIBLINGS of fb-entries, not children,
         so clearing fb-entries does not destroy them. -->
    <div id="fb-file-list">
      <div id="fb-empty"   class="fb-status hidden">Directory is empty.</div>
      <div id="fb-error"   class="fb-status fb-error hidden"></div>
      <div id="fb-entries"></div>
    </div>

    <div id="fb-debug" style="padding:4px 8px;font-size:10px;font-family:monospace;color:var(--vscode-descriptionForeground);border-top:1px solid var(--vscode-panel-border);max-height:80px;overflow-y:auto;"></div>

    <div id="fb-spinner" class="fb-spinner hidden">
      <span class="spin">&#9203;</span> Loading...
    </div>
  </div>

  <!-- Right: info / preview pane (vertical split: meta top, scan/projspec bottom) -->
  <div id="fb-info-pane">
    <div id="fb-info-header">
      <div id="fb-info-title">No file selected</div>
      <div id="fb-info-actions" class="hidden">
        <button id="btn-open-editor" class="primary" title="Open in editor">Open</button>
        <button id="btn-add-to-lib"  title="Add to projspec library">+ Library</button>
        <button id="btn-bookmark"    title="Bookmark this location">Bookmark</button>
        <button id="btn-delete-sel"  class="danger"  title="Delete">Delete</button>
        <button id="btn-rename-sel"  title="Rename">Rename</button>
      </div>
    </div>
    <div id="fb-info-top">
      <div id="fb-info-meta"></div>
      <div id="fb-info-preview"></div>
     </div>
     <div id="fb-scan-pane" class="hidden">
       <div id="fb-scan-panel-root"></div>
     </div>
  </div>

</div>

<!-- Bookmarks dropdown -->
<div id="bm-panel" class="hidden">
  <div class="bm-header">Bookmarks <button id="bm-close" class="fb-icon-btn">X</button></div>
  <div id="bm-list"></div>
  <div class="bm-footer">
    <button id="btn-bm-add-current">+ Bookmark current location</button>
  </div>
</div>

<!-- Storage-options modal -->
<div id="so-overlay" class="overlay hidden">
  <div class="fb-modal" role="dialog">
    <div class="fb-modal-title">Storage Options</div>
    <div class="fb-modal-body">
      <p class="hint">JSON dictionary of fsspec storage options (credentials, endpoints, etc.).</p>
      <label for="so-input">Storage options (JSON):</label>
      <textarea id="so-input" rows="5" spellcheck="false" placeholder='{"key": "...", "secret": "..."}'></textarea>
    </div>
    <div class="fb-modal-footer">
      <button id="so-cancel" class="secondary">Cancel</button>
      <button id="so-ok" class="primary">Apply</button>
    </div>
  </div>
</div>

<!-- New-file/dir modal -->
<div id="newentry-overlay" class="overlay hidden">
  <div class="fb-modal" role="dialog">
    <div class="fb-modal-title" id="newentry-title">New file</div>
    <div class="fb-modal-body">
      <label for="newentry-name">Name:</label>
      <input type="text" id="newentry-name" autocomplete="off" spellcheck="false" />
    </div>
    <div class="fb-modal-footer">
      <button id="newentry-cancel" class="secondary">Cancel</button>
      <button id="newentry-ok" class="primary">Create</button>
    </div>
  </div>
</div>

<!-- Rename modal -->
<div id="rename-overlay" class="overlay hidden">
  <div class="fb-modal" role="dialog">
    <div class="fb-modal-title">Rename</div>
    <div class="fb-modal-body">
      <label for="rename-input">New name:</label>
      <input type="text" id="rename-input" autocomplete="off" spellcheck="false" />
    </div>
    <div class="fb-modal-footer">
      <button id="rename-cancel" class="secondary">Cancel</button>
      <button id="rename-ok" class="primary">Rename</button>
    </div>
  </div>
</div>
`;

// ---------------------------------------------------------------------------
// CSS
// ---------------------------------------------------------------------------
function getFileBrowserCss(): string {
    return `
/* reset / base */
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; padding: 0;
  font-family: var(--vscode-font-family);
  color: var(--vscode-foreground);
  background: var(--vscode-editor-background);
  font-size: 13px;
}

/* layout */
#fb-app {
  display: flex;
  height: 100vh;
  overflow: hidden;
}
#fb-tree-pane {
  width: 45%;
  min-width: 260px;
  max-width: 600px;
  border-right: 1px solid var(--vscode-panel-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}
#fb-info-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}
#fb-info-top {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex: 0 0 auto;
  max-height: 45%;
}
#fb-scan-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  border-top: 2px solid var(--vscode-panel-border);
  overflow: hidden;
  min-height: 0;
}
#fb-scan-panel-root {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#fb-scan-panel-root #app {
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
#fb-scan-panel-root #library {
  width: 100% !important;
  max-width: 100% !important;
  border-right: none !important;
  flex: 0 0 auto;
  max-height: 50%;
  overflow: hidden;
}
#fb-scan-panel-root .toolbar,
#fb-scan-panel-root .search,
#fb-scan-panel-root #spinner { display: none !important; }
#fb-scan-panel-root #projects { flex: 1; overflow-y: auto; padding: 4px; }
#fb-scan-panel-root #details {
  flex: 1;
  border-top: 1px solid var(--vscode-panel-border);
  overflow: hidden;
  min-height: 0;
}

/* toolbar */
#fb-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 5px 6px;
  border-bottom: 1px solid var(--vscode-panel-border);
}
.fb-icon-btn {
  background: transparent;
  border: none;
  color: var(--vscode-foreground);
  cursor: pointer;
  padding: 3px 7px;
  border-radius: 3px;
  font-size: 13px;
  line-height: 1.2;
}
.fb-icon-btn:hover { background: var(--vscode-toolbar-hoverBackground); }
.fb-icon-btn:disabled { opacity: 0.4; cursor: default; }
.fb-spacer { flex: 1; }
.fb-go-btn {
  background: var(--vscode-button-background);
  color: var(--vscode-button-foreground);
  border: none;
  cursor: pointer;
  padding: 4px 10px;
  border-radius: 3px;
  font-size: 12px;
}
.fb-go-btn:hover { background: var(--vscode-button-hoverBackground); }

/* URL bar */
#fb-url-bar {
  display: flex;
  padding: 4px 6px;
  gap: 4px;
  border-bottom: 1px solid var(--vscode-panel-border);
}
#fb-url-input {
  flex: 1;
  background: var(--vscode-input-background);
  color: var(--vscode-input-foreground);
  border: 1px solid var(--vscode-input-border, transparent);
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 2px;
  outline: none;
  font-family: var(--vscode-editor-font-family, monospace);
}
#fb-url-input:focus { border-color: var(--vscode-focusBorder); }

/* breadcrumb */
#fb-breadcrumb {
  padding: 3px 8px;
  font-size: 11px;
  color: var(--vscode-descriptionForeground);
  border-bottom: 1px solid var(--vscode-panel-border);
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  min-height: 22px;
  align-items: center;
}
.bc-seg {
  cursor: pointer;
  color: var(--vscode-textLink-foreground);
  text-decoration: none;
}
.bc-seg:hover { text-decoration: underline; }
.bc-sep { color: var(--vscode-descriptionForeground); }

/* file list */
#fb-file-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.fb-entry {
  display: flex;
  align-items: center;
  padding: 3px 10px;
  cursor: pointer;
  gap: 6px;
  border: 1px solid transparent;
  border-radius: 2px;
  position: relative;
}
.fb-entry:hover { background: var(--vscode-list-hoverBackground); }
.fb-entry.active {
  background: var(--vscode-list-activeSelectionBackground);
  color: var(--vscode-list-activeSelectionForeground);
}
.fb-entry-icon { width: 16px; text-align: center; flex-shrink: 0; font-size: 14px; }
.fb-entry-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fb-entry-meta { font-size: 11px; color: var(--vscode-descriptionForeground); white-space: nowrap; flex-shrink: 0; }
.fb-entry.is-dir .fb-entry-name { font-weight: 500; }

.fb-status { padding: 20px; color: var(--vscode-descriptionForeground); text-align: center; }
.fb-error  { padding: 12px; color: var(--vscode-errorForeground); }

/* spinner */
#fb-spinner {
  position: absolute;
  bottom: 8px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--vscode-editorWidget-background);
  border: 1px solid var(--vscode-panel-border);
  border-radius: 12px;
  padding: 4px 12px;
  font-size: 12px;
  z-index: 20;
}
.spin { display: inline-block; animation: spin 1.2s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* info pane */
#fb-info-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--vscode-panel-border);
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 6px;
}
#fb-info-title {
  font-weight: bold;
  font-size: 13px;
  flex: 1;
  min-width: 0;
  word-break: break-all;
}
#fb-info-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
#fb-info-actions button {
  background: var(--vscode-button-secondaryBackground, var(--vscode-button-background));
  color: var(--vscode-button-secondaryForeground, var(--vscode-button-foreground));
  border: none;
  cursor: pointer;
  padding: 3px 9px;
  border-radius: 3px;
  font-size: 11px;
}
#fb-info-actions button:hover { background: var(--vscode-button-hoverBackground); }
#fb-info-actions button.primary {
  background: var(--vscode-button-background);
  color: var(--vscode-button-foreground);
}
#fb-info-actions button.danger {
  background: var(--vscode-errorForeground, #f44);
  color: #fff;
}
#fb-info-meta {
  padding: 10px 12px;
  font-size: 12px;
  color: var(--vscode-descriptionForeground);
  border-bottom: 1px solid var(--vscode-panel-border);
}
.info-row { display: flex; gap: 6px; margin-bottom: 4px; }
.info-key { font-weight: 600; color: var(--vscode-foreground); min-width: 100px; }
#fb-info-preview {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px;
}
.preview-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--vscode-descriptionForeground);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.preview-text {
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--vscode-textBlockQuote-background, rgba(128,128,128,0.1));
  padding: 8px;
  border-radius: 3px;
  max-height: 280px;
  overflow-y: auto;
}
.intake-card {
  background: var(--vscode-editorWidget-background);
  border: 1px solid var(--vscode-panel-border);
  border-radius: 4px;
  padding: 8px 10px;
  font-size: 12px;
}
.intake-type-badge {
  display: inline-block;
  background: var(--vscode-badge-background, #4d4d4d);
  color: var(--vscode-badge-foreground, #fff);
  border-radius: 4px;
  padding: 2px 8px;
  font-weight: 600;
  font-size: 12px;
  margin-bottom: 6px;
}
.intake-schema-hdr {
  font-weight: 600;
  font-size: 11px;
  color: var(--vscode-descriptionForeground);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin: 6px 0 3px;
}
.intake-col-row {
  display: flex;
  gap: 8px;
  padding: 1px 0;
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: 11px;
}
.intake-col-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.intake-col-dtype { color: var(--vscode-descriptionForeground); flex-shrink: 0; }
.intake-row { display: flex; gap: 6px; margin-bottom: 3px; flex-wrap: wrap; }
.intake-key { font-weight: 600; min-width: 80px; }
.intake-val { word-break: break-all; }

/* Bookmarks panel */
#bm-panel {
  position: absolute;
  top: 38px;
  left: 6px;
  z-index: 50;
  background: var(--vscode-menu-background, var(--vscode-editorWidget-background));
  color: var(--vscode-menu-foreground, var(--vscode-foreground));
  border: 1px solid var(--vscode-menu-border, var(--vscode-panel-border));
  border-radius: 4px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.35);
  min-width: 260px;
  max-width: 340px;
  max-height: 360px;
  display: flex;
  flex-direction: column;
}
.bm-header {
  display: flex;
  align-items: center;
  padding: 7px 10px;
  font-weight: bold;
  font-size: 12px;
  border-bottom: 1px solid var(--vscode-panel-border);
  gap: 6px;
}
.bm-header .fb-icon-btn { margin-left: auto; }
#bm-list {
  flex: 1;
  overflow-y: auto;
}
.bm-item {
  display: flex;
  align-items: center;
  padding: 5px 10px;
  gap: 6px;
  cursor: pointer;
  font-size: 12px;
}
.bm-item:hover { background: var(--vscode-list-hoverBackground); }
.bm-item-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bm-item-url { font-size: 10px; color: var(--vscode-descriptionForeground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bm-item-so { font-size: 10px; color: var(--vscode-descriptionForeground); font-style: italic; margin-top: 1px; }
.bm-remove {
  background: transparent; border: none; cursor: pointer;
  color: var(--vscode-descriptionForeground); padding: 1px 4px;
  border-radius: 2px; font-size: 11px;
}
.bm-remove:hover { color: var(--vscode-errorForeground); }
.bm-empty { padding: 12px 10px; color: var(--vscode-descriptionForeground); font-size: 12px; }
.bm-footer {
  border-top: 1px solid var(--vscode-panel-border);
  padding: 6px 10px;
}
.bm-footer button {
  background: transparent;
  border: none;
  color: var(--vscode-textLink-foreground);
  cursor: pointer;
  font-size: 11px;
  padding: 0;
}
.bm-footer button:hover { text-decoration: underline; }

/* Modals / overlays */
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}
.fb-modal {
  background: var(--vscode-editorWidget-background);
  color: var(--vscode-editorWidget-foreground, var(--vscode-foreground));
  border: 1px solid var(--vscode-editorWidget-border, var(--vscode-panel-border));
  border-radius: 6px;
  min-width: 340px;
  max-width: 85%;
  box-shadow: 0 8px 24px rgba(0,0,0,0.5);
  display: flex;
  flex-direction: column;
}
.fb-modal-title {
  padding: 10px 14px;
  font-weight: bold;
  font-size: 14px;
  border-bottom: 1px solid var(--vscode-panel-border);
}
.fb-modal-body { padding: 12px 14px; }
.fb-modal-body label {
  display: block;
  font-size: 12px;
  margin-bottom: 4px;
  color: var(--vscode-descriptionForeground);
}
.fb-modal-body input, .fb-modal-body textarea {
  width: 100%;
  background: var(--vscode-input-background);
  color: var(--vscode-input-foreground);
  border: 1px solid var(--vscode-input-border, var(--vscode-focusBorder, transparent));
  padding: 6px 8px;
  font-size: 13px;
  border-radius: 3px;
  outline: none;
  font-family: var(--vscode-editor-font-family, monospace);
  resize: vertical;
}
.fb-modal-body input:focus, .fb-modal-body textarea:focus { border-color: var(--vscode-focusBorder); }
.hint { font-size: 11px; color: var(--vscode-descriptionForeground); margin: 0 0 10px; line-height: 1.5; }
.fb-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  padding: 10px 14px;
  border-top: 1px solid var(--vscode-panel-border);
}
.fb-modal-footer button {
  border: none;
  cursor: pointer;
  padding: 6px 14px;
  font-size: 12px;
  border-radius: 3px;
}
.fb-modal-footer button.primary {
  background: var(--vscode-button-background);
  color: var(--vscode-button-foreground);
}
.fb-modal-footer button.primary:hover { background: var(--vscode-button-hoverBackground); }
.fb-modal-footer button.secondary {
  background: var(--vscode-button-secondaryBackground, transparent);
  color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
  border: 1px solid var(--vscode-panel-border);
}
.fb-modal-footer button.secondary:hover { background: var(--vscode-toolbar-hoverBackground); }

.hidden { display: none !important; }
`;
}

// ---------------------------------------------------------------------------
// JavaScript
// ---------------------------------------------------------------------------
function getFileBrowserJs(): string {
    return String.raw`
(function() {
    // Proof-of-life: visible immediately if the script block executes at all
    document.title = 'File Browser [script running]';

    // Show any uncaught JS errors as a red banner
    window.onerror = function(msg, src, line, col, err) {
        var d = document.createElement('div');
        d.style.cssText = 'position:fixed;top:0;left:0;right:0;padding:10px;background:#c00;color:#fff;font-family:monospace;font-size:12px;z-index:9999;white-space:pre-wrap;';
        d.textContent = 'FB error: ' + msg + ' (' + src + ':' + line + ')';
        document.body.appendChild(d);
    };

    const vscode = acquireVsCodeApi();

    // ── debug log ─────────────────────────────────────────────────────────
    const debugEl = document.getElementById('fb-debug');
    function dbg(msg) {
        if (debugEl) {
            const line = document.createElement('div');
            line.textContent = '[' + new Date().toISOString().slice(11,23) + '] ' + msg;
            debugEl.appendChild(line);
            debugEl.scrollTop = debugEl.scrollHeight;
        }
        console.log('[fb] ' + msg);
    }
    dbg('script started');

    // ── state ──────────────────────────────────────────────────────────────
    let bookmarks = [];
    let protocols = [];
    let currentUrl = '';
    let currentSo  = '';
    let history    = [];
    let histIdx    = -1;
    let selected   = null;
    let newentryMode = 'file';

    // ── DOM refs ───────────────────────────────────────────────────────────
    // NOTE: #fb-entries is the scrollable entry list. #fb-empty and
    // #fb-error are siblings of #fb-entries inside #fb-file-list and must
    // NEVER be cleared by setting innerHTML on their parent.
    const entriesEl   = document.getElementById('fb-entries');
    const emptyEl     = document.getElementById('fb-empty');
    const errorEl     = document.getElementById('fb-error');
    const urlInput    = document.getElementById('fb-url-input');
    const breadcrumb  = document.getElementById('fb-breadcrumb');
    const spinner     = document.getElementById('fb-spinner');
    const infoTitle   = document.getElementById('fb-info-title');
    const infoActions = document.getElementById('fb-info-actions');
    const infoMeta    = document.getElementById('fb-info-meta');
    const infoPreview = document.getElementById('fb-info-preview');
    const scanPane    = document.getElementById('fb-scan-pane');
    const scanStatus  = document.getElementById('fb-scan-status');
    const scanPanelRoot = document.getElementById('fb-scan-panel-root');
    const bmPanel     = document.getElementById('bm-panel');
    const bmList      = document.getElementById('bm-list');
    const soOverlay   = document.getElementById('so-overlay');
    const soInput     = document.getElementById('so-input');
    const neOverlay   = document.getElementById('newentry-overlay');
    const neTitle     = document.getElementById('newentry-title');
    const neInput     = document.getElementById('newentry-name');
    const renOverlay  = document.getElementById('rename-overlay');
    const renInput    = document.getElementById('rename-input');

    // Verify critical elements exist
    const missing = ['fb-entries','fb-empty','fb-error','fb-url-input','fb-breadcrumb',
                     'fb-spinner','fb-info-title','fb-info-actions'].filter(id => !document.getElementById(id));
    if (missing.length) { dbg('ERROR: missing elements: ' + missing.join(', ')); }
    else { dbg('all DOM elements found'); }

    // ── utilities ──────────────────────────────────────────────────────────
    function basename(url) {
        const s = (url || '').replace(/\/+$/, '');
        const i = s.lastIndexOf('/');
        return i >= 0 ? s.slice(i + 1) : s;
    }
    function parentUrl(url) {
        const s = (url || '').replace(/\/+$/, '');
        const protoEnd = s.indexOf('://');
        if (protoEnd >= 0) {
            const pathPart = s.slice(protoEnd + 3);
            const slash = pathPart.lastIndexOf('/');
            if (slash <= 0) return s.slice(0, protoEnd + 3) || s;
            return s.slice(0, protoEnd + 3 + slash);
        }
        const slash = s.lastIndexOf('/');
        if (slash <= 0) return '/';
        return s.slice(0, slash);
    }
    function fmtSize(bytes) {
        if (bytes == null) return '';
        const u = ['B','KB','MB','GB','TB'];
        let n = parseFloat(bytes);
        for (let i = 0; i < u.length; i++) {
            if (n < 1024 || i === u.length - 1) return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
            n /= 1024;
        }
    }
    function fmtDate(ts) {
        if (!ts) return '';
        const d = new Date(parseFloat(ts) * 1000);
        return d.toLocaleString();
    }
    function escHtml(s) {
        return String(s || '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function fileIcon(entry) {
        if (entry.type === 'directory') return '\uD83D\uDCC1'; // folder
        const name = (entry.basename || entry.name || '').toLowerCase();
        if (/\.(py|pyx|pyi)$/.test(name))                   return '\uD83D\uDC0D'; // snake
        if (/\.(js|ts|jsx|tsx)$/.test(name))                 return '\uD83D\uDCDC'; // scroll
        if (/\.(json|yaml|yml|toml|ini|cfg)$/.test(name))    return '\u2699\uFE0F'; // gear
        if (/\.(md|rst|txt|org)$/.test(name))                return '\uD83D\uDCC4'; // page
        if (/\.(csv|tsv|parquet|hdf5?|nc|zarr|feather)$/.test(name)) return '\uD83D\uDCCA'; // chart
        if (/\.(png|jpg|jpeg|gif|svg|webp|bmp|tiff?)$/.test(name))   return '\uD83D\uDDBC\uFE0F'; // picture
        if (/\.(zip|tar|gz|bz2|xz|7z|rar)$/.test(name))     return '\uD83D\uDCE6'; // package
        if (/\.(sh|bash|zsh|fish|ps1|bat|cmd)$/.test(name)) return '\uD83D\uDCBB'; // computer
        return '\uD83D\uDCC4'; // page
    }

    // ── browse result ──────────────────────────────────────────────────────
    function renderBrowse(data) {
        dbg('renderBrowse url=' + data.url + ' entries=' + (data.entries ? data.entries.length : 'none') + ' error=' + data.error);
        currentUrl = data.url || '';
        urlInput.value = currentUrl;
        renderBreadcrumb(currentUrl);

        // Clear only the entries container — never touch fb-empty/fb-error
        entriesEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        errorEl.classList.add('hidden');

        if (data.error) {
            errorEl.textContent = 'Error: ' + data.error;
            errorEl.classList.remove('hidden');
            return;
        }

        const entries = data.entries || [];
        if (entries.length === 0) {
            emptyEl.classList.remove('hidden');
            return;
        }

        for (const entry of entries) {
            const row = document.createElement('div');
            row.className = 'fb-entry' + (entry.type === 'directory' ? ' is-dir' : '');
            row.dataset.url  = entry.name;
            row.dataset.type = entry.type || 'file';

            const icon = document.createElement('span');
            icon.className = 'fb-entry-icon';
            icon.textContent = fileIcon(entry);

            const name = document.createElement('span');
            name.className = 'fb-entry-name';
            name.textContent = entry.basename || basename(entry.name);
            name.title = entry.name;

            const meta = document.createElement('span');
            meta.className = 'fb-entry-meta';
            if (entry.type !== 'directory' && entry.size != null) {
                meta.textContent = fmtSize(entry.size);
            }

            row.appendChild(icon);
            row.appendChild(name);
            row.appendChild(meta);

            row.addEventListener('click', function(e) {
                e.stopPropagation();
                selectEntry(entry.name, entry.type, row);
            });

            if (entry.type === 'directory') {
                row.addEventListener('dblclick', function(e) {
                    e.stopPropagation();
                    navigateTo(entry.name, currentSo);
                });
            }

            entriesEl.appendChild(row);
        }
        dbg('rendered ' + entries.length + ' entries');
    }

    function selectEntry(url, type, rowEl) {
        document.querySelectorAll('.fb-entry.active').forEach(function(el) { el.classList.remove('active'); });
        if (rowEl) rowEl.classList.add('active');
        selected = { url: url, type: type, so: currentSo };
        dbg('selected ' + type + ': ' + url);

        infoTitle.textContent = basename(url);
        infoActions.classList.remove('hidden');

        const isFile = type !== 'directory';
        document.getElementById('btn-open-editor').style.display = isFile ? '' : 'none';
        document.getElementById('btn-add-to-lib').style.display = type === 'directory' ? '' : 'none';

        infoMeta.innerHTML = '';
        infoPreview.innerHTML = '';

        // Always hide and reset the scan pane when selection changes
        if (scanPane) {
            scanPane.classList.add('hidden');
            if (scanStatus) scanStatus.textContent = '';
        }

        if (isFile) {
            dbg('posting inspect for ' + url);
            vscode.postMessage({ cmd: 'inspect', url: url, storageOptions: currentSo || undefined });
        } else {
            renderMeta({ type: 'directory', url: url });
            // Trigger projspec scan on directory selection
            if (scanPane) {
                scanPane.classList.remove('hidden');
                if (scanStatus) scanStatus.textContent = 'scanning...';
            }
            dbg('posting scanDir for ' + url);
            vscode.postMessage({ cmd: 'scanDir', url: url, storageOptions: currentSo || undefined });
        }
    }

    function renderBreadcrumb(url) {
        breadcrumb.innerHTML = '';
        if (!url) return;
        const protoMatch = url.match(/^([a-z][a-z0-9+.\-]*):\/\//i);
        let proto = '';
        let rest = url;
        if (protoMatch) {
            proto = protoMatch[0];
            rest = url.slice(proto.length);
        }
        const parts = rest.replace(/\/+$/, '').split('/').filter(Boolean);
        if (proto) {
            const link = document.createElement('span');
            link.className = 'bc-seg';
            link.textContent = proto;
            link.title = proto;
            link.addEventListener('click', function() { navigateTo(proto, currentSo); });
            breadcrumb.appendChild(link);
        }
        let accumulated = proto;
        for (let i = 0; i < parts.length; i++) {
            accumulated += (accumulated.slice(-1) === '/' ? '' : '/') + parts[i];
            const sep = document.createElement('span');
            sep.className = 'bc-sep';
            sep.textContent = ' / ';
            breadcrumb.appendChild(sep);
            const link = document.createElement('span');
            link.className = 'bc-seg';
            link.textContent = parts[i];
            const target = accumulated;
            link.title = target;
            link.addEventListener('click', function() { navigateTo(target, currentSo); });
            breadcrumb.appendChild(link);
        }
    }

    function renderMeta(data) {
        infoMeta.innerHTML = '';
        var rows = [];
        if (data.type)          rows.push(['Type', data.type]);
        if (data.size != null)  rows.push(['Size', fmtSize(data.size)]);
        if (data.last_modified) rows.push(['Modified', fmtDate(data.last_modified)]);
        if (data.mime_type)     rows.push(['MIME', data.mime_type]);
        for (var i = 0; i < rows.length; i++) {
            const row = document.createElement('div');
            row.className = 'info-row';
            row.innerHTML = '<span class="info-key">' + escHtml(rows[i][0]) + '</span><span>' + escHtml(String(rows[i][1])) + '</span>';
            infoMeta.appendChild(row);
        }
    }

    function renderInspect(data) {
        dbg('renderInspect name=' + data.name + ' error=' + data.error);
        renderMeta(data);
        infoPreview.innerHTML = '';

        if (data.intake) {
            const intake = data.intake;

            // Header: primary type badge
            const hdr = document.createElement('div');
            hdr.className = 'preview-label';
            hdr.textContent = 'Data type (intake)';
            infoPreview.appendChild(hdr);

            const card = document.createElement('div');
            card.className = 'intake-card';

            // Primary type — shown prominently
            if (intake.primary_type) {
                const typeRow = document.createElement('div');
                typeRow.className = 'intake-type-badge';
                typeRow.textContent = intake.primary_type;
                card.appendChild(typeRow);
            }

            // Additional recognised types (if more than one)
            if (intake.types && intake.types.length > 1) {
                const also = document.createElement('div');
                also.className = 'intake-row';
                also.innerHTML = '<span class="intake-key">also</span>'
                    + '<span class="intake-val">' + escHtml(intake.types.slice(1).join(', ')) + '</span>';
                card.appendChild(also);
            }

            // Schema: dtype dict → column table
            if (intake.dtype && typeof intake.dtype === 'object' && !Array.isArray(intake.dtype)) {
                const schemaHdr = document.createElement('div');
                schemaHdr.className = 'intake-schema-hdr';
                schemaHdr.textContent = 'Columns';
                card.appendChild(schemaHdr);
                const cols = Object.keys(intake.dtype);
                for (var ci = 0; ci < cols.length; ci++) {
                    const colRow = document.createElement('div');
                    colRow.className = 'intake-col-row';
                    colRow.innerHTML = '<span class="intake-col-name">' + escHtml(cols[ci]) + '</span>'
                        + '<span class="intake-col-dtype">' + escHtml(String(intake.dtype[cols[ci]])) + '</span>';
                    card.appendChild(colRow);
                }
            }

            // Other schema fields (shape, npartitions, etc.) — skip types/dtype/primary_type
            const SKIP = new Set(['types', 'dtype', 'primary_type', 'name']);
            const entries = Object.entries(intake);
            for (var i = 0; i < entries.length; i++) {
                const k = entries[i][0], v = entries[i][1];
                if (SKIP.has(k) || v == null) continue;
                const row = document.createElement('div');
                row.className = 'intake-row';
                const vStr = typeof v === 'object' ? JSON.stringify(v) : String(v);
                row.innerHTML = '<span class="intake-key">' + escHtml(k) + '</span>'
                              + '<span class="intake-val">' + escHtml(vStr) + '</span>';
                card.appendChild(row);
            }

            infoPreview.appendChild(card);
        }

        // Text preview — always shown for text files
        if (data.text_preview) {
            const label = document.createElement('div');
            label.className = 'preview-label';
            label.style.marginTop = '10px';
            label.textContent = 'Preview';
            infoPreview.appendChild(label);
            const pre = document.createElement('pre');
            pre.className = 'preview-text';
            pre.textContent = data.text_preview;
            infoPreview.appendChild(pre);
        }
    }

    // ── navigation ─────────────────────────────────────────────────────────
    function navigateTo(url, so, push) {
        const shouldPush = push !== false;
        dbg('navigateTo push=' + shouldPush + ' url=' + url);
        vscode.postMessage({ cmd: 'browse', url: url, storageOptions: so || undefined, push: shouldPush });
        selected = null;
        infoTitle.textContent = 'Loading...';
        infoActions.classList.add('hidden');
        infoMeta.innerHTML = '';
        infoPreview.innerHTML = '';
    }

    // ── bookmarks ──────────────────────────────────────────────────────────
    function renderBookmarks() {
        bmList.innerHTML = '';
        if (!bookmarks.length) {
            const e = document.createElement('div');
            e.className = 'bm-empty';
            e.textContent = 'No bookmarks yet.';
            bmList.appendChild(e);
            return;
        }
        for (var i = 0; i < bookmarks.length; i++) {
            const bm = bookmarks[i];
            const row = document.createElement('div');
            row.className = 'bm-item';
            const info = document.createElement('div');
            info.style.flex = '1';
            info.style.overflow = 'hidden';
            const lbl = document.createElement('div');
            lbl.className = 'bm-item-label';
            lbl.textContent = bm.label || bm.url;
            const urlDiv = document.createElement('div');
            urlDiv.className = 'bm-item-url';
            urlDiv.textContent = bm.url;
            info.appendChild(lbl);
            info.appendChild(urlDiv);
            // Show a key icon if the bookmark has stored storage_options
            if (bm.storage_options && Object.keys(bm.storage_options).length > 0) {
                const soTag = document.createElement('div');
                soTag.className = 'bm-item-so';
                soTag.title = 'Saved storage options: ' + JSON.stringify(bm.storage_options);
                soTag.textContent = '\uD83D\uDD11 credentials stored';
                info.appendChild(soTag);
            }
            const rm = document.createElement('button');
            rm.className = 'bm-remove';
            rm.title = 'Remove bookmark';
            rm.textContent = 'X';
            const bmUrl = bm.url;
            // Serialise the bookmark's own storage_options as a JSON string (or '').
            // These are scoped to this URL only — they replace currentSo entirely
            // when navigating to a different protocol/host.
            const bmSo = (bm.storage_options && Object.keys(bm.storage_options).length > 0)
                ? JSON.stringify(bm.storage_options) : '';
            rm.addEventListener('click', function(e) {
                e.stopPropagation();
                vscode.postMessage({ cmd: 'removeBookmark', url: bmUrl });
            });
            row.appendChild(info);
            row.appendChild(rm);
            row.addEventListener('click', function(ev) {
                if (ev.target === rm) return;
                bmPanel.classList.add('hidden');
                // Switch currentSo to the bookmark's own options (may be empty)
                currentSo = bmSo;
                navigateTo(bmUrl, bmSo);
            });
            bmList.appendChild(row);
        }
    }

    // ── toolbar ────────────────────────────────────────────────────────────
    document.getElementById('btn-back').addEventListener('click', function() {
        if (histIdx > 0) {
            histIdx--;
            const h = history[histIdx];
            currentSo = h.so;
            dbg('back to ' + h.url);
            vscode.postMessage({ cmd: 'browse', url: h.url, storageOptions: h.so || undefined, push: false });
            selected = null;
            infoTitle.textContent = 'Loading...';
            infoActions.classList.add('hidden');
            infoMeta.innerHTML = '';
            infoPreview.innerHTML = '';
        }
    });
    document.getElementById('btn-up').addEventListener('click', function() {
        const p = parentUrl(currentUrl);
        if (p && p !== currentUrl) { navigateTo(p, currentSo); }
    });
    document.getElementById('btn-refresh').addEventListener('click', function() {
        navigateTo(currentUrl, currentSo, false);
    });
    document.getElementById('btn-bm-dropdown').addEventListener('click', function(e) {
        e.stopPropagation();
        bmPanel.classList.toggle('hidden');
        if (!bmPanel.classList.contains('hidden')) renderBookmarks();
    });
    document.getElementById('bm-close').addEventListener('click', function() {
        bmPanel.classList.add('hidden');
    });
    document.getElementById('btn-bm-add-current').addEventListener('click', function() {
        bmPanel.classList.add('hidden');
        vscode.postMessage({ cmd: 'addBookmark', url: currentUrl, storageOptions: currentSo || undefined });
    });
    document.addEventListener('click', function(e) {
        if (!bmPanel.contains(e.target) && e.target !== document.getElementById('btn-bm-dropdown')) {
            bmPanel.classList.add('hidden');
        }
    });

    // Storage options
    document.getElementById('btn-so').addEventListener('click', function(e) {
        e.stopPropagation();
        soInput.value = currentSo;
        soOverlay.classList.remove('hidden');
        setTimeout(function() { soInput.focus(); }, 0);
    });
    document.getElementById('so-cancel').addEventListener('click', function() {
        soOverlay.classList.add('hidden');
    });
    document.getElementById('so-ok').addEventListener('click', function() {
        const val = soInput.value.trim();
        try {
            if (val) JSON.parse(val);
            currentSo = val;
        } catch(ex) {
            alert('Storage options must be valid JSON: ' + ex.message);
            return;
        }
        soOverlay.classList.add('hidden');
        dbg('storage options set: ' + currentSo);
    });
    soOverlay.addEventListener('click', function(e) {
        if (e.target === soOverlay) soOverlay.classList.add('hidden');
    });

    // Go / URL bar
    document.getElementById('btn-go').addEventListener('click', function() {
        const url = urlInput.value.trim();
        if (url) { navigateTo(url, currentSo); }
    });
    urlInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            const url = urlInput.value.trim();
            if (url) { navigateTo(url, currentSo); }
        }
    });

    // New file / folder
    document.getElementById('btn-new-file').addEventListener('click', function() {
        newentryMode = 'file';
        neTitle.textContent = 'New file';
        neInput.value = '';
        neOverlay.classList.remove('hidden');
        setTimeout(function() { neInput.focus(); }, 0);
    });
    document.getElementById('btn-new-dir').addEventListener('click', function() {
        newentryMode = 'dir';
        neTitle.textContent = 'New folder';
        neInput.value = '';
        neOverlay.classList.remove('hidden');
        setTimeout(function() { neInput.focus(); }, 0);
    });
    document.getElementById('newentry-cancel').addEventListener('click', function() {
        neOverlay.classList.add('hidden');
    });
    document.getElementById('newentry-ok').addEventListener('click', function() {
        const name = neInput.value.trim();
        if (!name) return;
        neOverlay.classList.add('hidden');
        if (newentryMode === 'file') {
            dbg('createFile ' + name);
            vscode.postMessage({ cmd: 'createFile', parentUrl: currentUrl, name: name, storageOptions: currentSo || undefined });
        } else {
            dbg('mkdir ' + name);
            vscode.postMessage({ cmd: 'mkdir', parentUrl: currentUrl, name: name, storageOptions: currentSo || undefined });
        }
    });
    neInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') document.getElementById('newentry-ok').click();
        if (e.key === 'Escape') neOverlay.classList.add('hidden');
    });
    neOverlay.addEventListener('click', function(e) {
        if (e.target === neOverlay) neOverlay.classList.add('hidden');
    });

    // Info panel actions
    document.getElementById('btn-open-editor').addEventListener('click', function() {
        if (!selected || selected.type === 'directory') return;
        dbg('openFile ' + selected.url);
        vscode.postMessage({ cmd: 'openFile', url: selected.url, storageOptions: selected.so || undefined });
    });
    document.getElementById('btn-add-to-lib').addEventListener('click', function() {
        if (!selected) return;
        dbg('addToLibrary ' + selected.url);
        vscode.postMessage({ cmd: 'addToLibrary', url: selected.url, storageOptions: selected.so || undefined });
    });
    document.getElementById('btn-bookmark').addEventListener('click', function() {
        const url = selected ? selected.url : currentUrl;
        dbg('addBookmark ' + url);
        vscode.postMessage({ cmd: 'addBookmark', url: url, storageOptions: currentSo || undefined });
    });
    document.getElementById('btn-delete-sel').addEventListener('click', function() {
        if (!selected) return;
        dbg('deleteEntry ' + selected.url);
        vscode.postMessage({
            cmd: 'deleteEntry',
            url: selected.url,
            isDir: selected.type === 'directory',
            storageOptions: selected.so || undefined,
        });
    });
    document.getElementById('btn-rename-sel').addEventListener('click', function() {
        if (!selected) return;
        renInput.value = basename(selected.url);
        renOverlay.classList.remove('hidden');
        setTimeout(function() { renInput.focus(); }, 0);
    });

    // Rename modal
    document.getElementById('rename-cancel').addEventListener('click', function() {
        renOverlay.classList.add('hidden');
    });
    document.getElementById('rename-ok').addEventListener('click', function() {
        const newName = renInput.value.trim();
        if (!newName || !selected) return;
        renOverlay.classList.add('hidden');
        dbg('rename ' + selected.url + ' -> ' + newName);
        vscode.postMessage({ cmd: 'renameEntry', url: selected.url, newName: newName, storageOptions: selected.so || undefined });
    });
    renInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') document.getElementById('rename-ok').click();
        if (e.key === 'Escape') renOverlay.classList.add('hidden');
    });
    renOverlay.addEventListener('click', function(e) {
        if (e.target === renOverlay) renOverlay.classList.add('hidden');
    });

    // ── embedded projspec panel ────────────────────────────────────────────
    // The shared panel JS was already run by the inline bootstrap script in
    // getHtml() before this IIFE executed.  The bootstrap installed
    // window.__fbPanelDeliver(msg) — call it to push a data message into
    // the embedded panel.

    function showProjectInPanel(data) {
        if (!scanPanelRoot) return;
        if (scanStatus) scanStatus.textContent = '';

        var proj = data.project;
        var url  = data.url || '';
        if (!proj) {
            if (scanStatus) scanStatus.textContent = data.error || 'no project data';
            return;
        }

        var lib = {};
        lib[url] = proj;
        var dataMsg = { type: 'data', library: lib, info: {}, enums: {} };

        if (typeof window.__fbPanelDeliver === 'function') {
            dbg('delivering to embedded panel: ' + url);
            window.__fbPanelDeliver(dataMsg);
        } else {
            dbg('ERROR: __fbPanelDeliver not available');
        }
    }

    // ── message bus ────────────────────────────────────────────────────────
    window.addEventListener('message', function(ev) {
        const msg = ev.data;
        dbg('recv type=' + msg.type);
        switch (msg.type) {
            case 'loading':
                spinner.classList.toggle('hidden', !msg.loading);
                break;

            case 'init':
                bookmarks = msg.bookmarks || [];
                protocols = msg.protocols || [];
                dbg('init: ' + bookmarks.length + ' bookmarks, ' + protocols.length + ' protocols');
                break;

            case 'browseResult':
                if (typeof msg.storageOptions === 'string') {
                    currentSo = msg.storageOptions;
                }
                renderBrowse(msg);
                if (msg.pushHistory) {
                    history = history.slice(0, histIdx + 1);
                    history.push({ url: msg.url, so: currentSo });
                    histIdx = history.length - 1;
                    dbg('history push, depth=' + history.length);
                }
                break;

            case 'inspectResult':
                renderInspect(msg);
                break;

            case 'projectScanned':
                dbg('projectScanned url=' + msg.url + ' error=' + msg.error);
                showProjectInPanel(msg);
                break;

            case 'bookmarksUpdated':
                bookmarks = msg.bookmarks || [];
                dbg('bookmarks updated: ' + bookmarks.length);
                if (!bmPanel.classList.contains('hidden')) renderBookmarks();
                break;

            case 'error':
                errorEl.textContent = 'Error: ' + (msg.message || 'unknown');
                errorEl.classList.remove('hidden');
                dbg('error: ' + msg.message);
                break;
        }
    });

    dbg('sending ready');
    vscode.postMessage({ cmd: 'ready' });
})();
`;

}
