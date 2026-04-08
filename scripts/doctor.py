#!/usr/bin/env python3
"""OpenClaw Gemma Pro – Setup Doctor / Pre-flight Validator.

Runs a suite of checks and prints a colour-coded PASS / WARN / FAIL table.

Usage:
    python scripts/doctor.py
    python -m scripts.doctor

Fixes issue #5: https://github.com/jthiruveedula/openclaw-gemma-pro/issues/5
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Colours (disabled on Windows unless FORCE_COLOR is set)
# ---------------------------------------------------------------------------
_USE_COLOUR = sys.stdout.isatty() or os.getenv("FORCE_COLOR")

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

GREEN = lambda t: _c(t, "0;32")
YELLOW = lambda t: _c(t, "1;33")
RED = lambda t: _c(t, "0;31")
BOLD = lambda t: _c(t, "1")

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    hint: str = ""

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_python_version() -> CheckResult:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        return CheckResult("Python version", Status.PASS, f"Python {major}.{minor} detected")
    return CheckResult(
        "Python version", Status.FAIL,
        f"Python {major}.{minor} detected",
        hint="Install Python >= 3.11 from https://python.org",
    )

def check_ollama_reachable() -> CheckResult:
    try:
        import httpx  # noqa: PLC0415
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        resp = httpx.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code == 200:
            return CheckResult("Ollama reachable", Status.PASS, f"GET {base_url}/api/tags -> 200")
        return CheckResult(
            "Ollama reachable", Status.FAIL,
            f"HTTP {resp.status_code}",
            hint="Start Ollama: ollama serve",
        )
    except Exception as exc:  # noqa: BLE001
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return CheckResult(
            "Ollama reachable", Status.FAIL,
            str(exc)[:100],
            hint=f"Ollama not reachable at {base_url}. Run: ollama serve",
        )

def _ollama_list() -> List[str]:
    """Return list of pulled model tags; empty list on error."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10  # noqa: S607
        )
        return result.stdout.lower().splitlines()
    except Exception:  # noqa: BLE001
        return []

def check_pro_model_pulled() -> CheckResult:
    model = os.getenv("OLLAMA_MODEL", "gemma4:27b")
    lines = _ollama_list()
    tag = model.split(":")[0].lower() if ":" in model else model.lower()
    if any(tag in line for line in lines):
        return CheckResult("PRO model pulled", Status.PASS, f"{model} found in ollama list")
    return CheckResult(
        "PRO model pulled", Status.FAIL,
        f"{model} not found",
        hint=f"Run: ollama pull {model}",
    )

def check_lite_model_pulled() -> CheckResult:
    model = os.getenv("OLLAMA_LITE_MODEL", "gemma4:4b")
    lines = _ollama_list()
    tag = model.split(":")[0].lower() if ":" in model else model.lower()
    if any(tag in line for line in lines):
        return CheckResult("LITE model pulled", Status.PASS, f"{model} found in ollama list")
    return CheckResult(
        "LITE model pulled", Status.FAIL,
        f"{model} not found",
        hint=f"Run: ollama pull {model}",
    )

def check_env_file() -> CheckResult:
    env_path = Path(".env")
    if env_path.exists() and env_path.stat().st_size > 0:
        return CheckResult(".env file", Status.PASS, f".env exists ({env_path.stat().st_size} bytes)")
    if not env_path.exists():
        return CheckResult(
            ".env file", Status.FAIL, ".env not found",
            hint="Run: cp .env.example .env  then fill in your values",
        )
    return CheckResult(".env file", Status.FAIL, ".env is empty", hint="Fill in values in .env")

def check_required_env_vars() -> CheckResult:
    required = ["OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_LITE_MODEL", "MEMORY_BASE_DIR"]
    placeholders = {"your_", "sk-...", "example", "placeholder"}
    missing = []
    for var in required:
        val = os.getenv(var, "")
        if not val or any(p in val.lower() for p in placeholders):
            missing.append(var)
    if not missing:
        return CheckResult("Required env vars", Status.PASS, "All required vars set")
    return CheckResult(
        "Required env vars", Status.FAIL,
        f"Missing/placeholder: {', '.join(missing)}",
        hint="Edit .env and set real values for these variables",
    )

def check_memory_dirs_writable() -> CheckResult:
    base = Path(os.getenv("MEMORY_BASE_DIR", "./memory"))
    dirs = [base / "raw", base / "daily", base / "facts"]
    bad = []
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        if not os.access(d, os.W_OK):
            bad.append(str(d))
    if not bad:
        return CheckResult("Memory dirs writable", Status.PASS, "All memory dirs writable")
    return CheckResult(
        "Memory dirs writable", Status.FAIL,
        f"Not writable: {', '.join(bad)}",
        hint="Run: chmod -R u+w memory/",
    )

def check_key_packages() -> CheckResult:
    packages = ["fastapi", "httpx", "telegram"]
    missing = [pkg for pkg in packages if importlib.util.find_spec(pkg) is None]
    if not missing:
        return CheckResult("Key packages", Status.PASS, "fastapi, httpx, python-telegram-bot importable")
    return CheckResult(
        "Key packages", Status.FAIL,
        f"Missing: {', '.join(missing)}",
        hint="Run: pip install -r requirements.txt",
    )

# Optional / WARN-only checks
def check_telegram_token() -> CheckResult:
    val = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if val and "your_telegram" not in val.lower():
        return CheckResult("Telegram token", Status.PASS, "TELEGRAM_BOT_TOKEN is set")
    return CheckResult(
        "Telegram token", Status.WARN,
        "TELEGRAM_BOT_TOKEN not set",
        hint="Set TELEGRAM_BOT_TOKEN in .env to enable Telegram channel",
    )

def check_twilio_tokens() -> CheckResult:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    number = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
    placeholders = {"ACxxxxxxx", "your_twilio", ""}
    if (sid and not any(p in sid for p in placeholders)
            and token and "your_twilio" not in token
            and number):
        return CheckResult("Twilio / WhatsApp tokens", Status.PASS, "Twilio credentials set")
    return CheckResult(
        "Twilio / WhatsApp tokens", Status.WARN,
        "Twilio credentials missing or placeholder",
        hint="Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER in .env",
    )

def check_ram() -> CheckResult:
    try:
        import psutil  # noqa: PLC0415
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        if ram_gb >= 16:
            return CheckResult("Available RAM", Status.PASS, f"{ram_gb:.1f} GB >= 16 GB")
        return CheckResult(
            "Available RAM", Status.WARN,
            f"{ram_gb:.1f} GB < 16 GB recommended for PRO model",
            hint="gemma4:27b requires >= 16 GB RAM. Use OLLAMA_LITE_MODEL for smaller machines.",
        )
    except ImportError:
        return CheckResult(
            "Available RAM", Status.WARN,
            "psutil not installed, RAM check skipped",
            hint="pip install psutil to enable RAM check",
        )

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

FAIL_CHECKS = [
    check_python_version,
    check_ollama_reachable,
    check_pro_model_pulled,
    check_lite_model_pulled,
    check_env_file,
    check_required_env_vars,
    check_memory_dirs_writable,
    check_key_packages,
]

WARN_CHECKS = [
    check_telegram_token,
    check_twilio_tokens,
    check_ram,
]

def _status_label(status: Status) -> str:
    labels = {
        Status.PASS: GREEN(f"[{'PASS':^4}]"),
        Status.WARN: YELLOW(f"[{'WARN':^4}]"),
        Status.FAIL: RED(f"[{'FAIL':^4}]"),
    }
    return labels[status]

def run_doctor(load_dotenv: bool = True) -> int:
    """Run all checks. Returns 0 on all-pass, 1 on any FAIL."""
    # Optionally load .env so vars are available
    if load_dotenv:
        try:
            from dotenv import load_dotenv as _ld  # noqa: PLC0415
            _ld(dotenv_path=Path(".env"), override=False)
        except ImportError:
            pass  # python-dotenv not installed; env must be pre-exported

    results: List[CheckResult] = []
    for fn in FAIL_CHECKS + WARN_CHECKS:
        results.append(fn())

    print()
    print(BOLD("=" * 60))
    print(BOLD("  OpenClaw Gemma Pro \u2013 Setup Doctor"))
    print(BOLD("=" * 60))
    print()

    col_w = max(len(r.name) for r in results) + 2
    for r in results:
        label = _status_label(r.status)
        print(f"  {label}  {r.name:<{col_w}} {r.message}")
        if r.hint and r.status != Status.PASS:
            print(f"         {YELLOW('Hint:')} {r.hint}")

    fails = [r for r in results if r.status == Status.FAIL]
    warns = [r for r in results if r.status == Status.WARN]
    passes = [r for r in results if r.status == Status.PASS]

    print()
    print(BOLD("=" * 60))
    summary_parts = [GREEN(f"{len(passes)} PASS"), YELLOW(f"{len(warns)} WARN"), RED(f"{len(fails)} FAIL")]
    print("  Summary: " + "  ".join(summary_parts))
    print(BOLD("=" * 60))
    print()

    if fails:
        print(RED(f"  Doctor found {len(fails)} failing check(s). Fix the issues above before launching."))
        return 1
    print(GREEN("  All critical checks passed. You're good to go!"))
    return 0

if __name__ == "__main__":
    sys.exit(run_doctor())
