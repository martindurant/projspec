/* projspec filebrowser panel — transport-agnostic.
 *
 * Like panel.js this script requires the host to set
 *   window.projspecFbTransport = { send(msg), onReady(dispatch) }
 * BEFORE this script runs.  The transport bridges JS↔host for all
 * filebrowser operations (browse, inspect, createFile, …).
 *
 * Optionally set window.projspecFbRoot to scope DOM queries to a
 * subtree (needed when the panel is embedded alongside other content).
 *
 * Hosts that embed the library panel inside the scan pane must also set
 * window.projspecFbPanelBootstrap = bootstrapFn (called once to
 * initialise the embedded panel.js instance for directory scan results).
 */
(function() {
    // ── Transport -----------------------------------------------------------
    const _transport = window.projspecFbTransport || {
        send: (msg) => { console.warn('projspec-fb: no transport configured', msg); },
        onReady: (dispatch) => { window.__projspecFbDeliver = dispatch; },
    };

    // Root scoping (mirrors panel.js pattern)
    const _fbRoot = window.projspecFbRoot || document;
    function $fbId(id) {
        if (_fbRoot === document) return document.getElementById(id);
        return _fbRoot.querySelector('#' + CSS.escape(id));
    }

    // Outbound (JS → host)
    const vscode = { postMessage: (msg) => _transport.send(msg) };

    // Show any uncaught JS errors as a red banner
    window.onerror = function(msg, src, line, col, err) {
        var d = document.createElement('div');
        d.style.cssText = 'position:fixed;top:0;left:0;right:0;padding:10px;background:#c00;color:#fff;font-family:monospace;font-size:12px;z-index:9999;white-space:pre-wrap;';
        d.textContent = 'FB error: ' + msg + ' (' + src + ':' + line + ')';
        document.body.appendChild(d);
    };

    // ── debug log ─────────────────────────────────────────────────────────
    const debugEl = $fbId('fb-debug');
    function dbg(msg) {
        if (debugEl) {
            const line = document.createElement('div');
            line.textContent = '[' + new Date().toISOString().slice(11,23) + '] ' + msg;
            debugEl.appendChild(line);
            debugEl.scrollTop = debugEl.scrollHeight;
        }
        console.log('[fb] ' + msg);
    }
    dbg('script started');

    // ── state ──────────────────────────────────────────────────────────────
    let bookmarks = [];
    let protocols = [];
    var libraryUrls = new Set();  // canonical URLs of entries in the project library
    var selectedIsFile = false;   // true when the current selection is a file (not a dir)
    let currentUrl = '';
    let currentSo  = '';
    let history    = [];
    let histIdx    = -1;
    let selected   = null;
    let newentryMode = 'file';

    // ── DOM refs ───────────────────────────────────────────────────────────
    // NOTE: #fb-entries is the scrollable entry list. #fb-empty and
    // #fb-error are siblings of #fb-entries inside #fb-file-list and must
    // NEVER be cleared by setting innerHTML on their parent.
    const entriesEl   = $fbId('fb-entries');
    const emptyEl     = $fbId('fb-empty');
    const errorEl     = $fbId('fb-error');
    const urlInput    = $fbId('fb-url-input');
    const breadcrumb  = $fbId('fb-breadcrumb');
    const spinner     = $fbId('fb-spinner');
    const infoTitle   = $fbId('fb-info-title');
    const infoActions = $fbId('fb-info-actions');
    const infoMeta    = $fbId('fb-info-meta');
    const infoPreview = $fbId('fb-info-preview');
    const scanPane      = $fbId('fb-scan-pane');
    const scanStatus    = $fbId('fb-scan-status');
    const scanPanelRoot = $fbId('fb-scan-panel-root');
    const fileContent   = $fbId('fb-file-content');
    const bmPanel     = $fbId('bm-panel');
    const bmList      = $fbId('bm-list');
    const soOverlay   = $fbId('so-overlay');
    const soInput     = $fbId('so-input');
    const neOverlay   = $fbId('newentry-overlay');
    const neTitle     = $fbId('newentry-title');
    const neInput     = $fbId('newentry-name');
    const renOverlay  = $fbId('rename-overlay');
    const renInput    = $fbId('rename-input');

    // Verify critical elements exist
    const missing = ['fb-entries','fb-empty','fb-error','fb-url-input','fb-breadcrumb',
                     'fb-spinner','fb-info-title','fb-info-actions'].filter(id => !$fbId(id));
    if (missing.length) { dbg('ERROR: missing elements: ' + missing.join(', ')); }
    else { dbg('all DOM elements found'); }

    // ── utilities ──────────────────────────────────────────────────────────
    function basename(url) {
        const s = (url || '').replace(/\/+$/, '');
        const i = s.lastIndexOf('/');
        return i >= 0 ? s.slice(i + 1) : s;
    }
    function parentUrl(url) {
        const s = (url || '').replace(/\/+$/, '');
        const protoEnd = s.indexOf('://');
        if (protoEnd >= 0) {
            const pathPart = s.slice(protoEnd + 3);
            const slash = pathPart.lastIndexOf('/');
            if (slash <= 0) return s.slice(0, protoEnd + 3) || s;
            return s.slice(0, protoEnd + 3 + slash);
        }
        const slash = s.lastIndexOf('/');
        if (slash <= 0) return '/';
        return s.slice(0, slash);
    }
    function fmtSize(bytes) {
        if (bytes == null) return '';
        const u = ['B','KB','MB','GB','TB'];
        let n = parseFloat(bytes);
        for (let i = 0; i < u.length; i++) {
            if (n < 1024 || i === u.length - 1) return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
            n /= 1024;
        }
    }
    function fmtDate(ts) {
        if (!ts) return '';
        const d = new Date(parseFloat(ts) * 1000);
        return d.toLocaleString();
    }
    function escHtml(s) {
        return String(s || '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function fileIcon(entry, inLibrary) {
        if (entry.type === 'directory') return inLibrary ? '\uD83D\uDDC2\uFE0F' : '\uD83D\uDCC1'; // 🗂️ or 📁
        const name = (entry.basename || entry.name || '').toLowerCase();
        if (/\.(py|pyx|pyi)$/.test(name))                   return '\uD83D\uDC0D'; // snake
        if (/\.(js|ts|jsx|tsx)$/.test(name))                 return '\uD83D\uDCDC'; // scroll
        if (/\.(json|yaml|yml|toml|ini|cfg)$/.test(name))    return '\u2699\uFE0F'; // gear
        if (/\.(md|rst|txt|org)$/.test(name))                return '\uD83D\uDCC4'; // page
        if (/\.(csv|tsv|parquet|hdf5?|nc|zarr|feather)$/.test(name)) return '\uD83D\uDCCA'; // chart
        if (/\.(png|jpg|jpeg|gif|svg|webp|bmp|tiff?)$/.test(name))   return '\uD83D\uDDBC\uFE0F'; // picture
        if (/\.(zip|tar|gz|bz2|xz|7z|rar)$/.test(name))     return '\uD83D\uDCE6'; // package
        if (/\.(sh|bash|zsh|fish|ps1|bat|cmd)$/.test(name)) return '\uD83D\uDCBB'; // computer
        return '\uD83D\uDCC4'; // page
    }

    // ── browse result ──────────────────────────────────────────────────────
    // ── tree rendering ─────────────────────────────────────────────────────

    // Sort state
    var sortCol = 'name';   // 'name' | 'size' | 'mtime'
    var sortAsc = true;
    // Last browse entries (root level) — kept for re-sort without re-fetch
    var lastBrowseEntries = [];

    function sortEntries(entries) {
        // Stable sort: directories always before files, then by chosen column
        function key(e) {
            if (sortCol === 'size')  return e.size  == null ? -1 : e.size;
            if (sortCol === 'mtime') return e.last_modified == null ? 0 : parseFloat(e.last_modified);
            // name: case-insensitive
            return (e.basename || basename(e.name || '')).toLowerCase();
        }
        return entries.slice().sort(function(a, b) {
            var aDir = a.type === 'directory' ? 0 : 1;
            var bDir = b.type === 'directory' ? 0 : 1;
            if (aDir !== bDir) return aDir - bDir;  // dirs before files always
            var ak = key(a), bk = key(b);
            var cmp = ak < bk ? -1 : ak > bk ? 1 : 0;
            return sortAsc ? cmp : -cmp;
        });
    }

    function updateSortHeaders() {
        (_fbRoot === document ? document : _fbRoot).querySelectorAll('.fb-col-hdr').forEach(function(el) {
            var col = el.dataset.col;
            var arrow = el.querySelector('.fb-sort-arrow');
            if (col === sortCol) {
                el.classList.add('active');
                if (arrow) arrow.textContent = sortAsc ? '\u25B4' : '\u25BE'; // ▴ ▾
            } else {
                el.classList.remove('active');
                if (arrow) arrow.textContent = '';
            }
        });
    }

    // Wire up column header clicks
    (_fbRoot === document ? document : _fbRoot).querySelectorAll('.fb-col-hdr').forEach(function(el) {
        el.addEventListener('click', function() {
            var col = el.dataset.col;
            if (col === sortCol) {
                sortAsc = !sortAsc;
            } else {
                sortCol = col;
                sortAsc = col === 'name';  // name defaults asc, size/mtime default desc
            }
            updateSortHeaders();
            // Re-render root entries with new sort (no network call)
            if (lastBrowseEntries.length > 0) {
                treeNodes = {};
                entriesEl.innerHTML = '';
                var sorted = sortEntries(lastBrowseEntries);
                for (var i = 0; i < sorted.length; i++) {
                    entriesEl.appendChild(makeEntryRow(sorted[i], 0));
                }
            }
        });
    });
    updateSortHeaders();

    // Map from directory URL -> child container element (for expand/collapse)
    var treeNodes = {};

    function makeEntryRow(entry, depth) {
        var isDir = entry.type === 'directory';
        var url   = entry.name;

        // Outer wrapper: row + (for dirs) a children container
        var wrapper = document.createElement('div');
        wrapper.className = 'fb-entry-wrapper';

        var row = document.createElement('div');
        row.className = 'fb-entry' + (isDir ? ' is-dir' : '');
        row.dataset.url  = url;
        row.dataset.type = entry.type || 'file';

        // Name cell: indent + toggle + icon + name
        var nameCell = document.createElement('span');
        nameCell.className = 'fb-entry-name-cell';

        var indent = document.createElement('span');
        indent.className = 'fb-entry-indent';
        indent.style.width = (depth * 12) + 'px';
        nameCell.appendChild(indent);

        var toggle = document.createElement('span');
        toggle.className = 'fb-entry-toggle';
        toggle.textContent = isDir ? '\u25B6' : '';  // ▶ for dirs, blank for files
        nameCell.appendChild(toggle);

        var icon = document.createElement('span');
        icon.className = 'fb-entry-icon';
        icon.textContent = fileIcon(entry, isDir && libraryUrls.has(url));
        nameCell.appendChild(icon);

        var nameEl = document.createElement('span');
        nameEl.className = 'fb-entry-name';
        nameEl.textContent = entry.basename || basename(url);
        nameEl.title = url;
        nameCell.appendChild(nameEl);

        // Size cell
        var sizeEl = document.createElement('span');
        sizeEl.className = 'fb-entry-size';
        if (entry.size != null) sizeEl.textContent = fmtSize(entry.size);

        // Modified cell
        var mtimeEl = document.createElement('span');
        mtimeEl.className = 'fb-entry-mtime';
        if (entry.last_modified) mtimeEl.textContent = fmtMtime(entry.last_modified);

        row.appendChild(nameCell);
        row.appendChild(sizeEl);
        row.appendChild(mtimeEl);

        // Children container (lazy-populated)
        var childrenEl = null;
        if (isDir) {
            childrenEl = document.createElement('div');
            childrenEl.className = 'fb-children';
            treeNodes[url] = childrenEl;
        }

        // Click: select
        row.addEventListener('click', function(e) {
            e.stopPropagation();
            selectEntry(url, entry.type, row);
        });

        // Toggle click: expand/collapse (stop propagation so row click doesn't fire)
        if (isDir) {
            toggle.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleDir(url, toggle, childrenEl);
            });
            // Double-click on row: navigate to dir as new root
            row.addEventListener('dblclick', function(e) {
                e.stopPropagation();
                navigateTo(url, currentSo);
            });
        }

        wrapper.appendChild(row);
        if (childrenEl) wrapper.appendChild(childrenEl);
        return wrapper;
    }

    function fmtMtime(ts) {
        if (!ts) return '';
        var d = new Date(parseFloat(ts) * 1000);
        var now = new Date();
        var diffDays = (now - d) / 86400000;
        if (diffDays < 1) {
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        if (diffDays < 180) {
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        }
        return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
    }

    function toggleDir(url, toggle, childrenEl) {
        var expanded = childrenEl.classList.contains('expanded');
        if (expanded) {
            // Collapse
            childrenEl.classList.remove('expanded');
            toggle.textContent = '\u25B6';  // ▶
        } else {
            // Expand: lazy-load if not yet populated
            childrenEl.classList.add('expanded');
            toggle.textContent = '\u25BC';  // ▼
            if (!childrenEl.dataset.loaded) {
                // Show loading indicator
                var loader = document.createElement('div');
                loader.className = 'fb-child-loading';
                loader.textContent = 'Loading...';
                childrenEl.appendChild(loader);
                // Request children from host
                vscode.postMessage({ cmd: 'expandDir', url: url, storageOptions: currentSo || undefined });
            }
        }
    }

    function handleExpandResult(data) {
        var parentUrl = data.parentUrl || data.url;
        var childrenEl = treeNodes[parentUrl];
        if (!childrenEl) { dbg('no treeNode for ' + parentUrl); return; }

        childrenEl.innerHTML = '';
        childrenEl.dataset.loaded = '1';

        if (data.error) {
            var errEl = document.createElement('div');
            errEl.className = 'fb-child-loading';
            errEl.textContent = 'Error: ' + data.error;
            childrenEl.appendChild(errEl);
            return;
        }

        var entries = data.entries || [];
        if (entries.length === 0) {
            var emptyMsg = document.createElement('div');
            emptyMsg.className = 'fb-child-loading';
            emptyMsg.textContent = 'Empty';
            childrenEl.appendChild(emptyMsg);
            return;
        }

        // Find the depth of the parent by looking at the DOM
        var parentRow = childrenEl.previousSibling;
        var parentIndent = parentRow ? (parseInt(parentRow.querySelector('.fb-entry-indent').style.width) || 0) : 0;
        var childDepth = Math.round(parentIndent / 12) + 1;

        var sorted = sortEntries(entries);
        for (var i = 0; i < sorted.length; i++) {
            childrenEl.appendChild(makeEntryRow(sorted[i], childDepth));
        }
        dbg('expanded ' + parentUrl + ': ' + sorted.length + ' children');
    }

    function renderBrowse(data) {
        dbg('renderBrowse url=' + data.url + ' entries=' + (data.entries ? data.entries.length : 'none') + ' error=' + data.error);
        currentUrl = data.url || '';
        urlInput.value = currentUrl;
        renderBreadcrumb(currentUrl);

        // Reset tree node map and stored entries
        treeNodes = {};
        lastBrowseEntries = [];

        entriesEl.innerHTML = '';
        emptyEl.classList.add('hidden');
        errorEl.classList.add('hidden');

        if (data.error) {
            errorEl.textContent = 'Error: ' + data.error;
            errorEl.classList.remove('hidden');
            return;
        }

        var entries = data.entries || [];
        if (entries.length === 0) {
            emptyEl.classList.remove('hidden');
            return;
        }

        lastBrowseEntries = entries;
        var sorted = sortEntries(entries);
        for (var i = 0; i < sorted.length; i++) {
            entriesEl.appendChild(makeEntryRow(sorted[i], 0));
        }
        dbg('rendered ' + sorted.length + ' entries');
    }

    function selectEntry(url, type, rowEl) {
        (_fbRoot === document ? document : _fbRoot).querySelectorAll('.fb-entry.active').forEach(function(el) { el.classList.remove('active'); });
        if (rowEl) rowEl.classList.add('active');
        selected = { url: url, type: type, so: currentSo };
        dbg('selected ' + type + ': ' + url);

        infoTitle.textContent = basename(url);
        infoActions.classList.remove('hidden');

        const isFile = type !== 'directory';
        selectedIsFile = isFile;
        $fbId('btn-open-editor').style.display = isFile ? '' : 'none';
        $fbId('btn-add-to-lib').style.display = type === 'directory' ? '' : 'none';

        infoMeta.innerHTML = '';
        infoPreview.innerHTML = '';

        // Reset both scan pane variants on every selection change.
        // Also send an empty library to the embedded panel so the previous
        // directory's project widget and details are cleared immediately.
        if (scanPane) {
            scanPane.classList.add('hidden');
            if (scanStatus) scanStatus.textContent = '';
        }
        if (scanPanelRoot) scanPanelRoot.classList.remove('hidden');
        if (fileContent)   { fileContent.classList.add('hidden'); fileContent.innerHTML = ''; }
        if (typeof window.__fbPanelDeliver === 'function') {
            window.__fbPanelDeliver({ type: 'data', library: {}, info: {}, enums: {} });
        }

        if (isFile) {
            // Show scan pane immediately with loading indicator — it gets populated
            // when both inspectResult and projectScanned arrive
            if (scanPane) {
                scanPane.classList.remove('hidden');
                if (scanStatus) scanStatus.textContent = 'inspecting...';
            }
            dbg('posting inspect for ' + url);
            vscode.postMessage({ cmd: 'inspect', url: url, storageOptions: currentSo || undefined });
        } else {
            renderMeta({ type: 'directory', url: url });
            // Trigger projspec scan on directory selection
            if (scanPane) {
                scanPane.classList.remove('hidden');
                if (scanStatus) scanStatus.textContent = 'scanning...';
            }
            dbg('posting scanDir for ' + url);
            vscode.postMessage({ cmd: 'scanDir', url: url, storageOptions: currentSo || undefined });
        }
    }

    function renderBreadcrumb(url) {
        breadcrumb.innerHTML = '';
        if (!url) return;
        const protoMatch = url.match(/^([a-z][a-z0-9+.\-]*):\/\//i);
        let proto = '';
        let rest = url;
        if (protoMatch) {
            proto = protoMatch[0];
            rest = url.slice(proto.length);
        }
        const parts = rest.replace(/\/+$/, '').split('/').filter(Boolean);
        if (proto) {
            const link = document.createElement('span');
            link.className = 'bc-seg';
            link.textContent = proto;
            link.title = proto;
            link.addEventListener('click', function() { navigateTo(proto, currentSo); });
            breadcrumb.appendChild(link);
        }
        let accumulated = proto;
        for (let i = 0; i < parts.length; i++) {
            accumulated += (accumulated.slice(-1) === '/' ? '' : '/') + parts[i];
            const sep = document.createElement('span');
            sep.className = 'bc-sep';
            sep.textContent = ' / ';
            breadcrumb.appendChild(sep);
            const link = document.createElement('span');
            link.className = 'bc-seg';
            link.textContent = parts[i];
            const target = accumulated;
            link.title = target;
            link.addEventListener('click', function() { navigateTo(target, currentSo); });
            breadcrumb.appendChild(link);
        }
    }

     function renderMeta(data) {
        infoMeta.innerHTML = '';
        var rows = [];
        // Don't show data.type — it's the JS message type, not a file type
        if (data.size != null)  rows.push(['Size', fmtSize(data.size)]);
        if (data.last_modified) rows.push(['Modified', fmtDate(data.last_modified)]);
        if (data.mime_type)     rows.push(['MIME', data.mime_type]);
        for (var i = 0; i < rows.length; i++) {
            const row = document.createElement('div');
            row.className = 'info-row';
            row.innerHTML = '<span class="info-key">' + escHtml(rows[i][0]) + '</span><span>' + escHtml(String(rows[i][1])) + '</span>';
            infoMeta.appendChild(row);
        }
    }

    function renderInspect(data) {
        dbg('renderInspect name=' + data.name + ' error=' + data.error);
        // For files: only show MIME in the meta strip — size/modified are already
        // visible in the file tree columns and would be redundant here.
        // The intake type and text preview now live in the scan pane below.
        infoMeta.innerHTML = '';
        if (data.mime_type) {
            const row = document.createElement('div');
            row.className = 'info-row';
            row.innerHTML = '<span class="info-key">MIME</span><span>' + escHtml(data.mime_type) + '</span>';
            infoMeta.appendChild(row);
        }
        // Clear preview — content is in the scan pane
        infoPreview.innerHTML = '';
    }

    // ── navigation ─────────────────────────────────────────────────────────
    function navigateTo(url, so, push) {
        const shouldPush = push !== false;
        dbg('navigateTo push=' + shouldPush + ' url=' + url);
        vscode.postMessage({ cmd: 'browse', url: url, storageOptions: so || undefined, push: shouldPush });
        selected = null;
        infoTitle.textContent = 'Loading...';
        infoActions.classList.add('hidden');
        infoMeta.innerHTML = '';
        infoPreview.innerHTML = '';
    }

    // ── bookmarks ──────────────────────────────────────────────────────────
    function refreshLibraryBadges() {
        // Walk every directory row in the DOM and swap the folder icon
        document.querySelectorAll('.fb-entry[data-type="directory"]').forEach(function(row) {
            var url = row.dataset.url || '';
            var iconEl = row.querySelector('.fb-entry-icon');
            if (!iconEl) return;
            iconEl.textContent = libraryUrls.has(url) ? '\uD83D\uDDC2\uFE0F' : '\uD83D\uDCC1'; // 🗂️ or 📁
        });
    }

    function renderBookmarks() {
        bmList.innerHTML = '';
        if (!bookmarks.length) {
            const e = document.createElement('div');
            e.className = 'bm-empty';
            e.textContent = 'No bookmarks yet.';
            bmList.appendChild(e);
            return;
        }
        for (var i = 0; i < bookmarks.length; i++) {
            const bm = bookmarks[i];
            const row = document.createElement('div');
            row.className = 'bm-item';
            const info = document.createElement('div');
            info.style.flex = '1';
            info.style.overflow = 'hidden';
            const lbl = document.createElement('div');
            lbl.className = 'bm-item-label';
            lbl.textContent = bm.label || bm.url;
            const urlDiv = document.createElement('div');
            urlDiv.className = 'bm-item-url';
            urlDiv.textContent = bm.url;
            info.appendChild(lbl);
            info.appendChild(urlDiv);
            // Show a key icon if the bookmark has stored storage_options
            if (bm.storage_options && Object.keys(bm.storage_options).length > 0) {
                const soTag = document.createElement('div');
                soTag.className = 'bm-item-so';
                soTag.title = 'Saved storage options: ' + JSON.stringify(bm.storage_options);
                soTag.textContent = '\uD83D\uDD11 credentials stored';
                info.appendChild(soTag);
            }
            const rm = document.createElement('button');
            rm.className = 'bm-remove';
            rm.title = 'Remove bookmark';
            rm.textContent = 'X';
            const bmUrl = bm.url;
            // Serialise the bookmark's own storage_options as a JSON string (or '').
            // These are scoped to this URL only — they replace currentSo entirely
            // when navigating to a different protocol/host.
            const bmSo = (bm.storage_options && Object.keys(bm.storage_options).length > 0)
                ? JSON.stringify(bm.storage_options) : '';
            rm.addEventListener('click', function(e) {
                e.stopPropagation();
                vscode.postMessage({ cmd: 'removeBookmark', url: bmUrl });
            });
            row.appendChild(info);
            row.appendChild(rm);
            row.addEventListener('click', function(ev) {
                if (ev.target === rm) return;
                bmPanel.classList.add('hidden');
                // Switch currentSo to the bookmark's own options (may be empty)
                currentSo = bmSo;
                navigateTo(bmUrl, bmSo);
            });
            bmList.appendChild(row);
        }
    }

    // ── toolbar ────────────────────────────────────────────────────────────
    $fbId('btn-back').addEventListener('click', function() {
        if (histIdx > 0) {
            histIdx--;
            const h = history[histIdx];
            currentSo = h.so;
            dbg('back to ' + h.url);
            vscode.postMessage({ cmd: 'browse', url: h.url, storageOptions: h.so || undefined, push: false });
            selected = null;
            infoTitle.textContent = 'Loading...';
            infoActions.classList.add('hidden');
            infoMeta.innerHTML = '';
            infoPreview.innerHTML = '';
        }
    });
    $fbId('btn-up').addEventListener('click', function() {
        const p = parentUrl(currentUrl);
        if (p && p !== currentUrl) { navigateTo(p, currentSo); }
    });
    $fbId('btn-refresh').addEventListener('click', function() {
        navigateTo(currentUrl, currentSo, false);
    });
    $fbId('btn-bm-dropdown').addEventListener('click', function(e) {
        e.stopPropagation();
        bmPanel.classList.toggle('hidden');
        if (!bmPanel.classList.contains('hidden')) renderBookmarks();
    });
    $fbId('bm-close').addEventListener('click', function() {
        bmPanel.classList.add('hidden');
    });
    $fbId('btn-bm-add-current').addEventListener('click', function() {
        bmPanel.classList.add('hidden');
        vscode.postMessage({ cmd: 'addBookmark', url: currentUrl, storageOptions: currentSo || undefined });
    });
    document.addEventListener('click', function(e) {
        if (!bmPanel.contains(e.target) && e.target !== $fbId('btn-bm-dropdown')) {
            bmPanel.classList.add('hidden');
        }
    });

    // Storage options
    $fbId('btn-so').addEventListener('click', function(e) {
        e.stopPropagation();
        soInput.value = currentSo;
        soOverlay.classList.remove('hidden');
        setTimeout(function() { soInput.focus(); }, 0);
    });
    $fbId('so-cancel').addEventListener('click', function() {
        soOverlay.classList.add('hidden');
    });
    $fbId('so-ok').addEventListener('click', function() {
        const val = soInput.value.trim();
        try {
            if (val) JSON.parse(val);
            currentSo = val;
        } catch(ex) {
            alert('Storage options must be valid JSON: ' + ex.message);
            return;
        }
        soOverlay.classList.add('hidden');
        dbg('storage options set: ' + currentSo);
    });
    soOverlay.addEventListener('click', function(e) {
        if (e.target === soOverlay) soOverlay.classList.add('hidden');
    });

    // Go / URL bar
    $fbId('btn-go').addEventListener('click', function() {
        const url = urlInput.value.trim();
        if (url) { navigateTo(url, currentSo); }
    });
    urlInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            const url = urlInput.value.trim();
            if (url) { navigateTo(url, currentSo); }
        }
    });

    // New file / folder
    $fbId('btn-new-file').addEventListener('click', function() {
        newentryMode = 'file';
        neTitle.textContent = 'New file';
        neInput.value = '';
        neOverlay.classList.remove('hidden');
        setTimeout(function() { neInput.focus(); }, 0);
    });
    $fbId('btn-new-dir').addEventListener('click', function() {
        newentryMode = 'dir';
        neTitle.textContent = 'New folder';
        neInput.value = '';
        neOverlay.classList.remove('hidden');
        setTimeout(function() { neInput.focus(); }, 0);
    });
    $fbId('newentry-cancel').addEventListener('click', function() {
        neOverlay.classList.add('hidden');
    });
    $fbId('newentry-ok').addEventListener('click', function() {
        const name = neInput.value.trim();
        if (!name) return;
        neOverlay.classList.add('hidden');
        if (newentryMode === 'file') {
            dbg('createFile ' + name);
            vscode.postMessage({ cmd: 'createFile', parentUrl: currentUrl, name: name, storageOptions: currentSo || undefined });
        } else {
            dbg('mkdir ' + name);
            vscode.postMessage({ cmd: 'mkdir', parentUrl: currentUrl, name: name, storageOptions: currentSo || undefined });
        }
    });
    neInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') $fbId('newentry-ok').click();
        if (e.key === 'Escape') neOverlay.classList.add('hidden');
    });
    neOverlay.addEventListener('click', function(e) {
        if (e.target === neOverlay) neOverlay.classList.add('hidden');
    });

    // Info panel actions
    $fbId('btn-open-editor').addEventListener('click', function() {
        if (!selected || selected.type === 'directory') return;
        dbg('openFile ' + selected.url);
        vscode.postMessage({ cmd: 'openFile', url: selected.url, storageOptions: selected.so || undefined });
    });
    $fbId('btn-add-to-lib').addEventListener('click', function() {
        if (!selected) return;
        dbg('addToLibrary ' + selected.url);
        vscode.postMessage({ cmd: 'addToLibrary', url: selected.url, storageOptions: selected.so || undefined });
    });
    $fbId('btn-bookmark').addEventListener('click', function() {
        const url = selected ? selected.url : currentUrl;
        dbg('addBookmark ' + url);
        vscode.postMessage({ cmd: 'addBookmark', url: url, storageOptions: currentSo || undefined });
    });
    $fbId('btn-delete-sel').addEventListener('click', function() {
        if (!selected) return;
        dbg('deleteEntry ' + selected.url);
        vscode.postMessage({
            cmd: 'deleteEntry',
            url: selected.url,
            isDir: selected.type === 'directory',
            storageOptions: selected.so || undefined,
        });
    });
    $fbId('btn-rename-sel').addEventListener('click', function() {
        if (!selected) return;
        renInput.value = basename(selected.url);
        renOverlay.classList.remove('hidden');
        setTimeout(function() { renInput.focus(); }, 0);
    });

    // Rename modal
    $fbId('rename-cancel').addEventListener('click', function() {
        renOverlay.classList.add('hidden');
    });
    $fbId('rename-ok').addEventListener('click', function() {
        const newName = renInput.value.trim();
        if (!newName || !selected) return;
        renOverlay.classList.add('hidden');
        dbg('rename ' + selected.url + ' -> ' + newName);
        vscode.postMessage({ cmd: 'renameEntry', url: selected.url, newName: newName, storageOptions: selected.so || undefined });
    });
    renInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') $fbId('rename-ok').click();
        if (e.key === 'Escape') renOverlay.classList.add('hidden');
    });
    renOverlay.addEventListener('click', function(e) {
        if (e.target === renOverlay) renOverlay.classList.add('hidden');
    });

    // ── embedded projspec panel ────────────────────────────────────────────
    // The shared panel JS was already run by the inline bootstrap script in
    // getHtml() before this IIFE executed.  The bootstrap installed
    // window.__fbPanelDeliver(msg) — call it to push a data message into
    // the embedded panel.

    // ── file content display (inline, no project widget) ─────────────────
    // For single-file selections we render content directly rather than
    // routing through the embedded library panel.  The rule:
    //   - If the file has meaningful data info (datatype + schema/metadata),
    //     show that.  Text preview is suppressed — the data description is
    //     the useful thing.
    //   - Otherwise show the text preview (first N lines).
    //   - If neither is available, hide the scan pane.
    function showFileInScanPane(data) {
        if (!scanPane || !fileContent) return;

        // Switch: hide the embedded panel root, show the file content div
        if (scanPanelRoot) scanPanelRoot.classList.add('hidden');
        fileContent.classList.remove('hidden');
        fileContent.innerHTML = '';

        var proj = data.project;
        var textPreview = data.text_preview || '';

        // Extract the dataset content from the data_project spec
        var dataset = null;
        if (proj && proj.specs && proj.specs.data_project) {
            var cont = proj.specs.data_project._contents || {};
            var keys = Object.keys(cont);
            for (var i = 0; i < keys.length; i++) {
                var item = cont[keys[i]];
                if (item && item.klass && item.klass[1] === 'dataset') {
                    dataset = item;
                    break;
                }
            }
        }

        var hasData = dataset && (
            dataset.datatype ||
            (dataset.schema && Object.keys(dataset.schema).length > 0) ||
            (dataset.metadata && Object.keys(dataset.metadata).length > 0)
        );

        if (hasData) {
            var card = document.createElement('div');
            card.className = 'fc-dataset';

            // Datatype heading
            if (dataset.datatype) {
                var dtEl = document.createElement('div');
                dtEl.className = 'fc-datatype';
                dtEl.textContent = dataset.datatype;
                card.appendChild(dtEl);
            }

            var meta = dataset.metadata || {};

            // HTML repr — render inline (sanitised: strip scripts/iframes)
            var htmlRepr = typeof meta.html_repr === 'string' ? meta.html_repr : null;
            if (htmlRepr) {
                var reprDiv = document.createElement('div');
                reprDiv.className = 'fc-html-repr';
                reprDiv.innerHTML = sanitizeHtmlRepr(htmlRepr);
                card.appendChild(reprDiv);
            }

            // Thumbnail image (data: URI only)
            var thumb = typeof meta.thumbnail === 'string' ? meta.thumbnail : null;
            if (thumb && /^data:image\//i.test(thumb)) {
                var img = document.createElement('img');
                img.src = thumb;
                img.className = 'fc-thumbnail';
                img.alt = 'thumbnail';
                card.appendChild(img);
            }

            // Schema / columns — only if no richer HTML repr
            if (!htmlRepr) {
                var schema = dataset.schema;
                if (schema && typeof schema === 'object' && !Array.isArray(schema)) {
                    var cols = Object.keys(schema);
                    if (cols.length > 0) {
                        var schemaHdr = document.createElement('div');
                        schemaHdr.className = 'fc-schema-hdr';
                        schemaHdr.textContent = 'Columns';
                        card.appendChild(schemaHdr);
                        for (var ci = 0; ci < cols.length; ci++) {
                            var colRow = document.createElement('div');
                            colRow.className = 'fc-col-row';
                            colRow.innerHTML = '<span class="fc-col-name">' + escHtml(cols[ci]) + '</span>'
                                + '<span class="fc-col-dtype">' + escHtml(String(schema[cols[ci]])) + '</span>';
                            card.appendChild(colRow);
                        }
                    }
                }
            }

            // Metadata key-values — skip html_repr/thumbnail (already rendered above).
            // reader_* / readers* fields are collected into a collapsible box at the end.
            var SKIP_META = new Set(['html_repr', 'thumbnail']);
            var mkeys = Object.keys(meta);
            var readerRows = [];
            for (var mi = 0; mi < mkeys.length; mi++) {
                var mk = mkeys[mi], mv = meta[mk];
                if (SKIP_META.has(mk) || mv == null) continue;
                var mvStr = typeof mv === 'object' ? JSON.stringify(mv) : String(mv);
                if (!mvStr || mvStr === '{}' || mvStr === '[]') continue;
                if (/^readers?(_|$)/i.test(mk) || mk === 'errors') {
                    readerRows.push([mk, mvStr]);
                    continue;
                }
                var kvEl = document.createElement('div');
                kvEl.className = 'fc-kv';
                kvEl.innerHTML = '<span class="fc-k">' + escHtml(mk) + ':</span>'
                    + '<span class="fc-v">' + escHtml(mvStr) + '</span>';
                card.appendChild(kvEl);
            }
            if (readerRows.length > 0) {
                var det = document.createElement('details');
                det.className = 'fc-reader-details';
                var sum = document.createElement('summary');
                sum.className = 'fc-reader-summary';
                sum.textContent = 'Reader info';
                det.appendChild(sum);
                for (var ri = 0; ri < readerRows.length; ri++) {
                    var rkvEl = document.createElement('div');
                    rkvEl.className = 'fc-kv';
                    rkvEl.innerHTML = '<span class="fc-k">' + escHtml(readerRows[ri][0]) + ':</span>'
                        + '<span class="fc-v">' + escHtml(readerRows[ri][1]) + '</span>';
                    det.appendChild(rkvEl);
                }
                card.appendChild(det);
            }

            // Structure tags (e.g. ["table"])
            var structure = dataset.structure;
            if (Array.isArray(structure) && structure.length > 0) {
                var stEl = document.createElement('div');
                stEl.className = 'fc-kv';
                stEl.innerHTML = '<span class="fc-k">structure:</span>'
                    + '<span class="fc-v">' + escHtml(structure.join(', ')) + '</span>';
                card.appendChild(stEl);
            }

            fileContent.appendChild(card);

            // For data files, also show text preview below if there's no html_repr
            // (e.g. CSV — the first few rows are more useful than just column names)
            if (!htmlRepr && textPreview) {
                var prevHdr = document.createElement('div');
                prevHdr.className = 'fc-schema-hdr';
                prevHdr.style.marginTop = '10px';
                prevHdr.textContent = 'Preview';
                fileContent.appendChild(prevHdr);
                var pre2 = document.createElement('pre');
                pre2.className = 'fc-text-preview';
                pre2.textContent = textPreview;
                fileContent.appendChild(pre2);
            }

        } else if (textPreview) {
            // Pure text file — just show the preview
            var pre = document.createElement('pre');
            pre.className = 'fc-text-preview';
            pre.textContent = textPreview;
            fileContent.appendChild(pre);

        } else {
            // Nothing to show — hide the scan pane
            scanPane.classList.add('hidden');
        }
    }

    // Minimal sanitiser for html_repr content (strips scripts, iframes, on* handlers)
    function sanitizeHtmlRepr(html) {
        var tpl = document.createElement('template');
        tpl.innerHTML = String(html);
        var walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_ELEMENT);
        var toRemove = [];
        var n = walker.nextNode();
        while (n) {
            var tag = n.tagName.toLowerCase();
            if (tag === 'script' || tag === 'iframe' || tag === 'object' || tag === 'embed') {
                toRemove.push(n);
            } else {
                var attrs = Array.from(n.attributes);
                for (var ai = 0; ai < attrs.length; ai++) {
                    var an = attrs[ai].name.toLowerCase();
                    if (an.startsWith('on')) { n.removeAttribute(attrs[ai].name); continue; }
                    if ((an === 'href' || an === 'src') && /^\s*javascript:/i.test(attrs[ai].value)) {
                        n.removeAttribute(attrs[ai].name);
                    }
                }
            }
            n = walker.nextNode();
        }
        for (var ri = 0; ri < toRemove.length; ri++) toRemove[ri].remove();
        return tpl.innerHTML;
    }

    function showProjectInPanel(data) {
        if (!scanPanelRoot) return;
        if (scanStatus) scanStatus.textContent = '';

        var proj = data.project;
        var url  = data.url || '';
        if (!proj) {
            if (scanStatus) scanStatus.textContent = data.error || 'no project data';
            return;
        }

        var lib = {};
        lib[url] = proj;
        // Pass info and enums so spec documentation popups and enum labels work
        var dataMsg = { type: 'data', library: lib, info: data.info || {}, enums: data.enums || {} };

        if (typeof window.__fbPanelDeliver === 'function') {
            dbg('delivering to embedded panel: ' + url);
            window.__fbPanelDeliver(dataMsg);
        } else {
            dbg('ERROR: __fbPanelDeliver not available');
        }
    }

    // ── message bus ────────────────────────────────────────────────────────
    // Inbound messages are delivered via transport.onReady(dispatch).
    function _fbDispatch(msg) {
        // (type logged selectively in each case handler)
        switch (msg.type) {
            case 'loading':
                spinner.classList.toggle('hidden', !msg.loading);
                break;

            case 'init':
                bookmarks = msg.bookmarks || [];
                protocols = msg.protocols || [];
                if (msg.libraryUrls) {
                    libraryUrls = new Set(msg.libraryUrls);
                    dbg('init: ' + bookmarks.length + ' bookmarks, ' + libraryUrls.size + ' library entries');
                } else {
                    dbg('init: ' + bookmarks.length + ' bookmarks, ' + protocols.length + ' protocols');
                }
                break;

            case 'browseResult':
                if (typeof msg.storageOptions === 'string') {
                    currentSo = msg.storageOptions;
                }
                renderBrowse(msg);
                if (msg.pushHistory) {
                    history = history.slice(0, histIdx + 1);
                    history.push({ url: msg.url, so: currentSo });
                    histIdx = history.length - 1;
                    dbg('history push, depth=' + history.length);
                }
                break;

            case 'inspectResult':
                renderInspect(msg);
                break;

            case 'expandResult':
                handleExpandResult(msg);
                break;

            case 'projectScanned':
                dbg('projectScanned url=' + msg.url);
                if (selectedIsFile) {
                    // Single file: inline display, no project widget
                    showFileInScanPane(msg);
                } else {
                    // Directory: full embedded library panel
                    showProjectInPanel(msg);
                }
                break;

            case 'bookmarksUpdated':
                bookmarks = msg.bookmarks || [];
                dbg('bookmarks updated: ' + bookmarks.length);
                if (!bmPanel.classList.contains('hidden')) renderBookmarks();
                break;

            case 'libraryUrlsUpdated':
                libraryUrls = new Set(msg.libraryUrls || []);
                dbg('library URLs updated: ' + libraryUrls.size);
                refreshLibraryBadges();
                break;

            case 'error':
                errorEl.textContent = 'Error: ' + (msg.message || 'unknown');
                errorEl.classList.remove('hidden');
                dbg('error: ' + msg.message);
                break;
        }
    }  // end _fbDispatch

    dbg('sending ready');
    // Register dispatch with transport and notify host
    window.__projspecFbDeliver = _fbDispatch;
    _transport.onReady(_fbDispatch);
    vscode.postMessage({ cmd: 'ready' });
})();
