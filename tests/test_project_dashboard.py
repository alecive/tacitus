#!/usr/bin/env python3
"""Tests for the dashboard's data layer (board / unfiled / apply_op).

Usage:
    python3 tests/test_project_dashboard.py
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, so a run of this file alone must not see it.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-dashboard-")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "preprocessor"))

import project_dashboard as pd  # noqa: E402
import project_store as ps  # noqa: E402


def _use(d: Path):
    ps.DB_PATH = d / "state" / "obligations-ledger.db"


def _seed(d: Path):
    _use(d)
    conn = ps._get_db()
    now = "2026-08-14T00:00:00Z"
    # The REAL ledger schema, so the fixture can never drift from what
    # the dashboard queries.
    import ledger as ledger_mod
    conn.executescript(ledger_mod.SCHEMA)

    def _insert(**kw):
        base = {"type": "task", "status": "open", "created_at": "2026-08-13",
                "updated_at": "2026-08-13"}
        base.update(kw)
        cols = ", ".join(base)
        conn.execute(f"INSERT INTO ledger ({cols}) VALUES "
                     f"({', '.join('?' * len(base))})", list(base.values()))

    _insert(id="led-1", description="loose item", from_name="A",
            from_email="kit.bancroft59@example.edu", source_message_id="AAMkMSG1")
    _insert(id="led-2", description="done", from_name="B",
            from_email="harper.juniper70@example.edu", status="completed")
    for pid, dom, parent in (("umbrella", "service", None),
                             ("child-a", "service", "umbrella"),
                             ("solo", "research", None)):
        conn.execute("INSERT INTO projects (id,name,domain,status,parent_id,"
                     "created_at,updated_at) VALUES (?,?,?,'active',?,?,?)",
                     (pid, pid.title(), dom, parent, now, now))
    conn.commit()


def test_board_groups_by_domain(d):
    _seed(d)
    b = pd.build_board()
    assert [p["id"] for p in b["domains"]["research"]] == ["solo"]
    assert {p["id"] for p in b["domains"]["service"]} == {"umbrella", "child-a"}


def test_board_and_desk_carry_the_project_channel_badge(d):
    """A registered project channel shows on the board card and the desk
    entry as the same {id, name, url}."""
    _seed(d)
    import ea_channels
    ea_channels.REGISTRY = d / "config" / "ea-channels.json"
    (d / "config").mkdir(exist_ok=True)
    ea_channels.register("C0BADGE001", "solo", "ea-solo",
                         url="https://app.slack.com/client/T1/C0BADGE001", created_by="slack")
    ea_channels.register("G0MIRROR01", name="tmp-d488ed1f", kind="session",
                         session_id="d488ed1f-f075-4ddd-bbf2-eb382ad3eb87")
    b = pd.build_board()
    cards = {p["id"]: p for ps in b["domains"].values() for p in ps}
    assert cards["solo"]["channel"] == {"id": "C0BADGE001", "name": "ea-solo",
                                        "url": "https://app.slack.com/client/T1/C0BADGE001",
                                        "archived": False, "archived_by": ""}
    assert "channel" not in cards["umbrella"]
    conn = ps._get_db()
    conn.execute("UPDATE projects SET next_actions='ship it' WHERE id='solo'")
    conn.execute("INSERT INTO project_ledger (project_id, ledger_id) VALUES ('solo','led-1')")
    conn.commit()
    desk = {e["id"]: e for e in pd.build_desk()}
    assert desk["solo"]["channel"]["name"] == "ea-solo"
    # App Home renders the badge as a link on the card line
    import app_home
    view = app_home.build_home_view(desk=list(desk.values()), unfiled=[], projects=[], loose_count=0)
    blob = json.dumps(view)
    assert "<https://app.slack.com/client/T1/C0BADGE001|#ea-solo>" in blob


def test_unfiled_excludes_closed_and_linked(d):
    _seed(d)
    assert [i["id"] for i in pd.unfiled_items()] == ["led-1"]
    assert pd.apply_op({"op": "link_ledger", "project": "solo",
                        "ledger_id": "led-1"})["ok"]
    assert pd.unfiled_items() == []


def test_set_domain_validates(d):
    _seed(d)
    assert pd.apply_op({"op": "set_domain", "project": "solo",
                        "value": "teaching"})["ok"]
    assert not pd.apply_op({"op": "set_domain", "project": "solo",
                            "value": "hobby"})["ok"]
    assert not pd.apply_op({"op": "set_domain", "project": "nope",
                            "value": "teaching"})["ok"]


def test_reparent_refuses_cycles(d):
    _seed(d)
    assert not pd.apply_op({"op": "set_parent", "project": "umbrella",
                            "value": "child-a"})["ok"]
    assert not pd.apply_op({"op": "set_parent", "project": "solo",
                            "value": "solo"})["ok"]
    assert pd.apply_op({"op": "set_parent", "project": "solo",
                        "value": "umbrella"})["ok"]
    assert pd.apply_op({"op": "set_parent", "project": "solo",
                        "value": None})["ok"]


def test_mutations_journal(d):
    _seed(d)
    pd.apply_op({"op": "set_domain", "project": "solo", "value": "teaching"})
    pd.apply_op({"op": "journal_add", "project": "solo", "text": "hello",
                 "kind": "decision"})
    conn = ps._get_db()
    events = [r["text"] for r in conn.execute(
        "SELECT text FROM project_events WHERE project_id='solo' ORDER BY id")]
    assert any("domain -> teaching" in e for e in events)
    assert "hello" in events


def test_loose_items_leave_the_tray(d):
    _seed(d)
    assert [i["id"] for i in pd.unfiled_items()] == ["led-1"]
    assert pd.apply_op({"op": "mark_loose", "ledger_id": "led-1"})["ok"]
    assert pd.unfiled_items() == []
    assert [i["id"] for i in pd.loose_items()] == ["led-1"]
    assert pd.apply_op({"op": "unmark_loose", "ledger_id": "led-1"})["ok"]
    assert [i["id"] for i in pd.unfiled_items()] == ["led-1"]


def test_desk_ranks_deadlines_and_carries_links(d):
    _seed(d)
    conn = ps._get_db()
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, from_name, "
        "from_email, deadline, source_message_id, created_at, updated_at) "
        "VALUES ('led-hot','deadline','open','due thing','C','yael.marchetti63@example.edu',"
        "'2026-08-16','AAMkHOT','2026-08-15','2026-08-15')")
    conn.commit()
    pd.apply_op({"op": "link_ledger", "project": "solo", "ledger_id": "led-hot"})
    pd.apply_op({"op": "add_link", "project": "solo",
                 "url": "https://docs.google.com/d/abc",
                 "title": "Agenda doc"})
    pd.apply_op({"op": "set_next_actions", "project": "solo",
                 "value": "Can this be sent yet?"})
    desk = pd.build_desk()
    assert desk and desk[0]["id"] == "solo"
    assert desk[0]["question"] == "Can this be sent yet?"
    assert desk[0]["links"][0]["title"] == "Agenda doc"
    assert "deadline" in desk[0]["why"]
    assert not pd.apply_op({"op": "add_link", "project": "solo",
                            "url": "javascript:alert(1)"})["ok"]


def test_desk_scheduled_items_do_not_count_as_on_you(d):
    _seed(d)
    conn = ps._get_db()
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, follow_up_after, "
        "created_at, updated_at) VALUES ('led-future','task','follow_up',"
        "'phase later','2099-01-01','2026-08-15','2026-08-15')")
    conn.execute(
        "INSERT INTO ledger (id, type, status, description, created_at, "
        "updated_at) VALUES ('led-now','task','open','do now',"
        "'2026-08-15','2026-08-15')")
    conn.commit()
    pd.apply_op({"op": "link_ledger", "project": "solo",
                 "ledger_id": "led-future"})
    pd.apply_op({"op": "link_ledger", "project": "solo",
                 "ledger_id": "led-now"})
    e = [x for x in pd.build_desk() if x["id"] == "solo"][0]
    assert e["on_you"] == 1
    assert e["scheduled"] == 1


def test_auto_merge_groups_by_entity_and_excludes_from_tray(d):
    _seed(d)
    conn = ps._get_db()
    for lid, desc, created in (
            ("m-1", "We received your Explore request (CIS990468)", "2026-08-10"),
            ("m-2", "Approved! Your New Explore request for CIS990468", "2026-08-11"),
            ("m-3", "Unrelated thing", "2026-08-12")):
        conn.execute("INSERT INTO ledger (id, type, status, description, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (lid, "response_needed", "open", desc, created, created))
    conn.commit()
    created = ps.auto_merge_scan(conn)
    assert len(created) == 1 and created[0]["entity"] == "CIS990468"
    assert created[0]["primary_id"] == "m-1"  # oldest
    assert created[0]["merged_ids"] == ["m-2"]
    ids = [i["id"] for i in pd.unfiled_items()]
    assert "m-2" not in ids and "m-1" in ids and "m-3" in ids
    primary = [i for i in pd.unfiled_items() if i["id"] == "m-1"][0]
    assert primary["merge"] == {"entity": "CIS990468", "count": 1}
    # idempotent: second scan creates nothing new
    assert ps.auto_merge_scan(conn) == []
    # unmerge restores the child to the tray
    conn.execute("DELETE FROM item_merges WHERE merged_id='m-2'")
    conn.commit()
    assert "m-2" in [i["id"] for i in pd.unfiled_items()]


def test_merge_prefers_purpose_built_primary_and_honors_vetoes(d):
    _seed(d)
    conn = ps._get_db()
    for lid, desc, created in (
            ("response-x1=", "Needs response: Assignment - NOTE-2026-0780", "2026-08-20T06:00"),
            ("example-notes-note-0780", "Note NOTE-2026-0780: shortlist", "2026-08-20T15:00")):
        conn.execute("INSERT INTO ledger (id, type, status, description, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (lid, "task", "open", desc, created, created))
    conn.commit()
    created = ps.auto_merge_scan(conn)
    # Purpose-built plugin item wins primary even though the auto-capture
    # is OLDER.
    assert created[0]["primary_id"] == "example-notes-note-0780"
    assert created[0]["merged_ids"] == ["response-x1="]
    # Veto: unmerge then rescan — the pairing must never come back.
    conn.execute("INSERT INTO merge_vetoes (merged_id, entity, vetoed_at) "
                 "VALUES ('response-x1=', 'NOTE-2026-0780', 'now')")
    conn.execute("DELETE FROM item_merges")
    conn.commit()
    assert ps.auto_merge_scan(conn) == []


def test_entity_extraction_shapes(d):
    assert ps.extract_entities("re JRNL-2026-0075.R1 and CIS990468 today") == \
        {"JRNL-2026-0075.R1", "CIS990468"}
    assert ps.extract_entities("meeting on 2026-08-16 at 10") == set()
    assert ps.extract_entities("COVID19 update") == set()
    assert ps.extract_entities("AI B1 S12") == set()


def test_app_home_view_builder(d):
    import app_home
    desk = [{"id": "bs-ai", "name": "BS in AI", "why": "deadline 2026-08-18",
             "question": "Can the agenda go out yet?",
             "left_off": "drafting in the doc",
             "links": [{"url": "https://docs.google.com/x", "title": "Agenda"}]}]
    unfiled = [{"id": "led-1", "description": "Some item", "from_name": "A",
                "email_link": "https://outlook.office365.com/owa/?ItemID=X"}]
    projects = [{"id": "bs-ai", "name": "BS in AI", "status": "active"}]
    import json as _json
    v = app_home.build_home_view(desk, unfiled, projects, loose_count=2)
    assert v["type"] == "home"
    asks = [e for b in v["blocks"] if b["type"] == "actions"
            for e in b["elements"] if e.get("action_id") == "home_ask"]
    assert _json.loads(asks[0]["value"])["project"] == "bs-ai"
    file_rows = [b for b in v["blocks"] if b["type"] == "actions"
                 and any(e.get("action_id") == "home_file"
                         for e in b["elements"])]
    assert file_rows and file_rows[0]["block_id"] == "unfiled-act::led-1"
    dones = [e for b in file_rows for e in b["elements"]
             if e.get("action_id") == "home_item_done"]
    assert dones and dones[0]["value"] == "led-1"
    menus = [b["accessory"] for b in v["blocks"]
             if b.get("accessory", {}).get("action_id") == "home_card_menu"]
    assert menus and len(menus[0]["options"]) == 3
    modal = app_home.journal_modal("bs-ai")
    assert modal["callback_id"] == "home_journal_modal"
    assert modal["private_metadata"] == "bs-ai"
    # unfiled row hyperlinks to the source email when a link exists
    row = [b for b in v["blocks"] if b.get("block_id") == "unfiled::led-1"][0]
    assert "<https://outlook.office365.com/owa/?ItemID=X|" in row["text"]["text"]
    # resolver-style links: relative for the board, absolute for Slack
    assert pd.email_weblink("led-1") == "/open?ledger=led-1"
    assert pd.email_weblink("") == ""
    assert pd.absolute_email_link("/open?ledger=x").startswith("http")
    assert app_home.parse_file_action("unfiled::led-1", "bs-ai") == {
        "ledger_id": "led-1", "loose": False, "project": "bs-ai"}
    assert app_home.parse_file_action("unfiled::led-1", "__loose__")["loose"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"  ✅ {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"  ❌ {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
