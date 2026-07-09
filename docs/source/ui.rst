UI Support
==========

`projspec` is designed to be useful in whichever interface you are already using —
it comes to you rather than the other way around. As well as the Python library
interface (which provides full power and flexibility), the following interactive
surfaces are supported. All graphical interfaces share the same HTML/CSS/JavaScript
panel rendered through their respective browser embedding, so they look and behave
identically wherever possible.


CLI
---

Installing `projspec` makes the ``projspec`` command available. The same binary is
called as a subprocess by the PyCharm and Anaconda-Desktop plugins. Unlike the rest
of the UIs described here, the CLI is already covered in the :ref:`quickstart`. Text
feedback is given for every command and parameter.

Here is the full tree of possible commands:

.. code-block:: text

    projspec
        config      Interact with the projspec config.
            defaults  Show default config settings and short descriptions.
            get       Get a value from the config.
            set       Set a config value.
            show      Show all contents of the config.
            unset     Remove a value from the config (returns to default).
        create      Create a new project of the given type in the given path.
        filebrowser File browser operations via fsspec (local and remote).
            browse              List the contents of a directory URL.
            inspect             File metadata and text/intake preview.
            read-file           Read a file and emit its contents as JSON.
            write-file          Write content to a file (create or overwrite).
            delete              Delete a file or directory.
            move                Move / rename a file or directory.
            mkdir               Create a directory.
            add-to-library      Scan a URL and add it to the project library.
            protocols           List available fsspec protocols.
            bookmarks
                list            List saved bookmarks.
                add             Add or update a bookmark.
                remove          Remove a bookmark by URL.
        info        Documentation about all the classes within projspec.
        library     Interact with the project library.
            clear    Clear all contents of the library.
            delete   Delete a project at the given URL from the library.
            list     Show contents of the library.
        make        Make the given artifact in the project at the given path.
        scan        Scan the given path for projects, and display.
        serve       Start the projspec HTTP server (see below).
        version     Show version and quit.

A dedicated ``projspec-server`` console script is also installed; it is equivalent
to ``projspec serve`` and is the command started automatically by the VS Code
extension (see *HTTP server* below).


HTTP server
-----------

Repeated subprocess spawning for every operation is slow, especially on cold-start.
To eliminate this overhead the VS Code extension (and any other UI that chooses to)
can run a persistent HTTP server:

.. code-block:: bash

    $ projspec-server                       # picks a free port automatically
    $ projspec-server --port 8765           # fixed port
    $ projspec-server --port 0 --port-file /tmp/projspec.port   # write chosen port to file

Install the required extras first:

.. code-block:: bash

    $ pip install 'projspec[serve]'

The server exposes every ``projspec`` and ``filebrowser`` operation as a JSON
endpoint (FastAPI/uvicorn). The VS Code extension starts it automatically on launch
using ``projspec-server --port 0 --port-file <tmp>``, reads the port from the file,
and falls back transparently to direct subprocess calls if the server is not
available (i.e. if ``fastapi``/``uvicorn`` are not installed).


GUIs
----

Each GUI consists of two tabs described below. The VSCode extension is the
most fully featured; all others use the same shared HTML/CSS/JS for the
webview-based hosts (Qt, PyCharm/IDEA, ipywidget) and a native Textual
equivalent for the terminal UI.

Project Library tab
~~~~~~~~~~~~~~~~~~~

The searchable list on the left shows every project that has been scanned,
either via the GUI or the CLI. For each project it displays:

* **Title** (directory basename) and full URL.
* Any stored ``storage_options`` (credentials / fsspec kwargs for remote filesystems).
* File count, total size, writable flag and last-modified / last-scanned timestamps.
* **Coloured chips** — one labelled *Global* (for project-level contents and artifacts)
  and one per matched spec type (e.g. *git_repo*, *pixi*, *python_library*). Clicking
  a chip populates the Details pane with information for that spec.

A **⋮ kebab menu** on each project offers:

* *Open with VSCode / PyCharm / Jupyter* — launches the project in that tool.
* *Show in file browser* — (VSCode only) switches to the File Browser tab and
  navigates to the project root, including any stored ``storage_options``.
* *Rescan* — re-runs ``projspec scan`` on the project and refreshes the library entry.
* *Create spec* — opens a typeahead picker to scaffold a new spec type inside the
  project directory.
* *Remove from library* — deletes the entry from the library.

The toolbar above the list provides **Add**, **Reload**, and **Configure** buttons.
"Add" opens the system file picker (local) or an Advanced dialog that accepts any
fsspec URL or glob pattern plus optional JSON ``storage_options``, making it possible
to add remote projects (S3, GCS, HTTP, …) without using the CLI.

Details tab / pane
~~~~~~~~~~~~~~~~~~

Clicking a chip switches the right-hand Details pane to show the contents and
artifacts for that spec (or the project-level global items). The pane is fully
cleared whenever the selection changes or the library is reloaded.

Each **Content widget** (blue border, *CONTENT* badge) displays the structured
information extracted by that spec — environments, metadata, dataset schema,
``html_repr`` thumbnails, etc. For dataset content produced by ``intake``, technical
reader fields (``reader_used``, ``reader_tier``, ``readers_attempted``, ``readers``,
``errors``) are folded into a collapsed **Reader info** disclosure at the bottom of
the widget rather than shown inline.

Each **Artifact widget** (amber border, *ARTIFACT* badge) displays the artifact
description. A **▶ Make** button runs the artifact's action (build, install, start
service, …) in a terminal. A **➡ Reveal** button is shown for local file artifacts;
it highlights the file in the IDE or OS file manager. An **ℹ Info** button shows the
class docstring in a small popup.


File Browser tab
~~~~~~~~~~~~~~~~

All graphical UIs now include a **File Browser** tab alongside the Project Library:

* Browse any fsspec-supported URL (local, S3, GCS, HTTP, FTP, …).
* Bookmarks with custom labels and stored ``storage_options``.
* Breadcrumb navigation, back/up buttons, sortable columns.
* Per-file info panel: MIME type, size, date; text preview; dataset summaries
  (schema, column types, HTML repr, thumbnail) for files recognised by
  ``intake``. Reader-specific fields are folded into a collapsed "Reader info"
  section.
* Per-directory embedded projspec scan panel showing matched specs and
  contents/artifacts inline.
* **+ Library** button — adds the selected directory to the Project Library and
  switches to the Library tab with the new entry selected.
* File operations: create, rename, delete, create folder, open in editor
  (remote files are fetched to a temp file; saves are written back).
* Storage-options (🔑) dialog for per-session credentials.
* Persistent bookmarks stored in ``~/.config/projspec/filebrowser_bookmarks.json``.

In the TUI the File Browser is implemented as a native Textual pane (no
webview); it lists directory entries, shows file metadata and projspec scan
results inline, and supports "Add to Library" with automatic tab-switch.

Cross-tab interactions (all UIs):

* Kebab → *Show in file browser* on a library project switches to the File
  Browser tab and navigates to that URL (including stored ``storage_options``).
* *+ Library* in the File Browser reloads the Library tab and selects the new
  entry.


VSCode
~~~~~~

The extension is available in the VS Code marketplace; you will also need
``projspec`` (the CLI) available on your ``PATH``.

The combined panel hosts two **tabs** in a single editor pane:

**Project Library tab**
  The standard two-pane library and details view described above. Launch with the
  *"Open projspec Panel"* command (⇧⌘P → ``projspec``), or click the projspec
  icon in the Activity Bar sidebar.

**File Browser tab**
  A full fsspec-backed file browser. Features include:

  * Browse any fsspec-supported URL (local, S3, GCS, HTTP, FTP, …).
  * Bookmarks / favourites with custom labels and stored ``storage_options``.
  * Breadcrumb navigation; double-click a directory to descend; Back / Up buttons.
  * Column headers for Name / Size / Modified with sortable click-to-sort.
  * Lazy-expanding tree (click the ▶ arrow to expand a sub-directory in-place).
  * Per-file info panel: MIME type, size, modified date; text preview for text files;
    dataset summary (schema, column types, HTML repr, thumbnail) for data files
    recognised by ``intake``.
  * Per-directory projspec scan panel embedded below the file info.
  * **+ Library** button — adds the selected directory to the Project Library and
    immediately switches to the Library tab with the new entry selected and
    highlighted.
  * File operations: create, rename, delete, create folder.
  * Open any text file in the VS Code editor (remote files are fetched to a temp
    file; saves are pushed back automatically).
  * Storage-options dialog (🔑) for supplying credentials per session.
  * Persistent bookmarks stored in ``~/.config/projspec/filebrowser_bookmarks.json``.

  Launch with *"Open File Browser"* or *"Open File Browser at Current Folder"* from
  the command palette, or by clicking the *File Browser* tab in the panel.

Cross-tab interactions:

* Kebab → *Show in file browser* on a library project switches to the File Browser
  tab and navigates to that project's URL root (with its stored ``storage_options``).
* *+ Library* in the File Browser reloads the Library tab and selects the new entry.

For fast operation the extension starts a persistent ``projspec-server`` process in
the background (requires ``pip install 'projspec[serve]'``). All requests are served
over HTTP instead of spawning a new Python process each time. If the server is
unavailable the extension falls back automatically to subprocess calls.


Notebooks
~~~~~~~~~

Requires the optional ``anywidget`` package; install via:

.. code-block:: bash

    $ pip install projspec[ipywidget]

Instances of ``ProjectLibrary`` render automatically using the combined panel
with both tabs — Project Library and File Browser:

.. code-block:: python

    from projspec.library import ProjectLibrary

    lib = ProjectLibrary()
    lib  # displays the two-tab panel in a Jupyter cell

The File Browser tab uses the kernel's filesystem (via ``filebrowser.py``
in-process), so it can browse any path or remote URL that the kernel can
reach. All filebrowser operations (browse, inspect, create/delete/rename,
bookmarks, "Add to Library") work in the notebook exactly as they do in
the other GUIs.

The widget works in JupyterLab, Jupyter Notebook classic, VS Code notebooks,
Google Colab, and marimo.


Jupyter extension
~~~~~~~~~~~~~~~~~

The separate ``jupyter-projspec`` repository implements a JupyterLab / Jupyter
classic extension that shows the projspec summary for any directory you navigate to
in the file browser. It also works for directories opened via ``jupyter-fs`` panels,
making remote directory scanning possible. There is currently no integration with the
project library (unlike the other UIs); use the Notebook interface above for that.

.. _jupyter-projspec: https://github.com/fsspec/jupyter-projspec
.. _jupyter-fs: https://github.com/jpmorganchase/jupyter-fs

Note that the kernel running a given notebook is not necessarily on the same machine
as the server providing the interface. "Make" on artifacts will only work for
projects local to the server process.


PyCharm / IDEA
~~~~~~~~~~~~~~

The plugin is available via the JetBrains Marketplace; you will also need
``projspec`` (the CLI) available on your ``PATH``. The same plugin works in any
JetBrains IDE (IntelliJ IDEA, PyCharm Professional, DataSpell, etc.).

The tool window (*View → Tool Windows → Project Library*) now hosts both
tabs. It uses the shared HTML/CSS/JS rendered inside JCEF (the Chromium engine
bundled with JetBrains IDEs); the HTML is generated by calling
``projspec.webui.get_combined_html()`` at startup so it is always in sync with
the other UIs. The File Browser tab calls ``projspec filebrowser`` subcommands
via the CLI, exactly as the library tab calls ``projspec library``, ``projspec
scan``, etc. All actions work identically to the VS Code extension.

A ``CLI path`` setting in *Settings → Tools → Projspec* lets you point to a
non-PATH ``projspec`` binary.


TUI
~~~

Bundled with the ``projspec`` package as the command ``projspec-tui``; requires
``textual``:

.. code-block:: bash

    $ pip install projspec[textual]

Key bindings: ``a`` Add, ``r`` Reload, ``/`` focus search, ``q`` Quit.

The TUI now has two tabs (``TabbedContent``): **Project Library** and **File
Browser**. The File Browser tab is implemented with native Textual widgets (not
a webview) — it lists directory entries, shows file metadata and an inline
projspec scan result, and supports "Add to Library" with an automatic switch to
the Library tab. There is no native system file picker; paths and remote URLs
must be typed into the text-box. When a *Make* artifact action runs, its output
appears in the terminal until the subprocess finishes. This interface is
considered more experimental than the rest.


Qt app
~~~~~~

Bundled with the ``projspec`` package as the command ``projspec-qt``; requires Qt:

.. code-block:: bash

    $ pip install projspec[qt]

The Qt app now renders the combined two-tab panel using ``QWebEngineView``. A
second ``QWebChannel`` bridge (``fb_bridge``) handles filebrowser messages, with
all file operations performed in-process via ``projspec.filebrowser``. Like the
notebook widget, the Qt app makes all calls in-process (no subprocess spawning),
making it the most responsive host. It is included primarily as the simplest
example of embedding the full projspec UI into a custom Qt application.


Anaconda Desktop
~~~~~~~~~~~~~~~~

An integration with Anaconda Desktop exists in an internal, experimental form.
Expect it to become public eventually, with Anaconda-specific functionality.
