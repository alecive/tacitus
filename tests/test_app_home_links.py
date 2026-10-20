#!/usr/bin/env python3
"""App Home link buttons carry web URLs only.

Slack rejects the whole home view (`invalid url`) for one file:// or
mailto: link button, and a project's link list may carry a local path.
Those links stay in the pack; the card shows the web ones.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import app_home  # noqa: E402


def _link_buttons(view: dict) -> list[dict]:
    out = []
    for b in view["blocks"]:
        if b.get("type") != "actions":
            continue
        out += [e for e in b["elements"] if e.get("type") == "button" and "url" in e]
    return out


def test_non_web_links_are_left_off_the_card():
    desk = [{
        "id": "budget-plan-2027", "name": "Budget plan", "links": [
            {"url": "file:///home/ea-bot/.ea/docs/budget-plan-2027/DECISION.md", "title": "DECISION"},
            {"url": "https://docs.google.com/document/d/abc/edit", "title": "Intake form"},
            {"url": "mailto:someone@example.org", "title": "mail"},
            {"url": "HTTP://rsl.ethz.ch/", "title": "RSL"},
        ]}]
    view = app_home.build_home_view(desk, unfiled=[], projects=[])
    urls = [e["url"] for e in _link_buttons(view)]
    assert urls == ["https://docs.google.com/document/d/abc/edit", "HTTP://rsl.ethz.ch/"], urls
    assert all(u.lower().startswith(("http://", "https://")) for u in urls)


def test_web_links_still_capped_and_ordered():
    links = [{"url": f"https://example.org/{i}", "title": f"doc {i}"} for i in range(6)]
    view = app_home.build_home_view([{"id": "p", "name": "P", "links": links}],
                                    unfiled=[], projects=[])
    urls = [e["url"] for e in _link_buttons(view)]
    assert urls == [f"https://example.org/{i}" for i in range(app_home.MAX_LINKS)]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL  {name}: {e!r}")
    print(f"{'OVERALL: [FAIL]' if failed else 'OVERALL: [PASS]'} ({failed} failed)")
    sys.exit(1 if failed else 0)
