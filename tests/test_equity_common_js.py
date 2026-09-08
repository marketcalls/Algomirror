"""
Guards for the shared equity JavaScript.

The equity templates carried twenty-seven copy-pasted helper functions, and
nineteen of them had already drifted between copies. That is how a formatter
ends up rendering rupees one way on Holdings and another on Positions, and it is
the failure mode that made extracting app/static/js/equity_common.js a
precondition for taking on more equity templates rather than a tidy-up
afterwards.

Extraction is only half the fix. Nothing stops the next screen from pasting its
own equitySetText back in, at which point the drift returns silently, because
the last definition parsed wins and no error is raised. These tests fail when
that happens.

They are deliberately static: they read the files rather than run a browser, so
they cost nothing and run everywhere.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEMPLATE_DIR = REPO_ROOT / 'app' / 'templates' / 'equity'
COMMON_JS = REPO_ROOT / 'app' / 'static' / 'js' / 'equity_common.js'

FUNCTION_RE = re.compile(r'function\s+([A-Za-z_$][\w$]*)\s*\(')

# watchlist.html keeps its own equityShowStale on purpose: it restyles the
# banner through className, where the shared one only toggles hidden. Different
# behaviour, so it was left alone rather than silently changed.
ALLOWED_OVERRIDES = {('watchlist.html', 'equityShowStale')}


def templates():
    return sorted(TEMPLATE_DIR.glob('*.html'))


def shared_function_names():
    return set(FUNCTION_RE.findall(COMMON_JS.read_text(encoding='utf-8')))


def functions_in(path):
    return FUNCTION_RE.findall(path.read_text(encoding='utf-8'))


def test_the_shared_module_exists_and_defines_helpers():
    assert COMMON_JS.exists()
    assert len(shared_function_names()) >= 10


def test_every_equity_template_loads_the_shared_module():
    """A template that defines nothing but calls a helper needs the script tag."""
    for path in templates():
        text = path.read_text(encoding='utf-8')
        assert 'js/equity_common.js' in text, f'{path.name} does not load equity_common.js'


def test_the_shared_module_is_loaded_before_the_inline_script():
    """Function declarations hoist per script, not across them."""
    for path in templates():
        text = path.read_text(encoding='utf-8')
        tag = text.find('equity_common.js')
        inline = text.find('<script>')
        assert tag < inline, f'{path.name} loads equity_common.js after its inline script'


@pytest.mark.parametrize('path', templates(), ids=lambda p: p.name)
def test_no_template_redefines_a_shared_helper(path):
    """The regression this suite exists for: a pasted copy shadowing the shared one."""
    shared = shared_function_names()
    redefined = [
        name for name in functions_in(path)
        if name in shared and (path.name, name) not in ALLOWED_OVERRIDES
    ]
    assert redefined == [], (
        f'{path.name} redefines {redefined}, which equity_common.js already provides. '
        'Delete the copy, or if the behaviour genuinely has to differ, rename it '
        'and add it to ALLOWED_OVERRIDES with the reason.'
    )


def test_no_helper_is_defined_twice_inside_one_template():
    """Two definitions in one file means the first is dead code."""
    for path in templates():
        names = functions_in(path)
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f'{path.name} defines {sorted(dupes)} more than once'


def test_the_shared_module_does_not_use_innerHTML():
    """The equity screens build DOM with textContent. Keep it that way."""
    text = COMMON_JS.read_text(encoding='utf-8')
    assert 'innerHTML' not in text
    assert 'insertAdjacentHTML' not in text
    assert 'document.write' not in text


def test_the_shared_module_tolerates_a_page_that_defines_no_globals():
    """
    It reads EQUITY_COLUMN_COUNT and equityAccountsRendered when a page has
    them. A bare reference would throw ReferenceError and take the whole
    screen's script down, so both must be typeof-guarded.
    """
    text = COMMON_JS.read_text(encoding='utf-8')
    # Comments name these globals when explaining them, which is not a reference.
    code = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    code = re.sub(r'//[^\n]*', '', code)

    for name in ('EQUITY_COLUMN_COUNT', 'equityAccountsRendered'):
        occurrences = list(re.finditer(re.escape(name), code))
        assert occurrences, f'{name} is no longer read; drop it from this test'
        for match in occurrences:
            window = code[max(0, match.start() - 60):match.start()]
            assert 'typeof' in window, (
                f'{name} is referenced without a typeof guard in equity_common.js'
            )


# Known debt, recorded rather than hidden.
#
# Adopting the client's screens brought his duplication with them: 28 helpers
# are defined in more than one template and all but one of the copies have
# ALREADY drifted from each other, including a whole sort engine repeated four
# times. Reconciling 28 drifted functions is careful work, because each one
# needs its copies compared and a superset chosen, and doing it in bulk would
# change behaviour on screens nobody asked to change.
#
# So this number is a ceiling, not an endorsement. It must go down, never up.
MAX_DUPLICATED_HELPERS = 28


def test_duplication_across_templates_never_grows():
    """
    A ratchet, not a target.

    Sixteen before the client's screens were adopted, 28 after. The gap is
    tracked debt: each of those is a helper that looks shared and is not, which
    is exactly how a money formatter ends up rendering one thing on Holdings and
    another on the Trade Book.
    """
    seen = {}
    for path in templates():
        for name in set(functions_in(path)):
            seen.setdefault(name, []).append(path.name)
    duplicated = {n: v for n, v in seen.items() if len(v) > 1}

    assert len(duplicated) <= MAX_DUPLICATED_HELPERS, (
        f'{len(duplicated)} helpers are duplicated across equity templates, above the '
        f'{MAX_DUPLICATED_HELPERS} ceiling. Extract the new one into '
        f'equity_common.js. Duplicated: {sorted(duplicated)}'
    )


# ---------------------------------------------------------------------------
# Behaviour tests for equityReadJson, exercised through node.
#
# This helper decides whether a failed order submit gets reported as "nothing
# was sent" or as "this may be live at a broker". Getting that backwards either
# sends the admin hunting through five broker terminals for an order nobody
# placed, or lets a real order sit unnoticed. It is worth running rather than
# only reading, so these drive the real file through node.
# ---------------------------------------------------------------------------

import json
import shutil
import subprocess
import tempfile
import os

NODE = shutil.which('node')
needs_node = pytest.mark.skipif(NODE is None, reason='node is not installed')


def run_js(snippet):
    """Load equity_common.js in node and evaluate a snippet against it."""
    harness = (
        "global.document = { getElementById: () => null, "
        "querySelector: () => null, createElement: () => ({ classList: { add(){}, remove(){} } }) };\n"
        + COMMON_JS.read_text(encoding='utf-8').replace("'use strict';", '', 1)
        + "\n;(async () => { const out = await (async () => { " + snippet
        + " })(); process.stdout.write(JSON.stringify(out)); })();"
    )
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False, encoding='utf-8') as fh:
        fh.write(harness)
        path = fh.name
    try:
        result = subprocess.run([NODE, path], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    finally:
        os.unlink(path)


def fake_response(status, body='{}', ok=None, json_ok=True):
    is_ok = (200 <= status < 300) if ok is None else ok
    parse = f'JSON.parse({body!r})' if json_ok else "(() => { throw new Error('not json'); })()"
    return f'{{ status: {status}, ok: {str(is_ok).lower()}, json: async () => {parse} }}'


@needs_node
def test_a_success_is_reported_as_ok():
    out = run_js(
        f"return await equityReadJson({fake_response(200, '{\"status\":\"success\"}')});"
    )
    assert out['ok'] is True
    assert out['definite'] is False


@needs_node
@pytest.mark.parametrize('status', [400, 401, 403, 404, 405, 409, 415, 422, 429])
def test_our_own_refusals_are_definite(status):
    """These are decided inside AlgoMirror, so no broker was contacted."""
    out = run_js(f"return await equityReadJson({fake_response(status, json_ok=False)});")
    assert out['ok'] is False
    assert out['definite'] is True, f'HTTP {status} should mean nothing was sent'


@needs_node
@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_server_errors_stay_indeterminate(status):
    """A 5xx may have reached the engine and died afterwards."""
    out = run_js(f"return await equityReadJson({fake_response(status, json_ok=False)});")
    assert out['ok'] is False
    assert out['definite'] is False, f'HTTP {status} must not claim nothing was sent'


@needs_node
def test_a_rate_limit_says_nothing_was_sent():
    """The exact case that used to read as 'it may still have reached a broker'."""
    out = run_js(f"return await equityReadJson({fake_response(429, json_ok=False)});")
    assert out['definite'] is True
    assert 'nothing was sent' in out['message'].lower()


@needs_node
def test_a_non_json_error_body_does_not_throw():
    """Flask returns HTML for 429 and 403, which response.json() cannot parse."""
    out = run_js(f"return await equityReadJson({fake_response(403, json_ok=False)});")
    assert out['data'] is None
    assert out['message']


@needs_node
def test_the_servers_own_message_wins_when_it_sends_one():
    body = '{"status":"error","message":"A GTT order needs a limit price"}'
    out = run_js(f"return await equityReadJson({fake_response(400, body)});")
    assert out['message'] == 'A GTT order needs a limit price'
    assert out['definite'] is True


# ---------------------------------------------------------------------------
# Polling and badges.
# ---------------------------------------------------------------------------

def test_no_equity_template_polls_at_all():
    """
    The architecture rule: equity screens are pushed to, never polled.

    The server knows the instant anything changes, because prices arrive on the
    shared WebSocket and order state on the order stream. A setInterval here
    would put back exactly the behaviour /equity/api/stream replaced, and would
    do it silently.
    """
    for path in templates():
        text = path.read_text(encoding='utf-8')
        assert 'setInterval(' not in text, f'{path.name} polls with setInterval'
        assert 'clearInterval(' not in text, f'{path.name} still manages a poll timer'
        assert 'equityStartPolling' not in text, (
            f'{path.name} uses the removed polling helper; use equityStartEventStream'
        )


def test_the_shared_module_offers_no_polling_helper():
    """A helper that polls is a helper somebody will reach for."""
    text = COMMON_JS.read_text(encoding='utf-8')
    # Comments explain what was removed and why, which is not a call.
    code = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    code = re.sub(r'//[^\n]*', '', code)

    assert 'equityStartPolling' not in code
    assert 'setInterval' not in code


def test_the_live_screens_subscribe_to_the_event_stream():
    expected = {'dashboard.html', 'holdings.html', 'watchlist.html',
                'order_book.html', 'trade_book.html', 'place_order.html'}
    using = {
        p.name for p in templates()
        if 'equityStartEventStream(' in p.read_text(encoding='utf-8')
    }
    assert expected <= using, f'not on the event stream: {sorted(expected - using)}'


def test_every_template_has_the_stream_badge_slot():
    """A dropped stream must be visible, since there is no polling fallback."""
    for path in templates():
        assert 'id="equity-stream-badge"' in path.read_text(encoding='utf-8'), (
            f'{path.name} has no stream badge slot'
        )


def test_every_template_has_the_badge_slots():
    for path in templates():
        text = path.read_text(encoding='utf-8')
        assert 'id="equity-feed-badge"' in text, f'{path.name} has no feed badge slot'
        assert 'id="equity-analyze-badge"' in text, f'{path.name} has no analyze badge slot'


def test_the_screens_with_a_price_feed_render_the_badge():
    """
    Every equity endpoint already returned a price_feed block and no screen ever
    showed it, so a silently dead subscription looked exactly like a quiet
    market. These five are the screens that display prices.
    """
    expected = {'dashboard.html', 'holdings.html', 'order_book.html',
                'trade_book.html', 'watchlist.html'}
    rendering = {
        p.name for p in templates()
        if 'equityRenderFeedBadge(' in p.read_text(encoding='utf-8')
    }
    assert expected <= rendering, f'missing the feed badge: {sorted(expected - rendering)}'


@needs_node
@pytest.mark.parametrize('source,authenticated,expected', [
    ('websocket', True, 'Live'),
    ('mixed', True, 'Mixed'),
    ('rest', True, 'REST'),
    ('websocket', False, 'Offline'),
    ('rest', False, 'Offline'),
])
def test_the_feed_badge_says_what_the_feed_is_doing(source, authenticated, expected):
    out = run_js(
        "let cls = '', txt = '', title = '';"
        "global.document.getElementById = () => ({"
        "  set className(v) { cls = v; }, get className() { return cls; },"
        "  set textContent(v) { txt = v; }, get textContent() { return txt; },"
        "  set title(v) { title = v; }, removeAttribute() {} });"
        f"equityRenderFeedBadge({{ source: '{source}', authenticated: {str(authenticated).lower()},"
        "  symbols_requested: 4, symbols_from_feed: 2, last_tick_age_seconds: 3 });"
        "return { cls, txt };"
    )
    assert out['txt'] == expected


@needs_node
def test_an_empty_view_hides_the_feed_badge():
    """No symbols held is not a feed problem, so it must not show as one."""
    out = run_js(
        "let cls = '', txt = '';"
        "global.document.getElementById = () => ({"
        "  set className(v) { cls = v; }, set textContent(v) { txt = v; }, removeAttribute() {} });"
        "equityRenderFeedBadge({ source: 'none', authenticated: true });"
        "return { cls, txt };"
    )
    assert out['cls'] == 'hidden'
    assert out['txt'] == ''


@needs_node
def test_a_missing_feed_block_does_not_throw():
    """An older endpoint that sends no price_feed must not break the screen."""
    out = run_js(
        "let cls = '';"
        "global.document.getElementById = () => ({"
        "  set className(v) { cls = v; }, set textContent(v) {}, removeAttribute() {} });"
        "equityRenderFeedBadge(undefined); return { cls };"
    )
    assert out['cls'] == 'hidden'


@needs_node
def test_analyze_mode_is_shown_and_absence_is_not():
    shown = run_js(
        "let cls = '', txt = '';"
        "global.document.getElementById = () => ({"
        "  set className(v) { cls = v; }, set textContent(v) { txt = v; },"
        "  set title(v) {}, removeAttribute() {} });"
        "equityRenderAnalyzeBadge(true); return { cls, txt };"
    )
    assert shown['txt'] == 'Analyze'

    hidden = run_js(
        "let cls = '', txt = '';"
        "global.document.getElementById = () => ({"
        "  set className(v) { cls = v; }, set textContent(v) { txt = v; },"
        "  set title(v) {}, removeAttribute() {} });"
        "equityRenderAnalyzeBadge(false); return { cls, txt };"
    )
    assert hidden['cls'] == 'hidden'


# ---------------------------------------------------------------------------
# The built stylesheet has to cover the markup.
#
# Adopting the client's screens brought his Tailwind classes with them, and the
# stylesheet is a BUILD ARTIFACT: Tailwind only emits a class it has seen while
# scanning. Taking his templates without rebuilding left 13 classes with no
# rule at all, so his accounts-card redesign lost its padding and gaps and the
# modals lost their width constraints. Nothing errors; it just renders wrong.
# ---------------------------------------------------------------------------

COMPILED_CSS = REPO_ROOT / 'app' / 'static' / 'css' / 'compiled.css'
BACKSLASH = chr(92)


def _built_class_names():
    css = COMPILED_CSS.read_text(encoding='utf-8')
    pattern = (r'\.([a-zA-Z0-9_-]+(?:' + re.escape(BACKSLASH)
               + r'[:/.\[\]%]+[a-zA-Z0-9_.\[\]%-]+)*)')
    return {c.replace(BACKSLASH, '') for c in re.findall(pattern, css)}


def test_every_class_the_equity_screens_use_is_in_the_built_css():
    """Fails when a template gains a class and nobody ran npm run build-css."""
    built = _built_class_names()

    missing = {}
    for path in templates():
        for match in re.finditer(r'class="([^"{}]+)"', path.read_text(encoding='utf-8')):
            for cls in match.group(1).split():
                # equity-* are the page-local styles each template defines inline.
                if cls.startswith('equity') or cls in built:
                    continue
                missing.setdefault(cls, set()).add(path.name)

    assert not missing, (
        'These classes have no rule in compiled.css, so they render as nothing. '
        'Run "npm run build-css". Missing: '
        + ', '.join(f'{c} ({", ".join(sorted(f))})' for c, f in sorted(missing.items()))
    )


def test_no_equity_template_uses_a_daisyui_v3_colour_token():
    """
    primary-focus and its siblings were removed in DaisyUI v4, which this
    project is on. Tailwind cannot emit them, so they are silently inert.
    """
    dead = ('primary-focus', 'secondary-focus', 'accent-focus', 'neutral-focus')
    for path in templates():
        text = path.read_text(encoding='utf-8')
        for token in dead:
            assert token not in text, (
                f'{path.name} uses {token}, removed in DaisyUI v4, so it does nothing'
            )
