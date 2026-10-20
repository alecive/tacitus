"""An id the mediator checked is the id the owner-side child uses.

The handle table (id_handles) is resident state, so a handle stands for
whatever ea-bot wrote there. The shim resolves handles before the request
leaves; a child the mediator starts must not resolve them again, a handle
resolves only to a plain id, and every Graph request refuses a path that
could reach another endpoint than the one its call names."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("EA_DIR", tempfile.mkdtemp(prefix="ea-test-idb-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preprocessor"))
import graph_client as gc  # noqa: E402
import id_handles as ih  # noqa: E402
import mail_mediation as mm  # noqa: E402

PLAIN = "AAMkAQQQQQQQQQQQQQAQQQQQQQQQQQQQQQQQQQQQQQQQQQQQAAAAAAQQ_QQQQ-QQQQ="


def _table(tmp_path, monkeypatch, entries):
    monkeypatch.setenv("EA_DIR", str(tmp_path))
    monkeypatch.setenv("EA_RUN_ID", "run-1")
    monkeypatch.delenv(ih.MEDIATED_VERB_ENV, raising=False)
    monkeypatch.setattr(ih, "_is_resident", lambda: True)
    p = ih.table_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(entries))


def test_a_mediated_child_never_resolves_a_handle(tmp_path, monkeypatch):
    _table(tmp_path, monkeypatch, {"m1": PLAIN})
    argv = ["update-draft", "--message-id", "m1", "--body", "x"]
    assert ih.resolve_argv(argv)[2] == PLAIN          # the resident's side resolves
    monkeypatch.setenv(ih.MEDIATED_VERB_ENV, "update-draft")
    assert ih.resolve_argv(argv) == argv              # the owner's child does not


def test_an_owner_process_with_a_run_id_never_resolves_a_handle(tmp_path, monkeypatch):
    """An owner process that runs a tool directly with EA_RUN_ID set (the
    remote briefing, the bot's delete-draft, a plugin child) takes no id
    from the resident's table, with or without the mediated-verb marker."""
    _table(tmp_path, monkeypatch, {"m1": PLAIN})
    monkeypatch.setattr(ih, "_is_resident", lambda: False)
    for argv in (["delete-draft", "--message-id", "m1"], ["create-reply", "--message-id=m1"],
                 ["get-event", "--event-id", "m1"]):
        assert ih.resolve_argv(argv) == argv


def test_only_the_resident_user_counts_as_the_resident(monkeypatch):
    import pwd
    me = os.geteuid()
    real = pwd.getpwnam
    monkeypatch.setattr(pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": me})()
                        if n == ih.RESIDENT_USER else real(n))
    assert ih._is_resident() is True
    monkeypatch.setattr(pwd, "getpwnam", lambda n: type("P", (), {"pw_uid": me + 1})())
    assert ih._is_resident() is False

    def missing(n):
        raise KeyError(n)
    monkeypatch.setattr(pwd, "getpwnam", missing)
    assert ih._is_resident() is False
    assert ih.RESIDENT_USER == mm.RESIDENT_USER


def test_a_handle_resolves_only_to_a_plain_id(tmp_path, monkeypatch):
    bad = [PLAIN + "/send", PLAIN + "?x=", "..", PLAIN + "%2Fsend", PLAIN + "#", "a b", ["x"], ""]
    _table(tmp_path, monkeypatch, {f"m{i}": v for i, v in enumerate(bad, 1)})
    for i in range(1, len(bad) + 1):
        assert ih.resolve_argv(["get-message", "--message-id", f"m{i}"])[-1] == f"m{i}"
        assert ih.resolve_argv([f"--draft-id=m{i}"]) == [f"--draft-id=m{i}"]


def test_the_handle_name_is_the_mediator_name():
    assert ih.MEDIATED_VERB_ENV == mm.MEDIATED_VERB_ENV
    assert ih._PLAIN_ID.pattern == mm._ID_RE.pattern


def test_the_mediator_checks_attachment_ids():
    ok = ["download-attachment", "--message-id", PLAIN, "--attachment-id", PLAIN + "x" * 600,
          "--output", "/tmp/a.pdf"]
    assert mm.sanitize("graph_actions", "download-attachment", ok) is None
    for bad in (PLAIN + "/..", PLAIN + "?a", "a%2f"):
        argv = ok[:4] + [bad] + ok[5:]
        assert "plain mail id" in mm.sanitize("graph_actions", "download-attachment", argv)
        argv = ok[:3] + [f"--attachment-id={bad}"] + ok[5:]
        assert "plain mail id" in mm.sanitize("graph_actions", "download-attachment", argv)


@pytest.mark.parametrize("endpoint", [
    f"/me/messages/{PLAIN}/send?x=/createReply",
    f"/me/messages/{PLAIN}#/move",
    f"/me/messages/{PLAIN}/../../events/x",
    f"/me/messages/./{PLAIN}",
    f"/me/messages/{PLAIN}%2F..%2Fsend",
    f"/me/messages/{PLAIN}\\send",
    f"/me/messages/{PLAIN} /send",
    f"/me/messages/{PLAIN}\n",
    "me/messages",
    "https://graph.microsoft.com.example.net/v1.0/me/messages",
    "https://example.net/v1.0/me/messages",
    "https://graph.microsoft.com/v1.0/me/messages/../events?$top=5",
])
def test_a_graph_path_that_could_reach_another_endpoint_is_refused(endpoint):
    with pytest.raises(ValueError):
        gc._check_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    f"/me/messages/{PLAIN}",
    f"/me/messages/{PLAIN}/attachments/{PLAIN}/$value",
    "/me/mailFolders/sentitems/messages",
    "/users/someone@example.org/calendar/getSchedule",
    "https://graph.microsoft.com/v1.0/me/messages?$skip=10&$top=10",
])
def test_plain_graph_paths_pass(endpoint):
    assert gc._check_endpoint(endpoint).endswith(endpoint.split("/v1.0")[-1])


def test_every_request_helper_checks_its_path(monkeypatch):
    calls = []
    for name in ("get", "post", "patch", "delete"):
        monkeypatch.setattr(gc.requests, name, lambda *a, **k: calls.append(a))
    monkeypatch.setattr(gc, "_headers", lambda: {})
    bad = f"/me/messages/{PLAIN}/send?x="
    for fn, args in ((gc.graph_get, (bad,)), (gc.graph_post, (bad, {})),
                     (gc.graph_patch, (bad, {})), (gc.graph_delete, (bad,)),
                     (gc.delete_draft, (PLAIN + "/send?",)),
                     (gc.download_attachment, (PLAIN, PLAIN + "/../x"))):
        with pytest.raises(ValueError):
            fn(*args)
    assert calls == []


def test_no_graph_call_puts_a_query_in_its_path():
    """graph_client refuses a path with `?` (an id must not be able to cut
    the path short), so every caller passes its OData query in params."""
    import re
    root = Path(__file__).resolve().parents[1]
    pat = re.compile(r"""graph_(?:get|get_all|post|patch|delete)\(\s*f?["'][^"']*\?|["']/(?:me|users)/[^"']*\?\$""")
    hits = []
    for f in [root / "ea-chat-bot.py", *(root / "preprocessor").rglob("*.py"), *(root / "scripts").rglob("*.py")]:
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.relative_to(root)}:{n}")
    assert hits == []
