Live tests against real servers; `tests/run_wall.py` globs only `tests/test_*.py`, so nothing here runs in the wall.
`test_zulip_live.py` wraps `scripts/zulip_smoke.py` and skips unless `EA_ZULIP_TEST_SITE`, `_BOT_EMAIL`, `_BOT_KEY`, `_OWNER_EMAIL` and `_OWNER_KEY` are set.
Run with `.venv/bin/python3 -m pytest tests/integration -q` after bootstrapping the server per `deploy/zulip/README.md`.
