"""Add a strand to strands.yaml, from the "Follow this story" issue form or by hand.

    python follow_story.py --from-issue      (the follow-story workflow; reads ISSUE_BODY)
    python follow_story.py "Name" --description "What it covers" [--example "A headline"]...

The strand's id is made from its name. Adding a strand changes the strands version, so the
next tag.py run tags the last BACKFILL_DAYS days again with it.
"""
import argparse
import json
import os
import re
import sys

import yaml

from collect import ROOT

STRANDS = ROOT / "strands.yaml"


class Fail(Exception):
    pass


def slug(name):
    """
    >>> slug("Spain's housing crisis!")
    'spains-housing-crisis'
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "").replace("’", "")).strip("-")[:40]


def parse_issue(body):
    """Read the issue form (### Label / value blocks).

    >>> parse_issue("### Strand name\\n\\nUS strikes on Iran\\n\\n### What it covers\\n\\nThe strikes.\\n\\n"
    ...             "### Example headlines (optional)\\n\\nOne\\nTwo\\n")
    {'name': 'US strikes on Iran', 'description': 'The strikes.', 'examples': ['One', 'Two']}
    """
    fields = {}
    for block in re.split(r"^### ", body.replace("\r\n", "\n"), flags=re.M)[1:]:
        label, _, value = block.partition("\n")
        fields[label.strip()] = value.strip()
    examples = fields.get("Example headlines (optional)", "")
    return {"name": fields.get("Strand name", ""), "description": fields.get("What it covers", ""),
            "examples": [] if examples == "_No response_" else [l.strip() for l in examples.split("\n") if l.strip()]}


def add(name, description, examples):
    name, description = " ".join(name.split()), " ".join(description.split())
    if not 2 <= len(name) <= 80:
        raise Fail("the name should be 2 to 80 characters")
    if len(description) < 20:
        raise Fail("please describe what the strand covers in a sentence or two")
    examples = [" ".join(e.split())[:200] for e in examples][:8]
    text = STRANDS.read_text(encoding="utf-8")
    strands = yaml.safe_load(text)["strands"]
    if any(s["name"].lower() == name.lower() for s in strands):
        raise Fail(f"a strand named {name!r} already exists")
    taken, sid, n = {s["id"] for s in strands}, slug(name) or "strand", 2
    base = sid
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    entry = {"id": sid, "name": name, "description": description, "examples": examples}
    # JSON scalars are valid YAML, so json.dumps gives safe quoting; comments above are kept.
    block = (f"\n  - id: {json.dumps(sid)}\n    name: {json.dumps(name, ensure_ascii=False)}\n"
             f"    description: {json.dumps(description, ensure_ascii=False)}\n"
             + ("    examples:\n" + "".join(f"      - {json.dumps(e, ensure_ascii=False)}\n" for e in examples)
                if examples else "    examples: []\n"))
    new_text = text.rstrip("\n") + "\n" + block
    if yaml.safe_load(new_text)["strands"][-1] != entry:
        raise Fail("internal check failed: the new entry did not read back the same")
    STRANDS.write_text(new_text, encoding="utf-8")
    return entry


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--description")
    ap.add_argument("--example", action="append", default=[])
    ap.add_argument("--from-issue", action="store_true",
                    help="read the issue form from the ISSUE_BODY env var (used by the workflow)")
    a = ap.parse_args()
    if a.from_issue:
        kwargs = parse_issue(os.environ.get("ISSUE_BODY", ""))
    elif a.name and a.description:
        kwargs = {"name": a.name, "description": a.description, "examples": a.example}
    else:
        ap.error("give a name and --description, or --from-issue")
    try:
        entry = add(**kwargs)
    except Fail as e:
        print(f"Not added: {e}")
        sys.exit(1)
    print(f"Now following `{entry['id']}`: {entry['name']}\n\n{entry['description']}")


if __name__ == "__main__":
    main()
