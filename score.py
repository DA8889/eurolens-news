"""Score headlines for European relevance and significance with prompt P1.

    python score.py [--days 3] [--budget 0.50] [--limit N]

Reads RSS (archive/) and GDELT (gdelt/) records from the last --days archive days,
skips ids already scored, and appends one line per item to
scores/YYYY/MM/<item's archive day>.jsonl:
    {"id", "relevance", "significance", "model", "prompt", "scored_at"}
An item the model fails to score is left unscored and retried next run, never given 0.
Needs OPENROUTER_API_KEY; without it this exits 0 with a message, so collection never breaks.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from collect import ROOT, iso

MODEL = "anthropic/claude-haiku-5.5"
PROMPT_VERSION = "gate_v1"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
CHUNK = 30
WORKERS = 8
RUN_BUDGET = 0.50    # USD per run
DAILY_BUDGET = 2.00  # USD per UTC day, summed from scores/costs.jsonl
SOURCES = ("archive", "gdelt")   # RSS and GDELT records, same shape
SCORES = ROOT / "scores"
COSTS = SCORES / "costs.jsonl"


class InsufficientCredits(Exception):
    pass


def load_prompt(version=PROMPT_VERSION):
    return (ROOT / "prompts" / f"{version}.txt").read_text(encoding="utf-8")


def line_for(n, r):
    """One numbered input line: N | source country | language | outlet | headline.

    >>> line_for(3, {"source_country_raw": "AT", "lang": "de", "source_name": "ORF", "title": "Budget\\nbeschlossen"})
    '3 | AT | de | ORF | Budget beschlossen'
    >>> line_for(1, {"source_country_raw": None, "lang": None, "source_name": None, "title": "X"})
    '1 | — | ? | ? | X'
    """
    title = " ".join(r["title"].split())
    return f"{n} | {r['source_country_raw'] or '—'} | {r['lang'] or '?'} | {r['source_name'] or '?'} | {title}"


def parse_json(content):
    """Model output -> parsed JSON; tolerates ```json fences, rejects empty output.
    Same approach as _parse_json_content in eurolens/scripts/extract_relations_facts.py.

    >>> parse_json('```json\\n{"1": [7, 3]}\\n```')
    {'1': [7, 3]}
    >>> parse_json("")
    Traceback (most recent call last):
    ValueError: empty response
    """
    if not content or not content.strip():
        raise ValueError("empty response")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def parse_scores(content, n):
    """{line number: (relevance, significance)} for valid lines only.

    Lines that are missing, malformed or out of 0-10 are left out so the caller can
    retry them; they are never filled with 0.

    >>> parse_scores('{"1": [7, 3], "2": [11, 2], "3": ["4", 5], "9": [1, 1]}', 3)
    {1: (7, 3), 3: (4, 5)}
    """
    data = parse_json(content)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    out = {}
    for i in range(1, n + 1):
        v = data.get(str(i))
        if not isinstance(v, list) or len(v) != 2:
            continue
        try:
            r, s = (int(x) for x in v)
        except (TypeError, ValueError):
            continue
        if 0 <= r <= 10 and 0 <= s <= 10:
            out[i] = (r, s)
    return out


def chat(model, system, user, max_tokens, reasoning=None, api_key=None):
    """One chat completion. Returns (content, finish_reason, usage). Retries transient errors."""
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            # Haiku 5.5 reasons by default (effort "medium"); measured live, that spent
            # the whole output budget thinking. Reasoning bills as output, so it is off
            # unless a caller asks for it.
            "reasoning": reasoning or {"enabled": False},
            "usage": {"include": True}}
    headers = {"Authorization": f"Bearer {api_key or os.environ['OPENROUTER_API_KEY']}"}
    last = None
    for attempt in range(4):
        try:
            resp = requests.post(API_URL, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** attempt * 3)
            continue
        if resp.status_code == 402:
            raise InsufficientCredits(resp.text[:300])
        if resp.status_code in (408, 429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            time.sleep(2 ** attempt * 3)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        j = resp.json()
        choice = (j.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content"), choice.get("finish_reason"), j.get("usage") or {}
    raise RuntimeError(f"gave up after retries: {last}")


class Meter:
    def __init__(self, budget):
        self.budget, self.spent, self.calls = budget, 0.0, 0
        self.tokens_in = self.tokens_out = self.tokens_reasoning = 0
        self.lock = threading.Lock()

    def add(self, usage):
        with self.lock:
            self.spent += float(usage.get("cost") or 0)
            self.calls += 1
            self.tokens_in += usage.get("prompt_tokens") or 0
            self.tokens_out += usage.get("completion_tokens") or 0
            self.tokens_reasoning += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0

    def exhausted(self):
        return self.spent >= self.budget


def score_chunk(records, system, meter, model=MODEL, retry=True, reasoning=None):
    """Score up to CHUNK records. Returns {id: (relevance, significance)} for those scored.
    `reasoning` is only for models that cannot turn it off (calibration's Sonnet reference)."""
    if meter.exhausted():
        return {}
    user = "\n".join(line_for(i + 1, r) for i, r in enumerate(records))
    # Reasoning tokens count against max_tokens, so give reasoning calls ample headroom.
    max_tokens = 40 * len(records) + 100 if reasoning is None else 16000
    content, finish, usage = chat(model, system, user, max_tokens, reasoning)
    meter.add(usage)
    try:
        got = parse_scores(content, len(records)) if finish != "length" else {}
    except (ValueError, json.JSONDecodeError):
        got = {}
    out = {records[i - 1]["id"]: rs for i, rs in got.items()}
    missing = [r for i, r in enumerate(records, 1) if i not in got]
    if missing and retry:
        out.update(score_chunk(missing, system, meter, model, retry=False, reasoning=reasoning))
    return out


def title_key(title):
    """Exact-duplicate key: casefolded, punctuation and spacing ignored.

    >>> title_key("EU, US agree deal!") == title_key("eu us  agree deal")
    True
    """
    return re.sub(r"\W+", " ", title.casefold()).strip()


def day_path(base, day):
    return ROOT / base / day[:4] / day[5:7] / f"{day}.jsonl"


def read_days(base, days):
    out = []
    for day in days:
        path = day_path(base, day)
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.extend(json.loads(line) for line in f if line.strip())
    return out


def spent_today(today):
    if not COSTS.exists():
        return 0.0
    with COSTS.open(encoding="utf-8") as f:
        return sum(json.loads(l)["cost"] for l in f if l.strip() and json.loads(l)["date"] == today)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--budget", type=float, default=RUN_BUDGET, help="USD cap for this run")
    ap.add_argument("--limit", type=int, help="score at most N items (testing)")
    a = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("score: OPENROUTER_API_KEY not set, skipping scoring")
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    days = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(a.days)]
    budget = min(a.budget, DAILY_BUDGET - spent_today(today))
    if budget <= 0:
        print(f"score: daily budget ${DAILY_BUDGET:.2f} reached, skipping")
        return

    scored = {s["id"] for s in read_days("scores", days + [(now + timedelta(days=1)).strftime("%Y-%m-%d")])}
    items = {r["id"]: r for base in SOURCES for r in read_days(base, days) if r["id"] not in scored}
    groups = defaultdict(list)            # exact-duplicate titles are scored once
    for r in items.values():
        groups[title_key(r["title"])].append(r)
    reps = [g[0] for g in groups.values()][: a.limit]
    chunks = [reps[i:i + CHUNK] for i in range(0, len(reps), CHUNK)]

    system, meter = load_prompt(), Meter(budget)
    results = {}
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for got in pool.map(lambda c: score_chunk(c, system, meter), chunks):
                results.update(got)
    except InsufficientCredits as e:
        print(f"score: OpenRouter credits exhausted, stopping ({e})", file=sys.stderr)

    lines_by_day = defaultdict(list)
    scored_at = iso(datetime.now(timezone.utc))
    n_items = 0
    for g in groups.values():
        rs = results.get(g[0]["id"])
        if not rs:
            continue
        for r in g:
            n_items += 1
            lines_by_day[r["fetched_at"][:10]].append(
                {"id": r["id"], "relevance": rs[0], "significance": rs[1],
                 "model": MODEL, "prompt": PROMPT_VERSION, "scored_at": scored_at})
    for day, lines in lines_by_day.items():
        path = day_path("scores", day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(l) + "\n" for l in lines)

    run = {"date": today, "run_at": scored_at, "cost": round(meter.spent, 6), "calls": meter.calls,
           "pending": len(items), "titles": len(reps), "scored": n_items,
           "unscored": len(items) - n_items, "budget_hit": meter.exhausted()}
    SCORES.mkdir(exist_ok=True)
    with COSTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run) + "\n")
    print("score:", json.dumps(run))


if __name__ == "__main__":
    main()
