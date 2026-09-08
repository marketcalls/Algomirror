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
