"""id_handles: handles beside every long id a read prints, resolved back
in the next call's argv, per run, under the resident's state; nothing
without a run id."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import id_handles as ih  # noqa: E402

MID = "AAMkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQAAAAAAQQ_QQQQ-QQQQQQQQQ_QQ_QQQQQQQQQQQ_QQQQ_QQQQQQQQAAQQQQQQAAQQQQQQQQ_QQQQ_QQQQQQQQAAQQQQQQAAA="
CID = "AAQkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQAQAAQQQ" + "x" * 40
EID = "AAMkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQAAAAAAQ" + "y" * 100


def test_annotation_and_resolution_within_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setenv("EA_RUN_ID", "run-1/abc")
    monkeypatch.setattr(ih, "_is_resident", lambda: True)    # the shim runs as the resident
    rows = [{"id": MID, "conversation_id": CID, "subject": "Slides", "body_preview": "x"},
            {"id": MID, "conversation_id": CID, "subject": "Re: Slides"}]
    out = ih.annotate_output(json.dumps(rows, indent=2) + "\n")
    data = json.loads(out)
    assert data[0]["ref"] == "m1" and data[0]["conversation_ref"] == "c1"
    assert data[1]["ref"] == "m1" and data[1]["conversation_ref"] == "c1"     # the same id, the same handle
    assert out.endswith("\n")
    # An event row's id is an e-handle; a short id is left alone.
    ev = {"count": 1, "events": [{"id": EID, "subject": "1:1", "start": "2026-10-13T20:00:00", "end": "2026-10-13T20:30:00"},
                                 {"id": "short", "subject": "x", "start": "", "end": ""}]}
    data = json.loads(ih.annotate_output(json.dumps(ev)))
    assert data["events"][0]["ref"] == "e1" and "ref" not in data["events"][1]
    table = ih.load()
    assert table["m1"] == MID and table["c1"] == CID and table["e1"] == EID
    p = ih.table_path()
    assert p.parent.name == ih.DIR and p.name == "run-1_abc.json"
    # The next call's argv comes back with the ids.
    argv = ["get-conversation", "--conversation-id", "c1", "--message-id=m1", "--ignore-event", "e1", "--top", "5"]
    assert ih.resolve_argv(argv) == ["get-conversation", "--conversation-id", CID, f"--message-id={MID}",
                                     "--ignore-event", EID, "--top", "5"]
    # An unknown handle and a plain id pass through unchanged.
    assert ih.resolve_argv(["get-message", "--message-id", "m9"]) == ["get-message", "--message-id", "m9"]
    assert ih.resolve_argv(["get-message", "--message-id", MID])[-1] == MID
    # Non-JSON output is untouched.
    assert ih.annotate_output("More results available. Refine --query") == "More results available. Refine --query"
    assert ih.annotate_output("") == ""


def test_nothing_happens_without_a_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.delenv("EA_RUN_ID", raising=False)
    text = json.dumps([{"id": MID}])
    assert ih.annotate_output(text) == text
    assert ih.resolve_argv(["get-message", "--message-id", "m1"]) == ["get-message", "--message-id", "m1"]
    assert ih.table_path() is None and ih.load() == {}
    assert not (tmp_path / "state" / ih.DIR).exists()

