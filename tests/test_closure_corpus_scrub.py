"""scripts/build_closure_corpus.py --scrub: the map, the address rule and
the residue check that keeps the owner's data out of the committed corpus."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "build_closure_corpus", REPO / "scripts" / "build_closure_corpus.py")
bcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcc)

PROFILE = {
    "name": "Pat Example-Owner",
    "email": "pat.owner@univ.test",
    "gmail_accounts": ["patowner99@gmail.com"],
    "institution": {"name": "Test University",
                    "domains": ["univ.test", "mail.univ.test"],
                    "related_domains": ["sister.test"]},
}
DOMS = bcc.owner_domains(PROFILE)
MAP = {"Pat Example-Owner": "Kit Calloway", "Pat": "Kit",
       "patowner99@gmail.com": "wren.fairbanks84@example.com"}


def test_owner_domains_come_from_the_address_and_the_institution():
    assert DOMS == ["mail.univ.test", "sister.test", "univ.test"]


def test_the_address_rule():
    assert bcc.scrub_address("a.b@univ.test", DOMS) == "a.b@example.edu"
    assert bcc.scrub_address("a.b@sub.sister.test", DOMS) == "a.b@example.edu"
    assert bcc.scrub_address("a.b@elsewhere.org", DOMS) == "a.b@example.com"
    assert bcc.scrub_address("a.b@example.org", DOMS) == "a.b@example.org"
    assert bcc.scrub_address("a.b@mail.example.edu", DOMS) == "a.b@mail.example.edu"


def test_scrub_text_applies_the_map_longest_first_then_addresses():
    text = ("Pat Example-Owner <pat.owner@univ.test> wrote to Pat's dean "
            "and cc'd patowner99@gmail.com and x@other.com")
    out = bcc.scrub_text(text, MAP, DOMS)
    assert out == ("Kit Calloway <pat.owner@example.edu> wrote to Kit's dean "
                   "and cc'd wren.fairbanks84@example.com and x@example.com")


def test_scrub_walks_the_document_and_its_keys():
    doc = {"profile": {"name": "Pat Example-Owner", "email": "pat.owner@univ.test"},
           "world": {"gmail_sent": {"patowner99@gmail.com": [
               {"from": "Pat <pat.owner@univ.test>", "n": 3, "ok": True}]}},
           "list": ["x@other.com", None]}
    out = bcc.scrub(doc, MAP, DOMS)
    assert out == {"profile": {"name": "Kit Calloway", "email": "pat.owner@example.edu"},
                   "world": {"gmail_sent": {"wren.fairbanks84@example.com": [
                       {"from": "Kit <pat.owner@example.edu>", "n": 3, "ok": True}]}},
                   "list": ["x@example.com", None]}


def test_residue_names_the_owner_after_a_partial_scrub():
    markers = bcc.owner_markers(PROFILE)
    labels = {label for label, _ in markers}
    assert {"Pat Example-Owner", "Pat", "Example-Owner", "pat.owner", "patowner99",
            "Test University", "univ.test"} <= labels
    doc = {"cases": [{"entry": {"from_email": "pat.owner@univ.test"}}],
           "ok": "Kit Calloway at example.edu", "k": {"univ.test": 1}}
    hits = bcc.residue(doc, markers)
    assert hits == ["$.cases[0].entry.from_email: 'Pat'", "$.k.univ.test (key): 'univ.test'"]
    # the generic address rule leaves a local part that still names the owner
    partial = bcc.scrub({"a": "pat.owner@univ.test", "b": "patowner99@gmail.com"}, {}, DOMS)
    assert partial == {"a": "pat.owner@example.edu", "b": "patowner99@example.com"}
    assert [h.split(":")[0] for h in bcc.residue(partial, markers)] == ["$.a", "$.b"]
    # a bare domain is not an address: the map, not the address rule, covers it
    full = bcc.scrub(doc, {"pat.owner@univ.test": "x@example.edu", "univ.test": "example.edu"}, DOMS)
    assert full["k"] == {"example.edu": 1} and bcc.residue(full, markers) == []


def test_name_tokens_match_whole_words_only():
    markers = dict(bcc.owner_markers({"name": "Ann Lee", "email": "al@univ.test"}))
    assert markers["Lee"].search("Lee wrote") and markers["Lee"].search("to lee.")
    assert not markers["Lee"].search("Fleet street")


def test_load_scrub_map_skips_comments_and_rejects_bad_shapes(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"_": "a comment", "Real": "Stand-in"}))
    assert bcc.load_scrub_map(p) == {"Real": "Stand-in"}
    p.write_text(json.dumps({"Real": 1}))
    with pytest.raises(ValueError):
        bcc.load_scrub_map(p)
    p.write_text("[]")
    with pytest.raises(ValueError):
        bcc.load_scrub_map(p)


def test_the_example_map_has_the_shape_and_names_nobody():
    m = bcc.load_scrub_map(REPO / "tests" / "fixtures" / "closure_corpus_scrub.example.json")
    assert m and all(isinstance(v, str) for v in m.values())
    for k in m:
        assert re.search(r"Real|Colleague|\.example$", k), k


def test_the_committed_corpus_holds_only_example_addresses():
    import sys
    sys.path.insert(0, str(REPO / "preprocessor"))
    import closure_scrubber as cs
    text = (REPO / "tests" / "fixtures" / "closure_corpus.json").read_text()
    doms = {m.rpartition("@")[2].lower() for m in bcc._ADDRESS_RE.findall(text)}
    stray = sorted(d for d in doms if not bcc._domain_of(d, bcc.EXAMPLE_DOMAINS))
    # Only the confirmation systems signal 4 matches by address.
    vendors = {s.rpartition("@")[2] for s in cs.CONFIRMATION_SENDER_SUBSTRINGS}
    assert stray and set(stray) <= vendors, stray
