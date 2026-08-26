"""Environment detection and Chrome auto-launch.

Makes the script work in any environment:
- Local development (Chrome running with --remote-debugging-port)
- CI/CD (auto-launch headless Chrome)
- Docker containers (auto-detect + launch)
- Cloud VMs (auto-launch + configure)

Functions:
- detect_chrome_path(): find Chrome binary
- find_cdp_url(): scan common CDP ports
- launch_chrome(): auto-launch Chrome with remote debugging
- is_headless_environment(): detect if running headless
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional


COMMON_CDP_PORTS = [9222, 9223, 9224, 9225]
COMMON_CHROME_PATHS = [
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    # Windows
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def is_headless_environment() -> bool:
    """Detect if running in a headless environment (CI, Docker, no display)."""
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return True
    if os.environ.get("HEADLESS") in ("1", "true", "yes"):
        return True
    if not sys.platform.startswith("win") and not sys.platform == "darwin":
        # Linux: check if DISPLAY is set
        return not os.environ.get("DISPLAY")
    return False


def detect_chrome_path() -> Optional[str]:
    """Find Chrome binary on the system."""
    # Check env var first
    env_path = os.environ.get("CHROME_PATH") or os.environ.get("CHROMIUM_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    # Check PATH
    for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

    # Check common locations
    for path_str in COMMON_CHROME_PATHS:
        if Path(path_str).exists():
            return path_str
    return None


def find_cdp_url(timeout: float = 3.0, ports: list[int] = None) -> Optional[str]:
    """Scan for Chrome DevTools Protocol endpoint."""
    ports = ports or COMMON_CDP_PORTS
    for port in ports:
        url = f"http://127.0.0.1:{port}"
        try:
            resp = urllib.request.urlopen(f"{url}/json/version", timeout=timeout)
            data = resp.read().decode()
            if "Browser" in data:
                return url
        except Exception:
            continue
    return None


def launch_chrome(chrome_path: Optional[str] = None,
                  port: int = 9222,
                  user_data_dir: Optional[str] = None,
                  headless: bool = False,
                  timeout: int = 30) -> Optional[str]:
    """Launch Chrome with remote debugging port.

    Returns the CDP URL if successful, None otherwise.
    """
    chrome_path = chrome_path or detect_chrome_path()
    if not chrome_path:
        print("  [env] Chrome binary not found. Set CHROME_PATH env var or install Chrome.")
        return None

    user_data_dir = user_data_dir or os.environ.get(
        "CHROME_USER_DATA",
        str(Path.home() / ".cache" / "mx-pub" / "chrome-profile"),
    )
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    if headless or is_headless_environment():
        cmd.append("--headless=new")

    print(f"  [env] launching Chrome: {chrome_path} (port={port})")
    try:
        # Detach so Chrome survives our process
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"  [env] launch failed: {e}")
        return None

    # Wait for CDP to be ready
    cdp_url = f"http://127.0.0.1:{port}"
    for _ in range(timeout):
        try:
            resp = urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2)
            if "Browser" in resp.read().decode():
                print(f"  [env] Chrome ready at {cdp_url}")
                return cdp_url
        except Exception:
            time.sleep(1)

    print(f"  [env] Chrome did not become ready within {timeout}s")
    return None


def ensure_chrome(cdp_url: Optional[str] = None, **launch_kwargs) -> str:
    """Ensure Chrome is running with CDP available.

    1. If cdp_url provided and reachable, return it.
    2. If any Chrome is running with CDP, return its URL.
    3. Otherwise, try to launch Chrome.
    4. Fall back to localhost:9222 (assume caller will handle connection failure).
    """
    if cdp_url:
        try:
            urllib.request.urlopen(f"{cdp_url}/json/version", timeout=3)
            return cdp_url
        except Exception:
            pass

    existing = find_cdp_url()
    if existing:
        return existing

    launched = launch_chrome(**launch_kwargs)
    if launched:
        return launched

    return "http://127.0.0.1:9222"  # last-resort default


def get_env_summary() -> dict:
    """Summary of detected environment (for logging)."""
    chrome_path = detect_chrome_path()
    cdp_url = find_cdp_url(timeout=2)
    return {
        "platform": sys.platform,
        "is_headless": is_headless_environment(),
        "chrome_path": chrome_path,
        "cdp_url": cdp_url,
        "ai_providers": {
            "claude": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "ollama": bool(os.environ.get("OLLAMA_HOST")) or Path("/usr/local/bin/ollama").exists(),
        },
    }