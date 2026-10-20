# Example config: University of Colorado Boulder

The templates in `config/` name no institution. These files carry the
values a CU Boulder install uses, as a worked example for the keys that
depend on the institution. Copy the parts you need into `~/.ea/config/`
and replace the personal placeholders.

- `user-profile.json`: the `institution` block (CU mail domains, the
  sister campuses the admissions filter treats as internal, the
  safety-rail domain, Zoom, the words that name the campus and its
  college).
- `list-digests.json`: three configured lists (a public robotics list and
  two CU funding newsletters).
- `vip_senders.txt`: example VIP addresses on CU domains.

The CU adapters live in an institution plugin, not in core: the
`cu-boulder` plugin (a private repo, installed with `scripts/ea-plugin.py`,
docs/plugins.md) files the CU CS travel reimbursement form, fills the CU
budget-justification template, and ships the CU letterhead style with the
`letterhead` block for this profile. Its README has the install steps.
Without it, the core still collects receipts and builds the trip packet
(docs/ea/reimbursement.md), and letters compile on whatever letterhead
`institution.letterhead` names. Another institution writes its own plugin
for those.
