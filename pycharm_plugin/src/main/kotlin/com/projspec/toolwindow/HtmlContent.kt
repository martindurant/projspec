package com.projspec.toolwindow

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.CapturingProcessHandler

/**
 * Generates the combined HTML/CSS/JS page (Project Library + File Browser tabs).
 *
 * The canonical HTML is produced by `projspec.webui.get_combined_html()` in Python.
 * We invoke this at startup via a short python3 subprocess and cache the result.
 *
 * The bootstrap JS strings are built here in Kotlin and passed to the Python
 * script as JSON-encoded command-line arguments — this avoids embedding Kotlin
 * string interpolations inside Python triple-quoted strings inside JS, which
 * causes escaping failures.
 */
object HtmlContent {

    fun buildHtml(): String {
        return _cachedHtml ?: buildHtmlFresh().also { _cachedHtml = it }
    }

    @Volatile private var _cachedHtml: String? = null

    private fun buildHtmlFresh(): String {
        // Build the bootstrap strings in Kotlin and pass them as argv[1..3]
        // so the Python script stays trivially simple and escaping-safe.
        val extraHead = "<style>$THEME_FALLBACKS_CSS</style>"
        val libBootstrap = LIB_BRIDGE_BOOTSTRAP
        val fbBootstrap  = FB_BRIDGE_BOOTSTRAP

        // Minimal Python script — just imports and prints.  All variable
        // content comes in through sys.argv to avoid any quoting issues.
        val script = """
import sys, json
extra_head    = sys.argv[1]
lib_bootstrap = sys.argv[2]
fb_bootstrap  = sys.argv[3]
try:
    from projspec.webui import get_combined_html
    html = get_combined_html(
        extra_head=extra_head,
        lib_bootstrap_js=lib_bootstrap,
        fb_bootstrap_js=fb_bootstrap,
    )
    sys.stdout.buffer.write(html.encode('utf-8'))
except Exception as e:
    import traceback
    print('ERROR: ' + str(e), file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
""".trimIndent()

        return try {
            val cmd = GeneralCommandLine(
                listOf("python3", "-c", script, extraHead, libBootstrap, fbBootstrap)
            )
            cmd.charset = Charsets.UTF_8
            val output = CapturingProcessHandler(cmd).runProcess(30_000)
            val stderr = output.stderr.trim()
            val stdout = output.stdout.trim()
            when {
                output.isTimeout ->
                    fallbackHtml("python3 timed out after 30 s")
                output.exitCode != 0 ->
                    fallbackHtml(stderr.ifBlank { stdout.ifBlank { "exit code ${output.exitCode}" } })
                stdout.isBlank() ->
                    fallbackHtml("python3 produced no output\n$stderr")
                else -> output.stdout
            }
        } catch (e: Exception) {
            fallbackHtml("${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun fallbackHtml(error: String): String = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>body{font-family:sans-serif;padding:20px;color:#ccc;background:#2b2b2b;}
h2{color:#f88;}pre{font-size:11px;color:#f88;white-space:pre-wrap;word-break:break-all;}</style>
</head><body>
<h2>projspec not available</h2>
<p>Could not load the projspec UI. Make sure <code>projspec</code> is installed
and on <code>PATH</code>, then restart the IDE.</p>
<pre>${error.take(2000).replace("<","&lt;")}</pre>
</body></html>"""

    // -------------------------------------------------------------------------
    //  Bridge bootstrap JS — injected by injectBridge() after every page load.
    //  Kept here so the JS is co-located with its Kotlin counterpart and there
    //  is no escaping interaction with the Python script.
    // -------------------------------------------------------------------------

    /**
     * Library-panel bridge bootstrap.
     * Installs window.projspecTransport backed by window.__javaBridge (set by
     * [ProjspecToolWindowPanel.injectBridge]).
     */
    val LIB_BRIDGE_BOOTSTRAP = """<script>
(function() {
    var bridge = null, dispatch = null;
    var pending = [], inbox = [];
    window.projspecTransport = {
        send: function(msg) {
            if (bridge) bridge.query(JSON.stringify(msg));
            else pending.push(msg);
        },
        onReady: function(d) {
            dispatch = d;
            while (inbox.length) dispatch(inbox.shift());
        },
    };
    window.__projspecLibBridgeConnect = function(b) {
        bridge = b;
        b.from_java = function(raw) {
            var msg; try { msg = JSON.parse(raw); } catch(e) { return; }
            if (dispatch) dispatch(msg); else inbox.push(msg);
        };
        while (pending.length) bridge.query(JSON.stringify(pending.shift()));
    };
})();
</script>"""

    /**
     * File-browser bridge bootstrap.
     * Installs window.projspecFbTransport backed by window.__javaFbBridge.
     */
    val FB_BRIDGE_BOOTSTRAP = """<script>
(function() {
    var bridge = null, dispatch = null;
    var pending = [], inbox = [];
    window.projspecFbTransport = {
        send: function(msg) {
            if (bridge) bridge.query(JSON.stringify(msg));
            else pending.push(msg);
        },
        onReady: function(d) {
            dispatch = d;
            while (inbox.length) dispatch(inbox.shift());
        },
    };
    window.projspecFbRoot = document.getElementById('tab-filebrowser') || document;
    window.__projspecFbBridgeConnect = function(b) {
        bridge = b;
        b.from_java = function(raw) {
            var msg; try { msg = JSON.parse(raw); } catch(e) { return; }
            if (dispatch) dispatch(msg); else inbox.push(msg);
        };
        while (pending.length) bridge.query(JSON.stringify(pending.shift()));
    };
})();
</script>"""

    // -------------------------------------------------------------------------
    //  Theme fallback CSS
    //  Binds --vscode-* variables to Darcula colours so the shared CSS works.
    // -------------------------------------------------------------------------

    val THEME_FALLBACKS_CSS = """
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
""".trimIndent()
}
