/*
 * Shared helpers for the Equity screens.
 *
 * These lived as copy-pasted definitions inside each template: equitySetText in
 * all eight, equityCell and equityMessageRow in five, and so on. Twenty-seven
 * helpers were duplicated and nineteen of them had already DRIFTED between
 * copies, which is how a money formatter ends up rendering one thing on
 * Holdings and another on Positions. Extracting them is a precondition for
 * taking on more equity templates, not a tidy-up to do afterwards.
 *
 * Reconciliation rules used when copies disagreed, all in the direction of the
 * superset so no caller loses behaviour:
 *
 *   equityMessageRow  Three copies honoured a `columns` argument and defaulted
 *                     to EQUITY_COLUMN_COUNT; one defaulted to a different
 *                     constant and one ignored the argument entirely. The
 *                     argument is now always honoured, and the per-page default
 *                     is read from the page if it defines one.
 *
 *   equityValue       Two copies took a fallback, one did not and returned ''.
 *                     The fallback now defaults to '', which is both.
 *
 *   equityShowStale   Four copies were identical; the fifth delegated to
 *                     equityShowBanner. The direct version is kept, since it
 *                     has no dependency on a helper that itself varies per page.
 *
 * Page-level globals this file may read when a page defines them:
 *   EQUITY_COLUMN_COUNT       default colSpan for an empty-table message row
 *   equityAccountsRendered    set by pages with an account filter
 *
 * No page is required to define them. Everything degrades rather than throwing,
 * because a ReferenceError here would take down the whole screen's script.
 */

'use strict';

/* ------------------------------------------------------------------ text */

function equitySetText(id, text, className) {
    const el = document.getElementById(id);
    if (!el) { return; }
    el.textContent = text;
    if (className !== undefined) {
        el.className = className;
    }
}

function equityValue(id, fallback) {
    const resolved = fallback === undefined ? '' : fallback;
    const el = document.getElementById(id);
    if (!el) { return resolved; }
    const raw = (el.value || '').trim();
    return raw === '' ? resolved : raw;
}

/* ----------------------------------------------------------------- cells */

function equityCell(text, className) {
    const td = document.createElement('td');
    if (className) { td.className = className; }
    td.textContent = text === null || text === undefined ? '' : String(text);
    return td;
}

function equitySideCell(side) {
    const td = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = side === 'SELL' ? 'badge badge-error badge-sm' : 'badge badge-success badge-sm';
    badge.textContent = side;
    td.appendChild(badge);
    return td;
}

function equityMessageRow(message, columns) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    // The argument wins; the page's own constant is the default; 1 is the
    // last resort so a page that defines neither still renders a valid row.
    let span = columns;
    if (!span) {
        span = (typeof EQUITY_COLUMN_COUNT !== 'undefined') ? EQUITY_COLUMN_COUNT : 1;
    }
    cell.colSpan = span;
    cell.className = 'text-center py-8 text-base-content/60';
    cell.textContent = message;
    row.appendChild(cell);
    return row;
}

/* --------------------------------------------------------------- badges */

function equityStatusDotClass(status, isStale) {
    if (isStale) { return 'w-2 h-2 rounded-full bg-warning inline-block'; }
    if (status === 'connected') { return 'w-2 h-2 rounded-full bg-success inline-block'; }
    if (status === 'disconnected') { return 'w-2 h-2 rounded-full bg-gray-400 inline-block'; }
    return 'w-2 h-2 rounded-full bg-error inline-block';
}

function equityStatusBadgeClass(status) {
    if (status === 'COMPLETED') { return 'badge badge-success badge-sm'; }
    if (status === 'PENDING') { return 'badge badge-info badge-sm'; }
    if (status === 'PARTIAL') { return 'badge badge-warning badge-sm'; }
    if (status === 'CANCELLED') { return 'badge badge-ghost badge-sm'; }
    return 'badge badge-outline badge-sm';
}

function equitySplitStatusBadgeClass(status) {
    if (status === 'COMPLETED') { return 'badge badge-success badge-sm'; }
    if (status === 'PENDING') { return 'badge badge-info badge-sm'; }
    if (status === 'PARTIAL') { return 'badge badge-warning badge-sm'; }
    if (status === 'INDETERMINATE') { return 'badge badge-error badge-sm'; }
    if (status === 'FAILED' || status === 'REJECTED') { return 'badge badge-error badge-sm'; }
    if (status === 'UNSUPPORTED') { return 'badge badge-warning badge-sm'; }
    if (status === 'SKIPPED' || status === 'CANCELLED') { return 'badge badge-ghost badge-sm'; }
    return 'badge badge-outline badge-sm';
}

/* --------------------------------------------------------------- banners */

function equityShowStale(message) {
    const banner = document.getElementById('equity-stale-banner');
    const text = document.getElementById('equity-stale-text');
    if (!banner || !text) { return; }
    if (message) {
        text.textContent = message;
        banner.classList.remove('hidden');
    } else {
        banner.classList.add('hidden');
    }
}

/* ---------------------------------------------------------------- search */

function equityCloseSearchResults() {
    const list = document.getElementById('equity-search-results');
    if (list) {
        list.textContent = '';
        list.classList.add('hidden');
    }
}

/* -------------------------------------------------------------- filters */

function equityRenderAccountOptions(options) {
    if (typeof equityAccountsRendered !== 'undefined' && equityAccountsRendered) { return; }
    const select = document.getElementById('equity-filter-account');
    if (!select) { return; }

    const chosen = select.value;
    select.textContent = '';

    const all = document.createElement('option');
    all.value = 'all';
    all.textContent = 'All Accounts';
    select.appendChild(all);

    ((options && options.accounts) || []).forEach(function (account) {
        const option = document.createElement('option');
        option.value = String(account.account_id);
        option.textContent = account.account_name + (account.is_active ? '' : ' (inactive)');
        select.appendChild(option);
    });

    select.value = chosen || 'all';
    if (typeof equityAccountsRendered !== 'undefined') {
        equityAccountsRendered = true;
    }
}
