"""Shared web UI resources for the projspec combined panel.

All GUI hosts (VSCode, Qt, PyCharm, ipywidget) render a combined two-tab
panel: **Project Library** on the left tab (library list + details) and
**File Browser** on the right tab (fsspec directory browser).  This package
owns the canonical HTML / CSS / JS for both tabs.

Public helpers
--------------

:func:`get_combined_html`
    Return a complete HTML document with both tabs.  This is the preferred
    entry point for all GUI hosts.

:func:`get_panel_html`
    Backwards-compatible: return only the library panel document (no tabs).

:func:`get_panel_css` / :func:`get_panel_js`
    Return the library panel CSS and JS as plain strings.

:func:`get_filebrowser_css` / :func:`get_filebrowser_js`
    Return the file browser CSS and JS as plain strings.

:func:`get_tabs_css` / :func:`get_tabs_js`
    Return the tab-bar CSS and JS as plain strings.

:func:`chrome_icons`
    Return the chrome emoji map (toolbar icons, kebab glyph, etc.).

Transport contract
------------------

Library panel
~~~~~~~~~~~~~
The host must define ``window.projspecTransport`` *before* ``panel.js`` runs::

    {
        send:    function(msg) { ... },       // JS -> host (cmd vocabulary)
        onReady: function(dispatch) { ... },  // host calls dispatch(msg) to push data
    }

File browser panel
~~~~~~~~~~~~~~~~~~
The host must define ``window.projspecFbTransport`` *before* ``filebrowser.js``
runs::

    {
        send:    function(msg) { ... },       // JS -> host (fb cmd vocabulary)
        onReady: function(dispatch) { ... },  // host calls dispatch(msg) to push data
    }

Optionally set ``window.projspecFbRoot`` to scope the file browser DOM queries
to a subtree (needed when both panels share a document).

The embedded scan sub-panel (shown inside the file browser when a directory is
selected) is a second instance of the library panel; its transport is set
separately using ``window.projspecTransport`` (or the equivalent) *before* the
second ``panel.js`` invocation, with ``window.projspecRoot`` pointed at
``#fb-scan-panel-root``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

__all__ = [
    "chrome_icons",
    "get_combined_html",
    "get_filebrowser_css",
    "get_filebrowser_html",
    "get_filebrowser_js",
    "get_panel_css",
    "get_panel_html",
    "get_panel_js",
    "get_tabs_css",
    "get_tabs_js",
    "resource_path",
]

_HERE = Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Return the absolute path to a bundled webui resource."""
    return _HERE / name


@lru_cache(maxsize=1)
def _chrome_raw() -> str:
    return (_HERE / "chrome.json").read_text(encoding="utf-8")


def chrome_icons() -> dict[str, str]:
    """Return the chrome emoji map (toolbar icons, kebab glyph, etc.)."""
    return json.loads(_chrome_raw())


@lru_cache(maxsize=1)
def get_panel_css() -> str:
    """Return the shared library-panel stylesheet as a plain string."""
    return (_HERE / "panel.css").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_panel_js() -> str:
    """Return the shared library-panel JavaScript as a plain string."""
    return (_HERE / "panel.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_filebrowser_css() -> str:
    """Return the file-browser stylesheet as a plain string."""
    return (_HERE / "filebrowser.css").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_filebrowser_js() -> str:
    """Return the file-browser JavaScript as a plain string."""
    return (_HERE / "filebrowser.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_tabs_css() -> str:
    """Return the tab-bar / combined-layout stylesheet as a plain string."""
    return (_HERE / "tabs.css").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_tabs_js() -> str:
    """Return the tab-switching coordination JavaScript as a plain string."""
    return (_HERE / "tabs.js").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _html_template() -> str:
    return (_HERE / "panel.html").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _filebrowser_html_template() -> str:
    return (_HERE / "filebrowser.html").read_text(encoding="utf-8")


def _resolve_panel_body(html_template: str) -> str:
    """Resolve icon markers in a panel HTML template and return the body."""
    html = html_template
    for key, glyph in chrome_icons().items():
        html = html.replace(f"<!--ICON:{key}-->", glyph)
    html = html.replace("/*__CSS__*/", get_panel_css())
    html = html.replace("<script>/*__JS__*/</script>", "")
    html = html.replace("<!--BOOTSTRAP-->", "")
    html = html.replace("<!--EXTRA_HEAD-->", "")
    body_start = html.index("<body>") + len("<body>")
    body_end = html.rindex("</body>")
    return html[body_start:body_end].strip()


def _panel_body_html() -> str:
    """Return the resolved body HTML of the library panel (no scripts)."""
    return _resolve_panel_body(_html_template())


def get_panel_html(
    *,
    extra_head: str = "",
    bootstrap_js: str = "",
    embedded: bool = False,
) -> str:
    """Return a self-contained HTML document hosting only the library panel.

    Backwards-compatible with all existing callers.  New callers should
    prefer :func:`get_combined_html`.
    """
    html = _html_template()
    for key, glyph in chrome_icons().items():
        html = html.replace(f"<!--ICON:{key}-->", glyph)
    html = html.replace("<!--EXTRA_HEAD-->", extra_head)
    html = html.replace("<!--BOOTSTRAP-->", bootstrap_js)
    html = html.replace("/*__CSS__*/", get_panel_css())
    html = html.replace("/*__JS__*/", get_panel_js())
    if embedded:
        html = html.replace("<body>", '<body class="embedded">', 1)
    return html


def get_filebrowser_html(panel_body_html: str = "") -> str:
    """Return the resolved file-browser HTML body (no enclosing document).

    ``panel_body_html`` is injected into the embedded scan panel placeholder
    ``<!--FB_SCAN_PANEL_BODY-->``.  Pass :func:`_panel_body_html()` (or leave
    empty to omit the scan sub-panel).
    """
    return _filebrowser_html_template().replace(
        "<!--FB_SCAN_PANEL_BODY-->", panel_body_html
    )


def get_combined_html(
    *,
    extra_head: str = "",
    lib_bootstrap_js: str = "",
    fb_bootstrap_js: str = "",
    scan_panel_bootstrap_js: str = "",
    initial_tab: str = "library",
    embedded: bool = False,
) -> str:
    """Return a complete HTML document with both tabs (Library + File Browser).

    Parameters
    ----------
    extra_head:
        HTML injected into ``<head>``, e.g. Qt's ``qwebchannel.js`` tag.
    lib_bootstrap_js:
        Script block run before ``panel.js`` to install
        ``window.projspecTransport`` for the library tab.
    fb_bootstrap_js:
        Script block run before ``filebrowser.js`` to install
        ``window.projspecFbTransport`` (and ``window.projspecFbRoot``,
        ``window.projspecFbPanelBootstrap`` if needed).
    scan_panel_bootstrap_js:
        Script block run before the second ``panel.js`` invocation that
        powers the embedded directory-scan sub-panel inside the file browser.
        Must install ``window.projspecTransport`` scoped to
        ``#fb-scan-panel-root`` and ``window.projspecRoot``.
    initial_tab:
        Which tab is visible on load: ``"library"`` (default) or
        ``"filebrowser"``.
    embedded:
        If ``True``, adds ``class="embedded"`` to ``<body>`` (height clamped
        to ``--projspec-panel-height``).
    """
    panel_body = _panel_body_html()
    fb_body = get_filebrowser_html(panel_body)

    lib_cls = "tab-pane active" if initial_tab == "library" else "tab-pane hidden"
    fb_cls = "tab-pane active" if initial_tab == "filebrowser" else "tab-pane hidden"
    lib_btn_cls = "tab-btn active" if initial_tab == "library" else "tab-btn"
    fb_btn_cls = "tab-btn active" if initial_tab == "filebrowser" else "tab-btn"

    all_css = "\n".join([get_tabs_css(), get_panel_css(), get_filebrowser_css()])

    body_class = ' class="embedded"' if embedded else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>projspec</title>
{extra_head}
<style>
{all_css}
</style>
</head>
<body{body_class}>
<div id="tab-bar">
  <button class="{lib_btn_cls}" id="tab-btn-library">Project Library</button>
  <button class="{fb_btn_cls}"  id="tab-btn-filebrowser">&#128193; File Browser</button>
</div>
<div id="tab-library" class="{lib_cls}">
{panel_body}
</div>
<div id="tab-filebrowser" class="{fb_cls}">
{fb_body}
</div>
{lib_bootstrap_js}
<script>{get_panel_js()}</script>
{scan_panel_bootstrap_js}
<script>{get_panel_js()}</script>
<script>{get_tabs_js()}</script>
{fb_bootstrap_js}
<script>{get_filebrowser_js()}</script>
<script>window.__projspecTabInit && window.__projspecTabInit({json.dumps(initial_tab)});</script>
</body>
</html>"""
