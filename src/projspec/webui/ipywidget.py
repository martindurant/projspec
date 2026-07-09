"""Jupyter / marimo combined widget: Project Library + File Browser tabs.

This module owns the *host* side of the shared webui transport for the
Jupyter Notebook / JupyterLab / VSCode-notebook / Colab / marimo
environments.  It builds an :mod:`anywidget` ``AnyWidget`` that loads the
shared HTML, CSS and JS from :mod:`projspec.webui` and drives it from
Python with the same command vocabulary as the VSCode extension and the
Qt app.

Only :mod:`anywidget` is required — :mod:`ipywidgets` is **not** needed.

The widget now hosts two tabs:

* **Project Library** — the searchable library list + details panel.
* **File Browser** — an fsspec-backed directory browser with bookmarks,
  file info, dataset summaries, and "Add to Library" integration.

Filebrowser commands are tagged with ``_fb: True`` in the message envelope
so the single ``msg:custom`` channel can route them to the correct Python
handler without ambiguity.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from projspec.webui import (
    chrome_icons,
    get_filebrowser_css,
    get_filebrowser_js,
    get_panel_css,
    get_panel_js,
    get_tabs_css,
    get_tabs_js,
)

if TYPE_CHECKING:  # pragma: no cover
    from projspec.library import ProjectLibrary


# ---------------------------------------------------------------------------
#  ESM module text
# ---------------------------------------------------------------------------

_ESM_TEMPLATE = r"""
const PANEL_HTML_BODY = __PANEL_HTML_BODY__;
const FB_HTML_BODY = __FB_HTML_BODY__;
const PANEL_CSS = __PANEL_CSS__;
const PANEL_JS = __PANEL_JS__;
const FB_CSS = __FB_CSS__;
const FB_JS = __FB_JS__;
const TABS_CSS = __TABS_CSS__;
const TABS_JS = __TABS_JS__;
const CHROME_ICONS = __CHROME_ICONS__;
const INITIAL_TAB = __INITIAL_TAB__;

// CSS variable fallbacks so --vscode-* tokens resolve in notebook environments
// that don't provide them (JupyterLab, Colab, VS Code notebooks, marimo).
// Values match the Darcula / VS Code dark palette used across all other hosts.
const THEME_CSS = `
:root {
    --vscode-font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --vscode-editor-font-family: "JetBrains Mono", Consolas, Menlo, monospace;
    --vscode-foreground: #cccccc;
    --vscode-editor-background: #1e1e1e;
    --vscode-editorWidget-background: #252526;
    --vscode-editorWidget-foreground: #cccccc;
    --vscode-editorWidget-border: #454545;
    --vscode-panel-border: #3c3c3c;
    --vscode-focusBorder: #007acc;
    --vscode-descriptionForeground: #858585;
    --vscode-button-background: #0e639c;
    --vscode-button-foreground: #ffffff;
    --vscode-button-hoverBackground: #1177bb;
    --vscode-button-secondaryBackground: #3a3d41;
    --vscode-button-secondaryForeground: #cccccc;
    --vscode-input-background: #3c3c3c;
    --vscode-input-foreground: #cccccc;
    --vscode-input-border: #3c3c3c;
    --vscode-list-hoverBackground: #2a2d2e;
    --vscode-list-activeSelectionBackground: #094771;
    --vscode-list-activeSelectionForeground: #ffffff;
    --vscode-menu-background: #252526;
    --vscode-menu-foreground: #cccccc;
    --vscode-menu-border: #454545;
    --vscode-menu-selectionBackground: #094771;
    --vscode-menu-selectionForeground: #ffffff;
    --vscode-menu-separatorBackground: #454545;
    --vscode-toolbar-hoverBackground: #2a2d2e;
    --vscode-disabledForeground: #858585;
    --vscode-textLink-foreground: #3794ff;
    --vscode-textBlockQuote-background: rgba(255,255,255,0.06);
    --vscode-symbolIcon-propertyForeground: #cccccc;
    --vscode-symbolIcon-stringForeground: #ce9178;
    --vscode-symbolIcon-numberForeground: #b5cea8;
    --vscode-symbolIcon-keywordForeground: #569cd6;
    --vscode-symbolIcon-enumeratorMemberForeground: #4ec9b0;
    --vscode-editorInfo-foreground: #3794ff;
    --vscode-editorInfo-border: #3794ff;
    --vscode-editorWarning-foreground: #cca700;
    --vscode-editorWarning-border: #cca700;
    --vscode-errorForeground: #f48771;
    --vscode-badge-background: #4d4d4d;
    --vscode-badge-foreground: #ffffff;
    --vscode-editorGroupHeader-tabsBackground: #252526;
}
`;

export function render({ model, el }) {
    // ── Root container ──────────────────────────────────────────────────
    const root = document.createElement('div');
    root.className = 'projspec-root';
    root.style.width = '100%';
    root.style.height = '600px';

    const styleEl = document.createElement('style');
    styleEl.textContent = THEME_CSS + '\n' + TABS_CSS + '\n' + PANEL_CSS + '\n' + FB_CSS;
    root.appendChild(styleEl);

    // Build tab structure
    const tabBar = document.createElement('div');
    tabBar.id = 'tab-bar';
    tabBar.innerHTML =
        '<button class="tab-btn" id="tab-btn-library">Project Library</button>' +
        '<button class="tab-btn" id="tab-btn-filebrowser">\uD83D\uDCC1 File Browser</button>';
    root.appendChild(tabBar);

    const libPane = document.createElement('div');
    libPane.id = 'tab-library';
    libPane.className = 'tab-pane hidden';
    libPane.innerHTML = PANEL_HTML_BODY;
    root.appendChild(libPane);

    const fbPane = document.createElement('div');
    fbPane.id = 'tab-filebrowser';
    fbPane.className = 'tab-pane hidden';
    fbPane.innerHTML = FB_HTML_BODY;
    root.appendChild(fbPane);

    window.__PROJSPEC_CHROME_ICONS__ = CHROME_ICONS;

    // ── Library transport ──────────────────────────────────────────────
    let libDispatch = null;
    const libInbox = [];
    function onLibMessage(raw) {
        if (raw._fb) return;
        if (libDispatch) libDispatch(raw); else libInbox.push(raw);
    }
    model.on('msg:custom', onLibMessage);
    window.projspecRoot = libPane;
    window.projspecTransport = {
        send: (msg) => model.send(msg),
        onReady: (d) => { libDispatch = d; while (libInbox.length) libDispatch(libInbox.shift()); },
    };
    try { new Function(PANEL_JS).call(window); } catch (e) { console.error('panel.js:', e); }

    // ── Scan sub-panel transport ────────────────────────────────────────
    const scanRoot = fbPane.querySelector('#fb-scan-panel-root');
    let fbPanelDispatch = null;
    const fbPanelInbox = [];
    window.__fbPanelDeliver = function(msg) {
        if (fbPanelDispatch) fbPanelDispatch(msg); else fbPanelInbox.push(msg);
    };
    window.projspecRoot = scanRoot;
    window.projspecTransport = {
        send: function() {},
        onReady: function(d) {
            fbPanelDispatch = d;
            fbPanelInbox.forEach(m => d(m)); fbPanelInbox.length = 0;
            delete window.projspecRoot; delete window.projspecTransport;
        },
    };
    try { new Function(PANEL_JS).call(window); } catch (e) { console.error('scan panel.js:', e); }

    // ── Filebrowser transport ──────────────────────────────────────────
    let fbDispatch = null;
    const fbInbox = [];
    function onFbMessage(raw) {
        if (!raw._fb) return;
        const msg = Object.assign({}, raw); delete msg._fb;
        if (fbDispatch) fbDispatch(msg); else fbInbox.push(msg);
    }
    model.on('msg:custom', onFbMessage);
    window.projspecFbRoot = fbPane;
    window.projspecFbTransport = {
        send: (msg) => model.send(Object.assign({}, msg, {_fb: true})),
        onReady: (d) => { fbDispatch = d; while (fbInbox.length) fbDispatch(fbInbox.shift()); },
    };
    try { new Function(FB_JS).call(window); } catch (e) { console.error('filebrowser.js:', e); }

    // ── Tab coordination ───────────────────────────────────────────────
    // Append root to el NOW so document.getElementById works inside tabs.js
    el.appendChild(root);
    try { new Function(TABS_JS).call(window); } catch (e) { console.error('tabs.js:', e); }
    if (window.__projspecTabInit) window.__projspecTabInit(INITIAL_TAB);

    // Handle _switch_tab messages from Python (neither lib nor fb handler catches these)
    function onSwitchTab(raw) {
        if (raw._switch_tab && window.__projspecShowTab) {
            window.__projspecShowTab(raw._switch_tab);
        }
    }
    model.on('msg:custom', onSwitchTab);

    return () => {
        model.off('msg:custom', onLibMessage);
        model.off('msg:custom', onFbMessage);
        model.off('msg:custom', onSwitchTab);
        try { el.removeChild(root); } catch {}
    };
}

export default { render };
"""


def _build_esm(initial_tab: str = "library") -> str:
    """Assemble the combined ESM module with all resources embedded."""
    from projspec.webui import get_combined_html, _panel_body_html, get_filebrowser_html

    panel_body = _panel_body_html()
    fb_body = get_filebrowser_html(panel_body)

    return (
        _ESM_TEMPLATE.replace("__PANEL_HTML_BODY__", json.dumps(panel_body))
        .replace("__FB_HTML_BODY__", json.dumps(fb_body))
        .replace("__PANEL_CSS__", json.dumps(get_panel_css()))
        .replace("__PANEL_JS__", json.dumps(get_panel_js()))
        .replace("__FB_CSS__", json.dumps(get_filebrowser_css()))
        .replace("__FB_JS__", json.dumps(get_filebrowser_js()))
        .replace("__TABS_CSS__", json.dumps(get_tabs_css()))
        .replace("__TABS_JS__", json.dumps(get_tabs_js()))
        .replace("__CHROME_ICONS__", json.dumps(chrome_icons()))
        .replace("__INITIAL_TAB__", json.dumps(initial_tab))
    )


# ---------------------------------------------------------------------------
#  Widget class
# ---------------------------------------------------------------------------


def _build_widget(library: "ProjectLibrary"):
    """Construct the anywidget-backed DOMWidget for ``library``."""
    try:
        import anywidget
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The ipywidget representation of ProjectLibrary requires the "
            "'anywidget' package.  Install it with "
            "``pip install projspec[ipywidget]`` or "
            "``pip install anywidget``."
        ) from exc

    class ProjectLibraryWidget(anywidget.AnyWidget):
        """Combined Library + File Browser widget."""

        _esm = _build_esm()
        _css = ""

        def __init__(self, library_obj: "ProjectLibrary", **kwargs: Any):
            super().__init__(**kwargs)
            self._library = library_obj
            self._info_data: dict[str, Any] = {}
            self._enum_members: dict[str, Any] = {}
            self.on_msg(self._on_frontend_message)

        # --- Routing --------------------------------------------------------
        def _on_frontend_message(
            self, _widget: Any, content: Any, _buffers: Any
        ) -> None:
            if not isinstance(content, dict):
                return
            try:
                if content.get("_fb"):
                    # Strip the tag before dispatching
                    msg = {k: v for k, v in content.items() if k != "_fb"}
                    self._on_fb_message(msg)
                else:
                    self._on_lib_message(content)
            except Exception as exc:
                self._toast(f"{content.get('cmd')}: {exc!r}")

        # --- Library messages -----------------------------------------------
        def _on_lib_message(self, content: dict) -> None:
            cmd = content.get("cmd")
            if cmd == "ready":
                self._send_initial_data()
            elif cmd == "reload":
                self._reload()
            elif cmd == "add":
                self._offer_add()
            elif cmd == "addConfirmed":
                self._add_confirmed(
                    content.get("path", ""), content.get("storageOptions", "")
                )
            elif cmd == "configure":
                _open_config_file(self._toast)
            elif cmd == "rescan":
                self._rescan(content.get("url", ""))
            elif cmd == "createSpec":
                self._offer_create_spec(content.get("url", ""))
            elif cmd == "createSpecConfirmed":
                self._create_spec_confirmed(
                    content.get("url", ""), content.get("spec", "")
                )
            elif cmd == "removeFromLibrary":
                url = content.get("url", "")
                self._library.entries.pop(url, None)
                if self._library.auto_save:
                    self._library.save()
                self._send_initial_data()
            elif cmd == "make":
                self._make(
                    content.get("url", ""),
                    content.get("spec"),
                    content.get("artifactType", ""),
                    content.get("name"),
                )
            elif cmd == "openWith":
                tool = content.get("tool", "")
                url = content.get("url", "")
                if tool == "filebrowser":
                    # Switch to file browser tab and navigate there
                    self.send({"_switch_tab": "filebrowser"})
                    self.send(
                        {
                            "_fb": True,
                            "type": "browseResult",
                            "pushHistory": False,
                            "storageOptions": "",
                            **_fb_browse_data(url),
                        }
                    )
                else:
                    _open_with(tool, url, self._toast)
            elif cmd == "revealFile":
                _reveal_file(content.get("fn", ""), self._toast)
            elif cmd == "copyToLocal":
                self._toast("Copy to local: not implemented")

        # --- Filebrowser messages -------------------------------------------
        def _on_fb_message(self, content: dict) -> None:
            import os

            cmd = content.get("cmd")
            if cmd == "ready":
                self._fb_init()
            elif cmd == "browse":
                so = _parse_so(content.get("storageOptions"))
                data = _fb_browse_data(content["url"], so)
                self._fb_send(
                    {
                        "type": "browseResult",
                        "pushHistory": content.get("push", True),
                        "storageOptions": json.dumps(so) if so else "",
                        **data,
                    }
                )
            elif cmd == "inspect":
                self._fb_inspect(
                    content["url"], _parse_so(content.get("storageOptions"))
                )
            elif cmd == "scanDir":
                self._fb_scan_dir(
                    content["url"], _parse_so(content.get("storageOptions"))
                )
            elif cmd == "expandDir":
                so = _parse_so(content.get("storageOptions"))
                from projspec.filebrowser import browse

                data = browse(content["url"], storage_options=so)
                self._fb_send(
                    {"type": "expandResult", "parentUrl": content["url"], **data}
                )
            elif cmd == "openFile":
                self._fb_open_file(
                    content["url"], _parse_so(content.get("storageOptions"))
                )
            elif cmd == "writeFile":
                self._fb_write_file(
                    content["url"],
                    content["content"],
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "createFile":
                self._fb_create_file(
                    content["parentUrl"],
                    content["name"],
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "deleteEntry":
                self._fb_delete_entry(
                    content["url"],
                    content.get("isDir", False),
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "renameEntry":
                self._fb_rename_entry(
                    content["url"],
                    content["newName"],
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "mkdir":
                self._fb_mkdir(
                    content["parentUrl"],
                    content["name"],
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "addBookmark":
                self._fb_bookmark_add(
                    content["url"],
                    content.get("label"),
                    _parse_so(content.get("storageOptions")),
                )
            elif cmd == "removeBookmark":
                self._fb_bookmark_remove(content["url"])
            elif cmd == "addToLibrary":
                self._fb_add_to_library(
                    content["url"], _parse_so(content.get("storageOptions"))
                )
            elif cmd == "goToUrl":
                so = _parse_so(content.get("storageOptions"))
                from projspec.filebrowser import browse

                data = browse(content["url"], storage_options=so)
                self._fb_send(
                    {
                        "type": "browseResult",
                        "pushHistory": True,
                        "storageOptions": json.dumps(so) if so else "",
                        **data,
                    }
                )

        def _fb_send(self, msg: dict) -> None:
            """Send a message to the filebrowser tab."""
            self.send({**msg, "_fb": True})

        def _fb_init(self) -> None:
            import os
            from projspec.filebrowser import bookmarks_list, supported_protocols

            bms = bookmarks_list()
            protos = supported_protocols()
            lib_urls = list(self._library.entries.keys())
            self._fb_send(
                {
                    "type": "init",
                    "bookmarks": bms,
                    "protocols": protos,
                    "libraryUrls": lib_urls,
                }
            )
            # Navigate to home
            from projspec.filebrowser import browse

            data = browse(os.path.expanduser("~"))
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": "",
                    **data,
                }
            )

        def _fb_inspect(self, url: str, so=None) -> None:
            from projspec.filebrowser import inspect_as_project

            data = inspect_as_project(url, storage_options=so)
            self._fb_send({"type": "inspectResult", **data})
            self._fb_send(
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

        def _fb_scan_dir(self, url: str, so=None) -> None:
            from projspec.filebrowser import scan_directory

            data = scan_directory(url, storage_options=so)
            self._fb_send(
                {
                    "type": "projectScanned",
                    "info": self._info_data,
                    "enums": self._enum_members,
                    **data,
                }
            )

        def _fb_open_file(self, url: str, so=None) -> None:
            import os, tempfile

            local = _url_to_local(url)
            if os.path.exists(local):
                _open_with_default(local, self._toast)
                return
            from projspec.filebrowser import read_file

            result = read_file(url, storage_options=so)
            if result.get("error"):
                self._toast(f"Open file: {result['error']}")
                return
            ext = os.path.splitext(url)[1] or ".txt"
            tmp = tempfile.NamedTemporaryFile(
                "w", suffix=ext, delete=False, encoding="utf-8"
            )
            tmp.write(result.get("content", ""))
            tmp.close()
            _open_with_default(tmp.name, self._toast)

        def _fb_write_file(self, url: str, content: str, so=None) -> None:
            from projspec.filebrowser import write_file, browse

            result = write_file(url, content, storage_options=so)
            if result.get("error"):
                self._toast(f"Write: {result['error']}")
                return
            parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
            data = browse(parent, storage_options=so)
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": json.dumps(so) if so else "",
                    **data,
                }
            )

        def _fb_create_file(self, parent_url: str, name: str, so=None) -> None:
            from projspec.filebrowser import write_file, browse

            new_url = parent_url.rstrip("/") + "/" + name
            result = write_file(new_url, "", storage_options=so)
            if result.get("error"):
                self._toast(f"Create: {result['error']}")
                return
            data = browse(parent_url, storage_options=so)
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": json.dumps(so) if so else "",
                    **data,
                }
            )

        def _fb_delete_entry(self, url: str, is_dir: bool, so=None) -> None:
            from projspec.filebrowser import delete, browse

            result = delete(url, storage_options=so, recursive=is_dir)
            if result.get("error"):
                self._toast(f"Delete: {result['error']}")
                return
            parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
            data = browse(parent, storage_options=so)
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": json.dumps(so) if so else "",
                    **data,
                }
            )

        def _fb_rename_entry(self, url: str, new_name: str, so=None) -> None:
            from projspec.filebrowser import move, browse

            parent = url.rstrip("/").rsplit("/", 1)[0] or "/"
            dst = parent.rstrip("/") + "/" + new_name
            result = move(url, dst, storage_options=so)
            if result.get("error"):
                self._toast(f"Rename: {result['error']}")
                return
            data = browse(parent, storage_options=so)
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": json.dumps(so) if so else "",
                    **data,
                }
            )

        def _fb_mkdir(self, parent_url: str, name: str, so=None) -> None:
            from projspec.filebrowser import mkdir, browse

            new_url = parent_url.rstrip("/") + "/" + name
            result = mkdir(new_url, storage_options=so)
            if result.get("error"):
                self._toast(f"New folder: {result['error']}")
                return
            data = browse(parent_url, storage_options=so)
            self._fb_send(
                {
                    "type": "browseResult",
                    "pushHistory": False,
                    "storageOptions": json.dumps(so) if so else "",
                    **data,
                }
            )

        def _fb_bookmark_add(self, url: str, label=None, so=None) -> None:
            from projspec.filebrowser import bookmark_add

            bms = bookmark_add(url, label=label or "", storage_options=so)
            self._fb_send({"type": "bookmarksUpdated", "bookmarks": bms})

        def _fb_bookmark_remove(self, url: str) -> None:
            from projspec.filebrowser import bookmark_remove

            bms = bookmark_remove(url)
            self._fb_send({"type": "bookmarksUpdated", "bookmarks": bms})

        def _fb_add_to_library(self, url: str, so=None) -> None:
            from projspec.filebrowser import add_to_projspec_library

            result = add_to_projspec_library(url, storage_options=so)
            if result.get("error"):
                self._toast(f"Add to library: {result['error']}")
                return
            self._toast(f"Added to library: {url}")
            # Refresh filebrowser library badges
            self._fb_send(
                {
                    "type": "libraryUrlsUpdated",
                    "libraryUrls": list(self._library.entries.keys()),
                }
            )
            # Reload library tab and select the new entry
            self._send_initial_data(select_url=url)
            # Tell the frontend to switch to the library tab
            self.send({"_switch_tab": "library"})

        # --- Outbound Python -> library frontend ----------------------------
        def _send_initial_data(self, select_url: str | None = None) -> None:
            from projspec.utils import class_infos

            if not self._info_data:
                self._info_data = class_infos()
                self._enum_members = _collect_enum_members()
            lib_dict = {
                url: proj.to_dict(compact=False)
                for url, proj in self._library.entries.items()
            }
            msg: dict = {
                "type": "data",
                "info": self._info_data,
                "enums": self._enum_members,
                "library": lib_dict,
            }
            if select_url:
                msg["selectUrl"] = select_url
            self.send(msg)

        def _reload(self) -> None:
            import os

            path = self._library.path
            if path and os.path.isfile(path):
                self._library.load()
            self._send_initial_data()

        def _set_busy(self, busy: bool) -> None:
            self.send({"type": "loading", "loading": bool(busy)})

        def _toast(self, message: str) -> None:
            print(f"[projspec] {message}")

        # --- Action helpers (library) --------------------------------------
        def _offer_add(self) -> None:
            self.send({"type": "openAddModal"})

        def _add_confirmed(self, path: str, storage_options: str = "") -> None:
            from projspec.utils import scan_glob

            path = (path or "").strip()
            if not path:
                return
            self._set_busy(True)
            try:
                found = False
                for proj in scan_glob(
                    path,
                    storage_options=storage_options,
                    walk=True,
                    add_to_library=False,
                ):
                    found = True
                    key = proj.fs.unstrip_protocol(proj.url)
                    self._library.add_entry(key, proj)
                    for child in (proj.children or {}).values():
                        if child.specs:
                            child_key = child.fs.unstrip_protocol(child.url)
                            self._library.add_entry(child_key, child)
                if not found:
                    self._toast(f"No directories found: {path}")
                    return
                self._send_initial_data()
            except Exception as exc:
                self._toast(f"Scan failed: {exc}")
            finally:
                self._set_busy(False)

        def _resolve_entry_path(self, url: str) -> str | None:
            if url and "://" in url:
                return url
            proj = self._library.entries.get(url)
            if proj is not None and getattr(proj, "path", None):
                try:
                    return proj.fs.unstrip_protocol(proj.url)
                except Exception:
                    return proj.path
            return _url_to_local(url) if url else None

        def _entry_storage_options(self, url: str) -> dict:
            proj = self._library.entries.get(url)
            return dict(getattr(proj, "storage_options", None) or {})

        def _rescan(self, url: str) -> None:
            import projspec

            if not url:
                return
            path = self._resolve_entry_path(url)
            if not path:
                self._toast(f"Cannot resolve path for {url}")
                return
            self._set_busy(True)
            try:
                proj = projspec.Project(
                    path, walk=False, storage_options=self._entry_storage_options(url)
                )
                self._library.entries[url] = proj
                if self._library.auto_save:
                    self._library.save()
                self._send_initial_data()
            finally:
                self._set_busy(False)

        def _offer_create_spec(self, url: str) -> None:
            proj = self._library.entries.get(url)
            existing = set((proj.specs if proj is not None else {}) or {})
            creatable = sorted(
                name
                for name, entry in (self._info_data.get("specs") or {}).items()
                if entry.get("create") and name not in existing
            )
            if not creatable:
                self._toast("No spec types available to create.")
                return
            self.send({"type": "openCreateSpecModal", "url": url, "specs": creatable})

        def _create_spec_confirmed(self, url: str, spec: str) -> None:
            if not spec or not url:
                return
            import projspec

            path = self._resolve_entry_path(url)
            if not path:
                self._toast(f"Cannot resolve path for {url}")
                return
            self._set_busy(True)
            try:
                so = self._entry_storage_options(url)
                proj = projspec.Project(path, walk=False, storage_options=so)
                proj.create(spec)
                fresh = projspec.Project(path, walk=False, storage_options=so)
                self._library.entries[url] = fresh
                if self._library.auto_save:
                    self._library.save()
                self._send_initial_data()
            finally:
                self._set_busy(False)

        def _make(
            self, url: str, spec: str | None, artifact_type: str, name: str | None
        ) -> None:
            import os

            qname = ".".join(p for p in (spec, artifact_type, name) if p)
            proj = self._library.entries.get(url)
            if proj is None:
                self._toast(f"Project not found: {url}")
                return
            if proj.path and not os.path.isabs(proj.path):
                fallback = _url_to_local(url)
                if os.path.isabs(fallback):
                    proj.path = fallback
                else:
                    proj.path = os.path.abspath(proj.path)
                proj.url = proj.path
            self._set_busy(True)
            try:
                art = proj.make(qname)
                self._toast(f"make {qname}: {art}")
            finally:
                self._set_busy(False)

    return ProjectLibraryWidget(library)


# ---------------------------------------------------------------------------
#  Subprocess-backed helpers shared by the Python widget host.
# ---------------------------------------------------------------------------
#
# These mirror the equivalent code in ``qtapp/main.py``.  Every action that
# *has* a meaningful effect in a notebook kernel is implemented here; the
# few that cannot (picking a folder with a native dialog, opening a modal
# dialog, etc.) are handled via additional frontend messages above.


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


def _fb_browse_data(url: str, so: dict | None = None) -> dict:
    """Return browse() result dict for a URL."""
    from projspec.filebrowser import browse

    return browse(url, storage_options=so)


def _url_to_local(url: str) -> str:
    """Strip a leading ``file://`` from *url* so the result is a plain path."""
    if url.startswith("file://"):
        return url[len("file://") :]
    return url


def _spawn_detached(cmd: list[str], toast) -> None:
    """Launch an external tool without blocking the kernel.

    Errors are reported via the supplied *toast* callback - we don't want
    a failed spawn to propagate as an exception into the cell output.
    """
    import subprocess

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        toast(f"Command not found: {cmd[0]}")
    except Exception as exc:
        toast(f"Failed to run {cmd[0]}: {exc!r}")


def _open_with_default(path: str, toast) -> None:
    """Open *path* with the OS default handler (equivalent of double-click)."""
    import os
    import subprocess
    import sys

    try:
        if sys.platform == "darwin":
            subprocess.call(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.call(["xdg-open", path])
    except Exception as exc:
        toast(f"Could not open {path}: {exc!r}")


def _open_with(tool: str, url: str, toast) -> None:
    """Dispatch the *Open with …* kebab-menu choices."""
    local = _url_to_local(url)
    if tool == "vscode":
        _spawn_detached(["code", local], toast)
    elif tool == "filebrowser":
        _open_with_default(local, toast)
    elif tool == "pycharm":
        _spawn_detached(["pycharm", local, "nosplash", "dontReopenProjects"], toast)
    elif tool == "jupyter":
        _spawn_detached(["jupyter", "lab", local], toast)
    else:
        toast(f"Unknown openWith tool: {tool!r}")


def _reveal_file(fn: str, toast) -> None:
    """Reveal *fn* (a file path or glob) in the OS file manager.

    When *fn* contains a wildcard we open the first match; if nothing
    matches we report it to the user.  Remote URLs are ignored.
    """
    import glob
    import os

    if not fn:
        return
    local = fn[len("file://") :] if fn.startswith("file://") else fn
    if "://" in local and not local.startswith("/"):
        toast(f"Remote file cannot be revealed: {fn}")
        return
    if any(c in local for c in "*?["):
        matches = sorted(glob.glob(local))
    else:
        matches = [local] if os.path.exists(local) else []
    if not matches:
        toast(f"No files match: {fn}")
        return
    target = matches[0]
    _open_with_default(os.path.dirname(target) or target, toast)


def _open_config_file(toast) -> None:
    """Open the projspec JSON config in the OS default editor.

    Creates the directory and a minimal default config file if they do
    not exist yet, mirroring the ``qtapp`` and VSCode extension
    ``Configure`` actions.
    """
    import json
    import os
    from pathlib import Path

    conf_dir = Path(
        os.environ.get("PROJSPEC_CONFIG_DIR") or (Path.home() / ".config" / "projspec")
    )
    conf_file = conf_dir / "projspec.json"
    if not conf_file.exists():
        try:
            conf_dir.mkdir(parents=True, exist_ok=True)
            conf_file.write_text(
                json.dumps(
                    {
                        "scan_types": [
                            ".py",
                            ".yaml",
                            ".yml",
                            ".toml",
                            ".json",
                            ".md",
                        ],
                        "scan_max_files": 100,
                        "scan_max_size": 5000,
                        "remote_artifact_status": False,
                        "capture_artifact_output": True,
                        "preferred_install_methods": ["conda", "pip"],
                    },
                    indent=4,
                )
            )
        except OSError as exc:
            toast(f"Could not create {conf_file}: {exc!r}")
            return
    _open_with_default(str(conf_file), toast)
    toast(
        "ProjSpec configuration — see the docs for all available fields: "
        "https://projspec.readthedocs.io/en/latest/config.html"
    )


def _collect_enum_members() -> dict[str, dict[str, int | str]]:
    """Mirror ``qtapp.main._collect_enum_members``.

    Walks every :class:`projspec.utils.Enum` subclass and returns
    ``{snake_case_name: {MEMBER: value}}``.  Used by the shared panel JS
    to render enum-valued fields with their member labels instead of
    their raw integer values.
    """
    import importlib
    import pkgutil

    import projspec.artifact
    import projspec.content
    import projspec.utils as pu

    for pkg in (projspec.content, projspec.artifact):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(m.name)
            except Exception:
                # Optional deps may keep some modules from loading; that's
                # fine - we just skip them for the enum map.
                pass

    from projspec.utils import camel_to_snake

    out: dict[str, dict[str, int | str]] = {}
    seen: set[type] = set()

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            walk(sub)
            members = {m.name: m.value for m in sub}  # type: ignore[attr-defined]
            out[camel_to_snake(sub.__name__)] = members

    walk(pu.Enum)
    return out


def make_widget(library: "ProjectLibrary"):
    """Return an anywidget-backed DOMWidget for ``library``.

    Public entry point used by :meth:`ProjectLibrary.widget`.
    """
    return _build_widget(library)


__all__ = ["make_widget"]
