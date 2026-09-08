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


def test_duplication_across_templates_keeps_going_down():
    """
    A ratchet, not a target. Twenty-seven helpers were duplicated before the
    extraction and sixteen still are, each with genuine per-page drift that
    needs reconciling one at a time. This fails if the count grows.
    """
    seen = {}
    for path in templates():
        for name in set(functions_in(path)):
            seen.setdefault(name, []).append(path.name)
    duplicated = {n: v for n, v in seen.items() if len(v) > 1}

    assert len(duplicated) <= 16, (
        f'{len(duplicated)} helpers are duplicated across equity templates, up from 16. '
        f'New duplicates: {sorted(duplicated)}'
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
