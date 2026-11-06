"""Short AI briefings for storylines: what the story is and its latest developments, written
only from the storyline's own headlines and the outlets' RSS excerpts.

    python summarize.py --review [--at 2026-11-05T15:06]    local: write exports/summaries_review_<time>.xlsx

stories.py calls update() each run for the storylines going into site/stories.json. A
storyline of 2+ events with MIN_HEADLINES headlines from MIN_OUTLETS outlets gets a summary
when it has none, or when it has news since its summary and that is REFRESH_HOURS old.

AI only phrases (Haiku), and every sentence is checked:
  - the writer (prompts/story_summary_v1.txt) gets up to RECENT_LINES + OLDER_LINES numbered
    source lines and cites line numbers for each sentence;
  - check() drops a sentence with no valid citation, or with a number in none of its lines;
  - a second call (prompts/summary_check_v1.txt) drops sentences their lines do not support;
  - a summary is kept if at least 2 sentences and two thirds of them survive, else stored empty
    (and not written again until it is due again).
Costs go to scores/costs.jsonl ("summaries"), under score.py's daily budget.

Outputs:
    stories/summaries/YYYY-MM.jsonl   every summary written, with its source lines and dropped
                                      sentences (committed; the newest line per storyline counts)
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from collect import ARCHIVE, ROOT, SITE, iso, read_archive
from score import (COSTS, DAILY_BUDGET, MODEL, WORKERS, InsufficientCredits, Meter, chat, gloss,
                   parse_json, spent_today, strip_em_dashes, title_key)

PROMPT = "story_summary_v1"
CHECK_PROMPT = "summary_check_v1"
MIN_HEADLINES = 6
MIN_OUTLETS = 3
REFRESH_HOURS = 6
RECENT_LINES = 40      # the newest headlines, at most PER_EVENT from one event
PER_EVENT = 4
OLDER_LINES = 15       # then the lead headline of older events, best first
RUN_BUDGET = 0.03      # USD per run; about $0.001 a summary
DAYS = 31              # archive days read: storylines span 30
SUMMARIES = ROOT / "stories" / "summaries"
NUMBER = re.compile(r"\d+(?:[.,  ]\d+)*")


def when(r):
    return r.get("published_at") or r["fetched_at"]


def excerpt(r):
    """The outlet's own excerpt, or None. A Google News "snippet" only lists other outlets' headlines.

    >>> excerpt({"url": "https://rte.ie/a", "title": "Deal agreed",
    ...          "snippet": "Ministers agreed a deal on Tuesday after talks ran through the night."})
    'Ministers agreed a deal on Tuesday after talks ran through the night.'
    >>> excerpt({"url": "https://news.google.com/rss/articles/x", "title": "Deal agreed",
    ...          "snippet": "Deal agreed RTE  Talks go on through the night as ministers meet BBC"}) is None
    True
    """
    s = " ".join((r.get("snippet") or "").split())
    if "news.google." in r["url"] or len(s) < 40 or title_key(s) == title_key(r["title"]):
        return None
    return s


def source_line(r):
    """One source line (numbered when sent): date published | outlet (country) | headline | excerpt.

    >>> source_line({"published_at": "2026-11-04T09:00:00Z", "fetched_at": "", "source_name": "ORF",
    ...              "source_country_raw": "AT", "title": "Budget\\nbeschlossen", "url": "https://orf.at/1"})
    '4 Nov 2026 | ORF (AT) | Budget beschlossen'
    """
    d = datetime.fromisoformat(when(r).replace("Z", "+00:00"))
    cc = r.get("source_country_raw")
    parts = [f"{d.day} {d:%b %Y}", r["source_name"] + (f" ({cc})" if cc and cc != "—" else ""),
             " ".join(r["title"].split())]
    x = excerpt(r)
    return " | ".join(parts + [x] if x else parts)


def numbered(lines):
    return "\n".join(f"[{n}] {line}" for n, line in enumerate(lines, 1))


def numbers(text):
    """Digit-numbers in a text, separators dropped, so "1,200" and "1.200" are both "1200".

    >>> sorted(numbers("On 4 November, 1,200 people; 3.5% (G7)"))
    ['1200', '35', '4', '7']
    """
    return {re.sub(r"\D", "", m) for m in NUMBER.findall(text)}


def eligible(story, records):
    rs = [records[m] for e in story["events"] for m in e["member_ids"] if m in records]
    return story["n_events"] >= 2 and len(rs) >= MIN_HEADLINES and len({r["source_name"] for r in rs}) >= MIN_OUTLETS


def due(story, prev, now):
    """Write a summary now? When there is none, or there is news since it and it is REFRESH_HOURS old.

    >>> now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    >>> due({"last_seen": "2026-11-05T11:00:00Z"}, None, now)
    True
    >>> [due({"last_seen": seen}, {"at": at}, now) for seen, at in [("2026-11-05T11:00:00Z", "2026-11-05T07:00:00Z"),
    ...     ("2026-11-05T11:00:00Z", "2026-11-05T05:00:00Z"), ("2026-11-05T04:00:00Z", "2026-11-05T05:00:00Z")]]
    [False, True, False]
    """
    if prev is None:
        return True
    return story["last_seen"] > prev["at"] and prev["at"] <= iso(now - timedelta(hours=REFRESH_HOURS))


def pick_lines(story, records):
    """Source records for a storyline, newest first: the newest RECENT_LINES headlines (at most
    PER_EVENT from one event, those with an excerpt first, an exact duplicate title once), then
    the lead headline of up to OLDER_LINES older events, best first."""
    seen, recent, used = set(), [], set()
    for e in story["events"]:                     # newest first
        if len(recent) >= RECENT_LINES:
            break
        used.add(e["id"])
        mine = sorted((records[m] for m in e["member_ids"] if m in records),
                      key=lambda r: (excerpt(r) is not None, when(r)), reverse=True)
        n = 0
        for r in mine:
            k = title_key(r["title"])
            if k not in seen and n < PER_EVENT and len(recent) < RECENT_LINES:
                seen.add(k)
                recent.append(r)
                n += 1
    older = []
    for e in sorted((e for e in story["events"] if e["id"] not in used), key=lambda e: -e["rank"]):
        r = records.get(e.get("title_id"))
        if r and title_key(r["title"]) not in seen and len(older) < OLDER_LINES:
            seen.add(title_key(r["title"]))
            older.append(r)
    return sorted(recent + older, key=when, reverse=True)


def check(data, lines):
    """Writer output -> (paragraphs of {"text", "sources"}, dropped sentences with "why").
    Sources are line numbers into `lines`, from 1; invalid ones are left out.

    >>> lines = ["4 Nov 2026 | ORF (AT) | 12 killed in Kyiv strike", "4 Nov 2026 | BBC (GB) | Kyiv mourns"]
    >>> kept, dropped = check({"paragraphs": [[
    ...     {"text": "A strike on Kyiv killed 12 people on 4 November.", "sources": [1]},
    ...     {"text": "Kyiv held a day of mourning — officials said.", "sources": [2, 9]},
    ...     {"text": "It was the 3rd strike this week.", "sources": [1, 2]},
    ...     {"text": "Talks resumed.", "sources": []}]]}, lines)
    >>> [[(s["text"], s["sources"]) for s in p] for p in kept]
    [[('A strike on Kyiv killed 12 people on 4 November.', [1]), ('Kyiv held a day of mourning, officials said.', [2])]]
    >>> [d["why"] for d in dropped]
    ['number not in its sources', 'no valid source']
    """
    paragraphs = data.get("paragraphs") if isinstance(data, dict) else None
    kept, dropped = [], []
    for p in (paragraphs if isinstance(paragraphs, list) else [])[:3]:
        out = []
        for s in p if isinstance(p, list) else []:
            if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
                continue
            text = strip_em_dashes(" ".join(s["text"].split()))
            src = sorted({n for n in s.get("sources") or [] if isinstance(n, int) and 1 <= n <= len(lines)})
            if not src:
                dropped.append({"text": text, "sources": [], "why": "no valid source"})
            elif not numbers(text) <= set().union(*(numbers(lines[n - 1]) for n in src)):
                dropped.append({"text": text, "sources": src, "why": "number not in its sources"})
            else:
                out.append({"text": text, "sources": src})
        if out:
            kept.append(out)
    return kept, dropped


def verdicts(data, n):
    """Checker answer -> {sentence number: reason} for the sentences not marked supported (a
    missing verdict counts as unsupported), or None if the answer is not a JSON object.

    >>> verdicts({"1": {"reason": "in line 3", "supported": True}, "2": {"reason": "no date", "supported": False}}, 3)
    {2: 'no date', 3: 'no verdict'}
    """
    if not isinstance(data, dict):
        return None
    out = {}
    for k in range(1, n + 1):
        v = data.get(str(k))
        if not isinstance(v, dict) or v.get("supported") is not True:
            out[k] = (v.get("reason") if isinstance(v, dict) else None) or "no verdict"
    return out


def unsupported(kept, lines, meter):
    """{(paragraph, sentence) index: reason} for the sentences the checker does not find
    supported by their cited lines, or None if its answer is unusable."""
    flat = [(i, j, s) for i, p in enumerate(kept) for j, s in enumerate(p)]
    user = "\n\n".join(f"{n}. {s['text']}\n" + "\n".join(f"  [{k}] {lines[k - 1]}" for k in s["sources"])
                       for n, (_, _, s) in enumerate(flat, 1))
    system = (ROOT / "prompts" / f"{CHECK_PROMPT}.txt").read_text(encoding="utf-8")
    bad = ask(system, user, 120 * len(flat) + 100, meter, lambda data: verdicts(data, len(flat)))
    if bad is None:
        return None
    return {(flat[n - 1][0], flat[n - 1][1]): why for n, why in bad.items()}


def ask(system, user, max_tokens, meter, read):
    """read(parsed JSON answer), asking a second time if the first answer is unusable (Haiku
    now and then answers in prose, or runs out of tokens); None if both are."""
    for _ in range(2):
        content, finish, usage = chat(MODEL, system, user, max_tokens)
        meter.add(usage)
        try:
            got = read(parse_json(content)) if finish != "length" else None
        except (ValueError, json.JSONDecodeError):
            got = None
        if got is not None:
            return got
    return None


def summarize(story, records, meter, now):
    """Write and check one storyline's summary; returns the record stored for it."""
    rs = pick_lines(story, records)
    lines = [source_line(r) for r in rs]
    system = (ROOT / "prompts" / f"{PROMPT}.txt").read_text(encoding="utf-8")
    data = ask(system, numbered(lines), 1000, meter,
               lambda d: d if isinstance(d, dict) and isinstance(d.get("paragraphs"), list) else None)
    kept, dropped = check(data, lines)
    total = sum(map(len, kept)) + len(dropped)
    if kept:
        bad = unsupported(kept, lines, meter)
        if bad is None:
            bad = {(i, j): "checker answer unusable" for i, p in enumerate(kept) for j in range(len(p))}
        else:
            bad = {ij: f"not supported: {why}" for ij, why in bad.items()}
        dropped += [{**kept[i][j], "why": why} for (i, j), why in sorted(bad.items())]
        kept = [p for p in ([s for j, s in enumerate(p) if (i, j) not in bad] for i, p in enumerate(kept)) if p]
    n = sum(map(len, kept))
    if n < 2 or 3 * n < 2 * total:
        dropped += [{**s, "why": "too few sentences survived"} for p in kept for s in p]
        kept = []

    def ids(nums):
        return [rs[k - 1]["id"] for k in nums]

    return {"story": story["id"], "at": iso(now), "model": MODEL, "prompt": PROMPT, "check": CHECK_PROMPT,
            "n_events": story["n_events"], "last_seen": story["last_seen"], "lines": [r["id"] for r in rs],
            "paragraphs": [[{"text": s["text"], "sources": ids(s["sources"])} for s in p] for p in kept],
            "dropped": [{"text": d["text"], "sources": ids(d["sources"]), "why": d["why"]} for d in dropped]}


def load_latest(now):
    """{storyline id: its newest stored summary}, from last month's file and this month's."""
    out = {}
    for d in (now.replace(day=1) - timedelta(days=1), now):      # newer lines win
        path = SUMMARIES / f"{d:%Y-%m}.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                out.update((s["story"], s) for s in map(json.loads, filter(str.strip, f)))
    return out


def public(summary, records):
    """A stored summary as site/stories.json shows it: its sentences, and the cited headlines,
    one per outlet in order of first citation. None if it kept no sentences."""
    if not summary or not summary["paragraphs"]:
        return None
    sources, outlets = [], set()
    for i in (i for p in summary["paragraphs"] for s in p for i in s["sources"]):
        r = records.get(i)
        if r and r["source_name"] not in outlets:
            outlets.add(r["source_name"])
            sources.append({"title": r["title"], "url": r["url"], "source_name": r["source_name"],
                            "country": r["source_country_raw"]})
    return {"at": summary["at"], "paragraphs": [[s["text"] for s in p] for p in summary["paragraphs"]],
            "sources": sources}


def update(now, stories):
    """Write the summaries that are due (Haiku), store them, and return {storyline id: its
    summary for site/stories.json, or None}. `stories` are stories.py's storylines, best first."""
    latest = load_latest(now)
    records = {r["id"]: r for r in read_archive(now, DAYS, ARCHIVE)}
    todo = [s for s in stories if due(s, latest.get(s["id"]), now) and eligible(s, records)]
    budget = min(RUN_BUDGET, DAILY_BUDGET - spent_today(now.strftime("%Y-%m-%d")))
    meter = Meter(max(budget, 0) if os.environ.get("OPENROUTER_API_KEY") else 0)   # no key: stored ones only

    def one(s):
        if meter.exhausted():
            return None
        try:
            return summarize(s, records, meter, now)
        except (InsufficientCredits, RuntimeError) as e:
            print(f"summaries: {s['id']} failed ({e})", file=sys.stderr)
            return None

    with ThreadPoolExecutor(WORKERS) as pool:
        written = [w for w in pool.map(one, todo if not meter.exhausted() else []) if w]
    if written:
        SUMMARIES.mkdir(parents=True, exist_ok=True)
        with (SUMMARIES / f"{now:%Y-%m}.jsonl").open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(w, ensure_ascii=False) + "\n" for w in written)
        latest.update((w["story"], w) for w in written)
    if meter.calls:
        with COSTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": now.strftime("%Y-%m-%d"), "run_at": iso(now), "job": "summaries",
                                "cost": round(meter.spent, 6), "calls": meter.calls}) + "\n")
    print(f"summaries: {len(written)} written ({sum(1 for w in written if w['paragraphs'])} kept) of {len(todo)} due, "
          f"${meter.spent:.4f} for {meter.calls} calls")
    return {s["id"]: public(latest.get(s["id"]), records) for s in stories}


def review(now, budget):
    """exports/summaries_review_<time>.xlsx: the newest summary of each storyline in
    site/stories.json, and every sentence with the lines it cites, for checking by hand."""
    import openpyxl
    from openpyxl.styles import Alignment, Font
    from stories import read_english

    site = json.loads((SITE / "stories.json").read_text(encoding="utf-8"))
    names = {s["id"]: s["name"] for s in site["storylines"]}
    latest = load_latest(now)
    rows = [latest[i] for i in names if i in latest]          # in stories.json order (best first)
    records = {r["id"]: r for r in read_archive(now, DAYS + 7, ARCHIVE)}

    def sentences(w):
        return ([("kept", s) for p in w["paragraphs"] for s in p]
                + [(f"dropped: {d['why']}", d) for d in w["dropped"]])

    # English for the cited headlines and excerpts (review only; event titles are already done)
    meter, english = Meter(budget), read_english(now)
    need = {}
    for w in rows:
        for _, s in sentences(w):
            for i in s["sources"]:
                r = records.get(i)
                if r and r["lang"] != "en":
                    if i not in english:
                        need[i] = r["title"]
                    if excerpt(r):
                        need[i + "#x"] = excerpt(r)
    english.update(gloss([{"id": k, "title": v} for k, v in need.items()], meter))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summaries"
    ws.append(["storyline", "summary", "sentences kept", "sentences dropped", "dropped (why)", "lines given",
               "outlets cited", "your_verdict", "your_notes", "written at", "storyline id"])
    for w in rows:
        cited = {records[i]["source_name"] for p in w["paragraphs"] for s in p for i in s["sources"] if i in records}
        ws.append([names[w["story"]], "\n\n".join(" ".join(s["text"] for s in p) for p in w["paragraphs"]),
                   sum(map(len, w["paragraphs"])), len(w["dropped"]),
                   "\n".join(f"{d['text']} ({d['why']})" for d in w["dropped"]), len(w["lines"]), len(cited),
                   "", "", w["at"], w["story"]])
    ws2 = wb.create_sheet("Sentences")
    ws2.append(["storyline", "sentence", "status", "text", "cited headline (English)", "cited headline (original)",
                "excerpt (English)", "excerpt (original)", "outlet", "country", "published", "url", "your_verdict",
                "storyline id", "headline id"])
    for w in rows:
        for n, (status, s) in enumerate(sentences(w), 1):
            for i in s["sources"] or [None]:
                r = records.get(i) or {}
                en = r.get("lang") == "en"
                ws2.append([names[w["story"]], n, status, s["text"],
                            r.get("title") if en else english.get(i, ""), r.get("title", ""),
                            (excerpt(r) or "") if en else english.get(f"{i}#x", ""), (excerpt(r) or "") if r else "",
                            r.get("source_name", ""), r.get("source_country_raw", ""), when(r) if r else "",
                            r.get("url", ""), "", w["story"], i or ""])
    for sheet, widths in ((ws, [28, 90, 9, 9, 50, 8, 8, 12, 30, 20, 22]),
                          (ws2, [24, 6, 22, 60, 50, 40, 60, 40, 18, 7, 20, 30, 12, 22, 18])):
        sheet.freeze_panes = "A2"
        for c in sheet[1]:
            c.font = Font(bold=True)
        for col, wd in zip("ABCDEFGHIJKLMNO", widths):
            sheet.column_dimensions[col].width = wd
        for row in sheet.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    ws3 = wb.create_sheet("About")
    for line in [f"Storyline summaries as of {iso(now)}: {len(rows)} storylines, "
                 f"{sum(1 for w in rows if w['paragraphs'])} with a kept summary",
                 f"Writer {MODEL} with prompts/{PROMPT}.txt; checker prompts/{CHECK_PROMPT}.txt",
                 "Summaries sheet: one row per storyline; mark a summary that misstates its sources (your_verdict = X)",
                 "Sentences sheet: one row per sentence and cited line, kept and dropped; English columns are "
                 "machine translations for this review only",
                 f"Translation cost for this workbook: ${meter.spent:.4f} for {meter.calls} calls (Haiku)"]:
        ws3.append([line])
    out = ROOT / "exports" / f"summaries_review_{now:%Y%m%dT%H%MZ}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    print(f"review: {len(rows)} storylines; translations ${meter.spent:.4f}; wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--review", action="store_true", help="write the review workbook (local; Haiku translations)")
    ap.add_argument("--at", help="local testing: as if it were this UTC time, e.g. 2026-11-05T15:06")
    ap.add_argument("--budget", type=float, default=0.05, help="USD cap for the review's translations")
    a = ap.parse_args()
    if not a.review:
        ap.error("stories.py writes the summaries; this script only has --review")
    if not os.environ.get("OPENROUTER_API_KEY"):
        ap.error("--review needs OPENROUTER_API_KEY")
    now = datetime.fromisoformat(a.at).replace(tzinfo=timezone.utc) if a.at else datetime.now(timezone.utc)
    review(now, a.budget)


if __name__ == "__main__":
    main()
