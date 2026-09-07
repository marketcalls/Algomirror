/*
 * The "Updated" line, on every equity screen.
 *
 * One definition, loaded by every screen that carries the line, because the
 * owner's rule is that inside a module one thing is done one way - and a line
 * that appears on nine screens is exactly the thing that drifts when each
 * screen writes its own.
 *
 * What it says: "Updated 06 Sep 2026, 12:47:05". Date as well as time, because
 * a screen left open overnight showed a time with no day against it and read
 * as current. No "IST" suffix: the whole application is Indian and the tag was
 * saying nothing the reader did not know.
 *
 * What its colour says: GREEN when the prices on this screen are MOVING,
 * AMBER when they are not. That is the difference between a figure that is
 * live and yesterday's figure, and it is worth a colour rather than a
 * sentence.
 *
 * MOVEMENT, NOT THE CLOCK. This was written first as trading hours - a
 * weekday between 09:15 and 15:30 - and the owner replaced it on 6 September
 * with the better rule: if an LTP on the screen has changed in the last
 * minute, the market is trading. A clock cannot know an exchange holiday and
 * this does not have to: on a holiday nothing moves, and nothing moving is
 * exactly what amber means.
 *
 * It also costs no server call and no new field. The screen already knows the
 * prices it just drew; it hands them here as one string and this remembers
 * whether that string changed.
 *
 * The elapsed time is measured between two SERVER timestamps, never against
 * the browser's own clock, which may be set to anything.
 *
 * Three states, and the third is the honest one:
 *   moving   - an LTP changed within the last minute. GREEN.
 *   still    - a minute has passed with nothing moving. AMBER.
 *   waiting  - the screen has only been looked at once, so no change can have
 *              been seen yet. No colour, because there is nothing to say.
 *
 * A screen with no prices on it - Settings, Accounts, the books - passes
 * nothing and gets no colour. A colour there would be a guess.
 */
(function () {
    'use strict';

    var IST = 'Asia/Kolkata';

    // ONE minute, at the owner's instruction on 7 September. It was two.
    //
    // He watched a live, moving market and the line stayed grey, so the window
    // was doing the opposite of its job. Shorter means the colour follows the
    // market more closely; the cost is that a genuinely quiet minute in a thin
    // stock now shows amber, which is a true statement about that screen even
    // when the market as a whole is open.
    var STILL_AFTER_MS = 60 * 1000;

    /*
     * What each line has seen: the prices it was last given, and the server
     * time at which they last CHANGED.
     *
     * Keyed by element id, so two lines on one screen - Place Order carries
     * three - never read each other's history.
     */
    var seen = {};

    /*
     * The month names, written out here rather than taken from the browser.
     *
     * Intl gives "Sept" for September under en-GB in some engines and "Sep" in
     * others, so the same screen read differently in two browsers. A fixed
     * table costs three lines and cannot drift.
     */
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    /*
     * The server sends UTC with NO timezone marker, and a marker-less value is
     * read by the browser as LOCAL time - which put every clock in this module
     * 5h30m behind. Mark it as UTC first, then render in IST explicitly, so the
     * screen agrees with the market whatever the machine's own clock is set to.
     */
    function parse(isoText) {
        if (!isoText) { return null; }
        var text = String(isoText);
        if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) { text += 'Z'; }
        var parsed = new Date(text);
        return isNaN(parsed.getTime()) ? null : parsed;
    }

    /* The date's parts as they read in India, whatever the browser is set to. */
    function istParts(date) {
        var parts = {};
        new Intl.DateTimeFormat('en-GB', {
            timeZone: IST, day: '2-digit', month: '2-digit',
            year: 'numeric', hour: '2-digit', minute: '2-digit',
            second: '2-digit', hour12: false
        }).formatToParts(date).forEach(function (part) {
            parts[part.type] = part.value;
        });
        return parts;
    }

    /*
     * Has anything moved lately?
     *
     * Returns 'moving', 'still' or 'waiting'. The first call on a line can
     * only ever return 'waiting': one reading is not a change.
     */
    function pulse(elementId, priceKey, at) {
        var previous = seen[elementId];
        if (!previous || previous.key !== priceKey) {
            seen[elementId] = {
                key: priceKey,
                changedAt: at,
                everChanged: Boolean(previous)
            };
            previous = seen[elementId];
        }

        if (!previous.everChanged) {
            // Nothing has been seen to change yet. That is 'waiting' while it
            // is still early, and 'still' once a minute has gone by with the
            // same prices - which is what a closed market looks like.
            return (at - previous.changedAt) <= STILL_AFTER_MS ? 'waiting' : 'still';
        }
        return (at - previous.changedAt) <= STILL_AFTER_MS ? 'moving' : 'still';
    }

    function format(date) {
        var parts = istParts(date);
        // "06 Sep 2026, 12:47:05". 24 hour, because every other time in this
        // module is, and a trading screen should not need am/pm read off it.
        var month = MONTHS[(Number(parts.month) || 1) - 1] || parts.month;
        return parts.day + ' ' + month + ' ' + parts.year + ', '
            + parts.hour + ':' + parts.minute + ':' + parts.second;
    }

    window.EquityUpdated = {
        format: format,

        /* Testing seam: forget what every line has seen. */
        reset: function () { seen = {}; },

        /*
         * Write the line into one element.
         *
         * Writes directly rather than through each screen's own equitySetText,
         * because those differ from screen to screen and this must not.
         */
        write: function (elementId, isoText, prefix, priceKey) {
            var el = document.getElementById(elementId);
            if (!el) { return; }

            var date = parse(isoText);
            if (date === null) {
                el.textContent = 'Not loaded yet';
                el.className = 'text-xs text-base-content/50 equity-num';
                el.title = '';
                return;
            }

            el.textContent = (prefix || 'Updated') + ' ' + format(date);

            // No prices on this screen, so no claim about them.
            if (priceKey === undefined || priceKey === null || priceKey === '') {
                el.className = 'text-xs text-base-content/50 equity-num';
                el.title = 'When this screen was last read from the server.';
                return;
            }

            var state = pulse(elementId, String(priceKey), date);
            if (state === 'moving') {
                el.className = 'text-xs equity-num text-success';
                el.title = 'A price on this screen changed within the last '
                    + 'minute, so the market is trading.';
            } else if (state === 'still') {
                el.className = 'text-xs equity-num text-warning';
                el.title = 'No price on this screen has changed for a '
                    + 'minute. The market is closed, or nothing here is '
                    + 'trading right now.';
            } else {
                el.className = 'text-xs text-base-content/50 equity-num';
                el.title = 'Read once so far. Whether prices are moving cannot '
                    + 'be told from a single reading; this turns green on the '
                    + 'first change.';
            }
        },

        /*
         * One string standing for every price on the screen.
         *
         * Built from the caller's rows so this file needs to know nothing
         * about their shape. A symbol is included as well as its price, so a
         * row appearing or leaving counts as a change - which it is.
         */
        priceKey: function (rows, symbolField, priceField) {
            if (!rows || !rows.length) { return ''; }
            var parts = [];
            for (var i = 0; i < rows.length; i += 1) {
                var row = rows[i] || {};
                parts.push(String(row[symbolField || 'symbol'] || '') + '='
                    + String(row[priceField || 'ltp'] === undefined
                        ? '' : row[priceField || 'ltp']));
            }
            return parts.join('|');
        }
    };
}());
