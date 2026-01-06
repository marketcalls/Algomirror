# AlgoMirror WSGI entry point
# Uses standard threading for background tasks (no eventlet - deprecated and Python 3.13+ incompatible)

import os
import sys
from datetime import datetime

def print_startup_banner():
    """Print a colorful startup banner similar to Claude Code"""

    # ANSI color codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Colors
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    WHITE = "\033[37m"

    # Box drawing characters - use ASCII fallback for Windows compatibility
    try:
        # Try Unicode box drawing characters
        test_str = "\u256d\u256e\u2570\u256f\u2500\u2502"
        test_str.encode(sys.stdout.encoding or 'utf-8')
        TL = "\u256d"  # Top left
        TR = "\u256e"  # Top right
        BL = "\u2570"  # Bottom left
        BR = "\u256f"  # Bottom right
        H = "\u2500"   # Horizontal
        V = "\u2502"   # Vertical
    except (UnicodeEncodeError, LookupError):
        # ASCII fallback for Windows console
        TL = "+"
        TR = "+"
        BL = "+"
        BR = "+"
        H = "-"
        V = "|"

    # Get version from pyproject.toml
    version = "1.0.0"
    try:
        import tomllib
        pyproject_path = os.path.join(os.path.dirname(__file__), 'pyproject.toml')
        if os.path.exists(pyproject_path):
            with open(pyproject_path, 'rb') as f:
                pyproject = tomllib.load(f)
                version = pyproject.get('project', {}).get('version', '1.0.0')
    except:
        pass

    # Get current working directory
    cwd = os.getcwd()
    if len(cwd) > 30:
        cwd = "..." + cwd[-27:]

    # Get current time
    now = datetime.now().strftime("%H:%M:%S")

    # Get environment
    env = os.environ.get('FLASK_ENV', 'development').capitalize()

    # AlgoMirror ASCII Art Logo
    logo_lines = [
        f"{CYAN}     _    _         {RESET}",
        f"{CYAN}    / \\  | | __ _  ___  {RESET}",
        f"{CYAN}   / _ \\ | |/ _` |/ _ \\ {RESET}",
        f"{CYAN}  / ___ \\| | (_| | (_) |{RESET}",
        f"{CYAN} /_/   \\_\\_|\\__, |\\___/ {RESET}",
        f"{CYAN}   {MAGENTA}Mirror{CYAN}  |___/      {RESET}",
    ]

    # Build the banner
    width = 85
    inner_width = width - 2

    # Header
    title = f" AlgoMirror v{version} "
    title_padded = f"{H * 3}{title}{H * (inner_width - len(title) - 3)}"

    print()
    print(f"{CYAN}{TL}{title_padded}{TR}{RESET}")

    # Empty line
    print(f"{CYAN}{V}{RESET}{' ' * inner_width}{CYAN}{V}{RESET}")

    # Logo and info side by side
    info_lines = [
        f"{BOLD}{WHITE}Multi-Account Trading Platform{RESET}",
        f"{DIM}Enterprise-grade OpenAlgo management{RESET}",
        "",
        f"{YELLOW}Status{RESET}",
        f"  Environment: {GREEN}{env}{RESET}",
        f"  Started at:  {WHITE}{now}{RESET}",
        f"  Directory:   {DIM}{cwd}{RESET}",
        "",
        f"{YELLOW}Services{RESET}",
        f"  Order Poller    {GREEN}Running{RESET}",
        f"  Risk Manager    {GREEN}Running{RESET}",
        f"  Supertrend Exit {GREEN}Running{RESET}",
    ]

    # Pad logo and info to same length
    max_lines = max(len(logo_lines), len(info_lines))
    while len(logo_lines) < max_lines:
        logo_lines.append(" " * 24)
    while len(info_lines) < max_lines:
        info_lines.append("")

    # Print logo and info side by side
    logo_width = 26
    info_width = inner_width - logo_width - 3

    for i in range(max_lines):
        logo = logo_lines[i] if i < len(logo_lines) else ""
        info = info_lines[i] if i < len(info_lines) else ""

        # Calculate visible length (without ANSI codes)
        def visible_len(s):
            import re
            return len(re.sub(r'\033\[[0-9;]*m', '', s))

        logo_padding = logo_width - visible_len(logo)
        info_padding = info_width - visible_len(info)

        print(f"{CYAN}{V}{RESET} {logo}{' ' * logo_padding}{DIM}|{RESET} {info}{' ' * info_padding}{CYAN}{V}{RESET}")

    # Empty line
    print(f"{CYAN}{V}{RESET}{' ' * inner_width}{CYAN}{V}{RESET}")

    # Footer with URL
    footer = f"  Server: {BOLD}http://localhost:8000{RESET}  "
    footer_visible_len = 28  # "  Server: http://localhost:8000  "
    footer_padding = inner_width - footer_visible_len
    print(f"{CYAN}{V}{RESET}{footer}{' ' * footer_padding}{CYAN}{V}{RESET}")

    # Bottom border
    print(f"{CYAN}{BL}{H * inner_width}{BR}{RESET}")
    print()


from app import create_app

app = create_app()

if __name__ == '__main__':
    # Print startup banner
    print_startup_banner()

    # Use use_reloader=False to prevent double initialization
    app.run(debug=False, host='0.0.0.0', port=8000, use_reloader=False)
