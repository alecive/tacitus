#!/usr/bin/env python3
"""preprocessor/mail_links.py: the redirect links in an email body and where
its quoted history starts."""
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import mail_links as ML  # noqa: E402

# The Puppet follow-up as the Gmail connector's create_draft saved it.
PUPPET_HTML = (
    '<a href="https://www.google.com/url?q=https://arxiv.org/abs/2102.01295&amp;source=gmail'
    '&amp;ust=1790885498969000&amp;sa=E" target="_blank">https://arxiv.org/abs/2102.01295'
    '&amp;source=gmail&amp;ust=1790885498969000&amp;sa=E</a>')


def test_the_connector_redirect_is_found():
    assert ML.redirect_links(PUPPET_HTML) == [
        "https://www.google.com/url?q=https://arxiv.org/abs/2102.01295&amp;source=gmail"
        "&amp;ust=1790885498969000&amp;sa=E"]


def test_calendar_safelinks_and_country_domains_are_redirects():
    text = ("Lot: https://www.google.com/url?q=https://maps.app.goo.gl/aC&sa=D&source=calendar. "
            "Doc (https://google.co.uk/url?q=https://x.org/a). "
            "Portal https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.edu&data=05")
    assert ML.redirect_links(text) == [
        "https://www.google.com/url?q=https://maps.app.goo.gl/aC&sa=D&source=calendar",
        "https://google.co.uk/url?q=https://x.org/a",
        "https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.edu&data=05"]


def test_plain_links_are_not_redirects():
    text = ("https://arxiv.org/abs/2507.15833 https://www.google.com/search?q=pi0 "
            "https://docs.google.com/document/d/1x/edit https://seedfund.nsf.gov/ "
            "https://www.google.com/maps/place/Boulder")
    assert ML.redirect_links(text) == []
    assert ML.redirect_links("") == [] and ML.redirect_links(None) == []


def test_quote_start_finds_a_trailing_quote():
    new = "Hi Tam,\n\nPointers below.\n\nKit\n\n"
    quote = ("On Tue, Sep 29, 2026 at 9:18 PM Tam Prescott <bo.ellery45@example.com> wrote:\n"
             "> Agenda\n>\n> > nested\n")
    assert ML.quote_start(new + quote) == len(new)
    assert ML.new_text(new + quote) == new
    wrapped = "Hi.\n\nOn Tue, Sep 29, 2026 at 9:18 PM Tam Prescott <\nmika.yamamura31@example.org> wrote:\n> a\n"
    assert ML.new_text(wrapped) == "Hi.\n\n"


def test_quote_start_ignores_what_is_not_a_trailing_quote():
    for text in (
        "On Monday we met. He wrote: fine.\nThanks.",           # prose, one line
        "On Tue Tam wrote:\n> a\nMy answer after the quote.",  # text after the quote
        "On Tue Tam wrote:\n",                                 # nothing quoted
        "Just a note.",
        "",
    ):
        assert ML.quote_start(text) == len(text), text


def test_the_warning_names_the_count_and_the_hosts():
    one = ML.redirect_warning(["https://www.google.com/url?q=https://a.org"])
    assert one.startswith(":warning: *Redirect links:* 1 link in this draft goes through "
                          "www.google.com, not to the real address.")
    assert "replace it before" in one
    two = ML.redirect_warning(["https://www.google.com/url?q=a",
                               "https://nam02.safelinks.protection.outlook.com/?url=b"])
    assert "2 links in this draft go through nam02.safelinks.protection.outlook.com, " \
           "www.google.com," in two and "replace them" in two
    assert ML.redirect_warning([]) == ""
    assert "—" not in one + two


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok {t.__name__}")
            passed += 1
        except Exception:
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
