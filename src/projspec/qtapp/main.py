"""Qt-based desktop UI for projspec — two-tab panel: Library + File Browser.

A single QMainWindow hosts a QWebEngineView rendering the combined
HTML/CSS/JS panel with a *Project Library* tab and a *File Browser* tab.
Python plays the role of the "extension host": it calls projspec and
filebrowser APIs directly (no subprocess) and communicates with both tabs
via two separate QWebChannel bridge objects.
"""

from __future__ import annotations

import json
import os
import os.path
from pathlib import Path
import subprocess
import sys
import warnings
import webbrowser

import projspec
from projspec.library import ProjectLibrary
from projspec.utils import class_infos

try:
    from PyQt5.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWebChannel import QWebChannel
    from PyQt5.QtWebEngineWidgets import QWebEngineSettings, QWebEngineView
    from PyQt5.QtWidgets import (
        QApplication,
        QFileDialog,
        QMainWindow,
        QMessageBox,
        QVBoxLayout,
        QWidget,
    )

    qt = True
except ImportError:
    # fallbacks to make this module importable and give a decent message
    QObject = object
    QMainWindow = object
    pyqtSignal = lambda *_: None
    pyqtSlot = lambda *_: lambda *_: None
    warnings.warn("PyQt5 not installed", ImportWarning)
    qt = False

from projspec.qtapp.views import get_qt_html


library = ProjectLibrary()


DEFAULT_CONFIG = {
    "scan_types": [".py", ".yaml", ".yml", ".toml", ".json", ".md"],
    "scan_max_files": 100,
    "scan_max_size": 5000,
    "remote_artifact_status": False,
    "capture_artifact_output": True,
    "preferred_install_methods": ["conda", "pip"],
}


class JsBridge(QObject):
    """Exposed to the webview under the global name ``bridge``.

    Every JavaScript → Python call goes through :meth:`handleMessage`, which
    decodes a JSON string and hands the dict off to a single Python handler.
    Python → JavaScript calls emit :attr:`from_python` whose connected JS-side
    slot dispatches on ``type``.
    """

    from_python = pyqtSignal(str)  # JSON-encoded outbound message

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handler = None

    def set_handler(self, handler) -> None:
        self._handler = handler

    @pyqtSlot(str)
    def handleMessage(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        if self._handler:
            self._handler(data)

    def send(self, msg: dict) -> None:
        """Serialise ``msg`` and hand it to the connected JS listener."""
        self.from_python.emit(json.dumps(msg))


class ProjspecWindow(QMainWindow):
    """Single-window Qt app: Project Library tab + File Browser tab."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Projspec Browser")
        self.resize(1400, 820)

        self._info_data: dict = {}
        self._enum_members: dict = {}

        # ── Library bridge ───────────────────────────────────────────────────
        self._bridge = JsBridge(self)
        self._bridge.set_handler(self._on_lib_message)

        # ── File-browser bridge ──────────────────────────────────────────────
        self._fb_bridge = JsBridge(self)
        self._fb_bridge.set_handler(self._on_fb_message)

        # ── Webview + channel ────────────────────────────────────────────────
        self._view = QWebEngineView(self)
        channel = QWebChannel(self._view.page())
        channel.registerObject("bridge", self._bridge)
        channel.registerObject("fb_bridge", self._fb_bridge)
        self._view.page().setWebChannel(channel)

        settings = self._view.settings()
        for attr in (
            "LocalContentCanAccessRemoteUrls",
            "LocalContentCanAccessFileUrls",
            "ErrorPageEnabled",
        ):
            constant = getattr(
                QWebEngineSettings.WebAttribute
                if hasattr(QWebEngineSettings, "WebAttribute")
                else QWebEngineSettings,
                attr,
                None,
            )
            if constant is not None:
                settings.setAttribute(constant, True)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self.setCentralWidget(central)

        # Write the combined HTML to a temp file and load via file:// URL.
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8"
        )
        tmp.write(get_qt_html())
        tmp.close()
        self._html_tempfile = tmp.name
        self._view.setUrl(QUrl.fromLocalFile(tmp.name))
        self._view.loadFinished.connect(self._on_load_finished)

        self._lib_busy = 0
        self._fb_busy = 0

    def closeEvent(self, event) -> None:  # - Qt naming
        """Remove the temp HTML file on window close."""
        try:
            os.unlink(self._html_tempfile)
        except (OSError, AttributeError):
            pass
        super().closeEvent(event)

    # ── Busy indicators ─────────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._lib_busy += 1
            if self._lib_busy == 1:
                self._bridge.send({"type": "loading", "loading": True})
        else:
            self._lib_busy = max(0, self._lib_busy - 1)
            if self._lib_busy == 0:
                self._bridge.send({"type": "loading", "loading": False})

    def _set_fb_busy(self, busy: bool) -> None:
        if busy:
            self._fb_busy += 1
            if self._fb_busy == 1:
                self._fb_bridge.send({"type": "loading", "loading": True})
        else:
            self._fb_busy = max(0, self._fb_busy - 1)
            if self._fb_busy == 0:
                self._fb_bridge.send({"type": "loading", "loading": False})

    # ── Initial load ────────────────────────────────────────────────────────

    def _on_load_finished(self, ok: bool) -> None:  # - signal arg
        self._reload(initial=True)
        self._fb_init()

    def _reload(self, initial: bool = False, select_url: str | None = None) -> None:
        self._set_busy(True)
        try:
            if initial or not self._info_data:
                self._info_data = class_infos()
                self._enum_members = _collect_enum_members()
            library.load()
            lib_dict = {
                url: proj.to_dict(compact=False)
                for url, proj in library.entries.items()
            }
            msg: dict = {
                "type": "data",
                "info": self._info_data,
                "enums": self._enum_members,
                "library": lib_dict,
            }
            if select_url:
                msg["selectUrl"] = select_url
            self._bridge.send(msg)
        except Exception as e:
            QMessageBox.warning(self, "projspec", f"Reload failed: {e}")
        finally:
            self._set_busy(False)

    def _fb_init(self) -> None:
        """Send initial data to the file browser tab."""
        import os
        from projspec.filebrowser import (
            bookmarks_list,
            supported_protocols,
        )

        self._set_fb_busy(True)
        try:
            bms = bookmarks_list()
            protos = supported_protocols()
            lib_urls = list(library.entries.keys())
            self._fb_bridge.send(
                {
                    "type": "init",
                    "bookmarks": bms,
                    "protocols": protos,
                    "libraryUrls": lib_urls,
                }
            )
            # Navigate to home directory
            self._fb_browse(os.path.expanduser("~"), push_history=False)
        except Exception as e:
            self._fb_bridge.send({"type": "error", "message": str(e)})
        finally:
            self._set_fb_busy(False)

    # ── Library message dispatcher ──────────────────────────────────────────

    def _on_lib_message(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        try:
            if cmd == "ready":
                self._reload(initial=True)
            elif cmd == "reload":
                self._reload()
            elif cmd == "add":
                self._action_add()
            elif cmd == "configure":
                self._action_configure()
            elif cmd == "openWith":
                self._action_open_with(msg.get("tool", ""), msg.get("url", ""))
            elif cmd == "rescan":
                self._action_rescan(msg.get("url", ""))
            elif cmd == "createSpec":
                self._action_create_spec(msg.get("url", ""))
            elif cmd == "createSpecConfirmed":
                self._action_create_spec_confirmed(
                    msg.get("url", ""), msg.get("spec", "")
                )
            elif cmd == "removeFromLibrary":
                self._action_remove(msg.get("url", ""))
            elif cmd == "make":
                self._action_make(
                    msg.get("url", ""),
                    msg.get("spec"),
                    msg.get("artifactType", ""),
                    msg.get("name"),
                )
            elif cmd == "copyToLocal":
                QMessageBox.information(
                    self, "projspec", "Copy to local: not implemented"
                )
            elif cmd == "revealFile":
                self._action_reveal_file(msg.get("fn", ""))
        except Exception as e:
            QMessageBox.warning(self, "projspec", f"{cmd}: {e}")

    # ── Filebrowser message dispatcher ──────────────────────────────────────

    def _on_fb_message(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        try:
            if cmd == "ready":
                self._fb_init()
            elif cmd == "browse":
                self._fb_browse(
                    msg["url"],
                    storage_options=msg.get("storageOptions") or None,
                    push_history=msg.get("push", True),
                )
            elif cmd == "inspect":
                self._fb_inspect(msg["url"], msg.get("storageOptions") or None)
            elif cmd == "scanDir":
                self._fb_scan_dir(msg["url"], msg.get("storageOptions") or None)
            elif cmd == "expandDir":
                self._fb_expand_dir(msg["url"], msg.get("storageOptions") or None)
            elif cmd == "openFile":
                self._fb_open_file(msg["url"], msg.get("storageOptions") or None)
            elif cmd == "writeFile":
                self._fb_write_file(
                    msg["url"], msg["content"], msg.get("storageOptions") or None
                )
            elif cmd == "createFile":
                self._fb_create_file(
                    msg["parentUrl"], msg["name"], msg.get("storageOptions") or None
                )
            elif cmd == "deleteEntry":
                self._fb_delete_entry(
                    msg["url"],
                    msg.get("isDir", False),
                    msg.get("storageOptions") or None,
                )
            elif cmd == "renameEntry":
                self._fb_rename_entry(
                    msg["url"], msg["newName"], msg.get("storageOptions") or None
                )
            elif cmd == "mkdir":
                self._fb_mkdir(
                    msg["parentUrl"], msg["name"], msg.get("storageOptions") or None
                )
            elif cmd == "addBookmark":
                self._fb_bookmark_add(
                    msg["url"], msg.get("label"), msg.get("storageOptions") or None
                )
            elif cmd == "removeBookmark":
                self._fb_bookmark_remove(msg["url"])
            elif cmd == "addToLibrary":
                self._fb_add_to_library(msg["url"], msg.get("storageOptions") or None)
            elif cmd == "goToUrl":
                self._fb_browse(
                    msg["url"], msg.get("storageOptions") or None, push_history=True
                )
        except Exception as e:
            self._fb_bridge.send({"type": "error", "message": str(e)})

    # ── Actions ─────────────────────────────────────────────────────────────

    def _action_add(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Add directory to library", str(Path.home())
        )
        if not path:
            return
        self._scan_and_reload(path, walk=True)

    def _action_configure(self) -> None:
        conf_dir = Path(
            os.environ.get("PROJSPEC_CONFIG_DIR")
            or (Path.home() / ".config" / "projspec")
        )
        conf_file = conf_dir / "projspec.json"
        if not conf_file.exists():
            conf_dir.mkdir(parents=True, exist_ok=True)
            conf_file.write_text(json.dumps(DEFAULT_CONFIG, indent=4))
        # Open in the OS default editor - there's no in-app editor here.
        _open_with_default(str(conf_file))
        # Show docs link so users are not left with a bare JSON file.
        webbrowser.open("https://projspec.readthedocs.io/en/latest/config.html")

    def _action_open_with(self, tool: str, url: str) -> None:
        local = _url_to_local(url)
        if tool == "vscode":
            _spawn_detached(["code", local])
        elif tool == "filebrowser":
            # Switch to the File Browser tab and navigate there
            self._view.page().runJavaScript(
                f"window.__projspecShowTab && window.__projspecShowTab('filebrowser');"
            )
            self._fb_browse(url, push_history=False)
        elif tool == "pycharm":
            _spawn_detached(["pycharm", local, "nosplash", "dontReopenProjects"])
        elif tool == "jupyter":
            _spawn_detached(["jupyter", "lab", local])

    def _action_rescan(self, url: str) -> None:
        self._rescan(url)

    def _action_create_spec(self, url: str) -> None:
        proj = library.entries.get(url)
        existing = set((proj.specs if proj is not None else {}) or {})
        creatable = sorted(
            name
            for name, entry in (self._info_data.get("specs") or {}).items()
            if entry.get("create") and name not in existing
        )
        creatable = sorted(
            name
            for name, entry in (self._info_data.get("specs") or {}).items()
            if entry.get("create") and name not in existing
        )
        if not creatable:
            QMessageBox.information(
                self, "Create spec", "No spec types available to create."
            )
            return
        self._bridge.send(
            {"type": "openCreateSpecModal", "url": url, "specs": creatable}
        )

    def _action_create_spec_confirmed(self, url: str, spec: str) -> None:
        if not spec:
            return
        self._set_busy(True)
        try:
            path = _url_to_local(url)
            existing = library.entries.get(url)
            storage_options = dict(getattr(existing, "storage_options", None) or {})
            proj = projspec.Project(path, walk=False, storage_options=storage_options)
            proj.create(spec)
            # Rescan and refresh.
            fresh = projspec.Project(path, walk=False, storage_options=storage_options)
            library.add_entry(path, fresh)
            self._reload()
        except Exception as e:
            QMessageBox.warning(self, "Create spec", f"Failed to create '{spec}': {e}")
        finally:
            self._set_busy(False)

    def _action_remove(self, url: str) -> None:
        if url in library.entries:
            del library.entries[url]
            library.save()
        self._reload()

    def _action_make(
        self,
        url: str,
        spec: str | None,
        artifact_type: str,
        name: str | None,
    ) -> None:
        qname = ".".join(p for p in (spec, artifact_type, name) if p)
        proj = library.entries.get(url)
        if proj is None:
            QMessageBox.warning(self, "Make", f"Project not found: {url}")
            return
        self._set_busy(True)
        try:
            art = proj.make(qname)
            QMessageBox.information(self, "Make", f"Done: {art}")
        except Exception as e:
            QMessageBox.warning(self, "Make", f"Make '{qname}' failed: {e}")
        finally:
            self._set_busy(False)

    def _action_reveal_file(self, fn: str) -> None:
        """Best-effort equivalent of the vscode ``revealInExplorer`` command."""
        if not fn:
            return
        local = fn[len("file://") :] if fn.startswith("file://") else fn
        # Remote artifacts can't be revealed.
        if "://" in local and not local.startswith("/"):
            QMessageBox.information(self, "Reveal", f"Remote file: {fn}")
            return
        matches = _expand_glob(local)
        if not matches:
            QMessageBox.information(self, "Reveal", f"No files match: {fn}")
            return
        target = matches[0]
        if len(matches) > 1:
            from PyQt5.QtWidgets import QInputDialog

            pick, ok = QInputDialog.getItem(
                self,
                "Reveal",
                f"{len(matches)} matches - pick one:",
                matches,
                0,
                False,
            )
            if not ok or not pick:
                return
            target = pick
        _open_with_default(os.path.dirname(target) or target)

    # ── File browser actions (all in-process via filebrowser.py) ────────────

    def _fb_browse(
        self, url: str, storage_options=None, push_history: bool = True
    ) -> None:
        from projspec.filebrowser import browse

        so = _parse_so(storage_options)
        data = browse(url, storage_options=so)
        self._fb_bridge.send(
            {
                "type": "browseResult",
                "pushHistory": push_history,
                "storageOptions": json.dumps(so) if so else "",
                **data,
            }
        )

    def _fb_inspect(self, url: str, storage_options=None) -> None:
        from projspec.filebrowser import inspect_as_project

        so = _parse_so(storage_options)
        data = inspect_as_project(url, storage_options=so)
        self._fb_bridge.send({"type": "inspectResult", **data})
        self._fb_bridge.send(
            {
                "type": "projectScanned",
                "url": url,
                "project": data.get("project"),
                "error": data.get("error"),
                "text_preview": data.get("text_preview"),
                "info": self._info_data,
                "enums": self._enum_members,
            }
        )

    def _fb_scan_dir(self, url: str, storage_options=None) -> None:
        from projspec.filebrowser import scan_directory

        so = _parse_so(storage_options)
        data = scan_directory(url, storage_options=so)
        self._fb_bridge.send(
            {
                "type": "projectScanned",
                "info": self._info_data,
                "enums": self._enum_members,
                **data,
            }
        )

    def _fb_expand_dir(self, url: str, storage_options=None) -> None:
        from projspec.filebrowser import browse

        so = _parse_so(storage_options)
        data = browse(url, storage_options=so)
        self._fb_bridge.send({"type": "expandResult", "parentUrl": url, **data})

    def _fb_open_file(self, url: str, storage_options=None) -> None:
        """Open a remote file: fetch content, write to temp file, open in default app."""
        import os, tempfile

        local = _url_to_local(url)
        if os.path.exists(local):
            _open_with_default(local)
            return
        from projspec.filebrowser import read_file

        so = _parse_so(storage_options)
        result = read_file(url, storage_options=so)
        if result.get("error"):
            QMessageBox.warning(self, "Open file", result["error"])
            return
        ext = os.path.splitext(url)[1] or ".txt"
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=ext, delete=False, encoding="utf-8"
        )
        tmp.write(result.get("content", ""))
        tmp.close()
        _open_with_default(tmp.name)

    def _fb_write_file(self, url: str, content: str, storage_options=None) -> None:
        from projspec.filebrowser import write_file

        so = _parse_so(storage_options)
        result = write_file(url, content, storage_options=so)
        if result.get("error"):
            QMessageBox.warning(self, "Write file", result["error"])
            return
        parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
        self._fb_browse(parent, storage_options, push_history=False)

    def _fb_create_file(self, parent_url: str, name: str, storage_options=None) -> None:
        from projspec.filebrowser import write_file

        so = _parse_so(storage_options)
        new_url = parent_url.rstrip("/") + "/" + name
        result = write_file(new_url, "", storage_options=so)
        if result.get("error"):
            QMessageBox.warning(self, "Create file", result["error"])
            return
        self._fb_browse(parent_url, storage_options, push_history=False)

    def _fb_delete_entry(self, url: str, is_dir: bool, storage_options=None) -> None:
        from projspec.filebrowser import delete

        label = url.rstrip("/").rsplit("/", 1)[-1] or url
        reply = QMessageBox.question(
            self,
            "Delete",
            f'Delete "{label}"?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        so = _parse_so(storage_options)
        result = delete(url, storage_options=so, recursive=is_dir)
        if result.get("error"):
            QMessageBox.warning(self, "Delete", result["error"])
            return
        parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
        self._fb_browse(parent, storage_options, push_history=False)

    def _fb_rename_entry(self, url: str, new_name: str, storage_options=None) -> None:
        from projspec.filebrowser import move

        so = _parse_so(storage_options)
        parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
        dst = parent.rstrip("/") + "/" + new_name
        result = move(url, dst, storage_options=so)
        if result.get("error"):
            QMessageBox.warning(self, "Rename", result["error"])
            return
        self._fb_browse(parent, storage_options, push_history=False)

    def _fb_mkdir(self, parent_url: str, name: str, storage_options=None) -> None:
        from projspec.filebrowser import mkdir

        so = _parse_so(storage_options)
        new_url = parent_url.rstrip("/") + "/" + name
        result = mkdir(new_url, storage_options=so)
        if result.get("error"):
            QMessageBox.warning(self, "New folder", result["error"])
            return
        self._fb_browse(parent_url, storage_options, push_history=False)

    def _fb_bookmark_add(self, url: str, label=None, storage_options=None) -> None:
        from projspec.filebrowser import bookmark_add

        so = _parse_so(storage_options)
        bms = bookmark_add(url, label=label or "", storage_options=so)
        self._fb_bridge.send({"type": "bookmarksUpdated", "bookmarks": bms})

    def _fb_bookmark_remove(self, url: str) -> None:
        from projspec.filebrowser import bookmark_remove

        bms = bookmark_remove(url)
        self._fb_bridge.send({"type": "bookmarksUpdated", "bookmarks": bms})

    def _fb_add_to_library(self, url: str, storage_options=None) -> None:
        from projspec.filebrowser import add_to_projspec_library

        self._set_fb_busy(True)
        try:
            so = _parse_so(storage_options)
            result = add_to_projspec_library(url, storage_options=so)
            if result.get("error"):
                QMessageBox.warning(self, "Add to library", result["error"])
                return
            # Refresh library URL badges in the filebrowser
            self._fb_bridge.send(
                {
                    "type": "libraryUrlsUpdated",
                    "libraryUrls": list(library.entries.keys()),
                }
            )
            # Reload library tab and select the new entry
            self._reload(select_url=url)
            # Switch to library tab
            self._view.page().runJavaScript(
                "window.__projspecShowTab && window.__projspecShowTab('library');"
            )
        finally:
            self._set_fb_busy(False)

    # ── Scan helper ─────────────────────────────────────────────────────────

    def _rescan(self, url: str) -> None:
        """Re-run ``Project(...)`` for an existing library entry and replace it.

        The original library key (*url*) is preserved so the entry's identity
        does not drift (selection in the UI is keyed on it).

        The path used to rebuild the project must keep its protocol so remote
        projects re-open against the right filesystem.  We prefer the library
        key itself when it already carries a protocol (e.g.
        ``memory:///proj``, ``s3://bucket/key``) - it is the authoritative
        protocol-qualified identifier the UI holds, and is reliable even when
        an older serialised library reconstructed the entry's filesystem as
        local.  Otherwise we fall back to the stored project's
        protocol-qualified URL.
        """
        if not url:
            return
        self._set_busy(True)
        try:
            existing = library.entries.get(url)
            storage_options = dict(getattr(existing, "storage_options", None) or {})
            path = _rescan_path(url, existing)
            proj = projspec.Project(path, walk=False, storage_options=storage_options)
            # Keep the original key so we update the entry in place rather than
            # creating a duplicate under a differently-formatted key.
            library.add_entry(url, proj)
            self._reload()
        except Exception as e:
            QMessageBox.warning(self, "Rescan", f"Rescan failed: {e}")
        finally:
            self._set_busy(False)

    def _scan_and_reload(self, url: str, walk: bool) -> None:
        self._set_busy(True)
        try:
            path = _url_to_local(url) if url.startswith("file://") else url
            # Re-supply storage_options from the existing library entry (if
            # any) so rescanning a remote project keeps working.
            existing = library.entries.get(url)
            storage_options = dict(getattr(existing, "storage_options", None) or {})
            proj = projspec.Project(path, walk=walk, storage_options=storage_options)
            if walk:
                for child_url, child in (proj.children or {}).items():
                    if child.specs:
                        library.add_entry(child_url, child)
            if proj.specs:
                library.add_entry(path, proj)
            self._reload()
        except Exception as e:
            QMessageBox.warning(self, "Scan", f"Scan failed: {e}")
        finally:
            self._set_busy(False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_so(storage_options) -> dict | None:
    """Parse a storage_options value that may be a JSON string, dict, or None."""
    if not storage_options:
        return None
    if isinstance(storage_options, dict):
        return storage_options or None
    if isinstance(storage_options, str):
        try:
            return json.loads(storage_options) or None
        except Exception:
            return None
    return None


def _url_to_local(url: str) -> str:
    """Strip ``file://`` prefix so the result is a plain path."""
    if url.startswith("file://"):
        return url[len("file://") :]
    return url


def _rescan_path(url: str, existing) -> str:
    """Resolve the path to re-open *url* as a Project, preserving protocol.

    Prefers the library key *url* when it already carries a protocol (it is the
    authoritative, protocol-qualified identifier and is reliable even if an
    older serialised library reconstructed the entry's filesystem as local).
    Otherwise falls back to the stored project's protocol-qualified URL, and
    finally to the key itself.
    """
    if "://" in url:
        return url
    if existing is not None:
        try:
            return existing.fs.unstrip_protocol(existing.url)
        except Exception:
            return getattr(existing, "path", url) or url
    return url


def _spawn_detached(cmd: list[str]) -> None:
    """Launch an external tool without blocking the Qt event loop."""
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        QMessageBox.warning(None, "projspec", f"Command not found: {cmd[0]}")
    except Exception as e:
        QMessageBox.warning(None, "projspec", f"Failed to run {cmd[0]}: {e}")


def _open_with_default(path: str) -> None:
    """Open ``path`` with the OS default handler."""
    try:
        if sys.platform == "darwin":
            subprocess.call(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.call(["xdg-open", path])
    except Exception:
        webbrowser.open(path)


def _expand_glob(pattern: str) -> list[str]:
    """Expand a glob pattern into an alphabetical list of concrete paths.

    Uses plain :mod:`glob` on the local filesystem.  For non-glob paths the
    list is ``[pattern]`` if the file exists, ``[]`` otherwise.
    """
    import glob

    if not any(c in pattern for c in "*?["):
        return [pattern] if os.path.exists(pattern) else []
    return sorted(glob.glob(pattern))


def _collect_enum_members() -> dict:
    """Mirror :code:`getEnumMembers` from the VSCode extension - maps
    snake-case enum class name to ``{MEMBER: value}``.  Used by the webview
    to display enum labels instead of raw integer values.
    """
    import importlib
    import pkgutil

    import projspec.artifact
    import projspec.content
    import projspec.utils as pu

    # Ensure every content / artifact module is imported so
    # ``Enum.__subclasses__()`` is complete.
    for pkg in (projspec.content, projspec.artifact):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(m.name)
            except Exception:
                # A module that fails to import shouldn't stop enum collection.
                pass

    from projspec.utils import camel_to_snake

    out: dict[str, dict[str, int | str]] = {}
    seen: set[type] = set()

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():  # type: ignore[misc]
            if sub in seen:
                continue
            seen.add(sub)
            walk(sub)
            # ``sub`` inherits from ``projspec.utils.Enum`` (a subclass of
            # ``enum.Enum``) and so is iterable over its members.  Static type
            # checkers don't know this because we accept any ``type``.
            members = {m.name: m.value for m in sub}  # type: ignore[attr-defined]
            out[camel_to_snake(sub.__name__)] = members

    walk(pu.Enum)
    return out


def main() -> None:
    if not qt:
        print("No Qt bindings found - cannot continue")
        return
    app = QApplication(sys.argv)
    app.setApplicationName("projspec")
    icon_path = os.path.join(os.path.dirname(__file__), "../../../..", "logo.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    window = ProjspecWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
