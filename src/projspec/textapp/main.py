"""Textual TUI for projspec - terminal equivalent of the VSCode extension.

This app mirrors the layout and interactions of the VSCode extension
(``vsextension/ACTIONS.md``) and the Qt app (``qtapp/``):

    ┌──────────────────────────┬────────────────────────────┐
    │   Library    (left)      │     Details   (right)      │
    │  Add Reload Configure    │                            │
    │  ┌ search ┐              │  <title>                   │
    │  project widgets...      │  doc / link                │
    │                          │  content/artifact widgets  │
    └──────────────────────────┴────────────────────────────┘

Each project widget shows basename (bold), URL, optional storage_options,
and a row of chips: ``Contents <N>``, ``Artifacts <N>``, one per registered
spec.  Each project has a kebab button opening a menu with Open-with /
Rescan / Create spec / Remove from library actions.

All project scanning, creation and ``make`` calls are performed in-process
against the projspec Python API rather than via subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any
import warnings

import projspec
from projspec.library import ProjectLibrary
from projspec.utils import class_infos

try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widget import Widget
    from textual.widgets import (
        Button,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        Static,
        TabbedContent,
        TabPane,
    )
except ImportError as e:
    warnings.warn("Textual is required for the TUI")
    App = None


# ---------------------------------------------------------------------------
#  Shared state
# ---------------------------------------------------------------------------

library = ProjectLibrary()


DEFAULT_CONFIG = {
    "scan_types": [".py", ".yaml", ".yml", ".toml", ".json", ".md"],
    "scan_max_files": 100,
    "scan_max_size": 5000,
    "remote_artifact_status": False,
    "capture_artifact_output": True,
    "preferred_install_methods": ["conda", "pip"],
}


# ---------------------------------------------------------------------------
#  Rendering helpers (YAML-style, enum labels, etc.)
# ---------------------------------------------------------------------------

# Colour palette for spec / content / artifact labels.  Kept close to the
# VSCode / Qt theme so the three UIs feel like the same product.
ROLE_COLOUR = {
    "project": "bold #dcb67a",
    "spec": "#dcdcaa",
    "content": "#4ec9b0",
    "artifact": "#ce9178",
    "field": "#cccccc",
    "value": "italic #9cdcfe",
    "enum": "bold #4ec9b0",
    "muted": "#858585",
}


# Emoji used for UI chrome (toolbar, kebab, action buttons) and as
# fallbacks when projspec does not supply an icon.  Keeping the same
# characters as ``qtapp/emoji.py`` so the two UIs look like siblings.
CHROME_ICONS = {
    "add": "➕",
    "reload": "🔄",
    "configure": "⚙️",
    "search": "🔍",
    "clear": "✖️",
    "kebab": "⋮",
    "play": "▶️",
    "info": "ℹ️",
    "reveal": "➡️",
}
DEFAULT_ICON = {"spec": "🧩", "content": "📄", "artifact": "📦"}


def _project_icon(kind: str, name: str, infos: dict) -> str:
    """Resolve the emoji to display next to a spec / content / artifact.

    ``infos`` is the cached result of ``projspec.utils.class_infos()``.
    ``projspec`` stores an emoji directly in each class's ``icon``
    attribute, so this is mostly a dict lookup with a category fallback.
    """
    cat = {"spec": "specs", "content": "content", "artifact": "artifact"}.get(kind)
    if cat:
        entry = (infos.get(cat) or {}).get(name) or {}
        if isinstance(entry, dict):
            icon = entry.get("icon")
            if isinstance(icon, str) and icon:
                return icon
    return DEFAULT_ICON.get(kind, "❔")


# Approximate width of the library pane in a typical terminal, minus the
# project card's border / padding.  Used by :func:`_wrap_chips` to split
# a flat chip list into wrapped rows.  Slightly conservative so wide
# emojis (which count as two cells in most monospace fonts) still fit.
_CHIPS_ROW_WIDTH = 36


def _wrap_chips(chips: list, row_width: int) -> list[list]:
    """Split ``chips`` into rows each no wider than ``row_width`` cells.

    Each chip is ``(label, url, kind, spec_name)``; the decision is based
    solely on ``label`` length plus 3 cells for padding/margin.  A chip
    that is itself wider than ``row_width`` still gets its own row - we
    never drop chips on the floor.
    """
    rows: list[list] = []
    current: list = []
    used = 0
    for chip in chips:
        label = chip[0]
        # Rough cell-width estimate: emoji presentation-qualified chars
        # render as 2 cells in most terminals; everything else as 1.
        width = sum(2 if ord(c) >= 0x1F000 else 1 for c in label) + 3
        if current and used + width > row_width:
            rows.append(current)
            current = []
            used = 0
        current.append(chip)
        used += width
    if current:
        rows.append(current)
    return rows


def _role(text: str, role: str) -> str:
    return f"[{ROLE_COLOUR.get(role, '#cccccc')}]{text}[/]"


def _basename(url: str) -> str:
    return (url.rstrip("/").rsplit("/", 1)[-1]) or url


def _fmt_age(ts: float) -> str:
    from projspec.proj.base import _humanize_age

    return _humanize_age(ts)


def _is_enum(v: Any) -> bool:
    """True for a serialised enum: ``{klass:['enum', name], value: ...}``."""
    return (
        isinstance(v, dict)
        and isinstance(v.get("klass"), list)
        and v["klass"][0] == "enum"
        and "value" in v
    )


def _enum_label(value: dict, enums: dict[str, dict[str, Any]]) -> str:
    """Return the member name for an enum dict, falling back to the raw value."""
    name = value["klass"][1]
    raw = value["value"]
    members = enums.get(name) or {}
    for mname, mval in members.items():
        if mval == raw:
            return mname
    return str(raw)


def _has_klass(v: Any) -> bool:
    """Recursively search for a ``klass`` key - mirrors the JS helper."""
    if not isinstance(v, (dict, list)):
        return False
    if isinstance(v, dict):
        if "klass" in v:
            return True
        return any(_has_klass(x) for x in v.values())
    return any(_has_klass(x) for x in v)


def _strip_klass(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k != "klass"}
    return obj


def _yaml_lines(
    data: Any, enums: dict[str, dict[str, Any]], indent: int = 0
) -> list[str]:
    """Render ``data`` as Rich-markup YAML lines.

    Lists drop indices; strings are unquoted; nested objects indent by two
    spaces.  Enum dicts are inlined with their member-name label.
    """
    pad = "  " * indent
    if _is_enum(data):
        return [f"{pad}{_role(_enum_label(data, enums), 'enum')}"]
    if isinstance(data, list):
        if not data:
            return [f"{pad}{_role('[]', 'muted')}"]
        out: list[str] = []
        for item in data:
            if _is_enum(item):
                out.append(f"{pad}- {_role(_enum_label(item, enums), 'enum')}")
            elif isinstance(item, (dict, list)):
                out.append(f"{pad}-")
                out.extend(_yaml_lines(item, enums, indent + 1))
            else:
                out.append(f"{pad}- {_fmt_primitive(item)}")
        return out
    if isinstance(data, dict):
        if not data:
            return [f"{pad}{_role('{}', 'muted')}"]
        out = []
        for k, v in data.items():
            # The web UIs embed these as live HTML / an image; a TUI can't, so
            # show a short placeholder rather than dumping the huge raw string.
            if k in ("html_repr", "thumbnail") and isinstance(v, str):
                note = "HTML preview" if k == "html_repr" else "image thumbnail"
                out.append(
                    f"{pad}{_role(str(k), 'field')}: "
                    f"{_role(f'<{note} available in graphical UI>', 'muted')}"
                )
            elif _is_enum(v):
                out.append(
                    f"{pad}{_role(str(k), 'field')}: "
                    f"{_role(_enum_label(v, enums), 'enum')}"
                )
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{_role(str(k), 'field')}:")
                out.extend(_yaml_lines(v, enums, indent + 1))
            else:
                out.append(f"{pad}{_role(str(k), 'field')}: {_fmt_primitive(v)}")
        return out
    return [f"{pad}{_fmt_primitive(data)}"]


def _fmt_primitive(v: Any) -> str:
    if v is None:
        return _role("null", "muted")
    if isinstance(v, bool):
        return _role("true" if v else "false", "muted")
    return _role(str(v), "value")


def _collect_enum_members() -> dict[str, dict[str, Any]]:
    """Mirror ``getEnumMembers()`` from the VSCode extension: maps
    snake-case enum class name to ``{MEMBER: value}``.
    """
    import importlib
    import pkgutil

    import projspec.artifact
    import projspec.content
    import projspec.utils as pu
    from projspec.utils import camel_to_snake

    for pkg in (projspec.content, projspec.artifact):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(m.name)
            except Exception:
                pass

    out: dict[str, dict[str, Any]] = {}
    seen: set[type] = set()

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():  # type: ignore[misc]
            if sub in seen:
                continue
            seen.add(sub)
            walk(sub)
            out[camel_to_snake(sub.__name__)] = {m.name: m.value for m in sub}  # type: ignore[attr-defined]

    walk(pu.Enum)
    return out


# ---------------------------------------------------------------------------
#  Modal screens
# ---------------------------------------------------------------------------


class KebabMenuModal(ModalScreen[str | None]):
    """Popup menu triggered by a project's kebab button.

    Returns the string key of the chosen action (``openVSCode``, ``rescan``, …)
    or ``None`` if dismissed.
    """

    DEFAULT_CSS = """
    KebabMenuModal { align: center middle; }
    #menu-box {
        background: #252526; border: solid #454545;
        padding: 0; width: 44; height: auto;
    }
    #menu-box ListView { background: #252526; }
    #menu-box ListItem { padding: 0 2; }
    #menu-box ListItem.disabled { color: #6a6a6a; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, is_local: bool) -> None:
        super().__init__()
        self._is_local = is_local

    def compose(self) -> ComposeResult:
        with Vertical(id="menu-box"):
            lv = ListView(id="menu-list")
            yield lv

    def on_mount(self) -> None:
        lv = self.query_one("#menu-list", ListView)
        if self._is_local:
            items = [
                ("openVSCode", "Open with VSCode"),
                ("openFilebrowser", "Show in file browser"),
                ("openPyCharm", "Open with PyCharm"),
                ("openJupyter", "Open with jupyter"),
                ("_sep", ""),
                ("rescan", "Rescan"),
                ("createSpec", "Create spec"),
                ("remove", "Remove from library"),
            ]
        else:
            items = [
                ("copyToLocal", "Copy to local  (not implemented)"),
                ("rescan", "Rescan"),
                ("remove", "Remove from library"),
            ]
        for key, label in items:
            if key == "_sep":
                item = ListItem(Label("─" * 40, classes="sep"))
                item.disabled = True
            else:
                item = ListItem(Label(label))
                item.data = key  # type: ignore[attr-defined]
                if key == "copyToLocal":
                    item.add_class("disabled")
                    item.disabled = True
            lv.append(item)
        lv.focus()

    @on(ListView.Selected, "#menu-list")
    def _on_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "data", None)
        self.dismiss(key)


class CreateSpecModal(ModalScreen[str | None]):
    """Modal with a filter input + suggestion list, returns the chosen spec."""

    DEFAULT_CSS = """
    CreateSpecModal { align: center middle; }
    #box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 60; height: auto;
    }
    #box Label { margin-bottom: 1; color: #dcb67a; text-style: bold; }
    #hint { color: #858585; text-style: none; margin-top: 0; margin-bottom: 1; }
    #suggestions { height: auto; max-height: 10; border: solid #3c3c3c; }
    #btn-row { margin-top: 1; height: 3; }
    #btn-row Button { margin-right: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, specs: list[str]) -> None:
        super().__init__()
        self._specs = list(specs)
        self._filtered: list[str] = list(specs)

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Create spec")
            yield Static("Start typing to filter. Enter accepts.", id="hint")
            yield Input(placeholder="Spec type...", id="type-input")
            yield ListView(id="suggestions")
            with Horizontal(id="btn-row"):
                yield Button("Create", variant="primary", id="btn-ok")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one("#type-input", Input).focus()

    def _refresh(self, term: str) -> None:
        lv = self.query_one("#suggestions", ListView)
        lv.clear()
        term = term.strip().lower()
        self._filtered = (
            [s for s in self._specs if term in s.lower()] if term else list(self._specs)
        )
        for name in self._filtered[:30]:
            lv.append(ListItem(Label(name)))

    @on(Input.Changed, "#type-input")
    def _on_input(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    @on(Input.Submitted, "#type-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        self._accept()

    @on(ListView.Selected, "#suggestions")
    def _on_pick(self, event: ListView.Selected) -> None:
        lbl = event.item.query_one(Label)
        pick = str(lbl.render()).strip()
        self.query_one("#type-input", Input).value = pick
        self._accept()

    @on(Button.Pressed, "#btn-ok")
    def _on_ok(self) -> None:
        self._accept()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def _accept(self) -> None:
        value = self.query_one("#type-input", Input).value.strip()
        if value not in self._specs and len(self._filtered) == 1:
            value = self._filtered[0]
        if value in self._specs:
            self.dismiss(value)


class AddPathModal(ModalScreen[tuple[str, str] | None]):
    """Ask the user for a directory (or URL / glob pattern) to add to the library.

    Returns a ``(path, storage_options)`` tuple, or ``None`` if cancelled.
    Terminal apps don't have a native folder picker, so a free-form path
    is the cleanest equivalent of the VSCode ``showOpenDialog``.
    """

    DEFAULT_CSS = """
    AddPathModal { align: center middle; }
    #box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 80; height: auto;
    }
    #hint { color: #858585; margin-bottom: 1; }
    #so-hint { color: #858585; margin-top: 1; }
    #btn-row { margin-top: 1; height: 3; }
    #btn-row Button { margin-right: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(
                "Path / pattern — a local directory, URL, or glob "
                "(e.g. ~/projects/* or s3://bucket/prefix):",
                id="hint",
            )
            yield Input(value=str(Path.home()), id="path-input")
            yield Label(
                "Storage options (JSON, optional) — fsspec credentials "
                'for remote filesystems, e.g. {"key": "AKIA…", "secret": "…"}:',
                id="so-hint",
            )
            yield Input(placeholder='{"key": "…", "secret": "…"}', id="so-input")
            with Horizontal(id="btn-row"):
                yield Button("Add", variant="primary", id="btn-ok")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#path-input", Input).focus()

    @on(Input.Submitted, "#path-input")
    def _on_path_submit(self, event: Input.Submitted) -> None:
        self.query_one("#so-input", Input).focus()

    @on(Input.Submitted, "#so-input")
    def _on_so_submit(self, event: Input.Submitted) -> None:
        self._accept()

    @on(Button.Pressed, "#btn-ok")
    def _on_ok(self) -> None:
        self._accept()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def _accept(self) -> None:
        path = self.query_one("#path-input", Input).value.strip()
        so = self.query_one("#so-input", Input).value.strip()
        if path:
            self.dismiss((path, so))
        else:
            self.dismiss(None)


class RevealPickModal(ModalScreen[str | None]):
    """Pick one path when a glob matches multiple files."""

    DEFAULT_CSS = """
    RevealPickModal { align: center middle; }
    #box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 80; height: auto;
    }
    #box Label { color: #dcb67a; text-style: bold; margin-bottom: 1; }
    #list { height: auto; max-height: 20; border: solid #3c3c3c; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, matches: list[str]) -> None:
        super().__init__()
        self._matches = list(matches)

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"{len(self._matches)} files match - pick one:")
            yield ListView(id="list")

    def on_mount(self) -> None:
        lv = self.query_one("#list", ListView)
        for m in self._matches:
            lv.append(ListItem(Label(m)))
        lv.focus()

    @on(ListView.Selected, "#list")
    def _on_pick(self, event: ListView.Selected) -> None:
        lbl = event.item.query_one(Label)
        self.dismiss(str(lbl.render()).strip())


class InfoPopupModal(ModalScreen[None]):
    """Pop up the ``projspec info`` docstring for a clicked ``(i)`` button."""

    DEFAULT_CSS = """
    InfoPopupModal { align: center middle; }
    #box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 70; height: auto; max-height: 30;
    }
    #title { color: #dcb67a; text-style: bold; margin-bottom: 1; }
    #doc { color: #cccccc; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, title: str, doc: str) -> None:
        super().__init__()
        self._title = title
        self._doc = doc

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="box"):
            yield Static(self._title, id="title")
            yield Static(self._doc or "(no documentation)", id="doc")

    def on_click(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
#  Custom widgets
# ---------------------------------------------------------------------------


class Chip(Static):
    """Clickable pill used to select a chip (Contents / Artifacts / spec)."""

    DEFAULT_CSS = """
    Chip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin: 0 1 0 0;
        background: #555;
        color: #dcdcaa;
    }
    Chip.global-chip {
        background: #888;
        color: #1e1e1e;
    }
    Chip:hover { background: #094771; }
    Chip.active { background: #094771; color: #ffffff; }
    """

    def __init__(
        self,
        label: str,
        url: str,
        kind: str,
        spec_name: str | None = None,
    ) -> None:
        super().__init__(label)
        self._url = url
        self._kind = kind
        self._spec_name = spec_name
        self.can_focus = True

    @property
    def selection_tuple(self) -> tuple[str, str, str | None]:
        return (self._url, self._kind, self._spec_name)

    def on_click(self) -> None:
        self.post_message(ChipClicked(self._url, self._kind, self._spec_name))


class ChipClicked(Message):
    """Message emitted when a :class:`Chip` is clicked.

    Bubbles up through the widget tree so the app can update its selection
    state and re-render the Details pane.
    """

    def __init__(self, url: str, kind: str, spec_name: str | None) -> None:
        super().__init__()
        self.url = url
        self.kind = kind
        self.spec_name = spec_name


class ProjectWidget(Static):
    """One entry in the Library list - title, URL, chips and kebab button."""

    DEFAULT_CSS = """
    ProjectWidget {
        border: solid #3c3c3c;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
        background: #252526;
    }
    ProjectWidget.active { border: solid #007acc; background: #094771; }
    ProjectWidget .title { color: #dcb67a; text-style: bold; }
    ProjectWidget .url { color: #858585; }
    ProjectWidget .storage { color: #858585; text-style: italic; }
    ProjectWidget .meta { color: #858585; }
    /* Each chips row is a plain horizontal run of ``Chip`` statics laid
       out left-to-right.  ``#chips-wrap`` stacks multiple such rows when
       the total chip width exceeds ``_CHIPS_ROW_WIDTH``. */
    ProjectWidget #chips-wrap { height: auto; padding-top: 0; }
    ProjectWidget .chips-row { height: 1; }
    /* Action button rows need 3 cells of height because Textual's default
       Button renders a 1-line border + 1-line label + 1-line border; clip
       it shorter and the label disappears. */
    ProjectWidget #kebab-row { height: 3; padding: 0; }
    ProjectWidget Button#kebab { min-width: 5; width: 5; padding: 0; }
    """

    def __init__(self, url: str, project: dict, infos: dict) -> None:
        super().__init__()
        self.url = url
        self.project = project
        self._infos = infos

    def compose(self) -> ComposeResult:
        yield Static(_basename(self.url), classes="title")
        yield Static(self.url, classes="url")
        so = self.project.get("storage_options") or {}
        if so:
            yield Static(f"storage_options: {json.dumps(so)}", classes="storage")
        meta_parts = []
        file_count = self.project.get("file_count")
        total_size = self.project.get("total_size")
        if file_count is not None and total_size is not None:
            from projspec.proj.base import _fmt_size

            meta_parts.append(
                f"{int(file_count):,} files, {_fmt_size(int(total_size))}"
            )
        is_writable = self.project.get("is_writable")
        if is_writable is not None:
            meta_parts.append("writable" if is_writable else "read-only")
        last_modified = self.project.get("last_modified")
        if last_modified is not None:
            age = _fmt_age(float(last_modified))
            by = self.project.get("last_modified_by")
            meta_parts.append("last modified " + age + (f" by {by}" if by else ""))
        scanned_at = self.project.get("scanned_at")
        if scanned_at is not None:
            meta_parts.append("scanned " + _fmt_age(float(scanned_at)))
        if meta_parts:
            yield Static(" · ".join(meta_parts), classes="meta")
        # Build the full list of chips first, then split into horizontal
        # rows that each fit into roughly ``_CHIPS_ROW_WIDTH`` cells.  This
        # keeps chips visible in narrow library panes (a plain Horizontal
        # happily lays chips past the right edge where they can't be
        # clicked) while still giving a pill-row look when there's room.
        chip_args: list[tuple[str, str, str, str | None]] = []
        contents = self.project.get("contents") or {}
        artifacts = self.project.get("artifacts") or {}
        global_count = len(contents) + len(artifacts)
        if global_count > 0:
            chip_args.append(
                (
                    "Global",
                    self.url,
                    "global",
                    None,
                )
            )
        for spec_name in (self.project.get("specs") or {}).keys():
            icon = _project_icon("spec", spec_name, self._infos)
            chip_args.append((f"{icon} {spec_name}", self.url, "spec", spec_name))
        with Vertical(id="chips-wrap"):
            for row in _wrap_chips(chip_args, _CHIPS_ROW_WIDTH):
                with Horizontal(classes="chips-row"):
                    for label, url, kind, spec_name in row:
                        chip = Chip(label, url, kind, spec_name)
                        if kind == "global":
                            chip.add_class("global-chip")
                        yield chip
        with Horizontal(id="kebab-row"):
            kebab_btn = Button(CHROME_ICONS["kebab"], id="kebab", variant="default")
            kebab_btn.tooltip = "More actions"
            yield kebab_btn

    @on(Button.Pressed, "#kebab")
    def _on_kebab(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(KebabPressed(self.url))


class KebabPressed(Message):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url


class ItemWidget(Static):
    """A single content/artifact widget in the Details pane.

    Mirrors the HTML ``.item-widget`` node: coloured outline, title line with
    icon/klass/name, optional Make/Reveal/Info actions, and a YAML-rendered
    body (or raw HTML when ``_html`` is present — not supported in a TUI, so
    we fall back to the YAML tree).
    """

    DEFAULT_CSS = """
    ItemWidget {
        border: solid #3c3c3c;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
        background: #252526;
    }
    ItemWidget.kind-content  { border: solid #3794ff; }
    ItemWidget.kind-artifact { border: solid #cca700; }
    ItemWidget .widget-title { text-style: bold; }
    ItemWidget .widget-subtitle { color: #858585; }
    ItemWidget .widget-kind-badge { color: #858585; text-style: bold; }
    ItemWidget #actions { height: 3; }
    ItemWidget #actions Button { min-width: 5; width: 6; margin-right: 1; padding: 0; }
    ItemWidget .body { padding: 0 0 0 1; }
    """

    def __init__(
        self,
        type_name: str,
        name: str | None,
        data: Any,
        kind: str,
        show_make: bool,
        spec_name: str | None,
        project_url: str,
        enums: dict[str, dict[str, Any]],
        infos: dict,
    ) -> None:
        super().__init__()
        self._type_name = type_name
        self._name = name
        self._data = data
        self._kind = kind
        self._show_make = show_make
        self._spec_name = spec_name
        self._project_url = project_url
        self._enums = enums
        self._infos = infos
        self.add_class("kind-" + kind)

    def compose(self) -> ComposeResult:
        data = self._data if isinstance(self._data, dict) else {}
        klass = (
            (data.get("klass") or [None, self._type_name])[1]
            if data
            else self._type_name
        )
        title_suffix = f"  - {self._name}" if self._name else ""
        icon = _project_icon(self._kind, klass, self._infos)
        badge = "CONTENT" if self._kind == "content" else "ARTIFACT"
        yield Static(
            f"{icon} [bold]{klass}[/][#858585]{title_suffix}[/]"
            f" [widget-kind-badge]{badge}[/]",
            classes="widget-title",
        )

        # Actions row - emoji-only labels to match the horizontal-TUI
        # style; the accessible name (tooltip) spells them out for users
        # who can see it.
        buttons: list[Widget] = []
        fn = data.get("fn") if isinstance(data, dict) else None
        if self._kind == "artifact" and isinstance(fn, str) and _is_local_path(fn):
            rb = Button(CHROME_ICONS["reveal"], id="btn-reveal")
            rb.tooltip = "Reveal file"
            buttons.append(rb)
        if self._show_make:
            mb = Button(CHROME_ICONS["play"], id="btn-make", variant="primary")
            mb.tooltip = "Make artifact"
            buttons.append(mb)
        ib = Button(CHROME_ICONS["info"], id="btn-info")
        ib.tooltip = "Show documentation"
        buttons.append(ib)
        if buttons:
            with Horizontal(id="actions"):
                for b in buttons:
                    yield b

        # Body: YAML tree (TUI has no HTML renderer, so ``_html`` is ignored
        # and we always show the structured data).
        stripped = _strip_klass(data) if isinstance(data, dict) else data
        lines = _yaml_lines(stripped, self._enums)
        yield Static("\n".join(lines) or " ", classes="body")

    @on(Button.Pressed, "#btn-make")
    def _make(self, event: Button.Pressed) -> None:
        event.stop()
        qname = ".".join(p for p in (self._spec_name, self._type_name, self._name) if p)
        self.post_message(MakeRequested(self._project_url, qname))

    @on(Button.Pressed, "#btn-reveal")
    def _reveal(self, event: Button.Pressed) -> None:
        event.stop()
        fn = self._data.get("fn") if isinstance(self._data, dict) else None
        if isinstance(fn, str):
            self.post_message(RevealRequested(fn))

    @on(Button.Pressed, "#btn-info")
    def _info(self, event: Button.Pressed) -> None:
        event.stop()
        data = self._data if isinstance(self._data, dict) else {}
        klass = (data.get("klass") or [None, self._type_name])[1]
        self.post_message(InfoRequested(self._kind, klass))


class MakeRequested(Message):
    def __init__(self, url: str, qname: str) -> None:
        super().__init__()
        self.url = url
        self.qname = qname


class RevealRequested(Message):
    def __init__(self, fn: str) -> None:
        super().__init__()
        self.fn = fn


class InfoRequested(Message):
    def __init__(self, kind: str, klass: str) -> None:
        super().__init__()
        self.kind = kind
        self.klass = klass


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _is_local_path(p: str) -> bool:
    if p.startswith("file://"):
        return True
    if "://" in p:
        return False
    return True


def _url_to_local(url: str) -> str:
    return url[len("file://") :] if url.startswith("file://") else url


def _entry_storage_options(url: str) -> str:
    """Storage options of the library entry *url*, as a JSON string ("" if none).

    Remote projects need their ``storage_options`` re-supplied when the
    ``Project`` is reconstructed on rescan, otherwise filesystem access fails.
    """
    import json as _json

    proj = library.entries.get(url)
    so = getattr(proj, "storage_options", None) or {}
    return _json.dumps(so) if so else ""


def _spawn_detached(cmd: list[str]) -> None:
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _open_default(path: str) -> None:
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
    import glob

    if not any(c in pattern for c in "*?["):
        return [pattern] if os.path.exists(pattern) else []
    return sorted(glob.glob(pattern))


# ---------------------------------------------------------------------------
#  Main application
# ---------------------------------------------------------------------------


APP_CSS = """
Screen { background: #1e1e1e; }

/* ── Library tab ──────────────────────────────────────────────────────── */
#library-pane {
    width: 2fr; min-width: 40;
    border-right: solid #3c3c3c;
}
#details-pane { width: 3fr; }

#toolbar { height: 3; padding: 0 1; background: #252526; border-bottom: solid #3c3c3c; }
#toolbar Button { margin-right: 1; min-width: 12; }

#search-row { height: 3; padding: 0 1; background: #252526; border-bottom: solid #3c3c3c; }
#search-row Input { width: 1fr; }

#projects { padding: 1 1; }

#details-header { height: auto; padding: 0 1; background: #252526; border-bottom: solid #3c3c3c; }
#details-title { color: #dcb67a; text-style: bold; }
#details-doc { color: #858585; margin-top: 0; }

#details-list { padding: 1; }

/* ── File browser tab ─────────────────────────────────────────────────── */
#fb-url-row { height: 3; padding: 0 1; background: #252526; border-bottom: solid #3c3c3c; }
#fb-url-row Input { width: 1fr; }
#fb-url-row Button { min-width: 6; margin-left: 1; }

#fb-toolbar { height: 3; padding: 0 1; background: #252526; border-bottom: solid #3c3c3c; }
#fb-toolbar Button { margin-right: 1; min-width: 8; }

#fb-entries-pane { width: 2fr; min-width: 36; border-right: solid #3c3c3c; padding: 0 1; }
#fb-info-pane    { width: 3fr; padding: 1; }
#fb-info-title   { color: #4fc1ff; text-style: bold; }
#fb-info-body    { color: #858585; }

/* ── Status bar ───────────────────────────────────────────────────────── */
#status { dock: bottom; height: 1; background: #007acc; color: white; padding: 0 1; }
"""


# ---------------------------------------------------------------------------
#  File browser entry widget
# ---------------------------------------------------------------------------


class FbEntry(Static):
    """One row in the file browser listing.

    A single click selects the entry; two clicks within 500 ms on a
    directory navigate into it (Textual has no native double-click event,
    so we implement it via click-time tracking).
    """

    COMPONENT_CLASSES = {"fb-entry--dir", "fb-entry--file"}

    class Selected(Message):
        def __init__(self, url: str, entry_type: str) -> None:
            super().__init__()
            self.url = url
            self.entry_type = entry_type

    class NavigateTo(Message):
        def __init__(self, url: str) -> None:
            super().__init__()
            self.url = url

    def __init__(self, entry: dict) -> None:
        self._entry = entry
        self._url = entry.get("name", "")
        self._type = entry.get("type", "file")
        name = (
            entry.get("basename")
            or self._url.rstrip("/").rsplit("/", 1)[-1]
            or self._url
        )
        icon = "📁" if self._type == "directory" else "📄"
        label = f"{icon} {name}"
        super().__init__(label)
        self._last_click: float = 0.0

    def on_click(self) -> None:
        import time

        now = time.monotonic()
        if self._type == "directory" and (now - self._last_click) < 0.5:
            # Two clicks within 500 ms → navigate
            self._last_click = 0.0
            self.post_message(FbEntry.NavigateTo(self._url))
        else:
            self._last_click = now
            self.post_message(FbEntry.Selected(self._url, self._type))


class StorageOptionsModal(ModalScreen[str | None]):
    """Edit the current storage-options JSON string.

    Returns the new JSON string (possibly empty) or ``None`` if cancelled.
    """

    DEFAULT_CSS = """
    StorageOptionsModal { align: center middle; }
    #so-box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 70; height: auto;
    }
    #so-hint { color: #858585; margin-bottom: 1; }
    #so-btn-row { margin-top: 1; height: 3; }
    #so-btn-row Button { margin-right: 1; }
    """

    BINDINGS = [Binding("escape", "app.pop_screen()", "Cancel")]

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="so-box"):
            yield Label(
                "Storage options (JSON) — fsspec credentials / endpoint "
                'overrides, e.g. {"key":"AKIA…","secret":"…"}. '
                "Leave blank for public / local access.",
                id="so-hint",
            )
            yield Input(
                value=self._current,
                placeholder='{"key": "…", "secret": "…"}',
                id="so-input",
            )
            with Horizontal(id="so-btn-row"):
                yield Button("Apply", variant="primary", id="so-ok")
                yield Button("Cancel", id="so-cancel")

    def on_mount(self) -> None:
        self.query_one("#so-input", Input).focus()

    @on(Input.Submitted, "#so-input")
    def _on_submitted(self) -> None:
        self._apply()

    @on(Button.Pressed, "#so-ok")
    def _on_ok(self) -> None:
        self._apply()

    @on(Button.Pressed, "#so-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def _apply(self) -> None:
        value = self.query_one("#so-input", Input).value.strip()
        if value:
            import json as _json

            try:
                _json.loads(value)
            except ValueError:
                self.query_one("#so-hint", Label).update(
                    "[red]Invalid JSON — please correct it.[/red]"
                )
                return
        self.dismiss(value)


class BookmarksModal(ModalScreen[dict | None]):
    """Show saved bookmarks and let the user navigate to one or remove it.

    Returns the URL to navigate to, or ``None`` if closed without selection.
    """

    DEFAULT_CSS = """
    BookmarksModal { align: center middle; }
    #bm-box {
        background: #252526; border: solid #454545;
        padding: 1 2; width: 70; height: auto; max-height: 30;
    }
    #bm-title { text-style: bold; margin-bottom: 1; }
    #bm-list  { height: auto; max-height: 20; }
    #bm-empty { color: #858585; }
    #bm-btn-row { margin-top: 1; height: 3; }
    #bm-btn-row Button { margin-right: 1; }
    """

    BINDINGS = [Binding("escape", "app.pop_screen()", "Close")]

    def __init__(self, bookmarks: list[dict], current_url: str = "") -> None:
        super().__init__()
        self._bookmarks = bookmarks
        self._current_url = current_url
        # Map safe widget id → full bookmark dict (URLs can't be used as
        # Textual widget ids — they contain "://", ".", "/" etc.)
        self._idx_to_bm: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="bm-box"):
            yield Label("★ Bookmarks", id="bm-title")
            with VerticalScroll(id="bm-list"):
                if not self._bookmarks:
                    yield Label("No bookmarks yet.", id="bm-empty")
                else:
                    for i, bm in enumerate(self._bookmarks):
                        url = bm.get("url", "")
                        label = bm.get("label") or url
                        wid = f"bm-item-{i}"
                        self._idx_to_bm[wid] = bm
                        yield Button(label, id=wid, classes="bm-item")
            with Horizontal(id="bm-btn-row"):
                yield Button("+ Bookmark current", variant="primary", id="bm-add")
                yield Button("Close", id="bm-close")

    def on_mount(self) -> None:
        items = self.query(".bm-item")
        if items:
            items.first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "bm-close":
            self.dismiss(None)
        elif bid == "bm-add":
            # Return a synthetic bookmark dict for the current location
            self.dismiss({"url": self._current_url, "_add": True})
        elif bid in self._idx_to_bm:
            self.dismiss(self._idx_to_bm[bid])
        event.stop()


class ProjspecApp(App):
    """Projspec terminal browser — Project Library + File Browser tabs."""

    TITLE = "Projspec Browser"
    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "reload", "Reload"),
        Binding("a", "add", "Add"),
        Binding("/", "focus_search", "Search"),
        Binding("f", "focus_fb_url", "FB URL", show=False),
    ]

    status_message: reactive[str] = reactive("Ready", init=False)

    def __init__(self) -> None:
        super().__init__()
        self._info: dict = {}
        self._enums: dict[str, dict[str, Any]] = {}
        self._selection: tuple[str, str, str | None] | None = None
        self._busy = 0
        self._fb_current_url: str = ""
        self._fb_selected_url: str = ""
        self._fb_selected_type: str = ""
        self._fb_storage_options: str = ""  # current SO JSON string (empty = none)
        self._fb_bookmarks: list[dict] = []  # cached from filebrowser.bookmarks_list()

    # ── Layout ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-library"):
            # ── Project Library tab ─────────────────────────────────────
            with TabPane("📚 Project Library", id="tab-library"):
                with Horizontal():
                    with Vertical(id="library-pane"):
                        with Horizontal(id="toolbar"):
                            yield Button(
                                f"{CHROME_ICONS['add']} Add",
                                id="btn-add",
                                variant="primary",
                            )
                            yield Button(
                                f"{CHROME_ICONS['reload']} Reload", id="btn-reload"
                            )
                            yield Button(
                                f"{CHROME_ICONS['configure']} Configure",
                                id="btn-configure",
                            )
                        with Horizontal(id="search-row"):
                            yield Input(placeholder="Search projects", id="search")
                            btn_clear = Button(
                                CHROME_ICONS["clear"], id="btn-clear", variant="default"
                            )
                            btn_clear.tooltip = "Clear search"
                            yield btn_clear
                        yield VerticalScroll(id="projects")
                    with Vertical(id="details-pane"):
                        with Vertical(id="details-header"):
                            yield Static("Details", id="details-title")
                            yield Static("", id="details-doc")
                        yield VerticalScroll(id="details-list")
            # ── File Browser tab ─────────────────────────────────────────
            with TabPane("📁 File Browser", id="tab-filebrowser"):
                with Vertical():
                    with Horizontal(id="fb-toolbar"):
                        yield Button("⬆ Up", id="btn-fb-up", variant="default")
                        yield Button(
                            "↻ Refresh", id="btn-fb-refresh", variant="default"
                        )
                        yield Button("★ Bookmarks", id="btn-fb-bm", variant="default")
                        yield Button("🔑 SO", id="btn-fb-so", variant="default")
                        yield Button("+ Lib", id="btn-fb-add-lib", variant="primary")
                    with Horizontal(id="fb-url-row"):
                        yield Input(placeholder="Path or URL", id="fb-url-input")
                        yield Button("Go", id="btn-fb-go", variant="primary")
                    with Horizontal():
                        yield VerticalScroll(id="fb-entries-pane")
                        with Vertical(id="fb-info-pane"):
                            yield Static("No selection", id="fb-info-title")
                            yield Static("", id="fb-info-body")
        yield Static(self.status_message, id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._reload(initial=True)
        # Load bookmarks then kick off the file browser at the home directory
        self._fb_load_bookmarks()
        self._fb_navigate(os.path.expanduser("~"))

    def watch_status_message(self, msg: str) -> None:
        try:
            self.query_one("#status", Static).update(msg)
        except Exception:
            pass

    # ── Busy helpers ────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._busy += 1
            if self._busy == 1:
                self.status_message = "Working..."
        else:
            self._busy = max(0, self._busy - 1)
            if self._busy == 0:
                self.status_message = "Ready"

    # ── Actions ─────────────────────────────────────────────────────────────

    def action_reload(self) -> None:
        self._reload()

    def action_add(self) -> None:
        def _cb(result: tuple[str, str] | None) -> None:
            if result:
                path, so = result
                self._scan_and_reload(path, walk=True, storage_options=so)

        self.push_screen(AddPathModal(), _cb)

    def action_focus_search(self) -> None:
        try:
            self.query_one("#search", Input).focus()
        except Exception:
            pass

    def action_focus_fb_url(self) -> None:
        try:
            self.query_one("#fb-url-input", Input).focus()
        except Exception:
            pass

    # ── File browser button handlers ────────────────────────────────────────

    @on(Button.Pressed, "#btn-fb-go")
    def _on_fb_go(self) -> None:
        try:
            url = self.query_one("#fb-url-input", Input).value.strip()
            if url:
                self._fb_navigate(url)
        except Exception:
            pass

    @on(Button.Pressed, "#btn-fb-up")
    def _on_fb_up(self) -> None:
        if self._fb_current_url:
            parent = _url_to_parent(self._fb_current_url)
            if parent and parent != self._fb_current_url:
                self._fb_navigate(parent)

    @on(Button.Pressed, "#btn-fb-refresh")
    def _on_fb_refresh(self) -> None:
        if self._fb_current_url:
            self._fb_navigate(self._fb_current_url)

    @on(Button.Pressed, "#btn-fb-so")
    def _on_fb_so(self) -> None:
        """Open the storage-options modal."""

        def _cb(result: str | None) -> None:
            if result is not None:
                self._fb_storage_options = result
                so_label = " [🔑]" if result else ""
                self.status_message = f"Storage options set{so_label}"

        self.push_screen(StorageOptionsModal(self._fb_storage_options), _cb)

    @on(Button.Pressed, "#btn-fb-bm")
    def _on_fb_bm(self) -> None:
        """Open the bookmarks modal."""

        def _cb(result: dict | None) -> None:
            if not result:
                return
            url = result.get("url", "")
            if not url:
                return
            if result.get("_add"):
                # Save current location as a bookmark
                self._fb_bookmark_add(url)
                return
            # Apply the bookmark's stored storage_options to the session SO,
            # then navigate.  This is the key step: without it, credentials
            # stored on the bookmark are silently ignored.
            bm_so = result.get("storage_options") or {}
            if bm_so:
                self._fb_storage_options = json.dumps(bm_so)
            self._fb_navigate(url)

        self.push_screen(BookmarksModal(self._fb_bookmarks, self._fb_current_url), _cb)

    @on(Button.Pressed, "#btn-fb-add-lib")
    def _on_fb_add_lib(self) -> None:
        if self._fb_selected_url and self._fb_selected_type == "directory":
            self._fb_add_to_library(self._fb_selected_url)

    @on(FbEntry.Selected)
    def _on_fb_entry_selected(self, event: "FbEntry.Selected") -> None:
        self._fb_selected_url = event.url
        self._fb_selected_type = event.entry_type
        self._fb_show_info(event.url, event.entry_type)
        event.stop()

    @on(FbEntry.NavigateTo)
    def _on_fb_navigate_to(self, event: "FbEntry.NavigateTo") -> None:
        self._fb_navigate(event.url)
        event.stop()

    # ── File browser core logic ──────────────────────────────────────────────

    def _fb_load_bookmarks(self) -> None:
        try:
            from projspec.filebrowser import bookmarks_list

            self._fb_bookmarks = bookmarks_list()
        except Exception:
            self._fb_bookmarks = []

    def _fb_so_dict(self) -> dict | None:
        """Parse ``self._fb_storage_options`` and return a dict, or None."""
        so_str = self._fb_storage_options
        if not so_str:
            return None
        try:
            import json as _json

            return _json.loads(so_str) or None
        except Exception:
            return None

    def _fb_navigate(self, url: str) -> None:
        from projspec.filebrowser import browse

        self._set_busy(True)
        try:
            so = self._fb_so_dict()
            result = browse(url, storage_options=so)
            if result.get("error"):
                # Show the full diagnostic in the info pane where it's always visible
                try:
                    self.query_one("#fb-info-title", Static).update("Browse error")
                    self.query_one("#fb-info-body", Static).update(
                        f"URL: {url!r}\nSO: {so!r}\nError: {result['error']}"
                    )
                except Exception:
                    pass
                return
            self._fb_current_url = result.get("url", url)
            try:
                self.query_one("#fb-url-input", Input).value = self._fb_current_url
            except Exception:
                pass
            entries_pane = self.query_one("#fb-entries-pane", VerticalScroll)
            for child in list(entries_pane.children):
                child.remove()
            entries = result.get("entries", [])
            if not entries:
                entries_pane.mount(Static("(empty)", classes="url"))
            else:
                for entry in entries:
                    entries_pane.mount(FbEntry(entry))
            try:
                self.query_one("#fb-info-title", Static).update("No selection")
                self.query_one("#fb-info-body", Static).update("")
            except Exception:
                pass
            self._fb_selected_url = ""
            self._fb_selected_type = ""
            so_indicator = " [🔑]" if self._fb_storage_options else ""
            self.status_message = f"📁 {self._fb_current_url}{so_indicator}"
        except Exception as e:
            try:
                self.query_one("#fb-info-title", Static).update("Browse exception")
                self.query_one("#fb-info-body", Static).update(
                    f"URL: {url!r}\nSO: {self._fb_so_dict()!r}\nException: {e}"
                )
            except Exception:
                pass
        finally:
            self._set_busy(False)

    def _fb_show_info(self, url: str, entry_type: str) -> None:
        name = url.rstrip("/").rsplit("/", 1)[-1] or url
        so = self._fb_so_dict()
        try:
            info_title = self.query_one("#fb-info-title", Static)
            info_body = self.query_one("#fb-info-body", Static)
            if entry_type == "directory":
                info_title.update(f"📁 {name}")
                from projspec.filebrowser import scan_directory

                result = scan_directory(url, storage_options=so)
                proj = result.get("project")
                if proj and proj.get("specs"):
                    spec_names = ", ".join(proj["specs"].keys())
                    info_body.update(f"Specs: {spec_names}")
                else:
                    info_body.update("(no projspec data)")
            else:
                from projspec.filebrowser import inspect_file

                result = inspect_file(url, storage_options=so)
                mime = result.get("mime_type", "")
                size = result.get("size")
                parts = []
                if mime:
                    parts.append(mime)
                if size is not None:
                    parts.append(_fmt_size(size))
                preview = result.get("text_preview", "")
                if preview:
                    parts.append("\n" + preview[:200])
                info_title.update(f"📄 {name}")
                info_body.update("\n".join(parts) if parts else "(no info)")
        except Exception as e:
            try:
                self.query_one("#fb-info-body", Static).update(f"Error: {e}")
            except Exception:
                pass

    def _fb_add_to_library(self, url: str) -> None:
        from projspec.filebrowser import add_to_projspec_library

        self._set_busy(True)
        try:
            result = add_to_projspec_library(url, storage_options=self._fb_so_dict())
            if result.get("error"):
                self.status_message = f"Add to library failed: {result['error']}"
            else:
                self.status_message = f"Added to library: {url}"
                self._reload()
                try:
                    self.query_one(TabbedContent).active = "tab-library"
                except Exception:
                    pass
        except Exception as e:
            self.status_message = f"Add to library failed: {e}"
        finally:
            self._set_busy(False)

    def _fb_bookmark_add(self, url: str) -> None:
        """Bookmark *url* and refresh the cached list."""
        try:
            from projspec.filebrowser import bookmark_add

            self._fb_bookmarks = bookmark_add(url, storage_options=self._fb_so_dict())
            self.status_message = f"Bookmarked: {url}"
        except Exception as e:
            self.status_message = f"Bookmark failed: {e}"

    @on(Button.Pressed, "#btn-add")
    def _on_add(self) -> None:
        self.action_add()

    @on(Button.Pressed, "#btn-reload")
    def _on_reload(self) -> None:
        self._reload()

    @on(Button.Pressed, "#btn-configure")
    def _on_configure(self) -> None:
        conf_dir = Path(
            os.environ.get("PROJSPEC_CONFIG_DIR")
            or (Path.home() / ".config" / "projspec")
        )
        conf_file = conf_dir / "projspec.json"
        if not conf_file.exists():
            conf_dir.mkdir(parents=True, exist_ok=True)
            conf_file.write_text(json.dumps(DEFAULT_CONFIG, indent=4))
        _open_default(str(conf_file))
        self.status_message = (
            f"Opened {conf_file} — docs: "
            "https://projspec.readthedocs.io/en/latest/config.html"
        )

    @on(Button.Pressed, "#btn-clear")
    def _on_clear(self) -> None:
        self.query_one("#search", Input).value = ""
        self._render_library()

    @on(Input.Changed, "#search")
    def _on_search(self) -> None:
        self._render_library()

    # ── Data loading ────────────────────────────────────────────────────────

    def _reload(self, initial: bool = False) -> None:
        self._set_busy(True)
        try:
            if initial or not self._info:
                self._info = class_infos()
                self._enums = _collect_enum_members()
            library.load()
            self._render_library()
            if self._selection and self._selection[0] not in library.entries:
                self._selection = None
            self._render_details()
        except Exception as e:
            self.status_message = f"Reload failed: {e}"
        finally:
            self._set_busy(False)

    # ── Rendering ───────────────────────────────────────────────────────────

    def _render_library(self) -> None:
        container = self.query_one("#projects", VerticalScroll)
        for child in list(container.children):
            child.remove()
        filter_txt = self.query_one("#search", Input).value.strip().lower()
        urls = sorted(library.entries.keys())
        any_shown = False
        for url in urls:
            proj = library.entries[url]
            pdict = proj.to_dict(compact=False)
            if filter_txt:
                hay = (
                    url
                    + " "
                    + _basename(url)
                    + " "
                    + " ".join((pdict.get("specs") or {}).keys())
                )
                if filter_txt not in hay.lower():
                    continue
            any_shown = True
            widget = ProjectWidget(url, pdict, self._info)
            if self._selection and self._selection[0] == url:
                widget.add_class("active")
            container.mount(widget)
        if not any_shown:
            container.mount(
                Static(
                    'Library is empty. Click "Add" to scan a directory.'
                    if not urls
                    else "No projects match the filter.",
                    classes="url",
                )
            )

    def _render_details(self) -> None:
        title_el = self.query_one("#details-title", Static)
        doc_el = self.query_one("#details-doc", Static)
        list_el = self.query_one("#details-list", VerticalScroll)
        for c in list(list_el.children):
            c.remove()

        if not self._selection:
            title_el.update("Details")
            doc_el.update("")
            return
        url, kind, spec_name = self._selection
        proj = library.entries.get(url)
        if proj is None:
            title_el.update("Details")
            doc_el.update("")
            return
        pdict = proj.to_dict(compact=False)

        if kind == "spec":
            title_el.update(_role(spec_name or "", "spec"))
            entry = (self._info.get("specs") or {}).get(spec_name or "") or {}
            doc_parts = []
            if entry.get("doc"):
                doc_parts.append(entry["doc"])
            if entry.get("link"):
                doc_parts.append(f"[#3794ff]{entry['link']}[/]")
            doc_el.update("\n".join(doc_parts))
            spec = (pdict.get("specs") or {}).get(spec_name or "") or {}
            self._mount_item_group(
                list_el,
                spec.get("_contents") or {},
                "content",
                False,
                spec_name,
                url,
            )
            self._mount_item_group(
                list_el,
                spec.get("_artifacts") or {},
                "artifact",
                True,
                spec_name,
                url,
            )
        elif kind == "global":
            title_el.update(_role("Global", "spec"))
            doc_el.update("")
            self._mount_item_group(
                list_el,
                pdict.get("contents") or {},
                "content",
                False,
                None,
                url,
            )
            self._mount_item_group(
                list_el,
                pdict.get("artifacts") or {},
                "artifact",
                True,
                None,
                url,
            )

    def _mount_item_group(
        self,
        container: VerticalScroll,
        items: dict,
        kind: str,
        show_make: bool,
        spec_name: str | None,
        project_url: str,
    ) -> None:
        if not items:
            return
        if not _has_klass(items):
            # Plain dict (e.g. git_repo's {remotes, tags, branches}) -- one
            # untitled YAML dump.
            lines = _yaml_lines(items, self._enums)
            wrap = Static("\n".join(lines), classes="body")
            wrap.styles.border = ("solid", "#3c3c3c")
            wrap.styles.padding = (0, 1)
            wrap.styles.margin_bottom = 1
            container.mount(wrap)
            return
        for type_name, entry in items.items():
            if entry is None:
                continue
            if isinstance(entry, list):
                for e in entry:
                    container.mount(
                        ItemWidget(
                            type_name,
                            None,
                            e,
                            kind,
                            show_make,
                            spec_name,
                            project_url,
                            self._enums,
                            self._info,
                        )
                    )
            elif isinstance(entry, dict) and "klass" in entry:
                container.mount(
                    ItemWidget(
                        type_name,
                        None,
                        entry,
                        kind,
                        show_make,
                        spec_name,
                        project_url,
                        self._enums,
                        self._info,
                    )
                )
            elif isinstance(entry, dict):
                for name, sub in entry.items():
                    container.mount(
                        ItemWidget(
                            type_name,
                            name,
                            sub,
                            kind,
                            show_make,
                            spec_name,
                            project_url,
                            self._enums,
                            self._info,
                        )
                    )

    # ── Messages from custom widgets ────────────────────────────────────────

    @on(ChipClicked)
    def _on_chip_clicked(self, event: ChipClicked) -> None:
        self._selection = (event.url, event.kind, event.spec_name)
        # Re-render library to update the "active" highlight, then the
        # details pane to show what the user picked.
        self._render_library()
        self._render_details()
        event.stop()

    @on(KebabPressed)
    def _on_kebab_pressed(self, event: KebabPressed) -> None:
        self._open_kebab(event.url)
        event.stop()

    @on(MakeRequested)
    def _on_make_requested(self, event: MakeRequested) -> None:
        self._action_make(event.url, event.qname)
        event.stop()

    @on(RevealRequested)
    def _on_reveal_requested(self, event: RevealRequested) -> None:
        self._action_reveal(event.fn)
        event.stop()

    @on(InfoRequested)
    def _on_info_requested(self, event: InfoRequested) -> None:
        self._action_info(event.kind, event.klass)
        event.stop()

    # ── Kebab / per-project actions ─────────────────────────────────────────

    def _open_kebab(self, url: str) -> None:
        def _cb(key: str | None) -> None:
            if not key:
                return
            if key == "openVSCode":
                _spawn_detached(["code", _url_to_local(url)])
            elif key == "openFilebrowser":
                # Switch to the File Browser tab and navigate there
                try:
                    self.query_one(TabbedContent).active = "tab-filebrowser"
                except Exception:
                    pass
                self._fb_navigate(url)
            elif key == "openPyCharm":
                _spawn_detached(
                    [
                        "pycharm",
                        _url_to_local(url),
                        "nosplash",
                        "dontReopenProjects",
                    ]
                )
            elif key == "openJupyter":
                _spawn_detached(["jupyter", "lab", _url_to_local(url)])
            elif key == "rescan":
                self._scan_and_reload(
                    url, walk=False, storage_options=_entry_storage_options(url)
                )
            elif key == "createSpec":
                self._open_create_spec(url)
            elif key == "remove":
                if url in library.entries:
                    del library.entries[url]
                    library.save()
                self._reload()

        self.push_screen(KebabMenuModal(is_local=url.startswith("file://")), _cb)

    def _open_create_spec(self, url: str) -> None:
        proj = library.entries.get(url)
        existing = set(proj.specs if proj is not None else {}) if proj else set()
        creatable = sorted(
            name
            for name, entry in (self._info.get("specs") or {}).items()
            if entry.get("create") and name not in existing
        )
        if not creatable:
            self.status_message = "No spec types available to create."
            return

        def _cb(pick: str | None) -> None:
            if not pick:
                return
            self._set_busy(True)
            try:
                path = _url_to_local(url)
                proj = library.entries.get(url)
                so = getattr(proj, "storage_options", None) or {}
                new = projspec.Project(path, walk=False, storage_options=so)
                new.create(pick)
                fresh = projspec.Project(path, walk=False, storage_options=so)
                library.add_entry(path, fresh)
                self.status_message = f"Created {pick} in {path}"
                self._reload()
            except Exception as e:
                self.status_message = f"Create failed: {e}"
            finally:
                self._set_busy(False)

        self.push_screen(CreateSpecModal(creatable), _cb)

    def _scan_and_reload(self, url: str, walk: bool, storage_options: str = "") -> None:
        self._set_busy(True)
        try:
            import json as _json

            so = _json.loads(storage_options) if storage_options.strip() else {}
            path = _url_to_local(url)
            proj = projspec.Project(path, walk=walk, storage_options=so)
            if walk:
                for child_url, child in (proj.children or {}).items():
                    if child.specs:
                        library.add_entry(child_url, child)
            if proj.specs:
                library.add_entry(path, proj)
            self.status_message = f"Scanned {path}"
            self._reload()
        except Exception as e:
            self.status_message = f"Scan failed: {e}"
        finally:
            self._set_busy(False)

    def _action_make(self, url: str, qname: str) -> None:
        proj = library.entries.get(url)
        if proj is None:
            self.status_message = f"Project not found: {url}"
            return
        self._set_busy(True)
        try:
            art = proj.make(qname)
            self.status_message = f"Done: {art}"
        except Exception as e:
            self.status_message = f"Make '{qname}' failed: {e}"
        finally:
            self._set_busy(False)

    def _action_reveal(self, fn: str) -> None:
        local = _url_to_local(fn)
        if "://" in local and not local.startswith("/"):
            self.status_message = f"Cannot reveal remote file: {fn}"
            return
        matches = _expand_glob(local)
        if not matches:
            self.status_message = f"No files match: {fn}"
            return
        if len(matches) == 1:
            _open_default(os.path.dirname(matches[0]) or matches[0])
            return

        def _cb(pick: str | None) -> None:
            if pick:
                _open_default(os.path.dirname(pick) or pick)

        self.push_screen(RevealPickModal(matches), _cb)

    def _action_info(self, kind: str, klass: str) -> None:
        table = self._info.get("content" if kind == "content" else "artifact") or {}
        entry = table.get(klass) or {}
        doc = entry.get("doc") or "(no documentation)"
        self.push_screen(InfoPopupModal(klass, doc))


def _url_to_parent(url: str) -> str:
    """Return the parent directory of *url*."""
    s = url.rstrip("/")
    proto_end = s.find("://")
    if proto_end >= 0:
        path_part = s[proto_end + 3 :]
        slash = path_part.rfind("/")
        if slash <= 0:
            return s[: proto_end + 3] or s
        return s[: proto_end + 3 + slash]
    slash = s.rfind("/")
    return s[:slash] if slash > 0 else "/"


def _fmt_size(n) -> str:
    """Human-readable file size."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if App is None:
        print("Cannot run without textual installed")
        return
    app = ProjspecApp()
    app.run()


if __name__ == "__main__":
    main()
