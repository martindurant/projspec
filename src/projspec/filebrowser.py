"""fsspec-backed file browser for projspec.

This module provides the Python IO layer for the projspec file browser UI
(VS Code extension, Jupyter widget, etc.).  All filesystem operations go
through :mod:`fsspec` so the same interface works for local paths, S3, GCS,
Azure, HTTP, SSH and any other fsspec-supported protocol.

Public interface (called via ``projspec filebrowser ...`` CLI)
--------------------------------------------------------------

``browse(url, storage_options)``
    List directory contents, returning a JSON-serialisable dict.

``inspect_file(url, storage_options, max_text_bytes)``
    Return file metadata + an optional text preview / intake summary.

``read_file(url, storage_options, max_bytes)``
    Return file contents as a UTF-8 string (for opening in VS Code).

``write_file(url, content, storage_options)``
    Write (create or overwrite) a file.

``delete(url, storage_options, recursive)``
    Delete a file or directory.

``move(src, dst, storage_options)``
    Move / rename a file or directory.

``mkdir(url, storage_options)``
    Create a directory.  No-op on object stores that don't support empty dirs.

``bookmarks_list(bookmarks_path)``
    Return the saved bookmarks list.

``bookmark_add(url, label, bookmarks_path)``
    Add or update a bookmark.

``bookmark_remove(url, bookmarks_path)``
    Remove a bookmark by URL.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import fsspec

# ---------------------------------------------------------------------------
# Bookmark persistence
# ---------------------------------------------------------------------------

_DEFAULT_BOOKMARKS_FILE = os.path.join(
    os.environ.get(
        "PROJSPEC_CONFIG_DIR",
        os.path.join(os.path.expanduser("~"), ".config", "projspec"),
    ),
    "filebrowser_bookmarks.json",
)


def _bookmarks_path(bookmarks_path: str | None = None) -> str:
    return bookmarks_path or _DEFAULT_BOOKMARKS_FILE


def bookmarks_list(bookmarks_path: str | None = None) -> list[dict]:
    """Return the list of saved bookmarks.

    Each bookmark is a dict with ``url``, ``label``, and optionally
    ``storage_options`` keys.
    """
    path = _bookmarks_path(bookmarks_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def bookmark_add(
    url: str,
    label: str = "",
    storage_options: dict | None = None,
    bookmarks_path: str | None = None,
) -> list[dict]:
    """Add or update a bookmark for *url*.

    ``storage_options`` are stored with the bookmark so that navigating
    to it later automatically uses the correct credentials.  They are
    scoped to the URL — a bookmark for ``s3://bucket-a`` and one for
    ``s3://bucket-b`` each carry their own independent options.

    Returns the updated bookmarks list.
    """
    path = _bookmarks_path(bookmarks_path)
    bms = bookmarks_list(path)
    entry: dict = {"url": url, "label": label or _basename(url)}
    if storage_options:
        entry["storage_options"] = storage_options
    # Update existing bookmark if URL already present.
    for i, bm in enumerate(bms):
        if bm.get("url") == url:
            entry["label"] = label or bm.get("label", _basename(url))
            bms[i] = entry
            _save_bookmarks(bms, path)
            return bms
    bms.append(entry)
    _save_bookmarks(bms, path)
    return bms


def bookmark_remove(
    url: str, bookmarks_path: str | None = None
) -> list[dict[str, str]]:
    """Remove the bookmark with the given *url*.  Returns updated list."""
    path = _bookmarks_path(bookmarks_path)
    bms = [b for b in bookmarks_list(path) if b.get("url") != url]
    _save_bookmarks(bms, path)
    return bms


def _save_bookmarks(bms: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bms, f, indent=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _basename(url: str) -> str:
    url = url.rstrip("/")
    return url.rsplit("/", 1)[-1] or url


def _parent(url: str) -> str:
    """Return the parent directory URL, preserving the protocol."""
    url = url.rstrip("/")
    # Find the last '/' that is not part of the protocol separator '://'
    proto_end = url.find("://")
    if proto_end >= 0:
        # Don't go above the bucket/host root
        path_part = url[proto_end + 3 :]
        slash = path_part.rfind("/")
        if slash < 0:
            # Already at root; return as-is
            return url
        return url[: proto_end + 3 + slash] or url[: proto_end + 3]
    slash = url.rfind("/")
    if slash <= 0:
        return "/"
    return url[:slash]


def _get_fs(url: str, storage_options: dict | None = None) -> tuple[Any, str]:
    """Return (fs, path) for the given URL and optional storage_options."""
    so = storage_options or {}
    fs, path = fsspec.url_to_fs(url, **so)
    return fs, path


def _entry_info(fs: Any, info: dict) -> dict:
    """Normalise an fsspec info dict into a stable shape for the UI."""
    out: dict[str, Any] = {}
    out["name"] = fs.unstrip_protocol(info.get("name", ""))
    out["type"] = info.get("type", "file")  # "directory" or "file"
    out["size"] = info.get("size")  # may be None for directories
    out["last_modified"] = _coerce_mtime(
        info.get("LastModified") or info.get("last_modified") or info.get("mtime")
    )
    out["basename"] = _basename(out["name"])
    return out


def _coerce_mtime(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    # datetime / Timestamp objects
    try:
        return float(v.timestamp())
    except Exception:
        pass
    try:
        import datetime

        if isinstance(v, str):
            dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt.timestamp()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def browse(url: str, storage_options: dict | None = None) -> dict:
    """List the contents of *url* (must be a directory).

    Returns::

        {
            "url": <canonical url>,
            "entries": [
                {
                    "name": <full path>,
                    "basename": <basename>,
                    "type": "directory"|"file",
                    "size": <int or null>,
                    "last_modified": <float epoch or null>,
                },
                ...
            ],
            "parent": <parent url or null>,
            "protocol": <protocol string>,
            "error": null or <error message>,
        }
    """
    try:
        fs, path = _get_fs(url, storage_options)
        try:
            raw = fs.ls(path, detail=True)
        except NotADirectoryError:
            # Treat as a single-file listing
            info = fs.info(path)
            raw = [info]
        entries = [_entry_info(fs, e) for e in raw]
        # Sort: directories first, then files, both alphabetically
        entries.sort(
            key=lambda e: (0 if e["type"] == "directory" else 1, e["basename"].lower())
        )
        canonical = fs.unstrip_protocol(path)
        parent_path = _parent(canonical) if canonical != url or path != "/" else None
        protocol = (
            fs.protocol
            if isinstance(fs.protocol, str)
            else (fs.protocol[0] if fs.protocol else "file")
        )
        return {
            "url": canonical,
            "entries": entries,
            "parent": parent_path,
            "protocol": protocol,
            "error": None,
        }
    except Exception as exc:
        return {
            "url": url,
            "entries": [],
            "parent": None,
            "protocol": "",
            "error": str(exc),
        }


def inspect_file(
    url: str,
    storage_options: dict | None = None,
    max_text_bytes: int = 4096,
) -> dict:
    """Return metadata and a text/data preview for a single file.

    Returns::

        {
            "url": <url>,
            "name": <basename>,
            "size": <int or null>,
            "last_modified": <float or null>,
            "mime_type": <str or null>,
            "intake": <intake summary dict or null>,
            "text_preview": <str or null>,
            "error": null or <error message>,
        }
    """
    try:
        fs, path = _get_fs(url, storage_options)
        info = fs.info(path)
        size = info.get("size")
        mtime = _coerce_mtime(
            info.get("LastModified") or info.get("last_modified") or info.get("mtime")
        )
        mime = _guess_mime(path)
        intake_summary = _intake_inspect(url, path, storage_options)
        text_preview = None

        # Always show a text preview when the file looks like text.
        # We check the extension exhaustively rather than relying on MIME alone
        # (many text formats have no registered MIME type or get None from
        # mimetypes.guess_type).  We then attempt strict UTF-8 decoding and
        # silently skip the preview if the bytes aren't valid UTF-8 — this
        # naturally rejects binary files with innocent-looking extensions.
        if _looks_like_text(path):
            try:
                with fs.open(path, "rb") as f:
                    raw_bytes = f.read(max_text_bytes)
                # strict=True: raises UnicodeDecodeError on invalid UTF-8
                text_preview = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text_preview = None  # binary content — skip preview
            except Exception:
                text_preview = None  # IO or other error

        return {
            "url": url,
            "name": _basename(path),
            "size": size,
            "last_modified": mtime,
            "mime_type": mime,
            "intake": intake_summary,
            "text_preview": text_preview,
            "error": None,
        }
    except Exception as exc:
        return {
            "url": url,
            "name": _basename(url),
            "size": None,
            "last_modified": None,
            "mime_type": None,
            "intake": None,
            "text_preview": None,
            "error": str(exc),
        }


def _intake_inspect(
    url: str, local_path: str, storage_options: dict | None
) -> dict | None:
    """Run intake inspection on a file and return a serialisable summary.

    Strategy:
    1. ``intake.recommend()`` — identifies the data type from the path/extension.
       Works without pandas or any heavy dep.
    2. ``source.discover()`` — tries to get schema (columns, dtype, shape).
       Requires the relevant reader (e.g. pandas for CSV).  Skipped on error.
    3. ``source.read()`` head — reads a small sample to get shape.
       Only attempted if discover succeeds and data is small enough.

    Returns a dict suitable for display, or None if intake is not installed
    or the file is not a recognised data type.
    """
    try:
        import intake as _intake
    except ImportError:
        return None

    so = storage_options or {}
    result: dict[str, Any] = {}

    # 1. Type recognition — works for any installed intake
    try:
        recs = (
            _intake.recommend(url, storage_options=so) if so else _intake.recommend(url)
        )
    except TypeError:
        # Older intake versions don't accept storage_options kwarg
        try:
            recs = _intake.recommend(url)
        except Exception:
            recs = []
    except Exception:
        recs = []

    if not recs:
        return None  # Intake doesn't know this file type

    type_names = [getattr(r, "__name__", str(r)) for r in recs]
    result["types"] = type_names
    result["primary_type"] = type_names[0]

    # 2. Try to open with the best available reader and discover schema
    reader = None
    open_fn_name = "open_" + type_names[0].lower()
    open_fn = getattr(_intake, open_fn_name, None)

    # Fall back to direct instantiation of the datatype reader
    if open_fn is None:
        try:
            dt = recs[0](url, storage_options=so or None)
            importable = [
                r
                for r in dt.possible_readers
                if getattr(r, "__name__", "") == "importable"
            ]
            if importable:
                reader = importable[0](dt).read
        except Exception:
            pass
    else:
        try:
            reader_src = open_fn(url)
        except Exception:
            reader_src = None

        if reader_src is not None:
            try:
                discovered = reader_src.discover()
                # discover() returns a Schema-like object or dict
                if hasattr(discovered, "__dict__"):
                    disc_dict = {
                        k: v
                        for k, v in vars(discovered).items()
                        if isinstance(
                            v, (str, int, float, bool, list, dict, type(None))
                        )
                    }
                elif isinstance(discovered, dict):
                    disc_dict = {
                        k: v
                        for k, v in discovered.items()
                        if isinstance(
                            v, (str, int, float, bool, list, dict, type(None))
                        )
                    }
                else:
                    disc_dict = {}
                if "dtype" in disc_dict and hasattr(disc_dict["dtype"], "items"):
                    # pandas dtype dict → convert to {col: dtype_str}
                    disc_dict["dtype"] = {
                        k: str(v) for k, v in disc_dict["dtype"].items()
                    }
                result.update({k: v for k, v in disc_dict.items() if v is not None})
            except Exception:
                pass  # discover may need pandas or other optional dep

    return result if result else None


# Extensions we treat as text for the purposes of showing a preview.
# Organised by category; err on the side of inclusion — the UTF-8
# strict-decode in inspect_file will silently reject anything that
# turns out to be binary.
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        # ── source code ──────────────────────────────────────────────────────
        "py",
        "pyi",
        "pyx",
        "pxd",  # Python
        "js",
        "mjs",
        "cjs",
        "jsx",  # JavaScript
        "ts",
        "tsx",
        "d.ts",  # TypeScript
        "rb",
        "rake",
        "gemspec",  # Ruby
        "java",
        "kt",
        "kts",
        "groovy",  # JVM
        "scala",
        "clj",
        "cljs",  # Scala / Clojure
        "c",
        "h",
        "cc",
        "cpp",
        "cxx",  # C / C++
        "hh",
        "hpp",
        "hxx",
        "cs",
        "fs",
        "fsx",
        "fsi",  # C# / F#
        "go",  # Go
        "rs",  # Rust
        "swift",  # Swift
        "m",
        "mm",  # Objective-C
        "r",
        "rmd",  # R
        "jl",  # Julia
        "lua",  # Lua
        "pl",
        "pm",
        "t",  # Perl
        "php",  # PHP
        "ex",
        "exs",  # Elixir
        "erl",
        "hrl",  # Erlang
        "hs",
        "lhs",  # Haskell
        "ml",
        "mli",  # OCaml
        "elm",  # Elm
        "dart",  # Dart
        "v",
        "vhd",
        "vhdl",  # Verilog / VHDL
        "zig",  # Zig
        "nim",  # Nim
        # ── shell / scripting ────────────────────────────────────────────────
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "psm1",
        "psd1",  # PowerShell
        "bat",
        "cmd",  # Windows batch
        "awk",
        "sed",
        # ── data / config ────────────────────────────────────────────────────
        "json",
        "jsonc",
        "json5",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "config",
        "properties",
        "env",
        "xml",
        "xsd",
        "xsl",
        "xslt",
        "html",
        "htm",
        "xhtml",
        "css",
        "scss",
        "sass",
        "less",
        "svg",
        "csv",
        "tsv",
        "psv",
        "ndjson",
        "jsonl",
        "graphql",
        "gql",
        "proto",  # Protocol Buffers
        "thrift",
        "avsc",  # Avro schema
        # ── documentation / markup ───────────────────────────────────────────
        "md",
        "markdown",
        "rst",
        "txt",
        "text",
        "adoc",
        "asciidoc",
        "org",
        "tex",
        "sty",
        "cls",
        "bib",  # LaTeX
        "pod",  # Perl docs
        "rdoc",
        # ── build / CI / infra ───────────────────────────────────────────────
        "makefile",
        "mk",
        "mak",
        "dockerfile",
        "cmake",
        "bazel",
        "bzl",
        "build",
        "gradle",
        "tf",
        "tfvars",  # Terraform
        "hcl",
        "nix",
        "cabal",
        "spec",  # RPM spec / test spec
        # ── notebooks / interactive ──────────────────────────────────────────
        "ipynb",  # JSON-based, readable
        # ── lock files / manifests ───────────────────────────────────────────
        "lock",  # various lockfiles (text)
        "sum",  # go.sum
        "mod",  # go.mod
        # ── misc plain-text formats ──────────────────────────────────────────
        "log",
        "diff",
        "patch",
        "sql",
        "pgsql",
        "graphml",
        "dot",
        "gv",  # Graphviz
        "puml",
        "pu",  # PlantUML
        "mmd",  # Mermaid
        "vim",
        "vimrc",
        "emacs",
        "el",
        "editorconfig",
        "gitignore",
        "gitattributes",
        "gitmodules",
        "hgignore",
        "dockerignore",
        "npmignore",
        "license",
        "licence",
        "authors",
        "contributors",
        "readme",
        "changelog",
        "news",
        "todo",
        "fixme",
    }
)


def _looks_like_text(path: str) -> bool:
    """Return True when *path*'s extension (or full basename for dotfiles /
    extensionless names) suggests the file is human-readable text."""
    import os

    name = os.path.basename(path).lower()
    # Dotfiles with no extension: .gitignore, .env, .editorconfig, …
    if name.startswith(".") and "." not in name[1:]:
        return name[1:] in _TEXT_EXTENSIONS or True  # dotfiles are usually text
    # Strip leading dot for the extension comparison
    if "." in name:
        ext = name.rsplit(".", 1)[-1]
        if ext in _TEXT_EXTENSIONS:
            return True
    # Extensionless or unrecognised — also accept files whose MIME is text/*
    mime = _guess_mime(path)
    if mime:
        if mime.startswith("text/"):
            return True
        if mime in {
            "application/json",
            "application/x-yaml",
            "application/toml",
            "application/javascript",
            "application/xml",
            "application/x-sh",
            "application/x-shellscript",
        }:
            return True
    # Bare filenames like "Makefile", "Dockerfile", "Rakefile", "Gemfile" …
    if name in {
        "makefile",
        "dockerfile",
        "rakefile",
        "gemfile",
        "podfile",
        "fastfile",
        "appfile",
        "vagrantfile",
        "berksfile",
        "guardfile",
        "capfile",
        "brewfile",
    }:
        return True
    return False


def _guess_mime(path: str) -> str | None:
    import mimetypes

    mt, _ = mimetypes.guess_type(path)
    return mt


def read_file(
    url: str,
    storage_options: dict | None = None,
    max_bytes: int = 5 * 1024 * 1024,
) -> dict:
    """Read a file and return its contents as a UTF-8 string.

    Files larger than *max_bytes* are rejected.

    Returns::

        {"url": ..., "content": <str>, "size": <int>, "error": null or <msg>}
    """
    try:
        fs, path = _get_fs(url, storage_options)
        info = fs.info(path)
        size = info.get("size", 0) or 0
        if size > max_bytes:
            return {
                "url": url,
                "content": None,
                "size": size,
                "error": f"File too large to open ({size} bytes > {max_bytes} limit)",
            }
        with fs.open(path, "rb") as f:
            raw = f.read()
        content = raw.decode("utf-8", errors="replace")
        return {"url": url, "content": content, "size": len(raw), "error": None}
    except Exception as exc:
        return {"url": url, "content": None, "size": None, "error": str(exc)}


def write_file(
    url: str,
    content: str,
    storage_options: dict | None = None,
) -> dict:
    """Write *content* (UTF-8 string) to *url*.

    Returns::

        {"url": ..., "error": null or <error message>}
    """
    try:
        fs, path = _get_fs(url, storage_options)
        with fs.open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"url": url, "error": None}
    except Exception as exc:
        return {"url": url, "error": str(exc)}


def delete(
    url: str,
    storage_options: dict | None = None,
    recursive: bool = False,
) -> dict:
    """Delete a file or directory at *url*.

    Returns::

        {"url": ..., "error": null or <error message>}
    """
    try:
        fs, path = _get_fs(url, storage_options)
        fs.rm(path, recursive=recursive)
        return {"url": url, "error": None}
    except Exception as exc:
        return {"url": url, "error": str(exc)}


def move(
    src: str,
    dst: str,
    storage_options: dict | None = None,
) -> dict:
    """Move / rename *src* to *dst* (same filesystem).

    Returns::

        {"src": ..., "dst": ..., "error": null or <error message>}
    """
    try:
        fs, src_path = _get_fs(src, storage_options)
        _, dst_path = _get_fs(dst, storage_options)
        fs.mv(src_path, dst_path)
        return {"src": src, "dst": dst, "error": None}
    except Exception as exc:
        return {"src": src, "dst": dst, "error": str(exc)}


def mkdir(
    url: str,
    storage_options: dict | None = None,
) -> dict:
    """Create a directory at *url*.

    On object stores (S3, GCS, …) that don't support empty directories this
    is a no-op; the directory will come into existence when the first file
    is written inside it.

    Returns::

        {"url": ..., "error": null or <error message>}
    """
    try:
        fs, path = _get_fs(url, storage_options)
        try:
            fs.mkdir(path, create_parents=True)
        except (NotImplementedError, AttributeError):
            # Object stores: silently ignore
            pass
        return {"url": url, "error": None}
    except Exception as exc:
        return {"url": url, "error": str(exc)}


def add_to_projspec_library(
    url: str,
    storage_options: dict | None = None,
) -> dict:
    """Scan *url* with projspec and add it to the project library.

    Returns::

        {"url": ..., "project": <dict or null>, "error": null or <msg>}
    """
    try:
        from projspec.utils import scan_glob

        projects = []
        for proj in scan_glob(
            url,
            storage_options=json.dumps(storage_options or {}),
            walk=False,
            add_to_library=True,
        ):
            projects.append(proj.to_dict(compact=False))
        if projects:
            return {"url": url, "project": projects[0], "error": None}
        return {
            "url": url,
            "project": None,
            "error": "No project found at this location",
        }
    except Exception as exc:
        return {"url": url, "project": None, "error": str(exc)}


def inspect_as_project(
    url: str,
    storage_options: dict | None = None,
) -> dict:
    """Inspect a single file and return a project-shaped dict for the UI.

    Produces the same ``to_dict(compact=False)`` structure as a real
    ``DataProject`` scan so the embedded library panel renders it
    identically — a single ``data_project`` spec chip whose contents
    are ``Dataset``-shaped dicts, exactly as produced by
    :class:`projspec.proj.data_project.DataProject`.

    Returns the same keys as ``scan_directory`` plus the raw inspect
    fields for the top-half metadata strip.
    """
    result = inspect_file(url, storage_options=storage_options)
    if result.get("error"):
        return {
            "url": url,
            "project": None,
            "intake": None,
            "text_preview": None,
            "error": result["error"],
        }

    intake = result.get("intake")  # {"primary_type": "Parquet", "types": [...], ...}
    text_preview = result.get("text_preview")
    mime = result.get("mime_type") or ""
    size = result.get("size")
    mtime = result.get("last_modified")
    name = result.get("name", _basename(url))

    # Try to get richer intake info using intake.readers.inspect.inspect_dataset
    # (same call DataProject uses).  Falls back gracefully if unavailable.
    intake_inspect: dict | None = None
    try:
        from intake.readers.inspect import inspect_dataset  # type: ignore

        intake_inspect = inspect_dataset(url, storage_options=storage_options or None)
    except Exception:
        pass

    # Build a Dataset-shaped content item (klass ["content", "dataset"])
    # matching the exact structure produced by DataProject._describe().
    dataset_content: dict[str, Any] = {
        "klass": ["content", "dataset"],
        "url": url,
        "datatype": (intake or {}).get("primary_type") or None,
        "structure": list((intake_inspect or {}).get("structure", [])),
        "schema": {},
        "n_files": 1,
        "total_size": size,
        "metadata": {},
    }

    # Populate schema from intake discover() output if available
    if intake_inspect:
        schema = intake_inspect.get("schema") or {}
        if schema:
            dataset_content["schema"] = schema
        meta = {
            k: v
            for k, v in intake_inspect.items()
            if k not in ("schema", "structure", "datatype", "url")
            and isinstance(v, (str, int, float, bool, list, dict, type(None)))
        }
        if meta:
            dataset_content["metadata"] = meta
    elif intake:
        # Fallback: populate schema from intake._intake_inspect output
        dtype = intake.get("dtype")
        if dtype and isinstance(dtype, dict):
            dataset_content["schema"] = dtype
        # surface npartitions, shape etc. in metadata
        meta = {
            k: v
            for k, v in intake.items()
            if k not in ("primary_type", "types", "dtype") and v is not None
        }
        if meta:
            dataset_content["metadata"] = meta

    # Contents dict keyed by the short file name (mimicking DataProject)
    contents: dict[str, Any] = {name: dataset_content}

    # Text-preview as a separate content item when available
    if text_preview:
        contents["text_preview"] = {
            "klass": ["content", "text_preview"],
            "preview": text_preview,
        }

    # Single spec: "data_project", just like DataProject scans
    project: dict[str, Any] = {
        "url": url,
        "specs": {
            "data_project": {
                "klass": ["spec", "data_project"],
                "_contents": contents,
                "_artifacts": {},
            }
        },
        # No top-level contents/artifacts — avoids the duplicate "Global" chip
        "contents": {},
        "artifacts": {},
        "klass": ["project", "data_project"],
        "file_count": 1,
        "total_size": size,
        "last_modified": mtime,
        "storage_options": storage_options or {},
        "scanned_at": None,
    }

    return {
        "url": url,
        "project": project,
        # Top-level fields for the inspectResult / renderMeta strip
        "name": name,
        "size": size,
        "last_modified": mtime,
        "mime_type": mime or None,
        "intake": intake,
        "text_preview": text_preview,
        "error": None,
    }


def supported_protocols() -> list[str]:
    """Return a list of protocols available in the current fsspec installation."""
    try:
        from fsspec.registry import known_implementations

        return sorted(known_implementations.keys())
    except Exception:
        return ["file", "s3", "gcs", "az", "abfs", "http", "https", "ftp", "memory"]


def scan_directory(
    url: str,
    storage_options: dict | None = None,
) -> dict:
    """Scan *url* with projspec and return the project dict for display.

    Does NOT add to the library.  Returns a JSON-serialisable dict::

        {
            "url": ...,
            "project": <compact=False project dict or null>,
            "error": null or <error message>,
        }
    """
    try:
        from projspec.proj import Project

        so = storage_options or {}
        proj = Project(url, storage_options=so, walk=False)
        return {
            "url": url,
            "project": proj.to_dict(compact=False),
            "error": None,
        }
    except Exception as exc:
        return {"url": url, "project": None, "error": str(exc)}
