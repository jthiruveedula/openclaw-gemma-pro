# Contributing to OpenClaw Gemma Pro

Thank you for your interest in contributing! Please read this guide before opening issues or submitting PRs.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/openclaw-gemma-pro.git`
3. Create a feature branch: `git checkout -b feat/your-feature-name`
4. Set up your environment: `cp .env.example .env` and fill in **REQUIRED** values
5. Install dependencies: `pip install -r requirements.txt`

## Secrets & Security Hygiene

> **CRITICAL: Never commit secrets, API keys, or credentials to this repository.**

### Rules

- **Never** commit a filled `.env` file. The `.gitignore` already excludes `.env` but **double-check before `git add`**.
- Do not hardcode API keys, tokens, phone numbers, or passwords in source code.
- Replace ALL placeholder values in `.env.example` with your own before running — look for `# REQUIRED` comments.
- The `TWILIO_WHATSAPP_NUMBER` in `.env.example` is Twilio's **sandbox test number** (`+14155238886`). Replace it with your own Twilio WhatsApp Business number before deploying to production.
- If you accidentally commit a secret, **immediately** rotate/revoke the credential and notify the maintainers.

### Pre-commit Checklist

```bash
# Verify no .env file is staged
git status | grep -E '\.env$'

# Run TruffleHog locally before pushing
docker run --rm -v "$PWD:/repo" trufflesecurity/trufflehog:latest filesystem /repo --only-verified
```

### CI Secret Scanning

Every PR runs TruffleHog (pinned to a specific version) via GitHub Actions. PRs with detected verified secrets will be blocked.

## Code Style

- Python: Follow PEP 8; we use `ruff` for linting (`ruff check .`)
- Type hints: Required for all new functions (checked by `mypy`)
- Security: Run `bandit -r guardrails/ workers/` before submitting
- Tests: Add tests for new features in `tests/`

## Commit Messages

We use Conventional Commits:

```
feat(#ISSUE): short description
fix(#ISSUE): short description
docs(#ISSUE): short description
refactor(#ISSUE): short description
```

## Pull Requests

- Link to the issue being fixed: `Closes #N`
- Ensure all CI checks pass
- Keep PRs focused — one issue per PR
- Update `README.md` if behavior changes

## Environment Variables

Variables in `.env.example` are annotated with:
- `# REQUIRED` — must be set before the app will run
- `# OPTIONAL` — can be left empty to disable the feature

Never push a `.env` with real values. When in doubt, run `git diff --cached` and check for secrets before committing.

## Reporting Security Issues

Do NOT open a public issue for security vulnerabilities. Instead, use GitHub's private security advisory feature or contact the maintainer directly.

