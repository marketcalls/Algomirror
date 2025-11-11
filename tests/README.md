# AlgoMirror Test Suite

This directory contains all test files for the AlgoMirror application.

## Test Files

### WebSocket Tests
- **`test_option_chain_websocket.py`** - Comprehensive test for option chain WebSocket streaming
  - Tests authentication, subscription, and data reception
  - Tests NIFTY option chain for specified expiry
  - Verifies real-time market data streaming

- **`test_single_websocket.py`** - Verifies single shared WebSocket connection
  - Ensures only ONE connection from AlgoMirror to OpenAlgo
  - Prevents broker connection limit issues (Error 429)

- **`test_websocket_connection.py`** - Basic WebSocket connectivity test

- **`test_websocket_failover.py`** - Tests WebSocket failover mechanisms

### Failover Tests
- **`test_live_failover.py`** - Live failover testing
- **`test_immediate_failover.py`** - Immediate failover scenarios
- **`test_get_backup_connections.py`** - Backup connection management

### Trading Hours Tests
- **`test_trading_hours.py`** - Trading hours and market schedule testing

### Monitoring Tests
- **`test_monitoring.py`** - Position monitoring and risk management tests

## Running Tests

### Individual Test
```bash
# From project root
python tests/test_option_chain_websocket.py

# Or using Python module syntax
python -m tests.test_option_chain_websocket
```

### All Tests (if using pytest)
```bash
# Install pytest if not already installed
pip install pytest

# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run specific test file
pytest tests/test_option_chain_websocket.py
```

### Using UV (faster)
```bash
# Install pytest with UV
uv pip install pytest

# Run tests
pytest tests/
```

## Test Requirements

Most tests require:
- **OpenAlgo REST API** running on port 5000
- **OpenAlgo WebSocket** running on port 8765
- **Valid API key** configured in test files
- **AlgoMirror application** (for integration tests)

### Standalone Tests
Some tests can run standalone without OpenAlgo:
- `test_single_websocket.py` - Checks connection counts
- `test_trading_hours.py` - Tests trading hours logic

### Integration Tests
These require full setup:
- `test_option_chain_websocket.py` - Requires OpenAlgo + broker connection
- `test_websocket_failover.py` - Requires multiple accounts configured
- `test_monitoring.py` - Requires active positions

## Configuration

Update API keys and URLs in test files as needed:
```python
API_KEY = "your_openalgo_api_key"
REST_API_URL = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:8765"
```

## Test Output

Tests provide detailed output:
- `[OK]` - Successful operations
- `[ERROR]` - Failures
- `[WARN]` - Warnings
- `[DATA]` - Market data received
- `[SUB]` - Subscriptions

## Continuous Integration

For CI/CD pipelines, ensure:
1. Mock OpenAlgo server for unit tests
2. Integration tests run only when OpenAlgo is available
3. Use environment variables for configuration

## Troubleshooting

### Import Errors
If you see import errors:
```bash
# Ensure you're in project root
cd D:\openalgo-multi\algomirror

# Run with Python module syntax
python -m tests.test_option_chain_websocket
```

### Connection Errors
- Check OpenAlgo is running: `curl http://127.0.0.1:5000/api/v1/ping`
- Check WebSocket: `netstat -ano | findstr :8765`
- Verify API key is valid

### Unicode Errors (Windows)
Tests use ASCII-only output for Windows compatibility. If you see encoding errors, ensure your terminal supports UTF-8.

## Writing New Tests

Template for new tests:
```python
"""
Test Description
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import TradingAccount

def test_something():
    """Test something"""
    app = create_app()
    with app.app_context():
        # Your test code here
        pass

if __name__ == "__main__":
    test_something()
```

## Test Coverage

To generate test coverage reports:
```bash
pip install pytest-cov
pytest tests/ --cov=app --cov-report=html
```

---

**Date**: 2025-11-11
**Status**: All test files migrated to tests/ directory
