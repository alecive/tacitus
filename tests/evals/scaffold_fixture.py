#!/usr/bin/env python3
"""
Scaffold a new eval fixture from an Opik production trace.

Fetches the trace, extracts the briefing data that was the input,
creates a fixture file, and stubs the assertion, so a bad production
trace becomes a regression test.

Usage:
  # From an Opik trace ID (copies briefing data from the trace):
  python3 tests/evals/scaffold_fixture.py --trace-id <opik-trace-id> \
      --name "CC email wrongly suggested reply" \
      --assertion cc_reply_false_positive

  # From a raw briefing JSON (e.g., from ~/.ea/state/briefing-latest.json):
  python3 tests/evals/scaffold_fixture.py --briefing ~/.ea/state/briefing-latest.json \
      --name "Missed deadline in Friday briefing" \
      --assertion missed_deadline_surfaced

  # Dry run (prints fixture JSON, doesn't write files):
  python3 tests/evals/scaffold_fixture.py --briefing ... --name ... --dry-run

After scaffolding:
  1. Edit the fixture to trim unnecessary data (keep only relevant emails/events)
  2. Write the assertion body in judge.py (the stub always fails)
  3. Add the fixture to promptfooconfig.yaml if using promptfoo
  4. Update .githooks/eval-coverage-map.json
  5. Run: python3 tests/evals/run_evals.py --verbose
"""

import argparse
import json
import re
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = EVAL_DIR / "fixtures"
JUDGE_FILE = EVAL_DIR / "judge.py"
RUNNER_FILE = EVAL_DIR / "run_evals.py"


def next_fixture_id(category: str) -> str:
    """Find the next available fixture ID for a category."""
    existing = []
    for f in FIXTURE_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text()).get("_meta", {})
            fid = meta.get("id", "")
            if fid.startswith(category + "-"):
                num = int(fid.split("-")[-1])
                existing.append(num)
        except (json.JSONDecodeError, ValueError):
            continue
    return f"{category}-{max(existing, default=0) + 1:03d}"


def slugify(name: str) -> str:
    """Convert a fixture name to a filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60]


def fetch_trace_briefing(trace_id: str) -> dict:
    """Fetch briefing data from an Opik trace."""
    try:
        import opik
        client = opik.Opik()
        trace = client.get_trace(trace_id)
        trace_input = trace.input or {}

        # For briefing traces, the briefing data is in metadata or
        # we need to reconstruct from the trace input fields.
        # For slack traces, the message is in trace_input["message"].
        return trace_input
    except ImportError:
        print("ERROR: opik not installed. Use --briefing instead.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not fetch trace {trace_id}: {e}", file=sys.stderr)
        sys.exit(1)


def load_briefing_file(path: str) -> dict:
    """Load briefing data from a JSON file.

    If the file is an existing fixture (has _meta + briefing keys),
    extract just the briefing section. If it's a raw briefing JSON
    (e.g., briefing-latest.json), use it as-is.
    """
    try:
        data = json.loads(Path(path).read_text())
        # If this is an existing fixture, extract the briefing section
        if "_meta" in data and "briefing" in data:
            return data["briefing"]
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: Could not read briefing file: {e}", file=sys.stderr)
        sys.exit(1)


def build_fixture(fixture_id: str, name: str, category: str,
                  description: str, briefing: dict, bug_ref: str) -> dict:
    """Construct the fixture JSON structure."""
    return {
        "_meta": {
            "id": fixture_id,
            "name": name,
            "category": category,
            **({"bug_ref": bug_ref} if bug_ref else {}),
            "description": description,
        },
        "briefing": briefing,
        "ledger_entries": [],
    }


def stub_assertion(assertion_name: str) -> str:
    """Generate a stub assertion function."""
    return f'''
def {assertion_name}(output: str, context: dict) -> dict:
    """{assertion_name}: TODO — write assertion logic.

    Scaffolded by scaffold_fixture.py. This stub always fails
    until you implement the actual check.

    Prompt-only mode: output is JSON (starts with '{{'), check data integrity.
    Full mode: output is Claude's text, check content.
    """
    # TODO: implement assertion logic
    return {{"pass": False, "score": 0.0, "reason": "STUB — not implemented yet"}}
'''


def update_judge(assertion_name: str, dry_run: bool) -> None:
    """Add stub assertion to judge.py and register in ASSERTIONS dict."""
    content = JUDGE_FILE.read_text()

    # Check if assertion already exists
    if f"def {assertion_name}(" in content:
        print(f"  judge.py: '{assertion_name}' already exists, skipping")
        return

    stub = stub_assertion(assertion_name)

    # Insert before the ASSERTIONS dict
    assertions_pattern = re.compile(r'^ASSERTIONS\s*=\s*\{', re.MULTILINE)
    match = assertions_pattern.search(content)
    if not match:
        print("  WARNING: Could not find ASSERTIONS dict in judge.py", file=sys.stderr)
        print(f"  Add manually:\n{stub}")
        return

    # Insert function before ASSERTIONS dict
    insert_pos = match.start()
    new_content = content[:insert_pos] + stub + "\n" + content[insert_pos:]

    # Add to ASSERTIONS dict
    # Find the closing brace of ASSERTIONS
    new_content = re.sub(
        r'(ASSERTIONS\s*=\s*\{[^}]+)',
        rf'\1    "{assertion_name}": {assertion_name},\n',
        new_content,
    )

    if dry_run:
        print(f"  judge.py: would add '{assertion_name}' stub + ASSERTIONS entry")
    else:
        JUDGE_FILE.write_text(new_content)
        print(f"  judge.py: added '{assertion_name}' stub + ASSERTIONS entry")


def update_runner(fixture_id: str, assertion_name: str, dry_run: bool) -> None:
    """Add fixture to fixture_assertions map in run_evals.py."""
    content = RUNNER_FILE.read_text()

    if f'"{fixture_id}"' in content:
        print(f"  run_evals.py: '{fixture_id}' already mapped, skipping")
        return

    # Find the fixture_assertions dict and add entry before closing brace
    pattern = re.compile(r'(fixture_assertions\s*=\s*\{.*?)(^\s*\})', re.DOTALL | re.MULTILINE)
    match = pattern.search(content)
    if not match:
        print("  WARNING: Could not find fixture_assertions in run_evals.py", file=sys.stderr)
        return

    new_entry = f'        "{fixture_id}": ["{assertion_name}"],\n'
    new_content = content[:match.start(2)] + new_entry + content[match.start(2):]

    if dry_run:
        print(f"  run_evals.py: would add '{fixture_id}' -> ['{assertion_name}']")
    else:
        RUNNER_FILE.write_text(new_content)
        print(f"  run_evals.py: added '{fixture_id}' -> ['{assertion_name}']")


def main():
    parser = argparse.ArgumentParser(
        description="Scaffold a new eval fixture from a production trace or briefing")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace-id", help="Opik trace ID to extract briefing from")
    source.add_argument("--briefing", help="Path to briefing JSON file")

    parser.add_argument("--name", required=True,
                        help="Human-readable fixture name (e.g., 'CC email wrongly prompted reply')")
    parser.add_argument("--assertion", required=True,
                        help="Assertion function name (e.g., 'cc_reply_false_positive')")
    parser.add_argument("--category", choices=["regression", "hard-success"],
                        default="regression",
                        help="Fixture category (default: regression)")
    parser.add_argument("--bug-ref", default="",
                        help="Bug reference (e.g., commit hash, issue URL)")
    parser.add_argument("--description", default="",
                        help="Detailed description (auto-generated if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be created without writing files")

    args = parser.parse_args()

    # Get briefing data
    if args.trace_id:
        print(f"Fetching trace {args.trace_id} from Opik...")
        briefing = fetch_trace_briefing(args.trace_id)
    else:
        briefing = load_briefing_file(args.briefing)

    # Assign fixture ID
    fixture_id = next_fixture_id(args.category)
    filename = slugify(args.name) + ".json"
    fixture_path = FIXTURE_DIR / filename
    description = args.description or f"Scaffolded from production trace. TODO: add detailed description."

    # Build fixture
    fixture = build_fixture(fixture_id, args.name, args.category,
                            description, briefing, args.bug_ref)

    print(f"\nScaffolding fixture: {fixture_id}")
    print(f"  File: tests/evals/fixtures/{filename}")
    print(f"  Category: {args.category}")
    print(f"  Assertion: {args.assertion}")

    if args.dry_run:
        print(f"\n--- Fixture JSON (dry run) ---")
        print(json.dumps(fixture, indent=2)[:2000])
        print(f"\n--- Would update ---")
        update_judge(args.assertion, dry_run=True)
        update_runner(fixture_id, args.assertion, dry_run=True)
        print("\nDry run complete. Run without --dry-run to create files.")
        return

    # Write fixture
    fixture_path.write_text(json.dumps(fixture, indent=2) + "\n")
    print(f"  Created: {fixture_path}")

    # Update judge.py
    update_judge(args.assertion, dry_run=False)

    # Update run_evals.py
    update_runner(fixture_id, args.assertion, dry_run=False)

    print(f"""
Done. Next steps:
  1. Edit {fixture_path.name} — trim to only the relevant emails/events
  2. Write assertion body in judge.py ({args.assertion})
  3. Add to promptfooconfig.yaml (if using promptfoo)
  4. Update .githooks/eval-coverage-map.json
  5. Verify: python3 tests/evals/run_evals.py --verbose
""")


if __name__ == "__main__":
    main()
