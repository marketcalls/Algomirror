"""
The three shapes the OpenAlgo SDK publishes, and the one this codebase uses.

The SDK returns cached market data nested by exchange:

    {"ltp":   {"NSE": {"RELIANCE": {"ltp": 2951.5}}}}
    {"quote": {"NSE": {"RELIANCE": {"ltp": ..., "close": ...}}}}
    {"depth": {"NSE": {"RELIANCE": {"buyBook": {"1": {"qty": 5}}}}}}

Iterating any of those directly hands the loop ('NSE', {...}), so code that
reads a field off that value silently gets nothing and drops every symbol. It
does not raise, it just returns empty, which is why it can sit unnoticed.

That trap was found and fixed once for LTP (_flatten_ltp, whose docstring
records it) and left in place for quotes and depth. get_quotes returned an empty
mapping for every symbol, always. Depth additionally used a THIRD naming that
matches neither the wire protocol nor the REST response: buyBook and sellBook
keyed by string ordinals, with qty rather than quantity.

Both were latent, since nothing called them, but they sat directly in the path
of the Quote-mode price work. These tests pin the normalisation so the next
caller gets the shape the rest of the codebase actually reads.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-ws-shapes-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'ws.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'ws-shapes-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app.utils.websocket_manager import ProfessionalWebSocketManager  # noqa: E402


class FakeClient:
    """Returns exactly what openalgo 2.0.4 returns."""

    def __init__(self, quotes=None, depth=None, raises=False):
        self._quotes = quotes
        self._depth = depth
        self._raises = raises

    def get_quotes(self):
        if self._raises:
            raise RuntimeError('feed down')
        return self._quotes

    def get_depth(self):
        if self._raises:
            raise RuntimeError('feed down')
        return self._depth


@pytest.fixture
def manager():
    m = ProfessionalWebSocketManager()
    m._valid_quote_cache = {}
    m.client = None
    yield m
    m.client = None
    m._valid_quote_cache = {}


NESTED_QUOTES = {'quote': {'NSE': {'RELIANCE': {'ltp': 2951.5, 'close': 2900.0}}}}
NESTED_DEPTH = {'depth': {'NSE': {'RELIANCE': {
    'ltp': 100.0,
    'buyBook': {'1': {'price': 100.0, 'qty': 5, 'orders': 2},
                '2': {'price': 99.5, 'qty': 3, 'orders': 1}},
    'sellBook': {'1': {'price': 100.5, 'qty': 4, 'orders': 1}},
}}}}


# ----------------------------------------------------------- the flattener

def test_the_nested_shape_is_flattened():
    flat = ProfessionalWebSocketManager._flatten_nested(NESTED_QUOTES, 'quote')
    assert list(flat) == ['NSE:RELIANCE']
    assert flat['NSE:RELIANCE']['ltp'] == 2951.5


def test_an_already_flat_shape_passes_through():
    """So this keeps working if the SDK ever changes back."""
    flat = ProfessionalWebSocketManager._flatten_nested(
        {'quote': {'NSE:TCS': {'ltp': 1.0}}}, 'quote')
    assert flat == {'NSE:TCS': {'ltp': 1.0}}


@pytest.mark.parametrize('raw', [None, 'nonsense', {}, {'quote': None}, {'quote': 'x'}])
def test_the_flattener_never_raises_on_rubbish(raw):
    assert ProfessionalWebSocketManager._flatten_nested(raw, 'quote') == {}


# -------------------------------------------------------------- get_quotes

def test_get_quotes_returns_symbols_not_exchanges(manager):
    """The regression: this returned an empty mapping for every symbol."""
    manager.client = FakeClient(quotes=NESTED_QUOTES)
    quotes = manager.get_quotes()['quote']

    assert 'NSE:RELIANCE' in quotes
    assert quotes['NSE:RELIANCE']['ltp'] == 2951.5


def test_get_quotes_still_drops_a_zero_price(manager):
    """The zero-value guard must survive the flattening."""
    manager.client = FakeClient(quotes={'quote': {'NSE': {'RELIANCE': {'ltp': 0}}}})
    assert manager.get_quotes()['quote'] == {}


def test_get_quotes_serves_the_last_valid_price_on_a_zero(manager):
    manager.client = FakeClient(quotes=NESTED_QUOTES)
    manager.get_quotes()

    manager.client = FakeClient(quotes={'quote': {'NSE': {'RELIANCE': {'ltp': 0}}}})
    quotes = manager.get_quotes()['quote']
    assert quotes['NSE:RELIANCE']['ltp'] == 2951.5


def test_get_quotes_survives_a_broken_feed(manager):
    manager.client = FakeClient(raises=True)
    assert manager.get_quotes() == {'quote': {}}


# --------------------------------------------------------------- get_depth

def test_get_depth_uses_the_wire_shape(manager):
    """
    buyBook/sellBook with ordinal keys and qty is a shape nothing else in this
    codebase reads. _normalise_depth and the depth handler both want buy/sell
    lists with quantity.
    """
    manager.client = FakeClient(depth=NESTED_DEPTH)
    book = manager.get_depth()['depth']['NSE:RELIANCE']

    assert 'buyBook' not in book and 'sellBook' not in book
    assert book['buy'][0] == {'price': 100.0, 'quantity': 5, 'orders': 2}
    assert book['sell'][0] == {'price': 100.5, 'quantity': 4, 'orders': 1}


def test_get_depth_keeps_the_levels_in_order(manager):
    """Ordinal keys are strings, so a plain sort would put "10" before "2"."""
    manager.client = FakeClient(depth=NESTED_DEPTH)
    book = manager.get_depth()['depth']['NSE:RELIANCE']

    assert [level['price'] for level in book['buy']] == [100.0, 99.5]


def test_get_depth_accepts_the_wire_shape_unchanged(manager):
    """A payload already in buy/sell list form must not be mangled."""
    manager.client = FakeClient(depth={'depth': {'NSE': {'RELIANCE': {
        'buy': [{'price': 1.0, 'quantity': 2, 'orders': 1}], 'sell': [],
    }}}})
    book = manager.get_depth()['depth']['NSE:RELIANCE']
    assert book['buy'] == [{'price': 1.0, 'quantity': 2, 'orders': 1}]


def test_get_depth_survives_a_broken_feed(manager):
    manager.client = FakeClient(raises=True)
    assert manager.get_depth() == {'depth': {}}
