"""botcore works with nothing from ea-system on the path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botcore import chat  # noqa: E402
from botcore.chat.memory_frontend import MemoryFrontend  # noqa: E402


def _card():
    return chat.ConfirmationCard(
        kind="send", title="Send the draft?",
        actions=[chat.CardAction("approve_send", "Send", "v1", "primary", "send"),
                 chat.CardAction("cancel_send", "Cancel", "v1", "danger", "cancel")],
        block_id="b1", fallback_text="Send the draft?")


def test_no_application_modules_are_imported():
    leaked = {"ea_config", "owner_trust", "resident_io", "preprocessor"} & set(sys.modules)
    assert not leaked, leaked


def test_a_card_posts_on_the_memory_frontend():
    fe = MemoryFrontend.from_config({})
    assert fe.post_card(fe.main_channel, _card()).id


def test_default_file_writer_replaces_atomically(tmp_path):
    d = tmp_path / "uploads"
    assert chat.files.make_dir(d)
    target = d / "a.bin"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    target.symlink_to(victim)
    assert chat.files.replace_bytes(target, b"new")
    assert target.read_bytes() == b"new" and not target.is_symlink()
    assert victim.read_text() == "keep"


def test_an_application_can_install_its_writers(tmp_path):
    calls = []
    saved = (chat.files.make_dir, chat.files.replace_bytes)
    try:
        chat.files.use(lambda p, m=0: calls.append(("dir", p)) or True,
                       lambda p, b, m=0: calls.append(("file", p)) or True)
        assert chat.files.make_dir(tmp_path) and chat.files.replace_bytes(tmp_path / "x", b"")
    finally:
        chat.files.use(*saved)
    assert [k for k, _ in calls] == ["dir", "file"]

