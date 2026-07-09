/* projspec combined-panel tab coordination.
 *
 * Shared by all GUI hosts.  Must be loaded AFTER both panel.js and
 * filebrowser.js have been initialised.
 *
 * Requires:
 *   window.__projspecTabInit(initialTab)   — called once by the host
 *       bootstrap to activate the correct starting tab.
 *
 * The host delivers messages to each tab by calling:
 *   window.__libDispatch(msg)      — deliver to library panel
 *   window.__projspecFbDeliver(msg) — deliver to filebrowser panel
 *
 * Both of these are set up by the respective panel bootstraps before
 * this script calls __projspecTabInit.
 */
(function () {
    var _activeTab = 'library';

    function showTab(tab) {
        _activeTab = tab;
        ['library', 'filebrowser'].forEach(function (t) {
            var pane = document.getElementById('tab-' + t);
            var btn  = document.getElementById('tab-btn-' + t);
            if (!pane || !btn) return;
            if (t === tab) {
                pane.classList.remove('hidden');
                pane.classList.add('active');
                btn.classList.add('active');
            } else {
                pane.classList.add('hidden');
                pane.classList.remove('active');
                btn.classList.remove('active');
            }
        });
    }

    // Wire tab buttons
    var libBtn = document.getElementById('tab-btn-library');
    var fbBtn  = document.getElementById('tab-btn-filebrowser');
    if (libBtn) libBtn.addEventListener('click', function () { showTab('library'); });
    if (fbBtn)  fbBtn.addEventListener('click',  function () { showTab('filebrowser'); });

    // Public API used by host to switch tabs programmatically
    window.__projspecShowTab = showTab;

    // Called by host bootstrap once everything is set up
    window.__projspecTabInit = function (initialTab) {
        showTab(initialTab || 'library');
    };
})();
