#!/usr/bin/env python3
"""The Slack card goldens are well formed.

tests/fixtures/slack_cards/ holds Block Kit captured from the card builders
by capture.py in that directory. The step-7 renderer test compares against
them; this file checks only that each golden is a usable reference: blocks
are a list, every interactive element in an actions block names its
action_id, and every button value is JSON or a short opaque string.
"""

import json
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "slack_cards"
GOLDENS = sorted(GOLDEN_DIR.glob("*__*.json"))
SHORT_VALUE_MAX = 64
BUILDERS = {
    "build_send_confirmation", "post_schedule_send_confirmation", "post_bulk_card",
    "post_event_confirmation", "post_event_update_confirmation",
    "post_event_delete_confirmation", "room_picker", "replace_buttons_with_status",
    "post_dm_breadcrumb",
}


def test_every_builder_has_a_golden():
    assert GOLDENS, f"no goldens in {GOLDEN_DIR}"
    assert {p.name.split("__")[0] for p in GOLDENS} == BUILDERS


@pytest.mark.parametrize("path", GOLDENS, ids=lambda p: p.stem)
def test_golden_is_well_formed(path):
    g = json.loads(path.read_text())
    assert set(g) >= {"inputs", "text", "blocks", "kwargs"}, sorted(g)
    assert isinstance(g["blocks"], list)
    assert isinstance(g["kwargs"], dict)
    for block in g["blocks"]:
        assert isinstance(block, dict) and block.get("type"), block
        if block["type"] != "actions":
            continue
        assert block["elements"], block
        for el in block["elements"]:
            assert el.get("action_id"), el
            if el["type"] != "button":
                continue
            value = el["value"]
            assert isinstance(value, str)
            try:
                json.loads(value)
            except ValueError:
                assert len(value) <= SHORT_VALUE_MAX, value
