# openclaw-gemma-pro

> **Self-hosted GenAI Pro stack** — Gemma 4 via Ollama + OpenClaw + WhatsApp/Telegram + daily memory indexing for persistent cross-channel context.

Run a production-quality personal AI assistant for near-zero recurring cost. No cloud API needed for routine use.

---

## Architecture

```
WhatsApp / Telegram
        |
        v
  Webhook Server (FastAPI)
        |
        v
  [Skill: chat-router]
    |           |
    v           v
  LITE tier   PRO tier
  gemma4:4b   gemma4:27b
  (via Ollama native endpoint)
        |
        v
  Memory Context Injection
  (daily summary + facts)
        |
        v
     Response
        |
        v
  Raw log -> memory/raw/
        |
  [Cron 2AM]
        v
  [Worker: memory_indexer]
    - Daily summary
    - Fact extraction
    - Prune old raw logs
```

## Repository Structure

```
openclaw-gemma-pro/
├── config/
│   ├── openclaw.json              # OpenClaw agent + model + channel config
│   └── model-routing.json         # Intent-based lite/pro routing rules
├── scripts/
│   └── bootstrap.sh               # One-command setup: Ollama, venv, cron
├── skills/
│   ├── chat-router/
│   │   └── SKILL.md               # Message routing + memory injection skill
│   └── daily-memory-indexer/
│       └── SKILL.md               # Nightly memory indexing skill
├── workers/
│   └── memory_indexer/
│       └── index_memory.py        # Python worker: summarize + fact extract
├── .env.example                   # Environment variable template
├── requirements.txt               # Python dependencies
├── .gitignore
├── LICENSE                        # MIT
└── README.md
```

## Quick Start

### Prerequisites
- Linux or macOS (Windows via WSL2)
- Python 3.11+
- 16GB+ RAM recommended (27B model)
- ~20GB free disk (for model weights)

### 1. Clone and bootstrap

```bash
git clone https://github.com/jthiruveedula/openclaw-gemma-pro.git
cd openclaw-gemma-pro
bash scripts/bootstrap.sh
```

This will:
- Install Ollama (if missing)
- Pull `gemma4:27b` (pro) and `gemma4:4b` (lite)
- Create a Python `.venv` and install all deps
- Copy `.env.example` → `.env`
- Create `memory/` directory structure
- Set up nightly cron job for memory indexer

### 2. Configure environment

```bash
vim .env  # or nano .env
```

Fill in:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` — for WhatsApp via Twilio
- `WEBHOOK_BASE_URL` — your public server URL

### 3. Test Ollama locally

```bash
ollama run gemma4:27b "What can you help me with?"
```

### 4. Test memory indexer (dry run)

```bash
.venv/bin/python workers/memory_indexer/index_memory.py --dry-run
```

### 5. Start webhook server

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Model Tiers

| Tier | Model | Use Cases | Cost |
|------|-------|-----------|------|
| **Lite** | `gemma4:4b` | Greet, chitchat, rewrites, short summaries | ~0 (local) |
| **Pro** | `gemma4:27b` | Code, planning, analysis, memory synthesis | ~0 (local) |
| **Cloud fallback** | `gpt-4o` | Only on Ollama errors (disabled by default) | Pay-per-use |

Routing is intent-based — see `config/model-routing.json`. Messages >500 tokens always go to Pro.

## Memory System

Three-layer memory prevents stale context without stuffing the full history into every prompt:

| Layer | Location | Contents |
|-------|----------|----------|
| **Raw** | `memory/raw/{channel}/{user}/{date}.jsonl` | All messages, one JSON per line |
| **Daily** | `memory/daily/{date}.json` | LLM-generated summary + active tasks |
| **Facts** | `memory/facts/master_facts.json` | Durable upserted fact store |

The `daily-memory-indexer` worker runs at 2 AM, reads raw logs, calls Gemma 4 Pro, and writes structured summaries. The `chat-router` skill injects today's summary + relevant facts into every live prompt (capped at `MAX_CONTEXT_TOKENS`).

## Cross-Channel Identity

Users are identified across channels via a unified profile:
```
whatsapp::+1234567890  ]
                        ]--> user profile --> shared memory
telegram::username123  ]
```

Configure identity mappings in `config/openclaw.json` under `channels`.

## Approval Gates

High-risk tools require explicit user confirmation before execution:
- `shell` — running terminal commands
- `browser` — automated web actions
- `file_write` — writing to local filesystem
- `external_post` — sending emails, messages, API posts

Configure in `config/openclaw.json` → `routing.approvalGates`.

## Environment Variables

See `.env.example` for all available options. Key variables:

```bash
OLLAMA_BASE_URL=http://localhost:11434   # Native Ollama endpoint (NOT /v1)
OLLAMA_MODEL=gemma4:27b
OLLAMA_LITE_MODEL=gemma4:4b
MEMORY_BASE_DIR=./memory
MAX_RAW_DAYS=30
TELEGRAM_BOT_TOKEN=...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
```

> **Important**: Use the native Ollama base URL `http://localhost:11434` — NOT the OpenAI-compatible `/v1` path, as this breaks tool-calling with Gemma 4.

## Memory Indexer Worker

Manual run options:

```bash
# Index yesterday (default)
.venv/bin/python workers/memory_indexer/index_memory.py

# Index a specific date
.venv/bin/python workers/memory_indexer/index_memory.py --date 2026-04-06

# Dry run (no Ollama call)
.venv/bin/python workers/memory_indexer/index_memory.py --dry-run
```

Cron (auto-configured by bootstrap.sh):
```
0 2 * * * /path/to/.venv/bin/python /path/to/workers/memory_indexer/index_memory.py
```

## Roadmap

- [ ] FastAPI webhook server (`app/main.py`) for Telegram + WhatsApp
- [ ] Cross-channel identity mapper
- [ ] Voice note transcription via Whisper
- [ ] Screenshot/image analysis skill
- [ ] Personal daily briefing skill (morning summary via Telegram)
- [ ] Repo watcher skill (GitHub PR summaries)
- [ ] ChromaDB semantic memory search
- [ ] Docker Compose setup
- [ ] GitHub Actions CI (lint, test)

## Contributing

PRs welcome. Keep skills self-contained under `skills/`, workers under `workers/`, and config in `config/`.

## License

MIT — see [LICENSE](LICENSE)

---

*Built by [@jthiruveedula](https://github.com/jthiruveedula)*
