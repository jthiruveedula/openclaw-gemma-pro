#!/usr/bin/env python3
"""
Daily Memory Indexer Worker
============================
Runs nightly (default: 2 AM via cron) to:
  1. Read raw per-channel message logs from memory/raw/
  2. Call Gemma 4 via Ollama to produce a daily summary
  3. Extract durable facts (preferences, tasks, contacts)
  4. Write structured JSON to memory/daily/ and memory/facts/
  5. Prune raw logs older than MAX_RAW_DAYS

Usage:
  python index_memory.py [--date YYYY-MM-DD] [--dry-run]

Cron setup (add to crontab):
  0 2 * * * /path/to/venv/bin/python /path/to/workers/memory_indexer/index_memory.py
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config (override via env vars)
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:27b")
MEMORY_BASE = Path(os.getenv("MEMORY_BASE_DIR", "./memory"))
RAW_DIR = MEMORY_BASE / "raw"
DAILY_DIR = MEMORY_BASE / "daily"
FACTS_DIR = MEMORY_BASE / "facts"
MAX_RAW_DAYS = int(os.getenv("MAX_RAW_DAYS", "30"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ensure_dirs():
    for d in [RAW_DIR, DAILY_DIR, FACTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_raw_messages(target_date: date) -> list[dict]:
    """Load all raw message files for a given date across all users/channels."""
    messages = []
    date_str = target_date.isoformat()
    for channel_dir in RAW_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for user_dir in channel_dir.iterdir():
            if not user_dir.is_dir():
                continue
            msg_file = user_dir / f"{date_str}.jsonl"
            if msg_file.exists():
                with open(msg_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entry = json.loads(line)
                                entry["_channel"] = channel_dir.name
                                entry["_user"] = user_dir.name
                                messages.append(entry)
                            except json.JSONDecodeError:
                                pass
    messages.sort(key=lambda m: m.get("ts", ""))
    return messages


def build_summary_prompt(messages: list[dict], target_date: date) -> str:
    convo_text = ""
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        channel = m.get("_channel", "?")
        user = m.get("_user", "?")
        convo_text += f"[{channel}/{user}] {role}: {text}\n"

    return f"""You are a memory indexer for a personal AI assistant.

Date: {target_date.isoformat()}

Below are all conversations from today across WhatsApp, Telegram, and other channels.

Your task:
1. Write a 3-5 sentence DAILY SUMMARY capturing the most important topics, decisions, and actions.
2. Extract DURABLE FACTS - preferences, ongoing projects, contact info, recurring reminders, or commitments the user mentioned.
   Format facts as a JSON array of objects with keys: "category", "key", "value", "confidence" (high/medium/low).

Respond in this exact JSON format:
{{
  "date": "{target_date.isoformat()}",
  "summary": "<3-5 sentence summary here>",
  "facts": [
    {{"category": "<category>", "key": "<key>", "value": "<value>", "confidence": "<high|medium|low>"}}
  ],
  "active_tasks": ["<task1>", "<task2>"],
  "message_count": {len(messages)}
}}

--- CONVERSATIONS ---
{convo_text[:6000]}
--- END CONVERSATIONS ---

Respond with valid JSON only, no markdown fences.
"""


def call_ollama(prompt: str) -> str:
    """Call Ollama generate API (native endpoint, NOT /v1)."""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 2048},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except httpx.HTTPError as e:
        print(f"[ERROR] Ollama call failed: {e}", file=sys.stderr)
        return ""


def parse_llm_json(raw: str) -> dict | None:
    """Extract JSON from LLM output, handling minor formatting issues."""
    raw = raw.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting first {...} block
        match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return None


def merge_facts(new_facts: list[dict], date_str: str) -> None:
    """Upsert new facts into the master facts index."""
    facts_file = FACTS_DIR / "master_facts.json"
    if facts_file.exists():
        with open(facts_file) as f:
            master = json.load(f)
    else:
        master = {"last_updated": "", "facts": {}}

    for fact in new_facts:
        key = f"{fact.get('category', 'general')}/{fact.get('key', 'unknown')}"
        master["facts"][key] = {
            "value": fact.get("value"),
            "confidence": fact.get("confidence", "medium"),
            "last_seen": date_str,
        }

    master["last_updated"] = date_str
    with open(facts_file, "w") as f:
        json.dump(master, f, indent=2)


def prune_old_raw(cutoff_days: int) -> int:
    """Delete raw message files older than cutoff_days. Returns count deleted."""
    cutoff = date.today() - timedelta(days=cutoff_days)
    deleted = 0
    for channel_dir in RAW_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for user_dir in channel_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for msg_file in user_dir.glob("*.jsonl"):
                try:
                    file_date = date.fromisoformat(msg_file.stem)
                    if file_date < cutoff:
                        msg_file.unlink()
                        deleted += 1
                except ValueError:
                    pass
    return deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(target_date: date, dry_run: bool = False) -> None:
    ensure_dirs()
    date_str = target_date.isoformat()
    print(f"[{datetime.now().isoformat()}] Starting memory indexing for {date_str}")

    messages = load_raw_messages(target_date)
    print(f"  Loaded {len(messages)} messages")

    if not messages:
        print("  No messages found, skipping LLM call")
        return

    prompt = build_summary_prompt(messages, target_date)

    if dry_run:
        print("  [DRY RUN] Skipping Ollama call")
        print(f"  Prompt preview (first 500 chars):\n{prompt[:500]}")
        return

    print(f"  Calling Ollama ({OLLAMA_MODEL}) for summary...")
    raw_response = call_ollama(prompt)

    if not raw_response:
        print("  [ERROR] Empty response from Ollama", file=sys.stderr)
        return

    result = parse_llm_json(raw_response)
    if not result:
        print("  [ERROR] Failed to parse LLM JSON response", file=sys.stderr)
        # Save raw response for debugging
        debug_file = DAILY_DIR / f"{date_str}_raw_debug.txt"
        debug_file.write_text(raw_response)
        return

    # Write daily summary
    daily_file = DAILY_DIR / f"{date_str}.json"
    with open(daily_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"  Written daily summary -> {daily_file}")

    # Merge facts
    facts = result.get("facts", [])
    if facts:
        merge_facts(facts, date_str)
        print(f"  Merged {len(facts)} facts into master_facts.json")

    # Prune old raw logs
    deleted = prune_old_raw(MAX_RAW_DAYS)
    if deleted:
        print(f"  Pruned {deleted} old raw message files (>{MAX_RAW_DAYS} days)")

    print(f"[{datetime.now().isoformat()}] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily memory indexer for OpenClaw Pro")
    parser.add_argument(
        "--date",
        default=str(date.today() - timedelta(days=1)),
        help="Date to index (YYYY-MM-DD). Defaults to yesterday.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse messages and build prompt but skip Ollama call",
    )
    args = parser.parse_args()

    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        print(f"[ERROR] Invalid date format: {args.date}", file=sys.stderr)
        sys.exit(1)

    run(target, dry_run=args.dry_run)

