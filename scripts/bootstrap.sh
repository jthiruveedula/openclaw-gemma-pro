#!/usr/bin/env bash
# =============================================================================
# OpenClaw Gemma Pro - Bootstrap Script
# Sets up Ollama, pulls Gemma 4 models, creates Python venv, and installs deps
# Usage: bash scripts/bootstrap.sh
# =============================================================================

set -euo pipefail

COLOR_GREEN="\033[0;32m"
COLOR_YELLOW="\033[1;33m"
COLOR_RED="\033[0;31m"
COLOR_RESET="\033[0m"

log()  { echo -e "${COLOR_GREEN}[bootstrap]${COLOR_RESET} $*"; }
warn() { echo -e "${COLOR_YELLOW}[warn]${COLOR_RESET} $*"; }
err()  { echo -e "${COLOR_RED}[error]${COLOR_RESET} $*" >&2; exit 1; }

# -------------------------------------------------------------------------
# 1. Check OS
# -------------------------------------------------------------------------
OS="$(uname -s)"
log "Detected OS: $OS"

# -------------------------------------------------------------------------
# 2. Install / verify Ollama
# -------------------------------------------------------------------------
if ! command -v ollama &>/dev/null; then
  log "Ollama not found. Installing..."
  if [[ "$OS" == "Linux" ]]; then
    curl -fsSL https://ollama.com/install.sh | sh
  elif [[ "$OS" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      brew install ollama
    else
      err "Homebrew not found. Install Ollama manually from https://ollama.com/download"
    fi
  else
    err "Unsupported OS: $OS. Install Ollama manually from https://ollama.com/download"
  fi
else
  log "Ollama already installed: $(ollama --version)"
fi

# -------------------------------------------------------------------------
# 3. Start Ollama server in background (if not running)
# -------------------------------------------------------------------------
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  log "Starting Ollama server in background..."
  ollama serve &>/tmp/ollama.log &
  OLLAMA_PID=$!
  sleep 3
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    err "Ollama server failed to start. Check /tmp/ollama.log"
  fi
  log "Ollama server started (PID $OLLAMA_PID)"
else
  log "Ollama server already running"
fi

# -------------------------------------------------------------------------
# 4. Pull Gemma 4 models
# -------------------------------------------------------------------------
PRO_MODEL="gemma4:27b"
LITE_MODEL="gemma4:4b"

log "Pulling Gemma 4 Pro model: $PRO_MODEL (this may take several minutes)"
ollama pull "$PRO_MODEL"

log "Pulling Gemma 4 Lite model: $LITE_MODEL"
ollama pull "$LITE_MODEL"

log "Verifying models..."
ollama list

# -------------------------------------------------------------------------
# 5. Python venv + dependencies
# -------------------------------------------------------------------------
PYTHON=$(command -v python3 || command -v python || echo "")
[[ -z "$PYTHON" ]] && err "Python 3.11+ not found. Please install it first."

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Python version: $PY_VERSION"

if [[ ! -d .venv ]]; then
  log "Creating Python virtual environment (.venv)"
  "$PYTHON" -m venv .venv
else
  log "Virtual environment .venv already exists"
fi

# Activate venv
# shellcheck disable=SC1091
source .venv/bin/activate

log "Installing Python dependencies from requirements.txt"
pip install --upgrade pip -q
pip install -r requirements.txt -q
log "Python deps installed"

# -------------------------------------------------------------------------
# 6. Create .env from .env.example if not present
# -------------------------------------------------------------------------
if [[ ! -f .env ]]; then
  log "Creating .env from .env.example"
  cp .env.example .env
  warn "Edit .env with your actual API keys before starting channels (Telegram, WhatsApp)."
else
  log ".env already exists, skipping copy"
fi

# -------------------------------------------------------------------------
# 7. Create memory directory structure
# -------------------------------------------------------------------------
log "Creating memory directory structure"
mkdir -p memory/raw memory/daily memory/facts

# -------------------------------------------------------------------------
# 8. Set up cron job for memory indexer (Linux/macOS)
# -------------------------------------------------------------------------
CRON_JOB="0 2 * * * $(pwd)/.venv/bin/python $(pwd)/workers/memory_indexer/index_memory.py >> $(pwd)/memory/indexer.log 2>&1"

if crontab -l 2>/dev/null | grep -q "index_memory.py"; then
  log "Cron job already set up for memory indexer"
else
  log "Adding cron job for nightly memory indexer (2 AM daily)"
  (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
  log "Cron job added"
fi

# -------------------------------------------------------------------------
# Done
# -------------------------------------------------------------------------
echo ""
log "Bootstrap complete!"
echo -e ""
echo -e "  Next steps:"
echo -e "  1. Edit .env with your Telegram bot token and Twilio/WhatsApp credentials"
echo -e "  2. Test Ollama: ollama run gemma4:27b 'Hello'"
echo -e "  3. Test memory indexer (dry run):"
echo -e "       .venv/bin/python workers/memory_indexer/index_memory.py --dry-run"
echo -e "  4. Start the webhook server (when channels are configured):"
echo -e "       .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
echo -e ""

