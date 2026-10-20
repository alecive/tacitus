#!/usr/bin/env python3
"""Daily digest of changes to the ea-bot stores that steer later runs,
posted to the health channel only on a change (preprocessor/persistence_digest.py
has the watch list and the rules).

Cron (owner, daily):  40 6 * * *  python3 /opt/ea-system/scripts/persistence-digest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "preprocessor"))

if __name__ == "__main__":
    import persistence_digest
    sys.exit(persistence_digest.main(sys.argv[1:]))
