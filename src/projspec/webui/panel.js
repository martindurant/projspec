/* Shared JavaScript for the projspec Project Library panel.
 *
 * Used by all projspec UIs: the VSCode extension (via `acquireVsCodeApi`),
 * the Qt app (via QWebChannel), the PyCharm plugin (via JBCefJSQuery), and
 * the ipywidget representation (via anywidget's model.send / on_msg).
 *
 * The host is responsible for installing a transport adapter *before* this
 * script runs, by defining `window.projspecTransport`.  The adapter is an
 * object with:
 *
 *   {
 *       // send(msg): Called by the panel when the user clicks a button
 *       // (e.g. {cmd:'reload'}).  msg is a plain JS object.
 *       send: (msg) => void,
 *
 *       // onReady(dispatch): Called once the panel is ready to receive
 *       // messages from the host.  The adapter should arrange for every
 *       // incoming message to be passed to dispatch(msgObj).  Incoming
 *       // messages have a 'type' field: 'data', 'loading', or
 *       // 'openCreateSpecModal'.
 *       onReady: (dispatch) => void,
 *   }
 *
 * If `window.projspecTransport` is not set by the time this script runs,
 * a no-op transport is installed and a console warning printed - useful
 * for testing the static HTML in a plain browser.
 */

(function() {
    // --- Transport ---------------------------------------------------------
    const transport = window.projspecTransport || {
        send: (_) => { console.warn('projspec: no transport configured'); },
        onReady: (dispatch) => {
            // Expose dispatch so hosts without a proper ready hook can still
            // push messages in from devtools.
            window.__projspecDeliver = dispatch;
        },
    };

    // --- Root scoping ------------------------------------------------------
    // Hosts that embed the panel inside a larger document (e.g. the Jupyter
    // ipywidget) set ``window.projspecRoot`` to the widget's root element
    // before this script runs.  All element lookups and document-wide
    // selectors are then scoped to that subtree, so the UI keeps working
    // even if the same IDs exist elsewhere on the page (for instance, when
    // the widget has been re-rendered or when multiple instances coexist).
    const panelRoot = window.projspecRoot || document;
    function $id(id) {
        if (panelRoot === document) return document.getElementById(id);
        return panelRoot.querySelector('#' + CSS.escape(id));
    }
    function $all(selector) {
        return (panelRoot === document ? document : panelRoot).querySelectorAll(selector);
    }


    let info = null;
    let enums = {};
    let library = {};
    // selection: { url: string, kind: 'contents'|'artifacts'|'spec', specName?: string }
    let selection = null;

    // Icon map.  Injected by the host as JSON; falls back to sane defaults
    // so the static HTML still renders if the host forgot.
    const CHROME_ICONS = window.__PROJSPEC_CHROME_ICONS__ || {
        add: '+', reload: '\u21BB', configure: '\u2699\uFE0F',
        search: '\u{1F50D}', clear: 'x', spinner: '\u29D7',
        chevron_up: '\u25B2', chevron_down: '\u25BC', kebab: '\u22EE',
        play: '\u25B6\uFE0F', info: '\u2139\uFE0F', reveal: '\u2192',
    };
    const DEFAULT_ICONS = { spec: '\u{1F9E9}', content: '\u{1F4C4}', artifact: '\u{1F4E6}' };

    function postMessage(msg) { transport.send(msg); }

    // --- Pastel palette for chip colours ----------------------------------
    const PALETTE = [
        '#f9c0c0', '#f9dcc0', '#f9f0c0', '#d9f9c0', '#c0f9d2',
        '#c0f9f0', '#c0e3f9', '#c0ccf9', '#d6c0f9', '#efc0f9',
        '#f9c0e3', '#d9c9b5', '#b5d9c9', '#b5c9d9', '#c9b5d9',
    ];
    function hashStr(s) {
        let h = 0;
        for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
        return Math.abs(h);
    }
    function chipColour(label) { return PALETTE[hashStr(label) % PALETTE.length]; }

    function basename(url) {
        try {
            const stripped = url.replace(/\/+$/, '');
            const idx = stripped.lastIndexOf('/');
            return idx >= 0 ? stripped.slice(idx + 1) : stripped;
        } catch { return url; }
    }
    function fmtSize(bytes) {
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let n = parseFloat(bytes);
        for (let i = 0; i < units.length; i++) {
            if (n < 1024 || i === units.length - 1)
                return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
            n /= 1024;
        }
    }
    function fmtAge(ts) {
        const secs = Math.floor(Date.now() / 1000 - parseFloat(ts));
        if (secs < 0) return 'just now';
        const days = Math.floor(secs / 86400);
        if (days === 0) {
            if (secs < 60) return 'just now';
            if (secs < 3600) {
                const m = Math.floor(secs / 60);
                return m + ' minute' + (m !== 1 ? 's' : '') + ' ago';
            }
            const h = Math.floor(secs / 3600);
            return h + ' hour' + (h !== 1 ? 's' : '') + ' ago';
        }
        if (days === 1) return 'yesterday';
        if (days < 30) return days + ' days ago';
        if (days < 365) return Math.floor(days / 30) + ' months ago';
        const yrs = Math.floor(days / 365);
        return yrs + ' year' + (yrs > 1 ? 's' : '') + ' ago';
    }
    function iconForSpec(snake) {
        const entry = info && info.specs && info.specs[snake];
        return (entry && entry.icon) || DEFAULT_ICONS.spec;
    }
    function iconForContent(snake) {
        const entry = info && info.content && info.content[snake];
        return (entry && entry.icon) || DEFAULT_ICONS.content;
    }
    function iconForArtifact(snake) {
        const entry = info && info.artifact && info.artifact[snake];
        return (entry && entry.icon) || DEFAULT_ICONS.artifact;
    }

    // --- DOM refs ----------------------------------------------------------
    const projectsEl = $id('projects');
    const spinnerEl = $id('spinner');
    const searchEl = $id('search');
    const searchClear = $id('search-clear');
    const detailsTitle = $id('details-title');
    const detailsInfo = $id('details-info');
    const detailsList = $id('details-list');
    const detailsToggle = $id('details-toggle');
    const popup = $id('popup');

    detailsToggle.addEventListener('click', () => {
        detailsInfo.classList.toggle('collapsed');
        const collapsed = detailsInfo.classList.contains('collapsed');
        detailsToggle.textContent = collapsed ? CHROME_ICONS.chevron_down : CHROME_ICONS.chevron_up;
        detailsToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });

    function render() {
        const filter = (searchEl.value || '').trim().toLowerCase();
        projectsEl.innerHTML = '';
        const urls = Object.keys(library).sort();
        let any = false;
        for (const url of urls) {
            const project = library[url];
            if (filter) {
                const hay = (url + ' ' + basename(url) + ' ' + Object.keys(project.specs || {}).join(' ')).toLowerCase();
                if (!hay.includes(filter)) continue;
            }
            any = true;
            projectsEl.appendChild(renderProject(url, project));
        }
        if (!any) {
            const e = document.createElement('div');
            e.style.padding = '20px';
            e.style.color = 'var(--vscode-descriptionForeground, #858585)';
            e.style.textAlign = 'center';
            e.textContent = urls.length === 0
                ? 'Library is empty. Click "Add" to scan a directory.'
                : 'No projects match the filter.';
            projectsEl.appendChild(e);
        }
        renderDetails();
    }

    function renderProject(url, project) {
        const wrap = document.createElement('div');
        wrap.className = 'project';
        wrap.dataset.url = url;

        const title = document.createElement('div');
        title.className = 'title';
        title.textContent = basename(url);
        wrap.appendChild(title);

        const urlLine = document.createElement('div');
        urlLine.className = 'url';
        urlLine.textContent = url;
        wrap.appendChild(urlLine);

        if (project.storage_options && Object.keys(project.storage_options).length > 0) {
            const so = document.createElement('div');
            so.className = 'storage-opts';
            so.textContent = 'storage_options: ' + JSON.stringify(project.storage_options);
            wrap.appendChild(so);
        }

        const metaParts = [];
        if (project.file_count != null && project.total_size != null)
            metaParts.push(parseInt(project.file_count).toLocaleString() + ' files, ' + fmtSize(project.total_size));
        if (project.is_writable != null)
            metaParts.push((project.is_writable === true || project.is_writable === 'True') ? 'writable' : 'read-only');
        if (project.last_modified != null) {
            const age = fmtAge(project.last_modified);
            const by = project.last_modified_by != null ? project.last_modified_by : null;
            metaParts.push('last modified ' + age + (by ? ' by ' + by : ''));
        }
        if (project.scanned_at != null)
            metaParts.push('scanned ' + fmtAge(project.scanned_at));
        if (metaParts.length > 0) {
            const meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = metaParts.join(' · ');
            wrap.appendChild(meta);
        }

        const chips = document.createElement('div');
        chips.className = 'chips';
        const contents = project.contents || {};
        const artifacts = project.artifacts || {};
        const globalCount = Object.keys(contents).length + Object.keys(artifacts).length;
        if (globalCount > 0) {
            const globalChip = makeChip('Global', url, 'global', null, null);
            globalChip.style.background = '#d0d0d0';
            chips.appendChild(globalChip);
        }
        for (const specName of Object.keys(project.specs || {})) {
            chips.appendChild(makeChip(specName, url, 'spec', specName, iconForSpec(specName)));
        }
        wrap.appendChild(chips);

        const kebabBtn = document.createElement('button');
        kebabBtn.className = 'kebab';
        kebabBtn.title = 'More actions';
        kebabBtn.textContent = CHROME_ICONS.kebab;
        kebabBtn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            toggleKebab(wrap, url);
        });
        wrap.appendChild(kebabBtn);

        if (selection && selection.url === url) wrap.classList.add('active');
        return wrap;
    }

    function makeChip(label, url, kind, specName, icon) {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.style.background = chipColour(label);
        if (icon)
            chip.innerHTML = '<span class="chip-icon">' + escapeHtml(icon) + '</span><span>' + escapeHtml(label) + '</span>';
        else chip.textContent = label;
        chip.addEventListener('click', (ev) => {
            ev.stopPropagation();
            selection = { url, kind, specName };
            $all('.project.active').forEach(el => el.classList.remove('active'));
            $all('.chip.active').forEach(el => el.classList.remove('active'));
            const projEl = ev.target.closest('.project');
            if (projEl) projEl.classList.add('active');
            chip.classList.add('active');
            renderDetails();
        });
        if (selection && selection.url === url &&
            ((selection.kind === kind && kind !== 'spec') ||
             (selection.kind === 'spec' && kind === 'spec' && selection.specName === specName))) {
            chip.classList.add('active');
        }
        return chip;
    }

    // --- Kebab menu --------------------------------------------------------
    let openKebab = null;
    function toggleKebab(projectEl, url) {
        if (openKebab && openKebab.el === projectEl) { closeKebab(); return; }
        closeKebab();
        const menu = document.createElement('div');
        menu.className = 'kebab-menu';
        const isLocal = url.startsWith('file://');
        if (isLocal) {
            addItem(menu, 'Open with VSCode', () => postMessage({ cmd: 'openWith', tool: 'vscode', url }));
            addItem(menu, 'Show in file browser', () => postMessage({ cmd: 'openWith', tool: 'filebrowser', url }));
            addItem(menu, 'Open with PyCharm', () => postMessage({ cmd: 'openWith', tool: 'pycharm', url }));
            addItem(menu, 'Open with jupyter', () => postMessage({ cmd: 'openWith', tool: 'jupyter', url }));
            addSeparator(menu);
            addItem(menu, 'Rescan', () => postMessage({ cmd: 'rescan', url }));
            addItem(menu, 'Create spec', () => postMessage({ cmd: 'createSpec', url }));
            addItem(menu, 'Remove from library', () => postMessage({ cmd: 'removeFromLibrary', url }));
        } else {
            const ctl = addItem(menu, 'Copy to local', () => postMessage({ cmd: 'copyToLocal', url }));
            ctl.classList.add('disabled');
            addItem(menu, 'Rescan', () => postMessage({ cmd: 'rescan', url }));
            addItem(menu, 'Remove from library', () => postMessage({ cmd: 'removeFromLibrary', url }));
        }
        projectEl.appendChild(menu);
        openKebab = { el: projectEl, menu };
        setTimeout(() => document.addEventListener('click', closeKebab, { once: true }), 0);
    }
    function addItem(menu, label, onClick) {
        const mi = document.createElement('div');
        mi.className = 'menu-item';
        mi.textContent = label;
        mi.addEventListener('click', (e) => {
            e.stopPropagation();
            if (!mi.classList.contains('disabled')) { onClick(); closeKebab(); }
        });
        menu.appendChild(mi);
        return mi;
    }
    function addSeparator(menu) {
        const sep = document.createElement('div');
        sep.className = 'menu-sep';
        menu.appendChild(sep);
    }
    function closeKebab() {
        if (openKebab) { openKebab.menu.remove(); openKebab = null; }
    }

    // --- Details panel -----------------------------------------------------
    function renderDetails() {
        detailsList.innerHTML = '';
        detailsInfo.innerHTML = '';
        if (!selection) { detailsTitle.textContent = 'Details'; return; }
        const project = library[selection.url];
        if (!project) { detailsTitle.textContent = 'Details'; return; }

        if (selection.kind === 'spec') {
            detailsTitle.textContent = selection.specName;
            const entry = info && info.specs && info.specs[selection.specName];
            if (entry) {
                const doc = document.createElement('div');
                doc.textContent = entry.doc || '';
                detailsInfo.appendChild(doc);
                if (entry.link) {
                    const a = document.createElement('a');
                    a.href = entry.link;
                    a.textContent = entry.link;
                    a.target = '_blank';
                    detailsInfo.appendChild(document.createElement('br'));
                    detailsInfo.appendChild(a);
                }
            }
            const spec = project.specs[selection.specName];
            if (spec) {
                renderItemGroup(spec._contents || {}, 'content', false, selection.specName);
                renderItemGroup(spec._artifacts || {}, 'artifact', true, selection.specName);
            }
        } else if (selection.kind === 'global') {
            detailsTitle.textContent = 'Global';
            renderItemGroup(project.contents || {}, 'content', false, undefined);
            renderItemGroup(project.artifacts || {}, 'artifact', true, undefined);
        }
    }

    /**
     * items is a mapping of typeName -> entry.  The "rich" case is when
     * values carry a 'klass' field (directly, nested, or in an array);
     * each such record becomes its own labelled widget.  If no 'klass'
     * appears anywhere, the whole mapping is rendered as a single untitled
     * YAML widget.
     */
    function renderItemGroup(items, kind, showMake, specName) {
        if (!items || typeof items !== 'object') return;
        const keys = Object.keys(items);
        if (keys.length === 0) return;
        if (!hasKlass(items)) {
            detailsList.appendChild(makePlainWidget(items));
            return;
        }
        for (const typeName of keys) {
            const entry = items[typeName];
            if (!entry) continue;
            if (Array.isArray(entry)) {
                for (const e of entry)
                    detailsList.appendChild(makeItemWidget(typeName, null, e, kind, showMake, specName));
            } else if (entry && typeof entry === 'object' && 'klass' in entry) {
                detailsList.appendChild(makeItemWidget(typeName, null, entry, kind, showMake, specName));
            } else if (entry && typeof entry === 'object') {
                for (const name of Object.keys(entry))
                    detailsList.appendChild(makeItemWidget(typeName, name, entry[name], kind, showMake, specName));
            } else {
                detailsList.appendChild(makePlainWidget({ [typeName]: entry }));
            }
        }
    }

    function hasKlass(v) {
        if (!v || typeof v !== 'object') return false;
        if (!Array.isArray(v) && 'klass' in v) return true;
        if (Array.isArray(v)) { for (const e of v) if (hasKlass(e)) return true; return false; }
        for (const k of Object.keys(v)) if (hasKlass(v[k])) return true;
        return false;
    }

    function makePlainWidget(data) {
        const w = document.createElement('div');
        w.className = 'item-widget';
        const tree = document.createElement('div');
        tree.className = 'tree yaml';
        tree.appendChild(renderYaml(data));
        w.appendChild(tree);
        return w;
    }

    function makeItemWidget(typeName, name, data, kind, showMake, specName) {
        const w = document.createElement('div');
        w.className = 'item-widget kind-' + kind;

        const title = document.createElement('div');
        title.className = 'widget-title';
        const klass = (data && data.klass && Array.isArray(data.klass)) ? data.klass[1] : typeName;
        const iconName = kind === 'content' ? iconForContent(klass) : iconForArtifact(klass);
        const badgeLabel = kind === 'content' ? 'Content' : 'Artifact';
        title.innerHTML = '<span class="widget-icon">' + escapeHtml(iconName) + '</span> ' + escapeHtml(klass)
            + (name ? ' <span class="widget-subtitle">- ' + escapeHtml(name) + '</span>' : '')
            + ' <span class="widget-kind-badge">' + escapeHtml(badgeLabel) + '</span>';
        w.appendChild(title);

        const actions = document.createElement('div');
        actions.className = 'widget-actions';

        const fn = (data && typeof data === 'object') ? data.fn : undefined;
        if (kind === 'artifact' && typeof fn === 'string' && fn.length > 0 && isLocalPath(fn)) {
            const rv = document.createElement('button');
            rv.title = 'Reveal ' + fn;
            rv.textContent = CHROME_ICONS.reveal;
            rv.addEventListener('click', (e) => {
                e.stopPropagation();
                postMessage({ cmd: 'revealFile', fn });
            });
            actions.appendChild(rv);
        }

        if (showMake) {
            const mk = document.createElement('button');
            mk.title = 'Make artifact';
            mk.textContent = CHROME_ICONS.play;
            mk.addEventListener('click', (e) => {
                e.stopPropagation();
                postMessage({
                    cmd: 'make',
                    url: selection.url,
                    spec: specName,
                    artifactType: typeName,
                    name: name || undefined,
                });
            });
            actions.appendChild(mk);
        }
        const ib = document.createElement('button');
        ib.title = 'Show documentation';
        ib.textContent = CHROME_ICONS.info;
        ib.addEventListener('click', (e) => {
            e.stopPropagation();
            showInfoPopup(klass, kind, e.clientX, e.clientY);
        });
        actions.appendChild(ib);
        w.appendChild(actions);

        const html = (kind === 'content' && data && typeof data === 'object') ? data._html : undefined;
        if (typeof html === 'string') {
            const body = document.createElement('div');
            body.className = 'widget-html';
            body.innerHTML = sanitizeHtml(html);
            w.appendChild(body);
        } else {
            // Datasets (and other content) may carry rich previews in
            // ``metadata.html_repr`` (an HTML fragment) and
            // ``metadata.thumbnail`` (a data: image URL). Embed those rather
            // than dumping their (often huge) raw strings into the YAML tree.
            const meta = (kind === 'content' && data && typeof data === 'object'
                && data.metadata && typeof data.metadata === 'object') ? data.metadata : null;
            const htmlRepr = meta && typeof meta.html_repr === 'string' ? meta.html_repr : null;
            const thumb = meta && typeof meta.thumbnail === 'string' ? meta.thumbnail : null;

            const tree = document.createElement('div');
            tree.className = 'tree yaml';
            tree.appendChild(renderYaml(stripPreview(stripKlass(data))));
            w.appendChild(tree);

            if (thumb) w.appendChild(thumbnailImg(thumb));
            if (htmlRepr) {
                const body = document.createElement('div');
                body.className = 'widget-html';
                body.innerHTML = sanitizeHtml(htmlRepr);
                w.appendChild(body);
            }
        }
        return w;
    }

    /**
     * Return a shallow copy of a content dict with the embedded-preview keys
     * (``metadata.html_repr`` / ``metadata.thumbnail``) removed, so the YAML
     * tree doesn't show their large raw strings - they are rendered as live
     * HTML / an image instead.
     */
    function stripPreview(obj) {
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return obj;
        if (!obj.metadata || typeof obj.metadata !== 'object' || Array.isArray(obj.metadata)) return obj;
        const meta = {};
        let changed = false;
        for (const k of Object.keys(obj.metadata)) {
            if (k === 'html_repr' || k === 'thumbnail') { changed = true; continue; }
            meta[k] = obj.metadata[k];
        }
        if (!changed) return obj;
        const out = {};
        for (const k of Object.keys(obj)) out[k] = obj[k];
        out.metadata = meta;
        return out;
    }

    /**
     * Build an <img> for a ``data:image/...`` thumbnail URL. Only accepts
     * data: image URLs (never remote/javascript URLs).
     */
    function thumbnailImg(src) {
        const wrap = document.createElement('div');
        wrap.className = 'widget-html';
        if (/^data:image\//i.test(src)) {
            const img = document.createElement('img');
            img.src = src;
            img.alt = 'thumbnail';
            wrap.appendChild(img);
        }
        return wrap;
    }

    /**
     * Minimal HTML sanitisation for content-provided ``_html`` fragments.
     * The markup originates from projspec itself, so we don't need a
     * full-blown sanitiser - just strip <script> / <iframe> / <object> /
     * <embed>, on* attributes and javascript: URLs.
     */
    function sanitizeHtml(html) {
        const tpl = document.createElement('template');
        tpl.innerHTML = String(html);
        const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_ELEMENT);
        const toRemove = [];
        let n = walker.nextNode();
        while (n) {
            const tag = n.tagName.toLowerCase();
            if (['script','iframe','object','embed'].includes(tag)) toRemove.push(n);
            else {
                for (const attr of Array.from(n.attributes)) {
                    const an = attr.name.toLowerCase();
                    if (an.startsWith('on')) { n.removeAttribute(attr.name); continue; }
                    if ((an === 'href' || an === 'src') && /^\s*javascript:/i.test(attr.value))
                        n.removeAttribute(attr.name);
                }
            }
            n = walker.nextNode();
        }
        for (const el of toRemove) el.remove();
        return tpl.innerHTML;
    }

    function stripKlass(obj) {
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return obj;
        const out = {};
        for (const k of Object.keys(obj)) if (k !== 'klass') out[k] = obj[k];
        return out;
    }

    // --- YAML-style renderer ----------------------------------------------
    function renderYaml(data) {
        const frag = document.createDocumentFragment();
        if (Array.isArray(data)) {
            if (data.length === 0) { frag.appendChild(textNode('[]', 'value empty')); return frag; }
            for (const item of data) frag.appendChild(yamlListItem(item));
            return frag;
        }
        if (data && typeof data === 'object') {
            const keys = Object.keys(data);
            if (keys.length === 0) { frag.appendChild(textNode('{}', 'value empty')); return frag; }
            for (const k of keys) frag.appendChild(yamlMapItem(k, data[k]));
            return frag;
        }
        frag.appendChild(primitiveSpan(data));
        return frag;
    }

    function yamlListItem(value) {
        const node = document.createElement('div');
        node.className = 'yaml-item list-item';
        const marker = document.createElement('span');
        marker.className = 'marker';
        marker.textContent = '- ';
        const body = document.createElement('span');
        body.className = 'body';
        if (isEnum(value)) {
            body.appendChild(enumSpan(value));
            node.appendChild(marker); node.appendChild(body);
            return node;
        }
        if (value && typeof value === 'object') {
            node.classList.add('collapsible');
            node.appendChild(marker);
            body.classList.add('empty');
            node.appendChild(body);
            const children = document.createElement('div');
            children.className = 'children';
            children.appendChild(renderYaml(value));
            node.appendChild(children);
            marker.addEventListener('click', (e) => {
                e.stopPropagation();
                node.classList.toggle('collapsed');
            });
            return node;
        }
        body.appendChild(primitiveSpan(value));
        node.appendChild(marker); node.appendChild(body);
        return node;
    }

    function yamlMapItem(key, value) {
        const node = document.createElement('div');
        node.className = 'yaml-item map-item';
        const label = document.createElement('div');
        label.className = 'label';
        if (isEnum(value)) {
            label.innerHTML = '<span class="key">' + escapeHtml(key) + '</span>: ';
            label.appendChild(enumSpan(value));
            node.appendChild(label);
            return node;
        }
        if (value && typeof value === 'object') {
            node.classList.add('collapsible');
            label.innerHTML = '<span class="key">' + escapeHtml(key) + '</span>:';
            node.appendChild(label);
            const children = document.createElement('div');
            children.className = 'children';
            children.appendChild(renderYaml(value));
            node.appendChild(children);
            label.addEventListener('click', (e) => {
                e.stopPropagation();
                node.classList.toggle('collapsed');
            });
            return node;
        }
        label.innerHTML = '<span class="key">' + escapeHtml(key) + '</span>: ';
        label.appendChild(primitiveSpan(value));
        node.appendChild(label);
        return node;
    }

    function isEnum(v) {
        return v && typeof v === 'object' && !Array.isArray(v)
            && Array.isArray(v.klass) && v.klass[0] === 'enum' && ('value' in v);
    }

    function enumSpan(v) {
        const name = v.klass[1];
        const raw = v.value;
        let label = null;
        const members = enums && enums[name];
        if (members) {
            for (const mname of Object.keys(members)) {
                if (members[mname] === raw) { label = mname; break; }
            }
        }
        const span = document.createElement('span');
        span.className = 'value enum';
        span.title = name + ' = ' + String(raw);
        span.textContent = label !== null ? label : String(raw);
        return span;
    }

    function primitiveSpan(v) {
        const s = document.createElement('span');
        s.className = 'value';
        if (typeof v === 'string') { s.classList.add('str'); s.textContent = v; }
        else if (typeof v === 'number') { s.classList.add('num'); s.textContent = String(v); }
        else if (typeof v === 'boolean') { s.classList.add('bool'); s.textContent = String(v); }
        else if (v === null || v === undefined) { s.classList.add('null'); s.textContent = 'null'; }
        else { s.textContent = String(v); }
        return s;
    }

    function textNode(text, cls) {
        const s = document.createElement('span');
        if (cls) s.className = cls;
        s.textContent = text;
        return s;
    }
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g,
            (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function isLocalPath(p) {
        if (p.startsWith('file://')) return true;
        return !/^[a-z][a-z0-9+.-]*:\/\//i.test(p);
    }

    // --- Info popup -------------------------------------------------------
    function showInfoPopup(klass, kind, mx, my) {
        const map = kind === 'content' ? (info && info.content) : (info && info.artifact);
        const entry = map && map[klass];
        if (!entry) {
            popup.innerHTML = '<em>No info for ' + escapeHtml(klass) + '</em>';
        } else {
            popup.innerHTML = '<div style="font-weight:bold;margin-bottom:4px;">'
                + escapeHtml(klass) + '</div>'
                + '<div style="white-space:pre-wrap;">' + escapeHtml(entry.doc || '') + '</div>';
        }
        popup.classList.remove('hidden');
        popup.style.left = '-9999px'; popup.style.top = '-9999px';
        requestAnimationFrame(() => {
            const w = popup.offsetWidth, h = popup.offsetHeight;
            let x = mx - w - 8, y = my - 8;
            if (x < 4) x = 4;
            if (y + h > window.innerHeight - 4) y = window.innerHeight - h - 4;
            if (y < 4) y = 4;
            popup.style.left = x + 'px'; popup.style.top = y + 'px';
        });
        setTimeout(() => document.addEventListener('click', () => popup.classList.add('hidden'), { once: true }), 0);
    }

    // --- Modal (create-spec + add-path) -----------------------------------
    //
    // A single modal DOM is reused for two related workflows:
    //
    //   - ``create-spec``: pick a spec type from a finite list (typeahead
    //     autocomplete against ``modalState.specs``).
    //   - ``add-path``: type a filesystem path to add to the library (no
    //     autocomplete - any non-empty string is accepted).
    //
    // ``modalState.mode`` selects the flavour; everything else - the input,
    // buttons, Esc/Enter handling - is shared.

    const modalOverlay = $id('modal-overlay');
    const modalInput = $id('modal-input');
    const modalSuggestions = $id('modal-suggestions');
    const modalOk = $id('modal-ok');
    const modalCancel = $id('modal-cancel');
    const modalTitle = $id('modal-title');
    const modalLabel = modalInput.previousElementSibling;  // the <label>
    let modalState = null;

    function openCreateSpecModal(url, specs) {
        modalState = {
            mode: 'create-spec',
            url,
            specs: specs.slice(),
            filtered: specs.slice(),
            active: 0,
        };
        modalTitle.textContent = 'Create spec';
        if (modalLabel) modalLabel.textContent = 'Spec type:';
        modalInput.placeholder = 'Start typing...';
        modalInput.value = '';
        modalOk.disabled = true;
        renderSuggestions();
        modalOverlay.classList.remove('hidden');
        setTimeout(() => modalInput.focus(), 0);
    }

    function openAddModal() {
        modalState = { mode: 'add-path' };
        modalTitle.textContent = 'Add project';
        if (modalLabel) modalLabel.textContent = 'Directory:';
        modalInput.placeholder = '/path/to/project';
        modalInput.value = '';
        modalOk.disabled = true;
        modalSuggestions.innerHTML = '';
        modalOverlay.classList.remove('hidden');
        setTimeout(() => modalInput.focus(), 0);
    }

    function closeModal() {
        modalOverlay.classList.add('hidden');
        modalState = null;
    }

    function renderSuggestions() {
        modalSuggestions.innerHTML = '';
        if (!modalState || modalState.mode !== 'create-spec') return;
        if (modalState.filtered.length === 0) {
            const e = document.createElement('div');
            e.className = 'empty';
            e.textContent = modalState.specs.length === 0 ? 'No spec types available' : 'No matches';
            modalSuggestions.appendChild(e);
            return;
        }
        modalState.filtered.forEach((s, i) => {
            const row = document.createElement('div');
            row.className = 'suggestion' + (i === modalState.active ? ' active' : '');
            row.textContent = s;
            row.addEventListener('mousedown', (e) => {
                e.preventDefault();
                modalState.active = i;
                modalInput.value = s;
                modalOk.disabled = false;
                submitModal();
            });
            modalSuggestions.appendChild(row);
        });
    }

    function filterSuggestions() {
        if (!modalState) return;
        if (modalState.mode === 'create-spec') {
            const q = modalInput.value.trim().toLowerCase();
            modalState.filtered = q
                ? modalState.specs.filter((s) => s.toLowerCase().includes(q))
                : modalState.specs.slice();
            modalState.active = 0;
            modalOk.disabled = !modalState.specs.includes(modalInput.value.trim());
            renderSuggestions();
        } else if (modalState.mode === 'add-path') {
            modalOk.disabled = modalInput.value.trim().length === 0;
        }
    }

    function submitModal() {
        if (!modalState) return;
        if (modalState.mode === 'create-spec') {
            let pick = modalInput.value.trim();
            if (!modalState.specs.includes(pick) && modalState.filtered.length === 1)
                pick = modalState.filtered[0];
            if (!modalState.specs.includes(pick)) return;
            const url = modalState.url;
            closeModal();
            postMessage({ cmd: 'createSpecConfirmed', url, spec: pick });
        } else if (modalState.mode === 'add-path') {
            const path = modalInput.value.trim();
            if (!path) return;
            closeModal();
            postMessage({ cmd: 'addConfirmed', path, storageOptions: '' });
        }
    }

    modalInput.addEventListener('input', filterSuggestions);
    modalInput.addEventListener('keydown', (e) => {
        if (!modalState) return;
        if (e.key === 'Escape') { closeModal(); e.preventDefault(); return; }
        if (e.key === 'Enter') { submitModal(); e.preventDefault(); return; }
        if (modalState.mode !== 'create-spec') return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            const dir = e.key === 'ArrowDown' ? 1 : -1;
            const n = modalState.filtered.length;
            if (n === 0) return;
            modalState.active = (modalState.active + dir + n) % n;
            modalInput.value = modalState.filtered[modalState.active];
            modalOk.disabled = false;
            renderSuggestions();
            e.preventDefault();
        }
        if (e.key === 'Tab' && modalState.filtered.length > 0) {
            modalInput.value = modalState.filtered[modalState.active];
            modalOk.disabled = false;
            filterSuggestions();
            e.preventDefault();
        }
    });
    modalOk.addEventListener('click', () => submitModal());
    modalCancel.addEventListener('click', () => closeModal());
    modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) closeModal();
    });

    // --- Toolbar ----------------------------------------------------------
    const addDropdown = $id('add-dropdown');
    $id('btn-add').addEventListener('click', () => {
        addDropdown.classList.add('hidden');
        postMessage({ cmd: 'add' });
    });
    $id('btn-add-chevron').addEventListener('click', (e) => {
        e.stopPropagation();
        addDropdown.classList.toggle('hidden');
    });
    $id('add-local').addEventListener('click', () => {
        addDropdown.classList.add('hidden');
        postMessage({ cmd: 'add' });
    });
    $id('add-advanced').addEventListener('click', () => {
        addDropdown.classList.add('hidden');
        openAddAdvancedModal();
    });
    document.addEventListener('click', () => addDropdown.classList.add('hidden'));

    // --- Advanced-add modal -----------------------------------------------
    const addAdvancedOverlay = $id('add-advanced-overlay');
    const addAdvancedPath    = $id('add-advanced-path');
    const addAdvancedSo      = $id('add-advanced-so');
    const addAdvancedOk      = $id('add-advanced-ok');
    const addAdvancedCancel  = $id('add-advanced-cancel');

    function openAddAdvancedModal() {
        addAdvancedPath.value = '';
        addAdvancedSo.value = '';
        addAdvancedOverlay.classList.remove('hidden');
        setTimeout(() => addAdvancedPath.focus(), 0);
    }
    function closeAddAdvancedModal() {
        addAdvancedOverlay.classList.add('hidden');
    }
    function submitAddAdvanced() {
        const p = addAdvancedPath.value.trim();
        if (!p) return;
        const so = addAdvancedSo.value.trim();
        closeAddAdvancedModal();
        postMessage({ cmd: 'addConfirmed', path: p, storageOptions: so });
    }
    addAdvancedOk.addEventListener('click', submitAddAdvanced);
    addAdvancedCancel.addEventListener('click', closeAddAdvancedModal);
    addAdvancedOverlay.addEventListener('click', (e) => {
        if (e.target === addAdvancedOverlay) closeAddAdvancedModal();
    });
    addAdvancedPath.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitAddAdvanced();
        if (e.key === 'Escape') closeAddAdvancedModal();
    });
    addAdvancedSo.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitAddAdvanced();
        if (e.key === 'Escape') closeAddAdvancedModal();
    });
    $id('btn-reload').addEventListener('click', () => postMessage({ cmd: 'reload' }));
    $id('btn-configure').addEventListener('click', () => postMessage({ cmd: 'configure' }));
    searchEl.addEventListener('input', () => render());
    searchClear.addEventListener('click', () => { searchEl.value = ''; render(); });

    // --- Incoming message dispatcher --------------------------------------
    function dispatch(msg) {
        if (!msg || typeof msg !== 'object') return;
        if (msg.type === 'loading') {
            spinnerEl.classList.toggle('hidden', !msg.loading);
        } else if (msg.type === 'data') {
            info = msg.info;
            enums = msg.enums || {};
            library = msg.library || {};
            if (selection && !library[selection.url]) selection = null;
            render();
        } else if (msg.type === 'openCreateSpecModal') {
            openCreateSpecModal(msg.url, msg.specs || []);
        } else if (msg.type === 'openAddModal') {
            openAddModal();
        }
    }
    // Expose for hosts that want to push messages directly.
    window.__projspecDeliver = dispatch;

    // Hand control to the transport; it will arrange for dispatch() to be
    // called for each incoming host->panel message, then tell the host the
    // panel is ready by sending {cmd:'ready'}.
    transport.onReady(dispatch);
    postMessage({ cmd: 'ready' });
})();
