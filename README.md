# 🤖 OpenClaw-Gemma-Pro
> **Self-hosted GenAI Pro stack** — Gemma 4 via Ollama + OpenClaw + WhatsApp/Telegram + daily memory indexing for persistent cross-channel context.
>
> [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Gemma](https://img.shields.io/badge/Gemma-2-green)](https://ollama.com)

[![OS](https://img.shields.io/badge/OS-%F0%9F%8D%8E%20Mac%20%7C%20%F0%9F%90%A7%20Linux%20%7C%20%F0%9F%AA%9F%20Windows-0078d7.svg)](#-installation) [![Model](https://img.shields.io/badge/LLM-Gemma%204%20via%20Ollama-FF6600.svg)](https://ollama.com/) [![Memory](https://img.shields.io/badge/Memory-3--Tier%20Indexed-22c55e.svg)](#multi-agent-architecture) [![Guardrails](https://img.shields.io/badge/Safety-Guardrails%20ON-dc2626.svg)](#guardrails) [![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://python.org)

Run a **production-quality personal AI assistant** for near-zero recurring cost.  
No cloud API key required. Runs entirely on your laptop or server.

---

## 📺 What It Looks Like

```
 +-------------------------+
 |  You (WhatsApp/Telegram) |
 +-----------+-------------+
             |
             v
 +-------------------------+     +------------------+
 |  FastAPI Webhook Server  +---->+  Skill Router    |
 +-------------------------+     +--------+---------+
                                          |
                          +--------------+--------------+
                          |                             |
                    LITE tier                      PRO tier
                    gemma2:9b                      gemma2:27b
                  (fast, cheap)                (smart, thorough)
                          |                             |
                          +--------------+--------------+
                                          |
                                          v
                              +---------------------+
                              |  Memory Injector     |
                              |  daily summary       |
                              |  + durable facts     |
                              +---------------------+
                                          |
                                          v
                              +---------------------+
                              |   Your Response      |
                              +---------------------+
```

---

## ⚡ Installation

Pick your operating system:

### 🍎 macOS

```bash
# Step 1 – Install Ollama (native Mac app)
brew install ollama
# OR download the .app from https://ollama.com/download/mac

# Step 2 – Pull Gemma model
ollama pull gemma2:27b        # PRO tier (16 GB RAM+)
# ollama pull gemma2:9b       # LITE tier (8 GB RAM)

# Step 3 – Clone & bootstrap
git clone https://github.com/jthiruveedula/openclaw-gemma-pro.git
cd openclaw-gemma-pro
bash scripts/bootstrap.sh

# Step 4 – Copy env and start
cp .env.example .env
# Edit .env with your Telegram/WhatsApp tokens
python -m uvicorn app.main:app --reload
```

---

### 🐧 Linux

```bash
# Step 1 – Install Ollama
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable ollama
sudo systemctl start ollama

# Step 2 – Pull Gemma model
ollama pull gemma2:27b        # PRO tier
# ollama pull gemma2:9b       # LITE tier

# Step 3 – Clone & bootstrap
git clone https://github.com/jthiruveedula/openclaw-gemma-pro.git
cd openclaw-gemma-pro
bash scripts/bootstrap.sh

# Step 4 – Configure & run
cp .env.example .env
nano .env  # Add your bot tokens
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

### 🪟 Windows

```powershell
# Step 1 – Install Ollama (Windows installer)
# Download from: https://ollama.com/download/windows
# Run OllamaSetup.exe, then open a NEW terminal

# Step 2 – Pull Gemma model
ollama pull gemma2:27b

# Step 3 – Clone & bootstrap
git clone https://github.com/jthiruveedula/openclaw-gemma-pro.git
cd openclaw-gemma-pro

# Use Python venv (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Step 4 – Configure & run
copy .env.example .env
notepad .env  # Add your bot tokens
python -m uvicorn app.main:app --reload
```

> **Windows tip:** Enable WSL2 for best performance: `wsl --install` then follow the Linux guide inside WSL.

---

## 📋 Minimum Requirements

| Tier | RAM | Disk | Model | Speed |
|------|-----|------|-------|-------|
| **LITE** | 8 GB | 6 GB | `gemma2:9b` | ~2 sec/reply |
| **PRO** | 16 GB | 18 GB | `gemma2:27b` | ~5 sec/reply |
| **PRO+GPU** | 16 GB + VRAM | 18 GB | `gemma2:27b` | <1 sec/reply |

---

## 🏗️ Architecture

```text
WhatsApp / Telegram
       |
       v
Webhook Server (FastAPI)
       |
       v
[Skill: chat-router]
       |         |
       v         v
   LITE tier   PRO tier
   gemma2:9b  gemma2:27b
  (Ollama :11434)
       |
       v
Memory Context Injection
(daily summary + durable facts)
       |
       v
    Response sent back
       |
       v
Appended to memory/raw/YYYY-MM-DD.jsonl
```

---

## 🤖 Multi-Agent Architecture

OpenClaw-Gemma-Pro runs a **DAG-based parallel agent framework**:

```text
User Goal
    |
    v
AgentCoordinator  (workers/orchestrator/coordinator.py)
    |
    +-- [1] PlannerAgent   --> calls Gemma, decomposes goal into subtasks
    |
    +-- [2] ExecutorAgents --> run subtasks IN PARALLEL (asyncio + semaphore)
    |         each action checked by ActionGuardrail before execution
    |
    +-- [3] MemoryAgent    --> persists results:
    |         raw/YYYY-MM-DD.jsonl
    |         daily/YYYY-MM-DD.md
    |         facts/index.jsonl
    |
    +-- [4] CriticAgent    --> scores output: pass / warn / fail
```

### Agent Table

| Agent | File | What It Does |
|-------|------|-------------|
| 📝 PlannerAgent | `workers/agents/planner_agent.py` | Decomposes goal into 2-6 subtasks via Gemma |
| ⚡ ExecutorAgent | `workers/agents/executor_agent.py` | Runs each task; gates shell/file ops through guardrail |
| 🧠 MemoryAgent | `workers/agents/memory_agent.py` | 3-tier memory: raw logs, daily summaries, durable facts |
| 🔍 CriticAgent | `workers/agents/critic_agent.py` | Reviews outputs, flags issues, returns score |

---

## 🛡️ Guardrails

A multi-layer safety net that prevents accidental destructive actions:

```text
Any risky action
       |
       v
ActionGuardrail.check()
   |
   +-- ALLOW  --> execute
   +-- WARN   --> log + notify user
   +-- BLOCK  --> hard-stop, reason logged

Blocked by default:
  rm -rf, DROP TABLE, shutil.rmtree memory/,
  shell=True with destructive patterns,
  writes to protected config paths
```

### CI Safety Jobs

| Job | What It Checks |
|-----|----------------|
| `lint-and-safety` | Ruff, Mypy, Bandit, Safety audit |
| `secret-scan` | TruffleHog verified-secrets scan |
| `delete-guard` | Blocks PRs deleting >10 files or containing memory-wipe diffs |

---

## 📁 Repo Structure

```
openclaw-gemma-pro/
├── .github/workflows/    # CI: guardrails, secret-scan, delete-guard
├── config/               # Model routing, OpenClaw config JSON
├── guardrails/           # ActionGuardrail + pre-commit safety hook
├── scripts/
│   └── bootstrap.sh      # One-shot setup for all platforms
├── skills/               # Chat-router, daily-memory-indexer
├── workers/
│   ├── agents/           # planner, executor, memory, critic
│   ├── memory_indexer/   # Daily memory indexing job
│   └── orchestrator/     # AgentCoordinator (DAG runner)
├── .env.example
├── requirements.txt
└── README.md
```

---

## ⚙️ Configuration

Edit `.env` (copy from `.env.example`):

```env
# Ollama endpoint (same for Mac/Linux/Windows)
OLLAMA_URL=http://localhost:11434

# Model selection
LITE_MODEL=gemma2:9b
PRO_MODEL=gemma2:27b

# Messaging (optional – needed for WhatsApp/Telegram)
TELEGRAM_BOT_TOKEN=your_token_here
WHATSAPP_VERIFY_TOKEN=your_verify_token

# Memory paths
MEMORY_ROOT=./memory
```

---

## 🚀 Quick Commands

```bash
# Run a one-off goal via multi-agent coordinator
python -m workers.orchestrator.coordinator "Summarise today's Telegram messages"

# Index yesterday's memory manually
python workers/memory_indexer/index_memory.py

# Install pre-commit safety hook
python guardrails/pre_commit_hook.py --install

# Run tests
pytest tests/ -v
```

---

## 📝 Roadmap

- [x] Multi-agent orchestration (Planner / Executor / Memory / Critic)
- [x] 3-tier daily memory indexing
- [x] Action guardrails + pre-commit safety
- [x] CI: secret scan + accidental-delete guard
- [ ] WhatsApp & Telegram webhook connectors
- [ ] Multi-turn conversation memory (Firestore)
- [ ] Desktop system-tray companion app
- [ ] Voice input via Whisper
- [ ] Web UI dashboard

---

## 🤝 Contributing

PRs welcome. Before opening one:

```bash
# Install the pre-commit hook (runs automatically on git commit)
python guardrails/pre_commit_hook.py --install

# Run linting manually
ruff check . && mypy workers/ guardrails/
```

---

## 📄 License

MIT — see [LICENSE](LICENSE)
