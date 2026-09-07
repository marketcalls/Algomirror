/* ---------------------------------------------------------------------------
   The investment note.

   Shared by the Watch List and Holdings, which is why it is a file rather than
   two copies in two templates. The dialog is built here and injected into the
   page the first time it is opened, so neither template carries markup for a
   feature neither of them owns.

   What this is for: everything else on those screens is a fact a broker
   reported. This is the only thing that is the admin's own - why he bought,
   and what would make him wrong. No API returns that, and it is the first
   thing forgotten.

   Three boxes rather than one. Investment Thesis, Risk and To Watch are
   different thoughts, and written as one paragraph in a hurry everything after
   the first one stops getting written. Risk is what could go wrong; To Watch is
   the number or the date that would tell you it IS going wrong, which is a
   different sentence.

   The thesis gets half the height and the other two a quarter each, at the
   owner's instruction. Side by side they would have been about 160px wide in
   this dialog, which is too narrow to write a sentence in.

   Earlier versions came off this dialog once, at the owner's instruction, and
   went back on on 6 September when he asked to be able to revert. They were
   recorded on the server throughout - a record costs nothing to keep and
   cannot be recovered once it stops being kept - so bringing them back was a
   change to this file alone.

   RESTORE PUTS THE TEXT IN THE BOXES. It does not write.

   That is deliberate and it is the whole safety of the feature. Saving is the
   one path that copies the CURRENT text into the history before overwriting
   it, so a restore that went straight to the server would have needed its own
   copy of that rule - and a rule written twice is a rule that will one day
   disagree with itself. Here there is nothing to disagree with: Restore fills
   the boxes, you read them, and Save does what Save has always done.

   Only classes already present in the compiled stylesheet are used here.
   Tailwind is compiled in this application, so a class invented in a JavaScript
   string renders as nothing at all.
   --------------------------------------------------------------------------- */

(function () {
    'use strict';

    var DIALOG_ID = 'equity-note-modal';
    var FETCH_TIMEOUT_MS = 20000;

    var state = {
        symbol: null,
        exchange: null,
        loaded: false,
        saving: false,
        onSaved: null,
        /* Earlier versions of this note, newest first, as the server sent
           them. They have always been in the payload; from 6 September the
           dialog draws them again. */
        versions: [],
        historyOpen: false
    };

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = text; }
        return node;
    }

    function build() {
        var existing = document.getElementById(DIALOG_ID);
        if (existing) { return existing; }

        var dialog = el('dialog', 'modal');
        dialog.id = DIALOG_ID;

        var box = el('div', 'modal-box max-w-2xl');

        /* One line: stock, what this is, and when it was last saved. Two lines
           were saying the stock's name twice. The saved time is inside the same
           line but small and grey - it is a footnote to the heading, not part
           of the heading. */
        var title = el('h3', 'font-bold text-lg');
        title.id = 'equity-note-title';
        var name = el('span');
        name.id = 'equity-note-name';
        title.appendChild(name);
        var when = el('span', 'text-sm text-base-content/60');
        when.id = 'equity-note-saved';
        /* Set here rather than with a class: Tailwind is COMPILED in this
           application and .font-normal is not in the built stylesheet, so the
           class would do nothing and this span would inherit the heading's
           bold. */
        when.style.fontWeight = '400';
        title.appendChild(when);
        box.appendChild(title);

        /* Half the height to the thesis, a quarter each to the other two.
           rows 10 / 5 / 5 is that split exactly. */
        box.appendChild(field('Investment Thesis', 'equity-note-thesis', 10,
            'Why you own this, or why you would.'));
        box.appendChild(field('Risk', 'equity-note-risk', 5,
            'What could go wrong.'));
        box.appendChild(field('To Watch', 'equity-note-to-watch', 5,
            'The number, date or trigger that would tell you it is going wrong.'));

        box.appendChild(history());

        var status = el('p', 'text-xs text-error mt-2');
        status.id = 'equity-note-status';
        box.appendChild(status);

        /* A second line, in green, for something that WORKED. The line above
           it is red and belongs to failures; putting "restored" in a red line
           would read as a fault. */
        var restored = el('p', 'text-xs text-success mt-2');
        restored.id = 'equity-note-restored';
        box.appendChild(restored);

        var actions = el('div', 'modal-action');
        var form = document.createElement('form');
        form.method = 'dialog';
        /* Cancel, not Back: this abandons something you were about to commit. */
        form.appendChild(el('button', 'btn btn-sm btn-outline equity-btn', 'Cancel'));
        actions.appendChild(form);

        var save = el('button', 'btn btn-sm btn-primary equity-btn', 'Save Note');
        save.type = 'button';
        save.id = 'equity-note-save';
        save.onclick = submit;
        actions.appendChild(save);
        box.appendChild(actions);

        dialog.appendChild(box);
        document.body.appendChild(dialog);
        return dialog;
    }

    function field(label, id, rows, help) {
        var wrap = el('div', 'form-control mt-3');

        var labelEl = el('label', 'label');
        labelEl.htmlFor = id;
        labelEl.appendChild(el('span', 'label-text font-semibold', label));
        wrap.appendChild(labelEl);

        var box = document.createElement('textarea');
        box.id = id;
        box.className = 'textarea textarea-bordered w-full';
        box.rows = rows;
        box.maxLength = 8000;
        wrap.appendChild(box);

        var hint = el('label', 'label');
        hint.appendChild(el('span', 'label-text-alt', help));
        wrap.appendChild(hint);

        return wrap;
    }

    /* ----------------------------------------------------------------
       History
       ---------------------------------------------------------------- */

    function history() {
        var wrap = el('div', 'mt-3');

        var bar = el('div', 'flex items-center gap-3');
        /* Hollow. It opens a panel and commits nothing, which is what hollow
           means everywhere else in this module. */
        var toggle = el('button', 'btn btn-xs btn-outline equity-btn', 'History');
        toggle.type = 'button';
        toggle.id = 'equity-note-history-toggle';
        toggle.onclick = toggleHistory;
        bar.appendChild(toggle);

        var note = el('span', 'text-xs text-base-content/60');
        note.id = 'equity-note-history-count';
        bar.appendChild(note);
        wrap.appendChild(bar);

        var panel = el('div', 'mt-2 hidden');
        panel.id = 'equity-note-history-panel';
        /* Inline, not a utility class. Tailwind is COMPILED here, so a class
           that is not already in the built stylesheet renders as nothing -
           and a panel with no height limit would push Save off the screen on
           a note with ten versions. */
        panel.style.maxHeight = '15rem';
        panel.style.overflowY = 'auto';
        panel.style.border = '1px solid oklch(var(--bc) / 0.2)';
        panel.style.borderRadius = '0.5rem';
        panel.style.padding = '0.5rem';
        wrap.appendChild(panel);

        return wrap;
    }

    function toggleHistory() {
        state.historyOpen = !state.historyOpen;
        drawHistory();
    }

    function preview(version) {
        /* The first words of whichever box has words in it, so a row can be
           told from the row above without opening it. */
        var fields = ['thesis', 'risk', 'to_watch'];
        for (var i = 0; i < fields.length; i++) {
            var text = String(version[fields[i]] || '').replace(/\s+/g, ' ').trim();
            if (text) {
                return text.length > 90 ? text.slice(0, 90) + '...' : text;
            }
        }
        return '(all three boxes empty)';
    }

    function filled(version) {
        var names = [];
        if (String(version.thesis || '').trim()) { names.push('Thesis'); }
        if (String(version.risk || '').trim()) { names.push('Risk'); }
        if (String(version.to_watch || '').trim()) { names.push('To Watch'); }
        return names.length ? names.join(', ') : 'nothing';
    }

    function drawHistory() {
        var toggle = document.getElementById('equity-note-history-toggle');
        var count = document.getElementById('equity-note-history-count');
        var panel = document.getElementById('equity-note-history-panel');
        if (!toggle || !panel || !count) { return; }

        var versions = state.versions || [];
        toggle.textContent = state.historyOpen ? 'Hide History' : 'History';
        toggle.disabled = !versions.length;

        /* A version is written only when a save overwrites text that HAD
           words in it, so a brand new note and a save that changed nothing
           both add none. Said out loud, because an empty list otherwise reads
           as a feature that is not working. */
        count.textContent = versions.length
            ? (versions.length === 1 ? '1 earlier version kept'
                : versions.length + ' earlier versions kept, newest first')
            : 'No earlier versions yet. One is kept each time you save over '
                + 'text that had words in it.';

        /* Emptied BEFORE the early return, not after it.
           Returning while the panel still held rows left the previous stock's
           note text sitting in the page - hidden, so nobody would have seen
           it, but there and one mistaken un-hide away from being shown
           against the wrong symbol. Caught by the harness. */
        panel.textContent = '';
        panel.classList.toggle('hidden', !state.historyOpen || !versions.length);
        if (!state.historyOpen || !versions.length) { return; }
        versions.forEach(function (version, index) {
            var row = el('div', 'flex items-center justify-between gap-3');
            if (index) {
                row.style.borderTop = '1px solid oklch(var(--bc) / 0.12)';
                row.style.paddingTop = '0.5rem';
                row.style.marginTop = '0.5rem';
            }

            var left = el('div');
            left.style.minWidth = '0';
            left.appendChild(el('div', 'text-xs font-semibold', stamp(version.saved_at)));
            var body = el('div', 'text-xs text-base-content/60', preview(version));
            body.style.overflowWrap = 'anywhere';
            left.appendChild(body);
            left.appendChild(el('div', 'text-xs text-base-content/50',
                'Had: ' + filled(version)));
            row.appendChild(left);

            /* Hollow, like the toggle: this fills the boxes and writes
               nothing. Save is the only button on this dialog that writes. */
            var put = el('button', 'btn btn-xs btn-outline equity-btn', 'Restore');
            put.type = 'button';
            put.style.flexShrink = '0';
            put.title = 'Put this text back in the boxes above. Nothing is '
                + 'saved until you press Save Note.';
            put.onclick = function () { restore(version); };
            row.appendChild(put);

            panel.appendChild(row);
        });
    }

    function restore(version) {
        setValue('equity-note-thesis', version.thesis);
        setValue('equity-note-risk', version.risk);
        setValue('equity-note-to-watch', version.to_watch);

        state.historyOpen = false;
        drawHistory();

        setText('equity-note-status', '');
        setText('equity-note-restored',
            'The text from ' + stamp(version.saved_at) + ' is now in the boxes '
            + 'above. Nothing has been saved yet - press Save Note to keep it, '
            + 'or Cancel to leave the note exactly as it was. What is in the '
            + 'note now will itself be kept in the history when you save.');
    }

    function value(id) {
        var node = document.getElementById(id);
        return node ? String(node.value || '') : '';
    }

    function setValue(id, text) {
        var node = document.getElementById(id);
        if (node) { node.value = text || ''; }
    }

    function setText(id, text) {
        var node = document.getElementById(id);
        if (node) { node.textContent = text || ''; }
    }

    /* The month names, written out.
     *
     * NOT taken from Intl. Asked for a short month name, Intl renders
     * September as "Sept" in some engines and "Sep" in others - it does say
     * "Sept" in Node - so the same date would read one way in this dialog and
     * another on the Updated line, which builds its month from a table for
     * exactly this reason. One table, one spelling, every screen.
     */
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    function stamp(iso) {
        /*
         * The server sends UTC with no timezone marker, and a marker-less value
         * is read by the browser as LOCAL time - which put this clock 5h30m
         * behind every other clock in the application. Mark it as UTC, then
         * render explicitly in IST so it agrees with the market whatever the
         * machine thinks the time is.
         *
         * Only NUMBERS come from Intl below - day, month number, year, hour,
         * minute. Those do not vary between engines. The month NAME and the
         * am/pm come from here.
         */
        if (!iso) { return ''; }
        try {
            var text = String(iso);
            if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) { text += 'Z'; }
            var d = new Date(text);
            if (isNaN(d.getTime())) { return String(iso); }

            var parts = {};
            new Intl.DateTimeFormat('en-GB', {
                timeZone: 'Asia/Kolkata',
                year: 'numeric', month: 'numeric', day: '2-digit',
                hour: '2-digit', minute: '2-digit', hour12: false
            }).formatToParts(d).forEach(function (part) {
                parts[part.type] = part.value;
            });

            var month = MONTHS[Number(parts.month) - 1];
            if (!month) { return String(iso); }

            /* 24 renders as hour "24" for midnight in some engines under
               hour12:false, which would print 12 pm for midnight if it were
               taken modulo 12 unguarded. */
            var hour = Number(parts.hour) % 24;
            var suffix = hour < 12 ? 'am' : 'pm';
            var shown = hour % 12;
            if (shown === 0) { shown = 12; }

            return parts.day + ' ' + month + ' ' + parts.year + ', '
                + (shown < 10 ? '0' + shown : String(shown))
                + ':' + parts.minute + ' ' + suffix;
        } catch (error) {
            return String(iso);
        }
    }

    function heading(savedAt) {
        /* GENUSPOWER - Investment Note - last saved 05 Sep 2026, 07:20 pm
           with only the first half carrying the heading's weight. */
        setText('equity-note-name',
            (state.symbol || '') + ' - Investment Note');
        setText('equity-note-saved',
            savedAt ? '  -  last saved ' + stamp(savedAt) : '');
    }

    function apply(note) {
        var data = note || {};
        setValue('equity-note-thesis', data.thesis);
        setValue('equity-note-risk', data.risk);
        setValue('equity-note-to-watch', data.to_watch);
        heading(data.updated_at);
        /* Redrawn from every read AND every save, so the version you have just
           created is in the list before you look for it. */
        state.versions = Array.isArray(data.versions) ? data.versions : [];
        drawHistory();
        state.loaded = true;
    }

    function open(symbol, exchange, onSaved) {
        state.symbol = String(symbol || '').toUpperCase();
        state.exchange = String(exchange || 'NSE').toUpperCase();
        state.onSaved = typeof onSaved === 'function' ? onSaved : null;
        state.loaded = false;

        build();
        heading(null);
        setText('equity-note-status', '');
        setText('equity-note-restored', '');
        setValue('equity-note-thesis', '');
        setValue('equity-note-risk', '');
        setValue('equity-note-to-watch', '');
        /* The dialog is built once and reused for every stock, so last
           stock's history has to be cleared here or it would be offered for
           restoring into this one. */
        state.versions = [];
        state.historyOpen = false;
        drawHistory();

        var dialog = document.getElementById(DIALOG_ID);
        if (dialog && typeof dialog.showModal === 'function') { dialog.showModal(); }

        load();
    }

    async function load() {
        var params = new URLSearchParams();
        params.set('symbol', state.symbol);
        params.set('exchange', state.exchange);
        try {
            var response = await fetch('/equity/api/notes?' + params.toString(), {
                credentials: 'same-origin',
                signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
            });
            var data = await response.json();
            if (data.status !== 'success') {
                setText('equity-note-status', data.message || 'The note could not be read.');
                return;
            }
            apply(data.note);
        } catch (error) {
            setText('equity-note-status',
                'The note could not be read. Nothing has been changed.');
            console.error('Equity note load failed:', error);
        }
    }

    async function submit() {
        var button = document.getElementById('equity-note-save');
        /* Disabled as the FIRST statement, so a double click cannot save twice
           and manufacture an empty version in the history. */
        if (state.saving) { return; }
        state.saving = true;
        if (button) { button.disabled = true; }
        setText('equity-note-status', '');
        setText('equity-note-restored', '');

        if (!state.loaded) {
            /* The read never finished, so what is in the boxes is not this
               stock's note - saving it would overwrite a note nobody has seen. */
            setText('equity-note-status',
                'This note has not finished loading, so nothing was saved. Close '
                + 'this and open it again.');
            state.saving = false;
            if (button) { button.disabled = false; }
            return;
        }

        try {
            var response = await fetchWithCSRF('/equity/api/notes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({
                    symbol: state.symbol,
                    exchange: state.exchange,
                    thesis: value('equity-note-thesis'),
                    risk: value('equity-note-risk'),
                    to_watch: value('equity-note-to-watch')
                }),
                signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
            });
            var data = await response.json();

            if (data.status !== 'success') {
                setText('equity-note-status', data.message || 'The note could not be saved.');
                return;
            }

            apply(data.note);
            if (typeof showToast === 'function') {
                showToast(data.message || 'Note saved', 'success');
            }
            var dialog = document.getElementById(DIALOG_ID);
            if (dialog && typeof dialog.close === 'function') { dialog.close(); }
            if (state.onSaved) {
                state.onSaved(state.symbol, state.exchange, Boolean(data.note && data.note.has_note));
            }
        } catch (error) {
            setText('equity-note-status',
                'The note could not be saved. Nothing has been changed. Copy your '
                + 'text somewhere before closing this.');
            console.error('Equity note save failed:', error);
        } finally {
            state.saving = false;
            if (button) { button.disabled = false; }
        }
    }

    function button(symbol, exchange, hasNote, onSaved) {
        /*
         * The Notes button for one table row.
         *
         * Filled when there is something behind it, outlined when there is not,
         * so the table says at a glance which stocks have been written about.
         * Without that you would have to open every row to find out, which is
         * the same as not having the feature.
         */
        var node = document.createElement('button');
        node.type = 'button';
        node.className = hasNote ? 'btn btn-xs btn-primary' : 'btn btn-xs btn-outline';
        node.textContent = 'Notes';
        node.title = hasNote
            ? 'Read or edit your note on ' + symbol
            : 'Write your thesis and the risk for ' + symbol;
        node.onclick = function () { open(symbol, exchange, onSaved); };
        return node;
    }

    window.EquityNotes = { open: open, button: button };
}());
