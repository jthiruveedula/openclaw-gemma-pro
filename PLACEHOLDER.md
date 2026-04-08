# Ollama Timeout Configuration

This file is a placeholder for Issue #6.

## Problem
- Ollama timeout of 120s causes false timeouts on Gemma 4 27B cold start
- Planner uses 60s hardcoded timeout

## TODO
- Increase timeout values to accommodate cold start scenarios
- Make timeout values configurable
- Add logging for timeout events
- Test with Gemma 4 27B model cold starts
