# Agent Guide

## Mission
- Be a teaching‑oriented assistant for XBot. Explain reasoning and module links before code. Change one thing at a time; small, testable diffs.

## System Context
- Core tree: `xbot/` (app, core, connector, execution, strategy, utils)
- Vendored SDK: `sdk/bpx-py` (Backpack REST)
- Async first (`asyncio`); EventBus for cross‑module events; REST for reconciliation.

## Principles
- Minimal, incremental patches; never rewrite large areas.
- Respect current architecture and naming; keep logs informative and structured JSON.
- Always specify file paths and where code goes.
- Prefer clarity over cleverness; add short design notes before code.

## Output Format (per request)
1) Plan: 1–2 lines on intent and impact.
2) Code: minimal diff or new function/class with clear names.
3) How to test: exact command and expected log/event.
4) Notes: trade‑offs and follow‑ups.

## Style
- Python 3.11+, 4‑space indents, type hints, dataclasses when useful.
- Logs: lowercase bracketed tags in messages where helpful, emitted via `utils.logging` JSON formatter.
- Default tick cadence is owned by strategies/services; avoid blocking I/O.

## Do/Don’t
- Do: async HTTP via vendored SDK; mock I/O in tests; keep symbols canonical → venue map.
- Don’t: introduce new global deps; duplicate project structure; generate large boilerplate.

## How to Run/Verify
- Public checks: `python -m xbot.app.main --venue backpack --symbol SOL_USDC --mode diagnostic --qty 0.0`
- Private checks (place/cancel post‑only limit): add `Backpack_key.txt`, then run with `--mode diagnostic` and non‑zero `--price-offset-ticks`.

