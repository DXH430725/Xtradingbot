# Repository Guidelines

## Project Structure & Module Organization
- Core package: `xbot/`
  - `app/` entrypoints (`main.py`, `ws_listen.py`) and config loader.
  - `connector/` exchange integrations and factory.
  - `core/` runtime utilities (clock, lifecycle, heartbeat, cache, eventbus).
  - `execution/` order routing, risk, positions, market data, models.
  - `strategy/` base and example strategies (`market`, `tracking_limit`).
  - `utils/` helpers (JSON logging, id generation).
- Docs: `xbot/docs/`
- Tests: `xbot/tests/`
- Config/logs: `conf/`, `logs/` (local only; keep secrets out of Git).

## Build, Test, and Development Commands
- Create env: `python -m venv .venv && .venv\Scripts\activate`
- Install deps: `pip install -r xbot/requirements.txt` (add `pyyaml` if using YAML configs).
- Run bot: `python -m xbot.app.main --venue backpack --symbol SOL --qty 1`
  - Or: `python xbot/app/main.py ...` when running from repo root.
- Websocket listener: `python -m xbot.app.ws_listen`
- Tests: `pytest -q`

## Coding Style & Naming Conventions
- Python 3, PEP 8, 4-space indentation; prefer type hints and dataclasses.
- Naming: modules/files `snake_case.py`; classes `PascalCase`; functions/vars `snake_case`.
- Logging: use `utils.logging.setup_logging()` at startup and `get_logger(__name__)`; emit structured JSON with `extra={...}` for fields like `venue`, `symbol`, `mode`.

## Testing Guidelines
- Framework: `pytest`.
- Name tests `tests/test_*.py` or alongside modules as appropriate.
- Add unit tests for new logic; mock external I/O and exchange connectors.
- Run `pytest -q` locally before opening a PR.

## Commit & Pull Request Guidelines
- Commits: imperative, concise subject (<=72 chars), e.g., "core: add wall-clock jitter guard"; reference issues when relevant.
- PRs: include summary, rationale, screenshots/log snippets if UI/ops behavior changes, and steps to reproduce or validate.
- Keep changes focused; update docs in `xbot/docs/` when behavior or config changes.

## Security & Configuration Tips
- Never commit real API keys or tokens. Use local files in `conf/` or environment variables; add new secrets to `.gitignore`.
- Config: pass `--config path/to/config.yaml|json` (YAML requires `pyyaml`). See `app/config.py` for supported fields (risk, heartbeat, symbol map).
