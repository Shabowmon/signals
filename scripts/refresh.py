#!/usr/bin/env python3
"""Daily Signals refresh.

Fetches the three podcast RSS feeds, diffs against episodes.json, classifies
new episodes with the OPPORTUNITY/RISK/THEME/NONE framework via the
Google Gemini API (free tier), appends them (newest first, rolling 3-month
window), and rewrites episodes.json. Called by .github/workflows/daily-refresh.yml.

Only network calls: the three RSS feeds + generativelanguage.googleapis.com.
Secrets: GEMINI_API_KEY env var (free key from Google AI Studio — no billing).
Model: GEMINI_MODEL env var, defaults to gemini-3.5-flash-lite (free tier,
fast; the parser retries/tolerates its occasional malformed JSON). The
flagship gemini-3.8-flash formats better but answers slowly on the free
tier — a bad trade for a 150-episode backfill. Override via repo variable.
Note: the 2.5-series models are restricted to pre-existing users — a new
free key gets a 404 on them, so don't set GEMINI_MODEL to a 2.5 model.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

FEEDS = [
    # (source label, rss url) — all three verified live 2026-10-19.
    # Odd Lots moved from Megaphone to Omny (old Megaphone URL 404s).
    ("Planet Money", "https://feeds.npr.org/510289/podcast.xml"),
    ("Up First",     "https://feeds.npr.org/510318/podcast.xml"),
    ("Odd Lots",     "https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/8a94442e-5a74-4fa2-8b8d-ae27003a8d6b/982f5071-765c-403d-969d-ae27003a8d83/podcast.rss"),
]
DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "episodes.json")
WINDOW_DAYS = 92          # rolling ~3-month timeline, matches the v1 seed
MAX_PER_RUN = 200         # hard cap on classifications per run (runaway guard)
MAX_DESC_CHARS = 1500

VERDICT_SYSTEM = """You classify podcast episodes for a personal money-insight timeline.
Classify from the title, show notes, and publish date below.
Reply with strict JSON only: {"verdict": "OPPORTUNITY|RISK|THEME|NONE", "bullets": ["...", "...", "..."]}
Rules:
- 3 bullets max, plain English, each at most 25 words.
- The LAST bullet must name the concrete money angle (who gains/loses, how, roughly when).
- OPPORTUNITY: a way an ordinary person could make or keep more money
  (tailwind, policy edge, exploitable inefficiency, underpriced asset).
- RISK: a threat to wealth (inflation, debt, regulation, bubble, fraud, job displacement).
- THEME: a structural trend worth watching (demographics, tech adoption, policy direction)
  with no direct trade attached.
- NONE: not money-relevant. Use it freely — a timeline full of OPPORTUNITY
  is a timeline nobody trusts."""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "signals-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def feed_items(source, url):
    root = ET.fromstring(fetch(url))
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        pub = (it.findtext("pubDate") or "").strip()
        # description or itunes:summary
        desc = (it.findtext("description") or "").strip()
        if not desc:
            for child in it:
                if child.tag.endswith("}summary"):
                    desc = (child.text or "").strip()
                    break
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()[:MAX_DESC_CHARS]
        try:
            dt = parsedate_to_datetime(pub).date()
        except Exception:
            dt = date.today()
        items.append({"source": source, "title": title, "url": link or guid,
                      "guid": guid, "date": dt, "desc": desc})
    return items


def classify(item, api_key, model):
    body = {
        "systemInstruction": {"parts": [{"text": VERDICT_SYSTEM}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": (
                f"Source: {item['source']}\n"
                f"Published: {item['date'].isoformat()}\n"
                f"Title: {item['title']}\n"
                f"Show notes: {item['desc'] or '(none)'}"
            )}],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 500,
            "responseMimeType": "application/json",
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    out = None
    last_text = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={
                    "x-goog-api-key": api_key,
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Free tier can throw transient 429/503s; back off and retry.
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        text = "".join(
            p.get("text", "")
            for c in resp.get("candidates", [])
            for p in c.get("content", {}).get("parts", [])
        )
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        last_text = text
        try:
            # The model sometimes appends prose after the JSON object; decode
            # just the first complete object instead of demanding purity.
            out, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
            break
        except ValueError:
            # Occasionally the model emits malformed JSON (unescaped quote or
            # control char). A fresh generation usually comes back clean.
            if attempt < 2:
                time.sleep(5)
                continue
            raise ValueError(f"Unparseable model response: {last_text[:300]!r}")
    verdict = str(out.get("verdict", "NONE")).upper()
    if verdict not in ("OPPORTUNITY", "RISK", "THEME", "NONE"):
        verdict = "NONE"
    bullets = [str(b) for b in out.get("bullets", [])][:3]
    while len(bullets) < 3:
        bullets.append("")
    return verdict, bullets


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set")
    model = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"

    with open(DATA_PATH) as f:
        raw_lines = f.readlines()
    body = "".join(l for l in raw_lines if not l.lstrip().startswith("//"))
    existing = json.loads(body)
    seen = {e.get("url") or e.get("title") for e in existing}

    # Collect candidates first. Two guards against runaway runs:
    #  - the date pre-filter drops the feeds' deep archives (some carry
    #    1,000+ episodes) before anything is classified — the timeline
    #    only keeps WINDOW_DAYS anyway;
    #  - MAX_PER_RUN caps classifications no matter what the feeds do.
    seen_titles = {e.get("title") for e in existing}
    cutoff_date = date.today() - timedelta(days=WINDOW_DAYS)
    candidates = []
    for source, url in FEEDS:
        print(f"Fetching {source} ...")
        for it in feed_items(source, url):
            if it["date"] < cutoff_date:
                continue
            key = it["url"] or it["guid"]
            if key in seen or it["title"] in seen_titles:
                continue
            seen.add(key)
            seen_titles.add(it["title"])
            candidates.append(it)
    candidates.sort(key=lambda it: it["date"], reverse=True)
    if len(candidates) > MAX_PER_RUN:
        print(f"{len(candidates)} new in-window episodes — capping at the "
              f"{MAX_PER_RUN} newest this run; the rest follow tomorrow.")
        candidates = candidates[:MAX_PER_RUN]
    print(f"{len(candidates)} episodes to classify.")

    new_eps = []
    for it in candidates:
        print(f"  new: {it['title']}")
        try:
            verdict, bullets = classify(it, api_key, model)
        except Exception as e:
            # One bad episode must never kill the run. Skipped episodes
            # are not in episodes.json, so the next run retries them.
            print(f"  SKIP (classify failed: {e})")
            continue
        new_eps.append({
            "date": it["date"].isoformat(),
            "source": it["source"],
            "title": it["title"],
            "verdict": verdict,
            "bullets": bullets,
            "url": it["url"],
        })

    if not new_eps:
        print("No new episodes.")
        return

    merged = new_eps + existing
    newest = max(e["date"] for e in merged)
    cutoff = (date.fromisoformat(newest) - timedelta(days=WINDOW_DAYS)).isoformat()
    merged = [e for e in merged if e["date"] >= cutoff]
    merged.sort(key=lambda e: e["date"], reverse=True)

    header = (f"// Signals data — generated {date.today().isoformat()} — "
              f"{len(merged)} episodes (rolling 3 months)\n")
    with open(DATA_PATH, "w") as f:
        f.write(header)
        f.write(json.dumps(merged, indent=1, ensure_ascii=False))
        f.write("\n")
    print(f"Wrote {len(merged)} episodes ({len(new_eps)} new).")


if __name__ == "__main__":
    main()
