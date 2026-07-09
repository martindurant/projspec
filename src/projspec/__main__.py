#!/usr/bin/env python
"""Simple example executable for this library"""

import json
import pydoc
import sys

import click

import projspec.proj
from projspec.config import temp_conf


# global runtime config
context = {}


@click.group()
def main():
    pass


@main.command("make")
@click.argument("artifact", type=str)
@click.argument("path", default=".", type=str)
@click.option(
    "--storage_options",
    default="",
    help="storage options dict for the given URL, as JSON",
)
@click.option(
    "--types",
    default="ALL",
    help='Type names to scan for (comma-separated list in camel or snake case); defaults to "ALL"',
)
@click.option(
    "--xtypes",
    default="NONE",
    help="List of spec types to ignore (comma-separated list in camel or snake case)",
)
@click.option(
    "--wait",
    default=True,
    is_flag=True,
    help="Wait for artifact to finish, for Process type artifacts only (default True)",
)
def make(artifact, path, storage_options, types, xtypes, wait):
    """Make the given artifact in the project at the given path.

    artifact: str , of the form [<spec>.]<artifact-type>[.<name>]

    path: str, path to the project directory, defaults to "."
    """
    if types in {"ALL", ""}:
        types = None
    else:
        types = types.split(",")
    proj = projspec.Project(
        path, storage_options=storage_options, types=types, xtypes=xtypes
    )
    with temp_conf(capture_artifact_output=False):
        art = proj.make(artifact)
        print("Created:", art)
    if wait and art.proc:
        art.proc.wait()
        print("Finished with code:", art.proc.returncode)


@main.command()
def version():
    """Show version and quit"""
    print(f"projspec version: {projspec.__version__}")


@main.command("scan")
@click.argument("patterns", nargs=-1)
@click.option(
    "--storage_options",
    default="",
    help="storage options dict for the given URL, as JSON",
)
@click.option(
    "--types",
    default="ALL",
    help='Type names to scan for (comma-separated list in camel or snake case); defaults to "ALL"',
)
@click.option(
    "--xtypes",
    default="NONE",
    help="List of spec types to ignore (comma-separated list in camel or snake case)",
)
@click.option(
    "--json-out",
    is_flag=True,
    default=False,
    help="JSON output, for projects only",
)
@click.option(
    "--walk", is_flag=True, help="Descend into child directories of each match"
)
@click.option("--summary", is_flag=True, help="Show abbreviated output")
@click.option("--library", is_flag=True, help="Add each result to the library")
def scan(
    patterns,
    storage_options,
    types,
    xtypes,
    json_out,
    walk,
    summary,
    library,
):
    """Scan directories and display results.

    PATTERNS is one or more directory paths or glob expressions.  When the
    shell expands a glob before invoking projspec, each expanded path is
    passed as a separate argument.  Glob expressions containing wildcards
    (e.g. '~/projects/*') are expanded by projspec itself via fsspec.
    If no PATTERNS are given, the current directory is scanned.
    """
    from projspec.utils import scan_glob

    if types in {"ALL", ""}:
        types = None
    else:
        types = types.split(",")

    for pattern in patterns or (".",):
        for proj in scan_glob(
            pattern,
            types=types,
            xtypes=xtypes,
            walk=walk,
            storage_options=storage_options,
            add_to_library=library,
        ):
            if summary:
                print(proj.text_summary())
            else:
                if json_out:
                    print(json.dumps(proj.to_dict(compact=False)))
                else:
                    print(proj)


@main.command("info")
@click.argument(
    "types",
    default="ALL",
)
def info(types=None):
    """Documentation about all the classes within projspec

    types: a specific class name, in Camel or snake_case; if not given,
        lists all types as JSON.
    """
    if types in {"ALL", "", None}:
        from projspec.utils import class_infos

        print(json.dumps(class_infos()))
    else:
        name = projspec.utils.camel_to_snake(types)
        cls = (
            projspec.proj.base.registry.get(name)
            or projspec.content.base.registry.get(name)
            or projspec.artifact.base.registry.get(name)
        )
        if cls:
            pydoc.doc(cls, output=sys.stdout)
        else:
            print("Name not found")


@main.command("create")
@click.argument("type")
@click.argument("path", default=".")
def create(type, path):
    """Create a new project of the given type in the given path.

    Returns the list of files created.

    (Path must be local, no storage_options)
    """
    from projspec.proj import Project
    from projspec.proj.base import ProjectSpec, registry

    if type not in registry:
        print(f"Unknown spec type: {type}")
        sys.exit(1)
    proj = Project(path)
    if type not in proj:
        try:
            files = proj.create(type)
        except NotImplementedError:
            supported = sorted(
                name
                for name, cls in registry.items()
                if cls._create is not ProjectSpec._create
            )
            print(
                f"Spec type '{type}' does not support creation.\n"
                f"Types that support creation: {', '.join(supported)}"
            )
            sys.exit(1)
        for afile in files:
            print(afile)
    else:
        print(f"Project already has a {type} spec")


@main.group("library")
def library():
    """Interact with the project library.

    Library file location is defined by config value "library_path".
    """


@library.command("list")
@click.option(
    "--json-out",
    is_flag=True,
    default=False,
    help="JSON output, for projects only",
)
def list(json_out):
    """Show contents of the library"""
    from projspec.library import ProjectLibrary

    library = ProjectLibrary()
    if json_out:
        print(
            json.dumps(
                {k: v.to_dict(compact=False) for k, v in library.entries.items()}
            )
        )
    else:
        for url in sorted(library.entries):
            proj = library.entries[url]
            print(f"{proj.text_summary(bare=True)}")


@library.command("clear")
def clear():
    """Clear all contents of the library"""
    from projspec.library import ProjectLibrary

    ProjectLibrary().clear()


@library.command("delete")
@click.argument("url")
def delete(url):
    """Delete the project at the given URL from the library.

    URL must be given as shows in `list`.
    """
    from projspec.library import ProjectLibrary

    library = ProjectLibrary()
    library.entries.pop(url)
    library.save()


@main.group("config")
def config():
    """Interact with the projspec config."""
    pass


@config.command("get")
@click.argument("key")
def get(key):
    """Get a value from the config."""
    from projspec.config import get_conf

    print(get_conf(key))


@config.command("show")
def show():
    """Show all contents of the config."""
    from projspec.config import conf

    # TODO: show docs and defaults for each key, from projspec.config.config_doc?
    # TODO: allow JSON output
    print(conf)


@config.command("defaults")
def defaults():
    """Show default config settings for all available values and their definitions"""
    from projspec.config import defaults, config_doc
    import os

    print("PROJSPEC_CONFIG_DIR", os.environ.get("PROJSPEC_CONFIG_DIR", "unset"))
    print()
    for k, v, d in zip(defaults(), defaults().values(), config_doc.values()):
        print(f"{k}: {v} -- {d}")


@config.command("create")
def create():
    """Create the config file with default values, if it doesn't exist"""
    from projspec.config import populate_if_empty

    print(populate_if_empty())


@config.command("unset")
@click.argument("key")
def unset(key):
    """Remove a value from the config (returns to default)"""
    from projspec.config import set_conf

    set_conf(key, None)


@config.command("set")
@click.argument("key")
@click.argument("value")
def set_(key, value):
    """Set a config value"""
    from projspec.config import set_conf

    set_conf(key, value)


# ---------------------------------------------------------------------------
# filebrowser subcommand group
# ---------------------------------------------------------------------------


@main.group("filebrowser")
def filebrowser():
    """File browser operations via fsspec (local and remote filesystems)."""


@filebrowser.command("browse")
@click.argument("url")
@click.option(
    "--storage-options",
    default="",
    help="fsspec storage options as JSON",
)
def fb_browse(url, storage_options):
    """List the contents of a directory URL.

    Outputs JSON with keys: url, entries, parent, protocol, error.
    """
    from projspec.filebrowser import browse

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(browse(url, storage_options=so)))


@filebrowser.command("inspect")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
@click.option(
    "--max-text-bytes",
    default=4096,
    type=int,
    help="Maximum bytes to include in text preview",
)
def fb_inspect(url, storage_options, max_text_bytes):
    """Inspect a single file: metadata + text/intake preview.

    Outputs JSON with keys: url, name, size, last_modified, mime_type,
    intake, text_preview, error.
    """
    from projspec.filebrowser import inspect_file

    so = json.loads(storage_options) if storage_options.strip() else None
    print(
        json.dumps(inspect_file(url, storage_options=so, max_text_bytes=max_text_bytes))
    )


@filebrowser.command("inspect-as-project")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
def fb_inspect_as_project(url, storage_options):
    """Inspect a file and return a project-shaped dict for the UI scan panel.

    Outputs JSON with the same shape as ``projspec scan --json-out`` so the
    file browser's embedded scan panel can render it identically to a directory
    scan.  Keys: url, project, name, size, last_modified, mime_type,
    text_preview, error.
    """
    from projspec.filebrowser import inspect_as_project

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(inspect_as_project(url, storage_options=so)))


@filebrowser.command("read-file")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
@click.option(
    "--max-bytes",
    default=5 * 1024 * 1024,
    type=int,
    help="Maximum file size that may be read",
)
def fb_read_file(url, storage_options, max_bytes):
    """Read a file and emit its contents as JSON.

    Outputs JSON with keys: url, content, size, error.
    """
    from projspec.filebrowser import read_file

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(read_file(url, storage_options=so, max_bytes=max_bytes)))


@filebrowser.command("write-file")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
@click.option(
    "--content-file",
    default="-",
    type=click.File("r", encoding="utf-8"),
    help="File to read content from (default: stdin)",
)
def fb_write_file(url, storage_options, content_file):
    """Write content to a file (create or overwrite).

    Content is read from --content-file or stdin.
    Outputs JSON with keys: url, error.
    """
    from projspec.filebrowser import write_file

    so = json.loads(storage_options) if storage_options.strip() else None
    content = content_file.read()
    print(json.dumps(write_file(url, content, storage_options=so)))


@filebrowser.command("delete")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
@click.option(
    "--recursive", is_flag=True, default=False, help="Delete directories recursively"
)
def fb_delete(url, storage_options, recursive):
    """Delete a file or directory.

    Outputs JSON with keys: url, error.
    """
    from projspec.filebrowser import delete

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(delete(url, storage_options=so, recursive=recursive)))


@filebrowser.command("move")
@click.argument("src")
@click.argument("dst")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
def fb_move(src, dst, storage_options):
    """Move / rename SRC to DST (same filesystem).

    Outputs JSON with keys: src, dst, error.
    """
    from projspec.filebrowser import move

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(move(src, dst, storage_options=so)))


@filebrowser.command("mkdir")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
def fb_mkdir(url, storage_options):
    """Create a directory.  No-op on object stores.

    Outputs JSON with keys: url, error.
    """
    from projspec.filebrowser import mkdir

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(mkdir(url, storage_options=so)))


@filebrowser.command("add-to-library")
@click.argument("url")
@click.option("--storage-options", default="", help="fsspec storage options as JSON")
def fb_add_to_library(url, storage_options):
    """Scan URL with projspec and add it to the project library.

    Outputs JSON with keys: url, project, error.
    """
    from projspec.filebrowser import add_to_projspec_library

    so = json.loads(storage_options) if storage_options.strip() else None
    print(json.dumps(add_to_projspec_library(url, storage_options=so)))


@filebrowser.command("protocols")
def fb_protocols():
    """List fsspec protocols available in this environment.

    Outputs JSON list of protocol strings.
    """
    from projspec.filebrowser import supported_protocols

    print(json.dumps(supported_protocols()))


@filebrowser.group("bookmarks")
def fb_bookmarks():
    """Manage file browser bookmarks."""


@fb_bookmarks.command("list")
def fb_bookmarks_list():
    """List saved bookmarks as JSON."""
    from projspec.filebrowser import bookmarks_list

    print(json.dumps(bookmarks_list()))


@fb_bookmarks.command("add")
@click.argument("url")
@click.option("--label", default="", help="Human-readable label for the bookmark")
def fb_bookmarks_add(url, label):
    """Add or update a bookmark.  Returns updated bookmarks list as JSON."""
    from projspec.filebrowser import bookmark_add

    print(json.dumps(bookmark_add(url, label=label)))


@fb_bookmarks.command("remove")
@click.argument("url")
def fb_bookmarks_remove(url):
    """Remove a bookmark by URL.  Returns updated bookmarks list as JSON."""
    from projspec.filebrowser import bookmark_remove

    print(json.dumps(bookmark_remove(url)))


@main.command("serve")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind the server to.",
)
@click.option(
    "--port",
    default=0,
    show_default=True,
    type=int,
    help="TCP port (0 = pick a free port automatically).",
)
@click.option(
    "--port-file",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help=(
        "If given, write the chosen port number to this file once the server "
        "is ready.  Useful for callers that need to discover a dynamically "
        "assigned port."
    ),
)
def serve(host, port, port_file):
    """Start the projspec HTTP server (requires fastapi + uvicorn).

    The server exposes all projspec and filebrowser operations as JSON
    endpoints so that callers (e.g. the VS Code extension) can avoid the
    overhead of spawning a new Python process for each operation.

    Install the required extras with:

        pip install 'projspec[serve]'
    """
    from projspec.server import run

    run(host=host, port=port, port_file=port_file)


if __name__ == "__main__":
    main()
