package com.projspec.toolwindow

import com.google.gson.Gson
import com.google.gson.JsonParser
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.util.concurrency.AppExecutorUtil
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.diagnostic.debug
import com.intellij.openapi.fileChooser.FileChooser
import com.intellij.openapi.fileChooser.FileChooserDescriptor
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VfsUtil
import com.intellij.openapi.wm.ToolWindow
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.ui.jcef.JBCefBrowserBase
import com.intellij.ui.jcef.JBCefJSQuery
import com.projspec.settings.ProjspecSettings
import com.projspec.util.CliResult
import com.projspec.util.Notifier
import com.projspec.util.OpenWithHelper
import com.projspec.util.PluginLogger
import com.projspec.util.ProjspecRunner
import com.projspec.util.ProjspecServer
import com.projspec.util.TerminalRunner
import org.cef.browser.CefBrowser
import org.cef.browser.CefFrame
import org.cef.handler.CefLoadHandlerAdapter
import java.awt.BorderLayout
import java.io.File
import java.nio.file.Files
import java.nio.file.Paths
import java.util.concurrent.atomic.AtomicInteger
import javax.swing.JPanel

/**
 * The "Project Library" tool window panel.
 *
 * Port of vsextension/src/panel.ts (class `ProjspecPanel`) to IntelliJ's JCEF
 * (Chromium) browser component.  The HTML/CSS/JS page is lifted verbatim from
 * the VSCode panel (see [HtmlContent]) — this file is the Kotlin equivalent
 * of the panel's message-handling backend.
 *
 * Mapping:
 *   createWebviewPanel                → JBCefBrowser
 *   acquireVsCodeApi() / postMessage  → JBCefJSQuery (window.__javaBridge)
 *   panel.webview.onDidReceiveMessage → [handleJsMessage]
 *   panel.webview.postMessage         → [deliverToWebview] via executeJavaScript
 *   execSync / spawn                  → ProjspecRunner / OpenWithHelper
 *   showOpenDialog(folder)            → FileChooser.chooseFile (folder descriptor)
 *   createTerminal / sendText         → TerminalRunner
 */
class ProjspecToolWindowPanel(
    private val project: Project,
    @Suppress("unused") private val toolWindow: ToolWindow,
) : JPanel(BorderLayout()) {

    private val gson = Gson()
    private val browser: JBCefBrowser = JBCefBrowser()
    /** Library-panel bridge (window.__javaBridge) */
    private val jsQuery: JBCefJSQuery = JBCefJSQuery.create(browser as JBCefBrowserBase)
    /** File-browser bridge (window.__javaFbBridge) */
    private val fbJsQuery: JBCefJSQuery = JBCefJSQuery.create(browser as JBCefBrowserBase)

    /** HTTP server — started off-EDT alongside the HTML build. */
    private val server = ProjspecServer()

    // Cached data matching panel.ts's `info`, `enums` and `library` fields.
    @Volatile private var info: String = "null"
    @Volatile private var enums: String = "{}"
    @Volatile private var library: String = "{}"
    @Volatile private var specNames: List<String> = emptyList()
    @Volatile private var libraryMap: Map<String, Any?> = emptyMap()

    private val busyCount = AtomicInteger(0)
    private val fbBusyCount = AtomicInteger(0)

    @Volatile private var webviewReady = false
    private val pending = ArrayDeque<String>()
    private val initialLoadStarted = java.util.concurrent.atomic.AtomicBoolean(false)

    init {
        jsQuery.addHandler { json ->
            handleJsMessage(json)
            null
        }
        fbJsQuery.addHandler { json ->
            handleFbJsMessage(json)
            null
        }

        browser.jbCefClient.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadEnd(b: CefBrowser?, frame: CefFrame?, httpStatusCode: Int) {
                if (frame?.isMain == true) injectBridge()
            }
        }, browser.cefBrowser)

        add(browser.component, BorderLayout.CENTER)

        // Load a lightweight placeholder immediately (EDT-safe — no subprocess).
        // Start the server and build the real HTML in parallel on background threads.
        browser.loadHTML(LOADING_HTML)
        pool {
            // Start server first so it's ready when the UI starts making requests.
            server.start()
            val html = HtmlContent.buildHtml()
            ApplicationManager.getApplication().invokeLater({
                browser.loadHTML(html)
            }, ModalityState.defaultModalityState())
        }
    }

    override fun removeNotify() {
        super.removeNotify()
        server.dispose()
    }

    // ------------------------------------------------------------------------
    //  JS bridge
    // ------------------------------------------------------------------------

    /**
     * Register `window.__javaBridge` in the newly-loaded page so the panel JS
     * can call back into Kotlin.  The shim matches the one the VSCode webview
     * runtime injects automatically via `acquireVsCodeApi()`.
     *
     * After installing the bridge we call `window.__projspecBridgeReady()`
     * which flushes any messages the page JS queued before the bridge was
     * available (including the initial `ready` handshake).
     *
     * We also kick off the initial data load directly from here — relying
     * solely on the JS `ready` round-trip is fragile if the bridge install
     * and the page's inline script race; by triggering the first `reload()`
     * from Kotlin we guarantee the subprocess calls happen even if the JS
     * never manages to send `ready`.
     */
    private fun injectBridge() {
        // Library bridge
        val libInject = jsQuery.inject(
            "msgJson",
            "function(response) {}",
            "function(error_code, error_message) {}",
        )
        // Filebrowser bridge
        val fbInject = fbJsQuery.inject(
            "msgJson",
            "function(response) {}",
            "function(error_code, error_message) {}",
        )
        browser.cefBrowser.executeJavaScript(
            """
            var __javaLibBridge = { query: function(msgJson) { $libInject } };
            var __javaFbBridge  = { query: function(msgJson) { $fbInject  } };
            if (typeof window.__projspecLibBridgeConnect === 'function') {
                window.__projspecLibBridgeConnect(__javaLibBridge);
            }
            if (typeof window.__projspecFbBridgeConnect === 'function') {
                window.__projspecFbBridgeConnect(__javaFbBridge);
            }
            """.trimIndent(),
            "",
            0,
        )
        if (!initialLoadStarted.getAndSet(true)) {
            webviewReady = true
            synchronized(pending) {
                while (pending.isNotEmpty()) {
                    val script = pending.removeFirst()
                    ApplicationManager.getApplication().invokeLater({
                        browser.cefBrowser.executeJavaScript(script, "", 0)
                    }, ModalityState.defaultModalityState())
                }
            }
            pool { reload(initial = true) }
            pool { fbInit() }
        }
    }

    /** Deliver a message to the *library* panel (via window.__libDispatch). */
    private fun deliverToWebview(msg: Any) {
        val json = gson.toJson(msg)
        val msgMap = msg as? Map<*, *>
        PluginLogger.info("LIB deliver type=${msgMap?.get("type")} size=${json.length}b")
        val script = "window.__libDispatch && window.__libDispatch($json);"
        if (!webviewReady) {
            synchronized(pending) { pending.addLast(script) }
            return
        }
        ApplicationManager.getApplication().invokeLater({
            browser.cefBrowser.executeJavaScript(script, "", 0)
        }, ModalityState.defaultModalityState())
    }

    /** Deliver a message to the *filebrowser* panel (via window.__projspecFbDeliver). */
    private fun deliverToFbWebview(msg: Any) {
        val json = gson.toJson(msg)
        val msgMap = msg as? Map<*, *>
        val type = msgMap?.get("type")
        val preview = when (type) {
            "browseResult"    -> "entries=${((msgMap["entries"] as? List<*>)?.size ?: 0)}"
            "projectScanned"  -> "project=${msgMap["project"] != null} error=${msgMap["error"]}"
            "inspectResult"   -> "error=${msgMap["error"]}"
            "error"           -> "message=${msgMap["message"]}"
            else              -> json.take(100)
        }
        PluginLogger.info("FB deliver type=$type $preview")
        val script = "window.__projspecFbDeliver && window.__projspecFbDeliver($json);"
        ApplicationManager.getApplication().invokeLater({
            browser.cefBrowser.executeJavaScript(script, "", 0)
        }, ModalityState.defaultModalityState())
    }

    // ------------------------------------------------------------------------
    //  Busy indicator — counted so nested operations don't flicker the spinner
    // ------------------------------------------------------------------------

    private fun beginBusy() {
        if (busyCount.incrementAndGet() == 1) {
            deliverToWebview(mapOf("type" to "loading", "loading" to true))
        }
    }

    private fun endBusy() {
        if (busyCount.decrementAndGet() == 0) {
            deliverToWebview(mapOf("type" to "loading", "loading" to false))
        }
    }

    private fun withBusy(work: Runnable) {
        beginBusy()
        try { work.run() } finally { endBusy() }
    }

    // ------------------------------------------------------------------------
    //  Inbound message dispatch (JS → Kotlin)
    // ------------------------------------------------------------------------

    @Suppress("UNCHECKED_CAST")
    private fun handleJsMessage(rawJson: String) {
        PluginLogger.info("LIB JS cmd=" + (try { com.google.gson.JsonParser.parseString(rawJson).asJsonObject.get("cmd")?.asString } catch (_: Exception) { "?" }) + " raw=" + rawJson.take(120))
        val msg: Map<String, Any?> = try {
            gson.fromJson(rawJson, Map::class.java) as Map<String, Any?>
        } catch (e: Exception) {
            PluginLogger.warn("Failed to parse JS message: ${rawJson.take(200)}")
            return
        }
        when (msg["cmd"] as? String) {
            "ready"              -> onReady()
            "reload"             -> pool { reload(initial = false) }
            "add"                -> addProject()
            "configure"          -> configure()
            "openWith"           -> openWith(msg["tool"] as? String ?: "", msg["url"] as? String ?: "")
            "rescan"             -> pool { rescan(msg["url"] as? String ?: "") }
            "createSpec"         -> createSpecFor(msg["url"] as? String ?: "")
            "createSpecConfirmed"-> pool { createSpecConfirmed(
                msg["url"] as? String ?: "",
                msg["spec"] as? String ?: "",
            ) }
            "removeFromLibrary"  -> pool { removeFromLibrary(msg["url"] as? String ?: "") }
            "make"               -> make(
                msg["url"] as? String ?: "",
                msg["spec"] as? String,
                msg["artifactType"] as? String ?: "",
                msg["name"] as? String,
            )
            "copyToLocal"        -> Notifier.info("Copy to local: not implemented", project)
            "revealFile"         -> revealFile(msg["fn"] as? String ?: "")
            else -> { /* ignore */ }
        }
    }

    // ------------------------------------------------------------------------
    //  Filebrowser inbound dispatch (JS → Kotlin, via fbJsQuery)
    // ------------------------------------------------------------------------

    @Suppress("UNCHECKED_CAST")
    private fun handleFbJsMessage(rawJson: String) {
        val msg: Map<String, Any?> = try {
            gson.fromJson(rawJson, Map::class.java) as Map<String, Any?>
        } catch (e: Exception) {
            PluginLogger.warn("Failed to parse FB JS message: ${rawJson.take(200)}")
            return
        }
        val cmd = msg["cmd"] as? String
        val url = msg["url"] as? String ?: msg["parentUrl"] as? String ?: ""
        val so  = msg["storageOptions"] as? String
        PluginLogger.info("FB cmd=$cmd url=$url so=${so?.take(80) ?: "null"}")
        when (cmd) {
            "ready"        -> pool { fbInit() }
            "browse"       -> pool { fbBrowse(msg["url"] as? String ?: "", so,
                                              msg["push"] != false) }
            "inspect"      -> pool { fbInspect(msg["url"] as? String ?: "", so) }
            "scanDir"      -> pool { fbScanDir(msg["url"] as? String ?: "", so) }
            "expandDir"    -> pool { fbExpandDir(msg["url"] as? String ?: "", so) }
            "openFile"     -> fbOpenFile(msg["url"] as? String ?: "")
            "writeFile"    -> pool { fbWriteFile(msg["url"] as? String ?: "",
                                                  msg["content"] as? String ?: "", so) }
            "createFile"   -> pool { fbCreateFile(msg["parentUrl"] as? String ?: "",
                                                    msg["name"] as? String ?: "", so) }
            "deleteEntry"  -> pool { fbDeleteEntry(msg["url"] as? String ?: "",
                                                    msg["isDir"] == true, so) }
            "renameEntry"  -> pool { fbRenameEntry(msg["url"] as? String ?: "",
                                                    msg["newName"] as? String ?: "", so) }
            "mkdir"        -> pool { fbMkdir(msg["parentUrl"] as? String ?: "",
                                             msg["name"] as? String ?: "", so) }
            "addBookmark"  -> pool { fbBookmarkAdd(msg["url"] as? String ?: "",
                                                    msg["label"] as? String, so) }
            "removeBookmark" -> pool { fbBookmarkRemove(msg["url"] as? String ?: "") }
            "addToLibrary" -> pool { fbAddToLibrary(msg["url"] as? String ?: "", so) }
            "goToUrl"      -> pool { fbBrowse(msg["url"] as? String ?: "", so, true) }
            else -> { /* ignore */ }
        }
    }

    private fun pool(fn: () -> Unit) {
        // ApplicationManager.executeOnPooledThread was deprecated in 2024.1.
        // AppExecutorUtil.getAppExecutorService() is the current replacement.
        AppExecutorUtil.getAppExecutorService().submit {
            try { fn() } catch (e: Exception) {
                Notifier.error("projspec: ${e.message}", project)
            }
        }
    }

    private fun onReady() {
        // If injectBridge() already kicked things off, the JS `ready`
        // message is just a late confirmation and has nothing to do.
        if (initialLoadStarted.getAndSet(true)) return
        webviewReady = true
        synchronized(pending) {
            while (pending.isNotEmpty()) {
                val script = pending.removeFirst()
                ApplicationManager.getApplication().invokeLater({
                    browser.cefBrowser.executeJavaScript(script, "", 0)
                }, ModalityState.defaultModalityState())
            }
        }
        pool { reload(initial = true) }
    }

    // ------------------------------------------------------------------------
    //  Data loading
    // ------------------------------------------------------------------------

    /**
     * Fetch `info`, enum members, and the library listing, then push the
     * combined payload to the webview.  On the first call we also load the
     * enum members (panel.ts refreshes them only on `initial`).
     */
    private fun reload(initial: Boolean) {
        PluginLogger.info("reload(initial=$initial) starting")
        withBusy {
            if (initial || info == "null") {
                // info — try server, fall back to CLI
                val infoMap = server.info()
                if (infoMap != null) {
                    info = gson.toJson(infoMap)
                } else {
                    when (val res = ProjspecRunner.runInfo()) {
                        is CliResult.Success -> info = ProjspecRunner.extractJson(res.stdout).ifEmpty { "null" }
                        is CliResult.Failure -> { Notifier.error("projspec info: ${res.message}", project); info = "null" }
                    }
                }
                // enum members — try server, fall back to python3 introspection
                val enumMap = server.enumMembers()
                enums = if (enumMap != null) gson.toJson(enumMap)
                        else ProjspecRunner.runEnumMembers().ifBlank { "{}" }
                specNames = extractCreatableSpecs(info)
            }
            // library — try server, fall back to CLI
            val libMap = server.libraryList()
            if (libMap != null) {
                library = gson.toJson(libMap)
                @Suppress("UNCHECKED_CAST")
                libraryMap = libMap as Map<String, Any?>
            } else {
                when (val res = ProjspecRunner.runLibraryList()) {
                    is CliResult.Success -> {
                        val extracted = ProjspecRunner.extractJson(res.stdout)
                        library = extracted.ifEmpty { "{}" }
                        libraryMap = parseMap(library)
                    }
                    is CliResult.Failure -> {
                        Notifier.error("projspec library list: ${res.message}", project)
                        library = "{}"
                        libraryMap = emptyMap()
                    }
                }
            }
            postData()
            PluginLogger.info("reload complete; library has ${libraryMap.size} entries")
        }
    }

    private fun postData() {
        // Build the payload as a raw JSON string to preserve the webview's
        // expected shape without re-serialising through Kotlin types.
        val script = """
            (function(){
              var msg = {type:'data', info: $info, enums: $enums, library: $library};
              window.__projspecDeliver && window.__projspecDeliver(msg);
            })();
        """.trimIndent()
        if (!webviewReady) {
            synchronized(pending) { pending.addLast(script) }
            return
        }
        ApplicationManager.getApplication().invokeLater {
            browser.cefBrowser.executeJavaScript(script, browser.cefBrowser.url, 0)
        }
    }

    /** Parse a JSON object string into a Map, or an empty map on failure. */
    @Suppress("UNCHECKED_CAST")
    private fun parseMap(json: String): Map<String, Any?> =
        try { gson.fromJson(json, Map::class.java) as Map<String, Any?> }
        catch (_: Exception) { emptyMap() }

    /**
     * Extract the snake-case names of every spec marked `create: true` in the
     * `info` payload.  Used to pre-populate the Create-spec modal.
     */
    private fun extractCreatableSpecs(rawInfo: String): List<String> {
        val out = mutableListOf<String>()
        try {
            val root = JsonParser.parseString(rawInfo)
            if (!root.isJsonObject) return out
            val specs = root.asJsonObject.getAsJsonObject("specs") ?: return out
            for ((name, entry) in specs.entrySet()) {
                if (entry.isJsonObject && entry.asJsonObject.has("create") &&
                    entry.asJsonObject.get("create").asBoolean) {
                    out += name
                }
            }
        } catch (_: Exception) {}
        return out.sorted()
    }

    // ------------------------------------------------------------------------
    //  Toolbar actions
    // ------------------------------------------------------------------------

    /**
     * "Add" button — open a folder picker, scan the chosen path, reload.
     *
     * VSCode: `showOpenDialog({ canSelectFolders: true })` + `projspec scan --library`.
     */
    private fun addProject() {
        // FileChooserDescriptorFactory static methods were deprecated in 2024.2;
        // use the FileChooserDescriptor constructor directly.
        // invokeLater with explicit ModalityState to avoid running in a write-unsafe context.
        ApplicationManager.getApplication().invokeLater({
            val descriptor = FileChooserDescriptor(false, true, false, false, false, false)
                .withTitle("Add to Library")
            val chosen = FileChooser.chooseFile(descriptor, project, null) ?: return@invokeLater
            val target = chosen.path
            pool {
                withBusy {
                    if (server.scan(target, addToLibrary = true) == null) {
                        val res = ProjspecRunner.runScan(target)
                        if (res is CliResult.Failure) Notifier.warning("projspec scan: ${res.message}", project)
                    }
                    reload(initial = false)
                }
            }
        }, ModalityState.defaultModalityState())
    }

    /**
     * "Configure" button — open the user's projspec.json (creating a default
     * if needed) in an editor tab.  Matches the VSCode panel behaviour.
     */
    private fun configure() {
        val dir = System.getenv("PROJSPEC_CONFIG_DIR")
            ?: Paths.get(System.getProperty("user.home"), ".config", "projspec").toString()
        val file = File(dir, "projspec.json")
        try {
            if (!file.exists()) {
                file.parentFile?.mkdirs()
                file.writeText(DEFAULT_CONFIG)
            }
        } catch (e: Exception) {
            Notifier.error("Could not write ${file.path}: ${e.message}", project)
            return
        }
        // VfsUtil.findFileByIoFile performs a VFS refresh but is safe to call
        // from a background thread.  We do the lookup on the pool and then
        // navigate back to the EDT to open the editor, avoiding a blocking
        // refresh on the EDT (which was the previous pattern with
        // LocalFileSystem.refreshAndFindFileByIoFile).
        pool {
            val vf = VfsUtil.findFileByIoFile(file, true)
            if (vf != null) {
                ApplicationManager.getApplication().invokeLater({
                    // OpenFileDescriptor.navigate() is the non-deprecated replacement
                    // for the two-arg FileEditorManager.openFile(vf, true) form.
                    OpenFileDescriptor(project, vf).navigate(true)
                }, ModalityState.defaultModalityState())
                Notifier.info(
                    "ProjSpec configuration — <a href=\"https://projspec.readthedocs.io/en/latest/config.html\">see the docs</a> for all available fields.",
                    project
                )
            } else {
                Notifier.error("Could not open ${file.path}", project)
            }
        }
    }

    // ------------------------------------------------------------------------
    //  Kebab-menu actions
    // ------------------------------------------------------------------------

    private fun openWith(tool: String, url: String) {
        when (tool) {
            "vscode"      -> OpenWithHelper.openWithVSCode(project, url)
            "filebrowser" -> {
                // Switch to the File Browser tab and navigate there
                ApplicationManager.getApplication().invokeLater({
                    browser.cefBrowser.executeJavaScript(
                        "window.__projspecShowTab && window.__projspecShowTab('filebrowser');",
                        "", 0)
                }, ModalityState.defaultModalityState())
                pool { fbBrowse(url, null, false) }
            }
            "pycharm"     -> OpenWithHelper.openWithPyCharm(project, url)
            "jupyter"     -> OpenWithHelper.openWithJupyter(project, url)
        }
    }

    private fun rescan(url: String) {
        withBusy {
            val path = OpenWithHelper.urlToPath(url)
            val soJson = entryStorageOptions(url)
            if (server.scan(path, addToLibrary = true, storageOptions = soJson) == null) {
                val res = ProjspecRunner.runScan(path, soJson)
                if (res is CliResult.Failure) Notifier.warning("projspec scan: ${res.message}", project)
            }
            reload(initial = false)
        }
    }

    /**
     * The storage_options of the library entry for [url], serialised back to a
     * JSON string (or null if absent/empty).  Remote projects need these
     * re-supplied when the Project is reconstructed on rescan, otherwise the
     * filesystem access fails.
     */
    private fun entryStorageOptions(url: String): String? {
        return try {
            @Suppress("UNCHECKED_CAST")
            val proj = libraryMap[url] as? Map<String, Any?> ?: return null
            val so = proj["storage_options"] as? Map<String, Any?> ?: return null
            if (so.isEmpty()) null else gson.toJson(so)
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Show the create-spec modal — but first filter the known spec list by
     * what is *not* already present in the selected project.  Mirrors the
     * VSCode panel's `createSpecFor` handler.
     */
    private fun createSpecFor(url: String) {
        val existing: Set<String> = try {
            @Suppress("UNCHECKED_CAST")
            val proj = libraryMap[url] as? Map<String, Any?> ?: emptyMap()
            (proj["specs"] as? Map<String, Any?>)?.keys ?: emptySet()
        } catch (_: Exception) { emptySet() }

        val creatable = specNames.filter { it !in existing }
        if (creatable.isEmpty()) {
            Notifier.info("No spec types available to create.", project)
            return
        }
        deliverToWebview(mapOf(
            "type" to "openCreateSpecModal",
            "url" to url,
            "specs" to creatable,
        ))
    }

    private fun createSpecConfirmed(url: String, spec: String) {
        withBusy {
            val path = OpenWithHelper.urlToPath(url)
            val soJson = entryStorageOptions(url)
            // create — server or CLI
            if (server.create(spec, path) == null) {
                val res = ProjspecRunner.runCreate(spec, path)
                if (res is CliResult.Failure) Notifier.warning("projspec create: ${res.message}", project)
            }
            // rescan — server or CLI
            if (server.scan(path, addToLibrary = true, storageOptions = soJson) == null) {
                ProjspecRunner.runScan(path, soJson)
            }
            reload(initial = false)
        }
    }

    private fun removeFromLibrary(url: String) {
        withBusy {
            if (!server.libraryDelete(url)) {
                val res = ProjspecRunner.runLibraryDelete(url)
                if (res is CliResult.Failure) Notifier.warning("projspec library delete: ${res.message}", project)
            }
            reload(initial = false)
        }
    }

    // ------------------------------------------------------------------------
    //  Artifact widget actions
    // ------------------------------------------------------------------------

    /**
     * Run `projspec make <spec>.<artifactType>[.<name>] <projectPath>` in a
     * terminal tab so the user can watch the build.
     */
    private fun make(url: String, spec: String?, artifactType: String, name: String?) {
        val parts = buildList {
            if (!spec.isNullOrBlank()) add(spec)
            add(artifactType)
            if (!name.isNullOrBlank()) add(name)
        }
        if (parts.size < 1) return
        val artifactArg = parts.joinToString(".")
        val projectPath = OpenWithHelper.urlToPath(url)
        val cli = ProjspecSettings.instance.cliPath
        ApplicationManager.getApplication().invokeLater {
            TerminalRunner.makeArtifact(project, artifactArg, projectPath, cli)
        }
    }

    /**
     * Reveal a file in the Project tool window.  Accepts a local path or a
     * `file://` URL, expands simple wildcard patterns (e.g. a wheel glob such
     * as `dist` + `/` + `*.whl`), and opens the first match via
     * `FileEditorManager`.  Remote URLs are ignored with an info notification.
     */
    private fun revealFile(fn: String) {
        if (fn.isBlank()) return
        val local = if (fn.startsWith("file://")) fn.removePrefix("file://") else fn
        if (Regex("^[a-z][a-z0-9+.-]*://", RegexOption.IGNORE_CASE).containsMatchIn(local)) {
            Notifier.info("Cannot reveal remote file: $fn", project)
            return
        }
        val matches = expandGlob(local)
        if (matches.isEmpty()) {
            Notifier.info("No files match: $fn", project)
            return
        }
        val target = matches.first()
        // VfsUtil.findFileByIoFile is the non-blocking replacement for
        // LocalFileSystem.refreshAndFindFileByPath.
        val vf = VfsUtil.findFileByIoFile(File(target), true)
        if (vf == null) {
            Notifier.warning("Could not reveal $target", project)
            return
        }
        ApplicationManager.getApplication().invokeLater({
            OpenFileDescriptor(project, vf).navigate(true)
        }, ModalityState.defaultModalityState())
    }

    /** Expand `*` and `?` wildcards in a single path segment or whole path. */
    private fun expandGlob(pattern: String): List<String> {
        if (!pattern.contains('*') && !pattern.contains('?') && !pattern.contains('[')) {
            return if (Files.exists(Paths.get(pattern))) listOf(pattern) else emptyList()
        }
        val isAbsolute = pattern.startsWith('/')
        val parts = pattern.split('/').filter { it.isNotEmpty() }
        var current: List<String> = listOf(if (isAbsolute) "/" else ".")
        for (seg in parts) {
            val re = globSegmentToRegex(seg)
            val next = mutableListOf<String>()
            for (dir in current) {
                val d = File(dir)
                if (!d.isDirectory) continue
                for (entry in d.list() ?: emptyArray()) {
                    if (re.matches(entry)) {
                        next += File(d, entry).path
                    }
                }
            }
            current = next
        }
        return current
    }

    private fun globSegmentToRegex(seg: String): Regex {
        val sb = StringBuilder("^")
        for (ch in seg) {
            when (ch) {
                '*' -> sb.append("[^/]*")
                '?' -> sb.append("[^/]")
                '.', '+', '^', '$', '{', '}', '(', ')', '|', '\\' ->
                    sb.append('\\').append(ch)
                else -> sb.append(ch)
            }
        }
        sb.append('$')
        return Regex(sb.toString())
    }

    // ------------------------------------------------------------------------
    //  Filebrowser actions — server first, CLI fallback
    // ------------------------------------------------------------------------

    private fun fbBeginBusy() {
        if (fbBusyCount.incrementAndGet() == 1) deliverToFbWebview(mapOf("type" to "loading", "loading" to true))
    }
    private fun fbEndBusy() {
        if (fbBusyCount.decrementAndGet() == 0) deliverToFbWebview(mapOf("type" to "loading", "loading" to false))
    }
    private fun fbWithBusy(work: () -> Unit) { fbBeginBusy(); try { work() } finally { fbEndBusy() } }

    private fun fbInit() {
        fbWithBusy {
            val bms = server.bookmarksList()
                ?: gson.fromJson(ProjspecRunner.runFbCall("filebrowser", "bookmarks", "list"), List::class.java)
                ?: emptyList<Any>()
            val protos = server.protocols()
                ?: gson.fromJson(ProjspecRunner.runFbCall("filebrowser", "protocols"), List::class.java)
                ?: emptyList<Any>()
            val libUrls: List<Any> = (server.libraryList()
                ?: run {
                    val res = ProjspecRunner.runLibraryList()
                    if (res is CliResult.Success) parseMap(ProjspecRunner.extractJson(res.stdout)) else null
                })?.keys?.toList() ?: emptyList()
            deliverToFbWebview(mapOf(
                "type" to "init",
                "bookmarks"   to bms,
                "protocols"   to protos,
                "libraryUrls" to libUrls,
            ))
            fbBrowse(System.getProperty("user.home") ?: "/", null, false)
        }
    }

    private fun fbBrowse(url: String, storageOptions: String?, pushHistory: Boolean) {
        val so = server.parseSo(storageOptions)
        val data: Map<String, Any?> = server.browse(url, so)
            ?: run {
                val raw = ProjspecRunner.runFbBrowse(url, storageOptions)
                try { @Suppress("UNCHECKED_CAST") gson.fromJson(raw, Map::class.java) as Map<String, Any?> }
                catch (_: Exception) { mapOf("url" to url, "entries" to emptyList<Any>(), "error" to raw) }
            }
        deliverToFbWebview(data + mapOf("type" to "browseResult",
                                         "pushHistory" to pushHistory,
                                         "storageOptions" to (storageOptions ?: "")))
    }

    private fun fbInspect(url: String, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        val data: Map<String, Any?> = server.inspectAsProject(url, so)
            ?: run {
                val raw = ProjspecRunner.runFbInspectAsProject(url, storageOptions)
                try { @Suppress("UNCHECKED_CAST") gson.fromJson(raw, Map::class.java) as Map<String, Any?> }
                catch (_: Exception) { mapOf("url" to url, "error" to raw) }
            }
        deliverToFbWebview(data + mapOf("type" to "inspectResult"))
        deliverToFbWebview(mapOf(
            "type"         to "projectScanned",
            "url"          to url,
            "project"      to data["project"],
            "error"        to data["error"],
            "text_preview" to data["text_preview"],
            "info"         to (gson.fromJson(info,  Map::class.java) ?: emptyMap<String, Any>()),
            "enums"        to (gson.fromJson(enums, Map::class.java) ?: emptyMap<String, Any>()),
        ))
    }

    private fun fbScanDir(url: String, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        val data: Map<String, Any?> = server.scanDirectory(url, so)
            ?: run {
                // CLI fallback: calls projspec.filebrowser.scan_directory directly via
                // a short Python script so the result is always a {url, project, error}
                // dict — even for directories with no matching spec types.
                val rawJson = ProjspecRunner.runScanDirectory(url, storageOptions)
                try {
                    @Suppress("UNCHECKED_CAST")
                    if (rawJson.isNotBlank() && rawJson != "{}")
                        gson.fromJson(rawJson, Map::class.java) as Map<String, Any?>
                    else
                        mapOf("url" to url, "project" to null, "error" to "CLI scan produced no output")
                } catch (_: Exception) {
                    mapOf("url" to url, "project" to null, "error" to "Could not parse CLI scan output")
                }
            }
        deliverToFbWebview(mapOf(
            "type"  to "projectScanned",
            "info"  to (gson.fromJson(info,  Map::class.java) ?: emptyMap<String, Any>()),
            "enums" to (gson.fromJson(enums, Map::class.java) ?: emptyMap<String, Any>()),
        ) + data)
    }

    private fun fbExpandDir(url: String, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        val data: Map<String, Any?> = server.browse(url, so)
            ?: run {
                val raw = ProjspecRunner.runFbBrowse(url, storageOptions)
                try { @Suppress("UNCHECKED_CAST") gson.fromJson(raw, Map::class.java) as Map<String, Any?> }
                catch (_: Exception) { mapOf("url" to url, "entries" to emptyList<Any>()) }
            }
        deliverToFbWebview(data + mapOf("type" to "expandResult", "parentUrl" to url))
    }

    private fun fbOpenFile(url: String) {
        OpenWithHelper.openWithFileBrowser(project, url)
    }

    private fun fbWriteFile(url: String, content: String, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        if (server.writeFile(url, content, so) == null) ProjspecRunner.runFbWriteFile(url, content, storageOptions)
        fbBrowse(url.trimEnd('/').substringBeforeLast('/').ifBlank { "/" }, storageOptions, false)
    }

    private fun fbCreateFile(parentUrl: String, name: String, storageOptions: String?) {
        val newUrl = parentUrl.trimEnd('/') + "/" + name
        val so = server.parseSo(storageOptions)
        if (server.writeFile(newUrl, "", so) == null) ProjspecRunner.runFbWriteFile(newUrl, "", storageOptions)
        fbBrowse(parentUrl, storageOptions, false)
    }

    private fun fbDeleteEntry(url: String, isDir: Boolean, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        if (server.delete(url, isDir, so) == null) ProjspecRunner.runFbDelete(url, isDir, storageOptions)
        fbBrowse(url.trimEnd('/').substringBeforeLast('/').ifBlank { "/" }, storageOptions, false)
    }

    private fun fbRenameEntry(url: String, newName: String, storageOptions: String?) {
        val parent = url.trimEnd('/').substringBeforeLast('/').ifBlank { "/" }
        val dst = parent.trimEnd('/') + "/" + newName
        val so = server.parseSo(storageOptions)
        if (server.move(url, dst, so) == null) ProjspecRunner.runFbMove(url, dst, storageOptions)
        fbBrowse(parent, storageOptions, false)
    }

    private fun fbMkdir(parentUrl: String, name: String, storageOptions: String?) {
        val newUrl = parentUrl.trimEnd('/') + "/" + name
        val so = server.parseSo(storageOptions)
        if (server.mkdir(newUrl, so) == null) ProjspecRunner.runFbMkdir(newUrl, storageOptions)
        fbBrowse(parentUrl, storageOptions, false)
    }

    private fun fbBookmarkAdd(url: String, label: String?, storageOptions: String?) {
        val so = server.parseSo(storageOptions)
        val bmsList: List<*> = server.bookmarkAdd(url, label, so)
            ?: (gson.fromJson(ProjspecRunner.runFbBookmarkAdd(url, label, storageOptions), List::class.java) ?: emptyList<Any>())
        deliverToFbWebview(mapOf("type" to "bookmarksUpdated", "bookmarks" to bmsList))
    }

    private fun fbBookmarkRemove(url: String) {
        val bmsList: List<*> = server.bookmarkRemove(url)
            ?: (gson.fromJson(ProjspecRunner.runFbBookmarkRemove(url), List::class.java) ?: emptyList<Any>())
        deliverToFbWebview(mapOf("type" to "bookmarksUpdated", "bookmarks" to bmsList))
    }

    private fun fbAddToLibrary(url: String, storageOptions: String?) {
        fbWithBusy {
            val so = server.parseSo(storageOptions)
            if (server.addToLibrary(url, so) == null) {
                val args = mutableListOf("filebrowser", "add-to-library", url)
                if (storageOptions != null) { args.add("--storage-options"); args.add(storageOptions) }
                ProjspecRunner.runFbCall(*args.toTypedArray())
            }
            // Refresh library URL badges
            val libUrls = (server.libraryList()
                ?: run {
                    val res = ProjspecRunner.runLibraryList()
                    if (res is CliResult.Success) parseMap(ProjspecRunner.extractJson(res.stdout)) else null
                })?.keys?.toList() ?: emptyList<Any>()
            deliverToFbWebview(mapOf("type" to "libraryUrlsUpdated", "libraryUrls" to libUrls))
            // Reload library tab and switch to it
            pool { reload(initial = false) }
            ApplicationManager.getApplication().invokeLater({
                browser.cefBrowser.executeJavaScript(
                    "window.__projspecShowTab && window.__projspecShowTab('library');", "", 0)
            }, ModalityState.defaultModalityState())
        }
    }

    private companion object {
        private val LOG = Logger.getInstance(ProjspecToolWindowPanel::class.java)

        /** Shown immediately while the real HTML is being generated off-EDT. */
        private val LOADING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>body{margin:0;padding:20px;font-family:sans-serif;background:#2b2b2b;color:#aaa;}</style>
</head><body>Loading projspec&#8230;</body></html>"""

        /** Written by the "Configure" button when the file does not exist. */
        private val DEFAULT_CONFIG = """
            {
                "scan_types": [".py", ".yaml", ".yml", ".toml", ".json", ".md"],
                "scan_max_files": 100,
                "scan_max_size": 5000,
                "remote_artifact_status": false,
                "capture_artifact_output": true,
                "preferred_install_methods": ["conda", "pip"]
            }
        """.trimIndent()
    }
}
