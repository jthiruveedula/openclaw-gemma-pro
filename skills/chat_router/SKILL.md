# Skill: chat-router

## Purpose
Routes incoming messages from WhatsApp, Telegram, or other channels to the correct model tier (lite/pro) and skill handler based on intent classification.

## Trigger
Every incoming message — this skill runs first in the pipeline.

## Behavior

1. **Receive** raw message payload from the channel webhook (WhatsApp/Telegram).
2. **Classify intent** using a lightweight prompt to the lite model:
   - Simple intents: `greet`, `chitchat`, `rewrite`, `summarize_short` → route to **lite** tier
   - Complex intents: `code`, `plan`, `debug`, `analyze`, `memory_synthesis`, `multi_step` → route to **pro** tier
   - Long messages (>500 tokens estimated) → always **pro**
3. **Load memory context** for the user:
   - Inject today's daily summary (from `memory/daily/YYYY-MM-DD.json`)
   - Inject relevant facts from `memory/facts/master_facts.json`
   - Keep injected context under `maxContextTokens` (default: 4096)
4. **Cross-channel identity**: map `whatsapp::+1234567890` and `telegram::username` to the same user profile so memory is shared.
5. **Approval gate check**: if the intended tool is in `approvalGates` (shell, browser, file_write, external_post), pause and ask user for confirmation before executing.
6. **Dispatch** to the selected model tier with enriched context.

## Input Schema
```json
{
  "channel": "whatsapp | telegram | slack | discord",
  "user_id": "<platform-specific-id>",
  "message": "<raw message text or media description>",
  "media_type": "text | image | audio | document",
  "ts": "<ISO8601 timestamp>"
}
```

## Output Schema
```json
{
  "routed_to": "lite | pro",
  "intent": "<classified intent>",
  "context_tokens_injected": 123,
  "response": "<final assistant reply>"
}
```

## Memory Injection Template
```
[MEMORY CONTEXT]
Date: {today}
Recent summary: {daily_summary}
Known facts:
{facts_list}
[END MEMORY]

User ({channel}): {message}
```

## Notes
- Always write the raw message to `memory/raw/{channel}/{user_id}/{date}.jsonl` before routing.
- The lite model handles classification to avoid burning pro-tier capacity on routing decisions.
- Voice notes (audio) should be transcribed first using Whisper before routing.

