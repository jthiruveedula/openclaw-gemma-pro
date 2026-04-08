#!/usr/bin/env python3
"""
guardrails/pre_commit_hook.py
=================================
Git pre-commit hook: scans staged diff for dangerous patterns before commit.

Install:
  python guardrails/pre_commit_hook.py --install

This copies itself to .git/hooks/pre-commit and makes it executable.
The hook runs automatically on every `git commit`.

Checks performed:
  - Hardcoded secrets / API keys / tokens in staged files
  - rm -rf or mass-delete shell commands
  - Accidental deletion of protected paths (memory/, facts/, config/)
  - os.system / subprocess with shell=True in Python files
  - Large file additions (>5MB) that shouldn't be committed
  - Staged deletion of critical config files
"""

from __future__ import annotations

import argparse
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
class Rule(NamedTuple):
    name: str
    pattern: re.Pattern
    severity: str  # ERROR | WARN
    message: str


RULES: list[Rule] = [
    Rule(
        name="secret_key",
        pattern=re.compile(
            r"(TELEGRAM_BOT_TOKEN|TWILIO_AUTH_TOKEN|OPENAI_API_KEY|sk-[A-Za-z0-9]{40,}|"
            r"AC[a-f0-9]{32}|password\s*=\s*[\"'][^\"']{6,})",
            re.IGNORECASE,
        ),
        severity="ERROR",
        message="Potential secret/credential detected. Use .env file, not hardcoded values.",
    ),
    Rule(
        name="rm_rf",
        pattern=re.compile(r"rm\s+(-[rRfF]+\s+){0,3}[-/]", re.IGNORECASE),
        severity="ERROR",
        message="Dangerous rm command detected. Verify this is intentional.",
    ),
    Rule(
        name="shell_true",
        pattern=re.compile(r"shell\s*=\s*True"),
        severity="WARN",
        message="subprocess with shell=True is a security risk. Use list args instead.",
    ),
    Rule(
        name="eval_exec",
        pattern=re.compile(r"\beval\s*\(|\bexec\s*\("),
        severity="WARN",
        message="eval/exec detected. Ensure input is fully trusted.",
    ),
    Rule(
        name="memory_wipe",
        pattern=re.compile(
            r"(wipe_memory|clear_facts|delete_memory|purge_facts|reset_context|shutil\.rmtree.*memory)",
            re.IGNORECASE,
        ),
        severity="ERROR",
        message="Memory wipe operation detected. This will permanently destroy context.",
    ),
    Rule(
        name="debug_breakpoint",
        pattern=re.compile(r"\bbreakpoint\(\)|pdb\.set_trace\(\)|import pdb"),
        severity="WARN",
        message="Debug breakpoint left in code.",
    ),
    Rule(
        name="todo_fixme",
        pattern=re.compile(r"#\s*(TODO|FIXME|HACK|XXX):"),
        severity="WARN",
        message="Unresolved TODO/FIXME comment.",
    ),
]

# Protected paths - staged deletions of these are blocked
PROTECTED_PATHS = [
    "config/openclaw.json",
    "config/model-routing.json",
    "guardrails/action_guardrail.py",
    "memory/facts/master_facts.json",
    ".env.example",
]

# Max file size for staged additions (bytes)
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def get_staged_diff() -> str:
    return _run(["git", "diff", "--cached", "--unified=0"])


def get_staged_files() -> list[tuple[str, str]]:
    """Returns list of (status, filepath) for staged files."""
    output = _run(["git", "diff", "--cached", "--name-status"])
    files = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            files.append((parts[0].strip(), parts[1].strip()))
    return files


def get_file_size_staged(filepath: str) -> int:
    """Return size of staged file blob."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "-s", f":0:{filepath}"],
            capture_output=True, text=True
        )
        return int(result.stdout.strip())
    except (ValueError, Exception):
        return 0


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class CheckResult(NamedTuple):
    severity: str
    rule: str
    filepath: str
    line_num: int
    line_content: str
    message: str


def check_diff_rules(diff: str) -> list[CheckResult]:
    """Run pattern rules against added lines in the staged diff."""
    results = []
    current_file = "<unknown>"
    line_num = 0

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            line_num = 0
            continue
        if line.startswith("@@"):
            # Parse new file line number
            m = re.search(r"\+([0-9]+)", line)
            line_num = int(m.group(1)) if m else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            line_num += 1
            for rule in RULES:
                if rule.pattern.search(content):
                    results.append(CheckResult(
                        severity=rule.severity,
                        rule=rule.name,
                        filepath=current_file,
                        line_num=line_num,
                        line_content=content.strip()[:120],
                        message=rule.message,
                    ))
        elif not line.startswith("-"):
            line_num += 1

    return results


def check_protected_deletions(staged_files: list[tuple[str, str]]) -> list[CheckResult]:
    """Block deletion of protected files."""
    results = []
    for status, filepath in staged_files:
        if status.startswith("D") and filepath in PROTECTED_PATHS:
            results.append(CheckResult(
                severity="ERROR",
                rule="protected_deletion",
                filepath=filepath,
                line_num=0,
                line_content="",
                message=f"PROTECTED FILE DELETION BLOCKED: {filepath}",
            ))
    return results


def check_large_files(staged_files: list[tuple[str, str]]) -> list[CheckResult]:
    """Warn on large file additions."""
    results = []
    for status, filepath in staged_files:
        if status.startswith("A") or status.startswith("M"):
            size = get_file_size_staged(filepath)
            if size > MAX_FILE_SIZE:
                results.append(CheckResult(
                    severity="WARN",
                    rule="large_file",
                    filepath=filepath,
                    line_num=0,
                    line_content="",
                    message=f"Large file ({size // 1024 // 1024}MB). Consider .gitignore or Git LFS.",
                ))
    return results


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
GREEN = "\033[0;32m"
RESET = "\033[0m"
BOLD = "\033[1m"


def run_checks() -> int:
    diff = get_staged_diff()
    staged_files = get_staged_files()

    if not diff and not staged_files:
        print(f"{GREEN}[pre-commit] No staged changes.{RESET}")
        return 0

    all_results: list[CheckResult] = []
    all_results += check_diff_rules(diff)
    all_results += check_protected_deletions(staged_files)
    all_results += check_large_files(staged_files)

    errors = [r for r in all_results if r.severity == "ERROR"]
    warns = [r for r in all_results if r.severity == "WARN"]

    for r in warns:
        print(f"{YELLOW}[WARN]{RESET}  [{r.rule}] {r.filepath}:{r.line_num}")
        print(f"       {r.message}")
        if r.line_content:
            print(f"       > {r.line_content}")

    for r in errors:
        print(f"{RED}[ERROR]{RESET} [{r.rule}] {r.filepath}:{r.line_num}")
        print(f"       {r.message}")
        if r.line_content:
            print(f"       > {r.line_content}")

    if errors:
        print(f"\n{RED}{BOLD}pre-commit BLOCKED: {len(errors)} error(s) must be resolved before committing.{RESET}")
        print(f"Use `git commit --no-verify` to bypass (not recommended).\n")
        return 1

    if warns:
        print(f"\n{YELLOW}{BOLD}pre-commit WARNING: {len(warns)} warning(s). Commit allowed.{RESET}\n")

    print(f"{GREEN}[pre-commit] All checks passed. {RESET}")
    return 0


def install_hook():
    hook_dir = Path(".git/hooks")
    if not hook_dir.exists():
        print("Not a git repository root. Run from the repo root.")
        sys.exit(1)

    hook_path = hook_dir / "pre-commit"
    src = Path(__file__).resolve()
    shutil.copy2(src, hook_path)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"{GREEN}[pre-commit] Hook installed at {hook_path}{RESET}")
    print("The hook will run automatically on every `git commit`.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaw pre-commit safety hook")
    parser.add_argument("--install", action="store_true", help="Install hook into .git/hooks/pre-commit")
    args = parser.parse_args()

    if args.install:
        install_hook()
    else:
        sys.exit(run_checks())

