# Python conventions

- Python 3.12, ruff for lint/format, pytest for tests.
- Type hints on public functions; `from __future__ import annotations`.
- No new dependencies without checking the license gate allowlist first.
- Async code uses asyncio; no thread pools unless unavoidable.
- Every bug fix ships with a regression test.
