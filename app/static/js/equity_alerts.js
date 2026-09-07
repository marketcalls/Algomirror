/**
 * Watch list price alerts, on every equity screen.
 *
 * Alerts are raised by the background monitor, not by the screen. That is the
 * whole point of them: they fire for as long as AlgoMirror is running, whether
 * or not anybody has the Watch List open. This file is only the delivery. It
 * asks the server every few seconds whether anything has fired that has not yet
 * been shown, pops what it finds into the bottom right corner, and then tells
 * the server it has been shown so it is not raised again.
 *
 * Acknowledging afterwards rather than letting the server mark them on read is
 * deliberate: a reply that never arrives would otherwise lose the alert in
 * silence, which is the one failure an alert must not have.
 *
 * Only alerts fired in the last few minutes are popped. An alert from this
 * morning is not news by the afternoon, and popping a whole morning's worth the
 * moment the module is opened means four visible at a time and the rest
 * scrolling past unread. Older ones stay unread, which is what the count on the
 * Alerts menu item shows and what the Log on that screen is for.
 *
 * Included by every equity template, so the corner behaves the same whichever
 * screen the admin is on. A page that wants to do something more with an alert
 * - the Alerts screen refreshes itself - sets window.EquityAlerts.onAlert.
 */
(function () {
    'use strict';

    var POLL_MS = 10000;
    var TOAST_MS = 25000;
    var MAX_TOASTS = 4;
    var PENDING_URL = '/equity/api/alerts/pending';
    var ACKNOWLEDGE_URL = '/equity/api/alerts/acknowledge';
    var UNREAD_URL = '/equity/api/alerts/unread';

    // Still the same id, now sitting on the Watch List item: the Alerts menu
    // it used to live on is gone, and the Watch List carries the Log.
    var NAV_BADGE_ID = 'equity-alert-nav-badge';
    var WATCHLIST_BADGE_ID = 'equity-watchlist-nav-badge';

    // The two on the Holdings item, replacing the single confirm-waiting badge
    // on 6 September at the owner's instruction: red for a stop loss breached,
    // green for a target reached. Nothing was lost by the swap - a breach
    // waiting for a decision is a breach, so it is still counted; it is only
    // no longer counted separately, and the red badge's hover says how many of
    // them are waiting.
    var SL_BADGE_ID = 'equity-sl-nav-badge';
    var TP_BADGE_ID = 'equity-tp-nav-badge';

    var STYLE_ID = 'equity-alert-style';
    var STACK_ID = 'equity-alert-stack';

    var polling = false;
    var seen = {};

    window.EquityAlerts = window.EquityAlerts || { onAlert: null };

    // ------------------------------------------------------------------
    // The corner
    // ------------------------------------------------------------------

    function injectStyle() {
        if (document.getElementById(STYLE_ID)) { return; }
        var style = document.createElement('style');
        style.id = STYLE_ID;
        // Written out here rather than as utility classes: the compiled
        // stylesheet is built from the classes the templates already use, and a
        // class that appears only in a JavaScript string is not among them.
        style.textContent =
            '#' + STACK_ID + '{position:fixed;right:1rem;bottom:1rem;z-index:60;' +
            'display:flex;flex-direction:column;gap:0.5rem;' +
            'max-width:min(24rem,calc(100vw - 2rem));}' +
            '#' + STACK_ID + ':empty{display:none;}';
        document.head.appendChild(style);
    }

    function stack() {
        var existing = document.getElementById(STACK_ID);
        if (existing) { return existing; }

        injectStyle();
        var created = document.createElement('div');
        created.id = STACK_ID;
        // Polite rather than assertive: a screen reader finishes what it is
        // saying before announcing the alert.
        created.setAttribute('aria-live', 'polite');
        document.body.appendChild(created);
        return created;
    }

    function toast(message) {
        var host = stack();

        var box = document.createElement('div');
        box.className = 'alert alert-warning shadow-lg';

        var text = document.createElement('span');
        text.className = 'text-sm';
        text.textContent = message;
        box.appendChild(text);

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn btn-ghost btn-xs';
        close.setAttribute('aria-label', 'Dismiss this alert');
        close.textContent = '✕';
        close.onclick = function () { remove(box); };
        box.appendChild(close);

        host.appendChild(box);

        while (host.children.length > MAX_TOASTS) {
            host.removeChild(host.firstChild);
        }

        setTimeout(function () { remove(box); }, TOAST_MS);
    }

    function remove(box) {
        if (box && box.parentNode) { box.parentNode.removeChild(box); }
    }

    // ------------------------------------------------------------------
    // The poll
    // ------------------------------------------------------------------

    function post(url, body) {
        if (typeof window.fetchWithCSRF === 'function') {
            return window.fetchWithCSRF(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(body)
            });
        }

        // Same request without the shared helper, for a page that has not
        // loaded it. The token is on the meta tag base.html writes.
        var meta = document.querySelector('meta[name="csrf-token"]');
        var headers = { 'Content-Type': 'application/json' };
        if (meta) { headers['X-CSRFToken'] = meta.getAttribute('content'); }
        return fetch(url, {
            method: 'POST',
            headers: headers,
            credentials: 'same-origin',
            body: JSON.stringify(body)
        });
    }

    function poll() {
        // A tab left in the background piles up nothing useful, and waking to a
        // wall of stale popups helps nobody. Wait until it is looked at again.
        if (document.hidden) { return; }
        if (polling) { return; }
        polling = true;

        fetch(PENDING_URL, { credentials: 'same-origin' })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (!data || data.status !== 'success') { return; }

                var alerts = data.alerts || [];
                var shown = [];
                var shownNotices = [];

                alerts.forEach(function (alert) {
                    // The feed carries two kinds of row from two tables, so an
                    // id is only unique alongside its source. Keying the seen
                    // set on the pair is what stops a price alert and a holding
                    // notice that happen to share an id from hiding each other.
                    var isNotice = alert.source === 'holding';
                    var key = (alert.source || 'alert') + ':' + alert.id;

                    // Two polls can overlap on a slow connection. The key set
                    // stops the same row being shown twice in that window.
                    if (seen[key]) { return; }
                    seen[key] = true;

                    toast(alert.message);
                    if (isNotice) {
                        shownNotices.push(alert.id);
                    } else {
                        shown.push(alert.id);
                    }

                    if (typeof window.EquityAlerts.onAlert === 'function') {
                        try {
                            window.EquityAlerts.onAlert(alert);
                        } catch (hookError) {
                            console.error('Equity alert hook failed:', hookError);
                        }
                    }
                });

                if (shown.length || shownNotices.length) {
                    post(ACKNOWLEDGE_URL, { ids: shown, notice_ids: shownNotices })
                        ['catch'](function (error) {
                            // Not fatal. The row has been seen; the worst case
                            // is that it is offered again on a later poll, and
                            // the key set above keeps it from being shown twice.
                            console.error('Equity alert acknowledge failed:', error);
                        });
                }
            })
            ['catch'](function (error) {
                console.error('Equity alert poll failed:', error);
            })
            ['finally'](function () {
                polling = false;
            });
    }

    // ------------------------------------------------------------------
    // The unread count on the menu
    // ------------------------------------------------------------------

    function paintBadge(id, count, title) {
        var badge = document.getElementById(id);
        if (!badge) { return; }
        var value = Number(count) || 0;
        badge.textContent = value ? String(value) : '';
        badge.classList.toggle('hidden', value === 0);
        // Set every time, and cleared when there is no title, so a badge can
        // never keep the hover text of a count it no longer shows.
        badge.title = title || '';
    }

    // "3 stop losses breached, 1 waiting for your decision."
    //
    // The count of decisions waiting has no badge of its own any more, so it
    // is said here instead of being dropped. Only ever ADDED to the red badge:
    // a target reached does not wait for anybody in Auto Sell, and saying it
    // twice would make two numbers out of one.
    function breachTitle(count, waiting) {
        var text = count + (count === 1 ? ' stop loss' : ' stop losses') + ' breached';
        if (Number(waiting) > 0) {
            text += ', ' + waiting + ' waiting for your decision';
        }
        return text + '.';
    }

    // Four badges, one request. The Alerts badge counts what has fired and not
    // been read; the Watch List badge counts alerts sitting fired; the two on
    // Holdings count stocks whose stop loss or target has been breached. They
    // answer different questions, so a breached stop loss still shows on
    // Holdings after its alert has been read off the Alerts badge.
    function refreshBadge() {
        if (!document.getElementById(NAV_BADGE_ID)
            && !document.getElementById(SL_BADGE_ID)
            && !document.getElementById(TP_BADGE_ID)
            && !document.getElementById(WATCHLIST_BADGE_ID)) {
            return;
        }

        fetch(UNREAD_URL, { credentials: 'same-origin' })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (!data || data.status !== 'success') { return; }
                paintBadge(NAV_BADGE_ID, data.unread);
                paintBadge(WATCHLIST_BADGE_ID, data.alerts_triggered);
                paintBadge(
                    SL_BADGE_ID,
                    data.sl_hit,
                    breachTitle(Number(data.sl_hit) || 0, data.confirm_pending)
                );
                paintBadge(
                    TP_BADGE_ID,
                    data.tp_hit,
                    (Number(data.tp_hit) || 0) === 1
                        ? '1 target reached.'
                        : (Number(data.tp_hit) || 0) + ' targets reached.'
                );
            })
            ['catch'](function () {
                // A badge that cannot be read is simply not shown. There is
                // nothing here worth putting an error on screen for.
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        poll();
        refreshBadge();
        setInterval(function () {
            poll();
            refreshBadge();
        }, POLL_MS);
    });

    // Coming back to a tab should not mean waiting out the rest of the interval
    // to hear about something that fired while it was hidden.
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) {
            poll();
            refreshBadge();
        }
    });
}());
