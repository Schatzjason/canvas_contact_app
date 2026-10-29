// Autosaving, auto-sizing notes textarea — shared by the student page and
// the inbox page's student panel.
//
//   var note = attachNoteAutosave(textarea, statusEl, saveUrl);
//   note.flush();   // save right now if there are unsaved edits
//
// saveUrl is the student's .../note endpoint; it takes {content: "..."}.
(function (global) {
  var LINE_HEIGHT = 1.65;   // matches the .notes-textarea CSS line-height
  var MIN_ROWS    = 3;
  var EXTRA_ROWS  = 2;
  var MAX_ROWS    = 15;
  var DEBOUNCE_MS = 800;

  global.attachNoteAutosave = function (ta, status, saveUrl) {
    var saveTimer = null;
    var dirty = false;

    function resize() {
      var content = ta.value;
      var lineCount = content === '' ? 0 : content.split('\n').length;
      var rows = content === ''
        ? MIN_ROWS
        : Math.min(MAX_ROWS, lineCount + EXTRA_ROWS);
      ta.rows = Math.max(MIN_ROWS, rows);
    }

    function save(keepalive) {
      clearTimeout(saveTimer);
      saveTimer = null;
      dirty = false;
      fetch(saveUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content: ta.value}),
        keepalive: !!keepalive,   // lets a save survive the page closing
      }).then(function (r) {
        if (!r.ok) throw new Error('save failed');
        status.textContent = 'Saved';
        status.style.opacity = '1';
        setTimeout(function () { status.style.opacity = '0'; }, 1500);
      }).catch(function () {
        dirty = true;
        status.textContent = 'Save failed';
        status.style.opacity = '1';
      });
    }

    function flush(keepalive) {
      if (dirty) save(keepalive);
    }

    ta.addEventListener('input', function () {
      resize();
      dirty = true;
      clearTimeout(saveTimer);
      status.textContent = '';
      saveTimer = setTimeout(save, DEBOUNCE_MS);
    });
    // Don't lose the last <800ms of typing when the tab closes or navigates away.
    global.addEventListener('pagehide', function () { flush(true); });

    resize();
    return {flush: flush, resize: resize};
  };
})(window);
