# UV Package Manager Setup Guide

This guide covers using UV (the fast Python package manager) with AlgoMirror.

## What is UV?

UV is a modern, blazingly fast Python package manager written in Rust. It's 10-100x faster than pip and provides better dependency resolution.

## Installation

### Install UV

#### Windows
```powershell
# Using PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or using pip
pip install uv
```

#### macOS/Linux
```bash
# Using curl
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or using pip
pip install uv
```

## Project Setup with UV

### Method 1: Using pyproject.toml (Recommended)

```bash
# Create virtual environment
uv venv

# Activate virtual environment
# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

# Install all dependencies from pyproject.toml
uv pip install -e .

# Install with development dependencies
uv pip install -e ".[dev]"

# Install with production dependencies (includes gunicorn, PostgreSQL)
uv pip install -e ".[production]"

# Install with testing dependencies
uv pip install -e ".[testing]"

# Install all optional dependencies
uv pip install -e ".[dev,production,testing]"
```

### Method 2: Using requirements.txt (Compatibility)

```bash
# Create virtual environment
uv venv

# Activate virtual environment
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux

# Install from requirements.txt
uv pip install -r requirements.txt
```

## Complete Development Setup with UV

```bash
# 1. Create and activate virtual environment
uv venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux

# 2. Install dependencies with development tools
uv pip install -e ".[dev]"

# 3. Install Node dependencies and build CSS
npm install
npm run build-css

# 4. Configure environment
cp .env.example .env
# Edit .env with appropriate values

# 5. Initialize database
python init_db.py

# 6. Run application
python app.py
```

## UV Commands Reference

### Package Management
```bash
# Install a package
uv pip install package-name

# Install specific version
uv pip install package-name==1.0.0

# Upgrade a package
uv pip install --upgrade package-name

# Uninstall a package
uv pip uninstall package-name

# List installed packages
uv pip list

# Show package information
uv pip show package-name

# Freeze dependencies
uv pip freeze > requirements.txt
```

### Sync Dependencies
```bash
# Sync environment with pyproject.toml
uv pip sync

# Update all dependencies to latest compatible versions
uv pip install --upgrade -e .
```

### Virtual Environment
```bash
# Create virtual environment
uv venv

# Create with specific Python version
uv venv --python 3.11

# Remove virtual environment
rm -rf .venv  # Linux/Mac
rmdir /s .venv  # Windows
```

## Performance Comparison

| Operation | pip | UV | Speed Improvement |
|-----------|-----|-----|-------------------|
| Install cold | 45s | 2s | 22.5x faster |
| Install warm | 15s | 0.5s | 30x faster |
| Dependency resolution | 10s | 0.2s | 50x faster |

## Advantages of UV

1. **Speed**: 10-100x faster than pip
2. **Better Dependency Resolution**: More accurate conflict detection
3. **Reproducible Builds**: Lock file support
4. **Drop-in Replacement**: Works with existing pip workflows
5. **Modern**: Built with Rust for performance and reliability

## Migration from pip to UV

### Update requirements.txt to pyproject.toml
The project now includes both:
- `requirements.txt` - Traditional pip format (still supported)
- `pyproject.toml` - Modern format with metadata (recommended)

### Existing pip installations
If you have an existing pip installation, you can switch to UV:

```bash
# Export current environment
pip freeze > current_requirements.txt

# Create new UV environment
deactivate  # Exit current venv
uv venv .venv-uv
.venv-uv\Scripts\activate  # Windows
source .venv-uv/bin/activate  # macOS/Linux

# Install using UV
uv pip install -e ".[dev]"
```

## Troubleshooting

### UV not found after installation
```bash
# Add UV to PATH
# Windows: Add %USERPROFILE%\.cargo\bin to PATH
# Linux/Mac: Add ~/.cargo/bin to PATH

# Or reinstall
pip install --upgrade uv
```

### Package installation fails
```bash
# Clear UV cache
uv cache clean

# Try installing with pip fallback
uv pip install --no-cache package-name
```

### Virtual environment issues
```bash
# Remove and recreate
rm -rf .venv
uv venv
uv pip install -e ".[dev]"
```

## Best Practices

1. **Use pyproject.toml**: Prefer `pyproject.toml` over `requirements.txt` for new projects
2. **Pin versions**: Use exact versions in production (`==` instead of `>=`)
3. **Regular updates**: Keep UV updated with `pip install --upgrade uv`
4. **Cache management**: Periodically clean cache with `uv cache clean`
5. **Virtual environments**: Always use virtual environments, never install globally

## CI/CD Integration

### GitHub Actions
```yaml
- name: Set up UV
  run: pip install uv

- name: Install dependencies
  run: |
    uv venv
    source .venv/bin/activate
    uv pip install -e ".[testing]"
```

### Docker
```dockerfile
RUN pip install uv
RUN uv pip install -e ".[production]"
```

## Additional Resources

- [UV Documentation](https://github.com/astral-sh/uv)
- [UV Installation Guide](https://github.com/astral-sh/uv#installation)
- [pyproject.toml Specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)

## Support

For issues related to:
- **UV itself**: https://github.com/astral-sh/uv/issues
- **AlgoMirror**: https://github.com/marketcalls/algomirror/issues

---

**Copyright © 2024 OpenFlare Technologies. All Rights Reserved.**
