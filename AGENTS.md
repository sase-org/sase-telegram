# sase-telegram - Agent Instructions

## Overview
Telegram integration chop for sase. Provides outbound notification delivery
and inbound action handling via Telegram bot API.

## Build & Run
```bash
just install    # Install in editable mode with dev deps
just lint       # ruff + mypy
just fmt        # Auto-format
just test       # pytest
just check      # lint + test
```

## Architecture
- `src/sase_telegram/credentials.py` — Bot token (via `pass`), chat ID and username (env vars)
- `src/sase_telegram/telegram_client.py` — Sync wrapper around async `python-telegram-bot`
- `src/sase_telegram/callback_data.py` — Encode/decode inline keyboard callback data
- `src/sase_telegram/pending_actions.py` — Persist pending actions to JSON file
- `src/sase_telegram/rate_limit.py` — Sliding window rate limiter
- `src/sase_telegram/scripts/` — CLI entry points for outbound/inbound chops
- Depends on `sase>=0.1.0` and `python-telegram-bot>=21.0`

## Code Conventions
- Absolute imports: `from sase_telegram.credentials import get_bot_token`
- Target Python 3.12+
- Follow ruff rules matching sase core
