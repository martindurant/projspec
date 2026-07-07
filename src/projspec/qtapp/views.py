"""HTML/CSS/JS for the Qt webview combined panel (Library + File Browser).

The shared HTML/CSS/JS lives in :mod:`projspec.webui`.  This module only
contributes the small Qt-specific bootstrap scripts that wire
:class:`QWebChannel` bridge objects to the shared transport protocols.

Two bridges are registered on the same channel:
  - ``bridge``    → library panel    (window.projspecTransport)
  - ``fb_bridge`` → file browser     (window.projspecFbTransport)

A third, no-send bridge handles the embedded scan sub-panel inside the file
browser (window.projspecTransport scoped to #fb-scan-panel-root).
"""

from __future__ import annotations

import json

from projspec.webui import chrome_icons, get_combined_html

# Re-exported for backwards compatibility.
CHROME = chrome_icons()

# ---------------------------------------------------------------------------
#  Darcula-theme CSS variable fallbacks
#  Injected into <head> so that --vscode-* variables resolve correctly in
#  JCEF (which does not provide them natively the way VS Code does).
# ---------------------------------------------------------------------------
_QT_THEME_CSS = """<style>
:root {
    --vscode-font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --vscode-editor-font-family: "JetBrains Mono", Consolas, Menlo, monospace;
    --vscode-foreground: #bbbbbb;
    --vscode-editor-background: #2b2b2b;
    --vscode-editorWidget-background: #3c3f41;
    --vscode-editorWidget-foreground: #bbbbbb;
    --vscode-editorWidget-border: #555555;
    --vscode-panel-border: #3c3f41;
    --vscode-focusBorder: #466d94;
    --vscode-descriptionForeground: #8a8a8a;
    --vscode-button-background: #365880;
    --vscode-button-foreground: #ffffff;
    --vscode-button-hoverBackground: #466d94;
    --vscode-button-secondaryBackground: #4c5052;
    --vscode-button-secondaryForeground: #bbbbbb;
    --vscode-input-background: #45494a;
    --vscode-input-foreground: #bbbbbb;
    --vscode-input-border: #646464;
    --vscode-list-hoverBackground: #4c5052;
    --vscode-list-activeSelectionBackground: #365880;
    --vscode-list-activeSelectionForeground: #ffffff;
    --vscode-menu-background: #3c3f41;
    --vscode-menu-foreground: #bbbbbb;
    --vscode-menu-border: #555555;
    --vscode-menu-selectionBackground: #365880;
    --vscode-menu-selectionForeground: #ffffff;
    --vscode-menu-separatorBackground: #555555;
    --vscode-toolbar-hoverBackground: #4c5052;
    --vscode-disabledForeground: #707070;
    --vscode-textLink-foreground: #589df6;
    --vscode-textBlockQuote-background: rgba(255,255,255,0.06);
    --vscode-symbolIcon-propertyForeground: #9876aa;
    --vscode-symbolIcon-stringForeground: #6a8759;
    --vscode-symbolIcon-numberForeground: #6897bb;
    --vscode-symbolIcon-keywordForeground: #cc7832;
    --vscode-symbolIcon-enumeratorMemberForeground: #4ec9b0;
    --vscode-editorInfo-foreground: #589df6;
    --vscode-editorInfo-border: #589df6;
    --vscode-editorWarning-foreground: #bbb529;
    --vscode-editorWarning-border: #bbb529;
    --vscode-errorForeground: #ff5555;
    --vscode-badge-background: #4d4d4d;
    --vscode-badge-foreground: #ffffff;
    --vscode-editorGroupHeader-tabsBackground: #252526;
}
html, body { height: 100%; margin: 0; padding: 0; overflow: hidden; }
</style>"""

_QT_EXTRA_HEAD = (
    '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>\n' + _QT_THEME_CSS
)

# Bootstrap for the *library* tab — wires channel.objects.bridge.
_QT_LIB_BOOTSTRAP = r"""
<script>
window.__PROJSPEC_CHROME_ICONS__ = __CHROME_ICONS_JSON__;
(function() {
    let bridge = null, dispatch = null;
    const pending = [], inbox = [];
    window.projspecTransport = {
        send: (msg) => {
            if (bridge) bridge.handleMessage(JSON.stringify(msg));
            else pending.push(msg);
        },
        onReady: (d) => {
            dispatch = d;
            while (inbox.length) dispatch(inbox.shift());
        },
    };
    // QWebChannel is set up once; both bridges are registered on it.
    // We defer connecting until the channel resolves (see fb bootstrap below
    // which initialises the shared channel object window.__qtChannel).
    function connectLib(channel) {
        bridge = channel.objects.bridge;
        bridge.from_python.connect((raw) => {
            let msg; try { msg = JSON.parse(raw); } catch { return; }
            if (dispatch) dispatch(msg); else inbox.push(msg);
        });
        while (pending.length) bridge.handleMessage(JSON.stringify(pending.shift()));
    }
    // If the channel is already resolved (scan-panel bootstrap ran first),
    // connect immediately; otherwise wait for it.
    if (window.__qtChannel) { connectLib(window.__qtChannel); }
    else { window.__qtLibConnectPending = connectLib; }
})();
</script>
"""

# Bootstrap for the embedded scan sub-panel inside the file browser.
# Uses a no-send transport — the sub-panel only displays data, never sends
# commands back to the host.
_QT_SCAN_PANEL_BOOTSTRAP = r"""
<script>
(function() {
    var root = document.getElementById('fb-scan-panel-root');
    if (!root) return;
    var pending = [];
    window.__fbPanelDeliver = function(msg) {
        if (window.__fbPanelDispatch) { window.__fbPanelDispatch(msg); }
        else { pending.push(msg); }
    };
    window.projspecRoot = root;
    window.projspecTransport = {
        send: function() {},  // scan sub-panel never sends commands
        onReady: function(d) {
            window.__fbPanelDispatch = d;
            pending.forEach(function(m) { d(m); });
            pending = [];
            delete window.projspecRoot;
            delete window.projspecTransport;
        },
    };
})();
</script>
"""

# Bootstrap for the *file browser* tab — wires channel.objects.fb_bridge.
# Initialises the shared QWebChannel and connects both bridges.
_QT_FB_BOOTSTRAP = r"""
<script>
(function() {
    let bridge = null, dispatch = null;
    const pending = [], inbox = [];
    window.projspecFbTransport = {
        send: (msg) => {
            if (bridge) bridge.handleMessage(JSON.stringify(msg));
            else pending.push(msg);
        },
        onReady: (d) => {
            dispatch = d;
            while (inbox.length) dispatch(inbox.shift());
        },
    };
    // Set the initial root for the filebrowser DOM scope
    window.projspecFbRoot = document.getElementById('tab-filebrowser') || document;

    new QWebChannel(qt.webChannelTransport, (channel) => {
        window.__qtChannel = channel;
        // Connect filebrowser bridge
        bridge = channel.objects.fb_bridge;
        bridge.from_python.connect((raw) => {
            let msg; try { msg = JSON.parse(raw); } catch { return; }
            if (dispatch) dispatch(msg); else inbox.push(msg);
        });
        while (pending.length) bridge.handleMessage(JSON.stringify(pending.shift()));
        // Connect library bridge (deferred from lib bootstrap)
        if (window.__qtLibConnectPending) {
            window.__qtLibConnectPending(channel);
            delete window.__qtLibConnectPending;
        }
    });
})();
</script>
"""


def get_qt_html(initial_tab: str = "library") -> str:
    """Return the full combined HTML document for the Qt webview."""
    chrome_json = json.dumps(chrome_icons(), separators=(",", ":"))
    lib_bootstrap = _QT_LIB_BOOTSTRAP.replace("__CHROME_ICONS_JSON__", chrome_json)
    return get_combined_html(
        extra_head=_QT_EXTRA_HEAD,
        lib_bootstrap_js=lib_bootstrap,
        scan_panel_bootstrap_js=_QT_SCAN_PANEL_BOOTSTRAP,
        fb_bootstrap_js=_QT_FB_BOOTSTRAP,
        initial_tab=initial_tab,
    )


# Backwards-compat alias used by existing callers
def get_panel_html() -> str:
    return get_qt_html(initial_tab="library")


__all__ = ["CHROME", "get_panel_html", "get_qt_html"]
