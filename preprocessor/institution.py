"""
The user's institution: which mail domains are internal, which domain the
first-email safety rail names, the video platform, and the words that are
the institution's own boilerplate.

Reads the `institution` block of ~/.ea/config/user-profile.json. The block
lives in the profile because it describes who the user is, setup writes it
with the other profile answers, and every chat and scheduling prompt
already carries the whole profile. The admissions filter's on/off switch
stays in ea-config.json (`admissions_filter.enabled`) with the other
operational switches; this block only names the domains it uses.

Every field has a fallback derived from the user's own address, so an
install without the block treats its own domain as internal and carries
no other institution's values:

  "institution": {
    "name": "Example University",          # else the profile's "university"
    "domains": ["example.edu"],             # internal mail; else the user's own domain
    "related_domains": ["med.example.edu"], # sister campuses, internal for the admissions filter
    "safety_rail_domain": "example.edu",    # "a first email outside <domain>"; else domains[0]
    "video": {"platform": "Zoom", "link": "https://...",   # link else the profile's zoom_link
              "meeting_id": "123 456 7890"},               # else the profile's zoom_meeting_id
    "boilerplate_tokens": ["example", "campus"],           # never a person's name
    "boilerplate_phrases": ["www.example.edu"],            # newsletter footer text
    "letterhead": {"template_dir": "~/.ea/templates/letterhead",
                   "asset_dirs": ["~/.ea/plugins/<institution>/letterhead"],
                   "required_assets": ["letterhead.sty"]}
  }

`letterhead.asset_dirs` are more directories a required asset may come
from, after `template_dir`: an institution plugin ships its letterhead
style and logos there, and the user's own files (the template, the
example letter, the signature) stay in `template_dir`.

Reads fresh from disk on each call; EA_DIR is read at call time so tests
and lanes that set it see their own profile.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def profile() -> dict:
    """The parsed user-profile.json, or {} when it is missing or broken."""
    try:
        data = json.loads((_ea_dir() / "config" / "user-profile.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def block() -> dict:
    data = profile().get("institution")
    return data if isinstance(data, dict) else {}


def _clean_domain(value: str) -> str:
    return str(value or "").strip().lower().lstrip("@")


def own_domain() -> str:
    """The domain of the user's own address, '' when there is none."""
    email = str(profile().get("email") or "")
    return _clean_domain(email.rsplit("@", 1)[-1]) if "@" in email else ""


def _domain_list(key: str) -> tuple[str, ...]:
    raw = block().get(key)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    out = []
    for item in raw:
        d = _clean_domain(item)
        if d and d not in out:
            out.append(d)
    return tuple(out)


def domains() -> tuple[str, ...]:
    """The internal mail domains, lowercase, no '@'. Falls back to the
    domain of the user's own address."""
    listed = _domain_list("domains")
    if listed:
        return listed
    own = own_domain()
    return (own,) if own else ()


def related_domains() -> tuple[str, ...]:
    return tuple(d for d in _domain_list("related_domains") if d not in domains())


def all_domains() -> tuple[str, ...]:
    """Internal plus related domains."""
    return domains() + related_domains()


def _matches(addr: str, pool: tuple[str, ...]) -> bool:
    addr = str(addr or "").strip().lower()
    if "@" not in addr or not pool:
        return False
    host = addr.rsplit("@", 1)[-1].rstrip(">").strip()
    return any(host == d or host.endswith("." + d) for d in pool)


def is_internal(addr: str) -> bool:
    """True when the address is on an internal domain or a subdomain of one."""
    return _matches(addr, domains())


def is_internal_or_related(addr: str) -> bool:
    return _matches(addr, all_domains())


def own_suffix() -> str:
    """'@<domain>' of the user's own address, else of the first internal
    domain, else ''. The solver and the calendar mirror treat this one
    domain as internal."""
    d = own_domain() or (domains()[0] if domains() else "")
    return "@" + d if d else ""


def internal_suffixes() -> tuple[str, ...]:
    """The internal domains as '@domain' suffixes, for code that matches
    the end of an address."""
    return tuple("@" + d for d in domains())


def safety_rail_domain() -> str:
    """The domain the first-email safety rail names. '' when nothing is
    configured and the user's address has no domain."""
    named = _clean_domain(block().get("safety_rail_domain", ""))
    if named:
        return named
    pool = domains()
    return pool[0] if pool else ""


def safety_rail_phrase() -> str:
    """'a first email outside <domain>', or a domain-free wording."""
    d = safety_rail_domain()
    return f"a first email outside {d}" if d else "a first email outside the user's institution"


def name() -> str:
    """The institution's name: the block's name, else the profile's
    university, else ''."""
    return str(block().get("name") or profile().get("university") or "").strip()


def video() -> dict:
    """{"platform": ..., "link": ...}. The link falls back to the profile's
    zoom_link; the platform falls back to Zoom when a zoom_link is set."""
    raw = block().get("video")
    raw = raw if isinstance(raw, dict) else {}
    link = str(raw.get("link") or profile().get("zoom_link") or "").strip()
    platform = str(raw.get("platform") or ("Zoom" if profile().get("zoom_link") else "")).strip()
    return {"platform": platform, "link": link}


def video_platform() -> str:
    """The video platform's name ("Zoom", "Microsoft Teams"), '' when the
    profile names none."""
    return video()["platform"]


def video_link() -> str:
    """The user's own meeting link, '' when none is configured. The
    profile's legacy zoom_link is the fallback, through video()."""
    return video()["link"]


def video_meeting_id() -> str:
    """The meeting id written under the link, '' when there is none: the
    block's meeting_id, else the profile's legacy zoom_meeting_id."""
    raw = block().get("video")
    raw = raw if isinstance(raw, dict) else {}
    return str(raw.get("meeting_id") or profile().get("zoom_meeting_id") or "").strip()


def video_join_block() -> str:
    """The lines an online event's body carries: 'Join <Platform> meeting',
    the link, and the meeting id when there is one. '' without a link."""
    v = video()
    if not v["link"]:
        return ""
    lines = [f"Join {v['platform']} meeting" if v["platform"] else "Join the meeting online", v["link"]]
    mid = video_meeting_id()
    if mid:
        lines.append(f"Meeting ID: {mid}")
    return "\n".join(lines)


def _word_list(key: str) -> tuple[str, ...]:
    raw = block().get(key)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(str(w).strip().lower() for w in raw if str(w).strip())


def boilerplate_tokens() -> tuple[str, ...]:
    """Words that name the institution and so never count as a person's
    name: the configured tokens plus each label of the internal domains
    (minus the top-level label)."""
    out = list(_word_list("boilerplate_tokens"))
    for d in all_domains():
        for label in d.split(".")[:-1]:
            if label and label not in out:
                out.append(label)
    return tuple(out)


def boilerplate_phrases() -> tuple[str, ...]:
    """Newsletter footer phrases that mark the institution's boilerplate."""
    out = list(_word_list("boilerplate_phrases"))
    for d in domains():
        www = "www." + d
        if www not in out:
            out.append(www)
    return tuple(out)


def letterhead() -> dict:
    """{"template_dir": Path, "asset_dirs": [Path, ...], "required_assets":
    [...]} for compiled letters. Defaults to ~/.ea/templates/letterhead, no
    extra asset directory and no required asset."""
    raw = block().get("letterhead")
    raw = raw if isinstance(raw, dict) else {}
    template_dir = str(raw.get("template_dir") or "~/.ea/templates/letterhead")
    assets = raw.get("required_assets")
    assets = [str(a) for a in assets] if isinstance(assets, list) else []
    dirs = raw.get("asset_dirs")
    dirs = [dirs] if isinstance(dirs, str) else dirs
    dirs = ([Path(os.path.expanduser(str(d).strip())) for d in dirs if str(d).strip()]
            if isinstance(dirs, list) else [])
    return {"template_dir": Path(os.path.expanduser(template_dir)), "asset_dirs": dirs,
            "required_assets": assets}
