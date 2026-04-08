# Skill: daily-memory-indexer

## Purpose
Orchestrates the nightly memory indexing pipeline. Triggered by cron scheduler or manually invoked via a chat command like `!index today`.

## Trigger
- **Scheduled**: Cron at 2 AM daily (`0 2 * * *`)
- **Manual**: User sends `!memory index [date]` in any channel

## Pipeline Steps

```
[Raw JSONL Logs]
      |
      v
[Load & Merge Messages by User+Channel]
      |
      v
[Build Prompt for Gemma 4 Pro]
      |
      v
[Ollama /api/generate Call]
      |
      v
[Parse JSON Response]
      |
    /-+--\
   /        \
  v          v
[Daily       [Facts
 Summary]    Upsert]
memory/      memory/
daily/       facts/
      |
      v
[Prune Raw Logs >30 days]
      |
      v
[Done]
```

## Memory Directory Structure
```
memory/
  raw/
    whatsapp/
      +1234567890/
        2026-04-07.jsonl    # one JSONL entry per message
    telegram/
      username123/
        2026-04-07.jsonl
  daily/
    2026-04-07.json         # daily summary + active tasks
    2026-04-06.json
  facts/
    master_facts.json       # upserted durable fact store
```

## daily/YYYY-MM-DD.json Schema
```json
{
  "date": "2026-04-07",
  "summary": "User discussed project X planning and asked about BigQuery optimization. A flight booking reminder was set for tomorrow.",
  "facts": [
    {"category": "project", "key": "active_project", "value": "Project X migration", "confidence": "high"},
    {"category": "preference", "key": "preferred_airline", "value": "United", "confidence": "medium"}
  ],
  "active_tasks": [
    "Review BigQuery migration plan",
    "Book flight to Dallas"
  ],
  "message_count": 47
}
```

## Context Injection for Live Prompts
When the chat-router skill injects memory into a live prompt:
```
[MEMORY]
As of 2026-04-07:
- Active project: Project X migration
- Preferred airline: United
Yesterday: User discussed BigQuery optimization and set a flight booking reminder.
Active tasks: Review BigQuery migration plan | Book flight to Dallas
[/MEMORY]
```

## Manual Commands
| Command | Action |
|---|---|
| `!memory index` | Index yesterday's messages |
| `!memory index 2026-04-06` | Index specific date |
| `!memory facts` | Show current master facts |
| `!memory summary` | Show last daily summary |
| `!memory dry-run` | Test without Ollama call |

## Notes
- Raw logs are JSONL format (one JSON object per line) for easy streaming and grep.
- Facts use upsert semantics: same `category/key` overwrites previous value.
- The indexer uses Gemma 4 Pro (27B) for quality summaries; it runs at 2 AM when idle.
- `MAX_RAW_DAYS=30` by default; tune via env var to balance storage vs context history.

