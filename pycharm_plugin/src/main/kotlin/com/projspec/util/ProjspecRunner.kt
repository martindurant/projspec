package com.projspec.util

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.CapturingProcessHandler
import com.projspec.settings.ProjspecSettings

/**
 * Centralised wrapper around every `projspec` CLI invocation used by the
 * plugin.  Mirrors the TypeScript `projspec.ts` helpers in the VSCode
 * extension (vsextension/src/projspec.ts).
 *
 * All calls are synchronous and block the calling thread — they MUST NOT be
 * invoked from the EDT.  Call sites use `ApplicationManager.executeOnPooledThread`
 * or `ProgressManager.runProcessWithProgressSynchronously`.
 *
 * The CLI binary path is read from [ProjspecSettings] on every call so user
 * changes take effect without restarting the IDE.
 */
object ProjspecRunner {

    private val cli: String
        get() = ProjspecSettings.instance.cliPath

    // -------------------------------------------------------------------------
    // Commands used by the tool-window webview
    // VSCode equivalents live in vsextension/src/projspec.ts.
    // -------------------------------------------------------------------------

    /** `projspec info` — returns spec/artifact/content metadata (JSON). */
    fun runInfo(): CliResult = run(listOf(cli, "info"))

    /** `projspec library list --json-out` — returns project library JSON. */
    fun runLibraryList(): CliResult = run(listOf(cli, "library", "list", "--json-out"))

    /**
     * `projspec scan --library <path>` — scan & register a directory.
     *
     * *storageOptions*, when non-blank, is forwarded as `--storage_options`
     * (a JSON string) so remote projects (s3://, gcs://, …) can be re-scanned
     * with their filesystem credentials/flags intact.
     */
    fun runScan(path: String, storageOptions: String? = null): CliResult {
        val args = mutableListOf(cli, "scan", "--library")
        if (!storageOptions.isNullOrBlank()) {
            args.add("--storage_options")
            args.add(storageOptions)
        }
        args.add(path)
        return run(args)
    }

    /** `projspec create <spec> <path>` — create a new spec inside a project. */
    fun runCreate(spec: String, path: String): CliResult =
        run(listOf(cli, "create", spec, path))

    /** `projspec library delete <url>` — remove a URL from the library. */
    fun runLibraryDelete(url: String): CliResult =
        run(listOf(cli, "library", "delete", url))

    // -------------------------------------------------------------------------
    // Enum members (python3 introspection subprocess)
    //
    // `projspec info` does not expose Enum members, so — exactly as the VSCode
    // extension does — we call python3 directly to walk projspec.utils.Enum
    // subclasses and print {snake_name: {MEMBER_NAME: value}} as JSON.
    // -------------------------------------------------------------------------

    /**
     * Return a JSON string mapping snake-cased enum class name → {MEMBER_NAME:
     * value}, or an empty JSON object `"{}"` if python3 is unavailable or the
     * import fails.  The result is consumed by the webview's YAML renderer to
     * display enum values as their member name instead of a raw integer.
     */
    fun runEnumMembers(): String {
        val script = """
            import json, importlib, pkgutil
            import projspec.utils as pu
            import projspec.content, projspec.artifact
            for pkg in (projspec.content, projspec.artifact):
                for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + '.'):
                    importlib.import_module(m.name)
            from projspec.utils import camel_to_snake
            out, seen = {}, set()
            def walk(cls):
                for sub in cls.__subclasses__():
                    if sub in seen: continue
                    seen.add(sub); walk(sub)
                    out[camel_to_snake(sub.__name__)] = {m.name: m.value for m in sub}
            walk(pu.Enum)
            print(json.dumps(out))
            """.trimIndent()
        return when (val r = run(listOf("python", "-c", script))) {
            is CliResult.Success -> r.stdout.trim().ifBlank { "{}" }
            is CliResult.Failure -> "{}"
        }
    }

    // -------------------------------------------------------------------------
    // Filebrowser CLI wrappers
    // -------------------------------------------------------------------------

    /**
     * Generic filebrowser subcommand call.  Returns the raw stdout string
     * (JSON) or an empty JSON object on failure.
     */
    fun runFbCall(vararg args: String): String {
        val fullArgs = listOf(cli) + args.toList()
        return when (val r = run(fullArgs)) {
            is CliResult.Success -> extractJson(r.stdout).ifBlank { "{}" }
            is CliResult.Failure -> "{}"
        }
    }

    /** `projspec filebrowser inspect-as-project <url>` — file metadata + project-shaped dict. */
    fun runFbInspectAsProject(url: String, storageOptions: String?): String {
        val args = mutableListOf(cli, "filebrowser", "inspect-as-project", url)
        if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
        return when (val r = run(args)) {
            is CliResult.Success -> extractJson(r.stdout).ifBlank { "{}" }
            is CliResult.Failure -> """{"url":"$url","project":null,"error":"${r.message.replace("\"","'")}"}"""
        }
    }

    /** `projspec filebrowser browse <url> [--storage-options JSON]` */
    fun runFbBrowse(url: String, storageOptions: String?): String {
        val args = mutableListOf(cli, "filebrowser", "browse", url)
        if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
        return when (val r = run(args)) {
            is CliResult.Success -> extractJson(r.stdout).ifBlank { "{}" }
            is CliResult.Failure -> """{"url":"$url","entries":[],"error":"${r.message.replace("\"","'")}"}"""
        }
    }

    /** `projspec filebrowser write-file <url>` (content via stdin/tmp file) */
    fun runFbWriteFile(url: String, content: String, storageOptions: String?) {
        // Write content to a temp file then pass via --content-file
        val tmp = java.io.File.createTempFile("projspec_fb_", ".txt")
        try {
            tmp.writeText(content, Charsets.UTF_8)
            val args = mutableListOf(cli, "filebrowser", "write-file", url,
                "--content-file", tmp.absolutePath)
            if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
            run(args)
        } finally { tmp.delete() }
    }

    /** `projspec filebrowser delete <url> [--recursive] [--storage-options JSON]` */
    fun runFbDelete(url: String, recursive: Boolean, storageOptions: String?) {
        val args = mutableListOf(cli, "filebrowser", "delete", url)
        if (recursive) args.add("--recursive")
        if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
        run(args)
    }

    /** `projspec filebrowser move <src> <dst> [--storage-options JSON]` */
    fun runFbMove(src: String, dst: String, storageOptions: String?) {
        val args = mutableListOf(cli, "filebrowser", "move", src, dst)
        if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
        run(args)
    }

    /** `projspec filebrowser mkdir <url> [--storage-options JSON]` */
    fun runFbMkdir(url: String, storageOptions: String?) {
        val args = mutableListOf(cli, "filebrowser", "mkdir", url)
        if (!storageOptions.isNullOrBlank()) { args.add("--storage-options"); args.add(storageOptions) }
        run(args)
    }

    /** `projspec filebrowser bookmarks add <url> [--label LABEL]` → JSON bookmarks list */
    fun runFbBookmarkAdd(url: String, label: String?, storageOptions: String?): String {
        val args = mutableListOf(cli, "filebrowser", "bookmarks", "add", url)
        if (!label.isNullOrBlank()) { args.add("--label"); args.add(label) }
        return when (val r = run(args)) {
            is CliResult.Success -> extractJson(r.stdout).ifBlank { "[]" }
            is CliResult.Failure -> "[]"
        }
    }

    /** `projspec filebrowser bookmarks remove <url>` → JSON bookmarks list */
    fun runFbBookmarkRemove(url: String): String {
        return when (val r = run(listOf(cli, "filebrowser", "bookmarks", "remove", url))) {
            is CliResult.Success -> extractJson(r.stdout).ifBlank { "[]" }
            is CliResult.Failure -> "[]"
        }
    }

    // -------------------------------------------------------------------------
    // Internal execution
    // -------------------------------------------------------------------------

    /**
     * Execute an external command and capture stdout/stderr.
     *
     * Runs via the login shell (`$SHELL -l -c`) so the user's PATH
     * (conda envs, pyenv, Homebrew, etc.) is available exactly as it
     * would be in a terminal — even when launched from a macOS GUI app.
     *
     * Every call is logged with args, duration, and result.
     *
     * NOTE: blocks the calling thread for up to 60 s.  Never call from the EDT.
     */
    fun run(args: List<String>): CliResult {
        // Shell-quote each argument so spaces and special chars survive
        fun quote(s: String) = "'" + s.replace("'", "'\\''") + "'"
        val shellCmd = args.joinToString(" ") { quote(it) }
        val shell = System.getenv("SHELL")?.takeIf { it.isNotBlank() } ?: "/bin/sh"
        val fullArgs = listOf(shell, "-l", "-c", shellCmd)

        PluginLogger.info("CLI call: $shellCmd")
        val t0 = System.currentTimeMillis()
        return try {
            val commandLine = GeneralCommandLine(fullArgs)
            val handler = CapturingProcessHandler(commandLine)
            val output = handler.runProcess(60_000)
            val ms = System.currentTimeMillis() - t0

            when {
                output.isTimeout -> {
                    PluginLogger.error("CLI timeout after 60 s: $shellCmd")
                    CliResult.Failure("projspec timed out after 60 s", -1)
                }
                output.exitCode != 0 -> {
                    val err = output.stderr.ifBlank { output.stdout }.ifBlank { "Exit code ${output.exitCode}" }
                    PluginLogger.warn("CLI exit ${output.exitCode} (${ms}ms): $shellCmd\n  stderr: ${output.stderr.trim().take(500)}\n  stdout: ${output.stdout.trim().take(500)}")
                    CliResult.Failure(err, output.exitCode)
                }
                else -> {
                    PluginLogger.info("CLI ok (${ms}ms): $shellCmd  stdout(${output.stdout.length}b)")
                    CliResult.Success(output.stdout)
                }
            }
        } catch (e: Exception) {
            val ms = System.currentTimeMillis() - t0
            PluginLogger.error("CLI exception (${ms}ms): $shellCmd\n  ${e.javaClass.simpleName}: ${e.message}")
            CliResult.Failure("Failed to launch: ${e.message}")
        }
    }

    /**
     * Extract the first balanced JSON object/array from CLI stdout.  Some
     * projspec subcommands print banners before the JSON payload; this
     * helper is the Kotlin equivalent of `parseJsonOutput` in projspec.ts.
     *
     * Returns an empty string if no JSON can be found.
     */
    fun extractJson(stdout: String): String {
        val trimmed = stdout.trim()
        if (trimmed.isEmpty()) return ""
        val firstChar = trimmed.first()
        if (firstChar == '{' || firstChar == '[') return trimmed
        val start = trimmed.indexOfFirst { it == '{' || it == '[' }
        if (start < 0) return ""
        return trimmed.substring(start)
    }
}
