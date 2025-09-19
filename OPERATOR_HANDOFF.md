Operator Handoff: How to Ping the Agent and What to Capture

When you see an issue (e.g., TP placed but no visible order, count mismatches), use this guide to gather the right evidence and reach out efficiently.

What to Capture
- Timestamps and symbol
- Strategy logs around the moment:
  - Lines containing: `tp submit intent`, `tp placed`, `tp verify`
  - Any `imbalance detected` / `coverage ok` lines
- Connector logs around submission:
  - `place_limit submit:` and `place_limit result:` for the same COI
- Diagnostics output (run close to the event time):
  - `python mm_bot/bin/diagnose_tp_mismatch.py`
  - `python mm_bot/bin/diagnose_tp_side_vs_ro.py`
- Config context:
  - `XTB_SYMBOL`, `XTB_LIGHTER_ACCOUNT_INDEX`, base URL, and keys file path used by the running process

How to Ping the Agent
- In this workspace’s chat, send a short note with the subject and attach the above artifacts. Example:
  - Subject: “TP placed but no UI order at 20:51, BTC, acct=73545”
  - Paste relevant log snippets and the diagnostics outputs.
  - If you prefer, reference the log file path and timestamps instead of pasting.

Quick Triage Checklist (self-serve)
- Is account_index and symbol exactly matching the UI you’re observing?
- Do you see `place_limit result: ... err=None` but `tp verify: ws_open=False rest_open=False`? This indicates the submission likely didn’t reach the exchange; re-check credentials, account, or rate limits.
- Is `matched_by_side` much larger than `matched_by_reduce_only` in diagnose_tp_side_vs_ro? That’s expected with the current strategy counting method; the side-based count includes non-reduce-only orders.

Where Logs Are Emitted
- Strategy (Trend Ladder): logger name `mm_bot.strategy.trend_ladder`
- Lighter Connector: logger name `mm_bot.connector.lighter`

Optional: Extra Data That Helps
- A screenshot of the UI’s order list around the same timestamp (blur keys if needed)
- The COI(s) printed in logs and whether they appear anywhere in WS/REST or the UI

