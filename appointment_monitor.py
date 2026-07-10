"""
appointment_monitor.py

Weekly scan of UK/Ireland enterprise tech trade press for newly appointed
CIOs, CDIOs, CTOs, CDOs and equivalent senior technology leaders (Head of IT,
VP Technology, Technology Director, etc.), so Chris can reach out during the
high-value first-100-days window.

Sources:
  1. Named trade press RSS feeds (ComputerWeekly, UKTN, Digit.fyi, The Stack,
     CIO.com, Tech Monitor, Computing.co.uk, diginomica).
  2. NewsAPI.org broad sweep across senior tech leadership title phrases
     (optional — skipped if NEWS_API_KEY is unset).

Each candidate article is classified and extracted by Claude, deduped on
person name + new employer against a rolling SQLite baseline, and written
out as a weekly markdown digest grouped into Tier 1 (C-suite) / Tier 2
(senior technology roles with likely buying authority).
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# RSS sources. is_uk_focused sources skip the LLM's UK/Ireland relevance
# check (they're already UK-only outlets); CIO.com is global, so its
# articles are still checked for UK/Ireland relevance downstream.
RSS_SOURCES = {
    # NB: /rss is an HTML index of feeds, not a feed itself — this is the
    # broad "all content" feed underneath it.
    "ComputerWeekly":  {"url": "https://www.computerweekly.com/rss/All-Computer-Weekly-content.xml", "is_uk_focused": True},
    "UKTN":            {"url": "https://www.uktech.news/feed",              "is_uk_focused": True},
    "Digit.fyi":       {"url": "https://www.digit.fyi/feed/",               "is_uk_focused": True},
    "The Stack":       {"url": "https://www.thestack.technology/feed/",     "is_uk_focused": True},
    "CIO.com":         {"url": "https://www.cio.com/feed/",                 "is_uk_focused": False},
    # Tech Monitor sits behind Akamai bot protection and 403s intermittently
    # even with browser-like headers — kept in since it isn't a permanent
    # block, but expect occasional skipped runs (logged as a warning, not fatal).
    "Tech Monitor":    {"url": "https://www.techmonitor.ai/feed",           "is_uk_focused": True},
    "diginomica":      {"url": "https://diginomica.com/feed",               "is_uk_focused": True},
    # Computing.co.uk has no discoverable working RSS feed as of 2026-07 (404
    # on all standard paths) — omitted until a working feed URL turns up.
}

# A browser-like User-Agent avoids bot-detection 403s some trade press
# sites (ComputerWeekly, Computing.co.uk) return to bare `requests` calls.
RSS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
RSS_TIMEOUT_SECONDS = 20

# Quick pre-filter before spending an LLM call: an article must mention a
# senior-tech-title keyword AND an appointment-signal verb. This is deliberately
# wide — the LLM does the real judgment call on relevance and tier.
#
# Short acronyms need word-boundary matching: plain substring "in" checks would
# match "cto" inside "director"/"sector"/"factor", "cio" inside other words, etc.
TITLE_KEYWORDS_PHRASES = [
    "chief information officer", "chief digital officer", "chief technology officer",
    "chief data officer", "group cio", "group cto", "head of it",
    "vp technology", "vp of technology", "director of technology",
    "technology director", "digital director",
]
TITLE_KEYWORDS_ACRONYMS = ["cio", "cdio", "cto", "cdo"]
_TITLE_ACRONYM_RE = re.compile(
    r"\b(" + "|".join(TITLE_KEYWORDS_ACRONYMS) + r")\b", re.IGNORECASE
)
APPOINTMENT_VERBS = [
    "appoint", "appointed", "appointment", "joins", "joined", "joining",
    "named", "names", "hires", "hired", "hiring", "welcomes", "welcome",
    "promotes", "promoted", "promotion", "steps up", "takes on", "takes up",
    "new cio", "new cto", "new cdo", "new cdio",
]

# NewsAPI.org broad sweep — phrases grouped to keep each query short.
# Batched (rather than one big OR) to stay well under NewsAPI's query length
# limits and to keep each request's result set easy to reason about.
NEWS_API_BASE = "https://newsapi.org/v2/everything"
NEWS_API_PHRASE_GROUPS = [
    ['"appointed as Chief Information Officer"', '"appointed as Chief Digital Officer"',
     '"appointed as Chief Technology Officer"', '"appointed as Chief Data Officer"'],
    ['"joins as CIO"', '"joins as CDIO"', '"joins as CTO"', '"joins as CDO"'],
    ['"named Chief Information Officer"', '"named Chief Technology Officer"',
     '"new CIO"', '"new CTO"'],
    ['"Head of IT"', '"VP Technology"', '"VP of Technology"'],
    ['"Director of Technology"', '"Technology Director"', '"Group CIO"', '"Group CTO"'],
]
NEWS_API_LOOKBACK_DAYS = 8   # weekly cadence + 1 day overlap buffer
NEWS_API_PAGE_SIZE     = 100
NEWS_API_MAX_PAGES     = 2   # per phrase group — plenty for a weekly, narrow query
NEWS_API_SLEEP_SECONDS = 1.0

LLM_MODEL = "claude-haiku-4-5-20251001"

# Legal-suffix stripping for simple employer-name normalisation in dedup.
_STRIP_SUFFIXES = re.compile(
    r"\b(limited|ltd|llp|plc|inc|group|holdings|uk|ireland|the|co)\b\.?",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
VERSION    = (SCRIPT_DIR / "VERSION").read_text().strip()
DATA_DIR   = SCRIPT_DIR / "data"
OUTPUT_DIR = SCRIPT_DIR / "output"
LOGS_DIR   = SCRIPT_DIR / "logs"

DB_PATH = DATA_DIR / "appointments_baseline.db"
TODAY   = date.today().isoformat()
DIGEST_PATH  = OUTPUT_DIR / f"digest_{TODAY}.md"
LATEST_PATH  = OUTPUT_DIR / "digest_latest.md"
LOG_FILE     = LOGS_DIR / f"appointment_monitor_{TODAY}.log"


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("appointment_monitor")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles_seen (
            article_url  TEXT PRIMARY KEY,
            source       TEXT,
            fetched_at   TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key           TEXT UNIQUE NOT NULL,
            person_name         TEXT NOT NULL,
            new_title           TEXT NOT NULL,
            new_employer        TEXT NOT NULL,
            previous_role       TEXT,
            start_date          TEXT,
            tier                INTEGER NOT NULL,
            context_summary     TEXT,
            ambiguous_title     INTEGER NOT NULL DEFAULT 0,
            primary_source_url  TEXT NOT NULL,
            primary_source_name TEXT NOT NULL,
            other_sources       TEXT,
            first_seen          TEXT NOT NULL,
            last_updated        TEXT NOT NULL
        );
    """)
    conn.commit()


def article_already_seen(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM articles_seen WHERE article_url = ?", (url,)
    ).fetchone() is not None


def mark_article_seen(conn: sqlite3.Connection, url: str, source: str, ts: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO articles_seen (article_url, source, fetched_at) VALUES (?, ?, ?)",
        (url, source, ts),
    )


def normalise_employer(name: str) -> str:
    cleaned = _WS.sub(" ", _STRIP_SUFFIXES.sub(" ", name)).strip().lower()
    return cleaned or name.strip().lower()


def make_dedup_key(person_name: str, new_employer: str) -> str:
    return f"{person_name.strip().lower()}|{normalise_employer(new_employer)}"


def upsert_appointment(conn: sqlite3.Connection, record: Dict, source_name: str,
                        source_url: str, ts: str, logger: logging.Logger) -> bool:
    """Insert a new appointment, or attach a corroborating source to an existing
    one (keeping the most detailed article as primary). Returns True if this
    is a newly-seen appointment (should appear in this week's digest)."""
    key = make_dedup_key(record["person_name"], record["new_employer"])
    row = conn.execute(
        "SELECT id, context_summary, other_sources FROM appointments WHERE dedup_key = ?",
        (key,),
    ).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO appointments
               (dedup_key, person_name, new_title, new_employer, previous_role, start_date,
                tier, context_summary, ambiguous_title, primary_source_url, primary_source_name,
                other_sources, first_seen, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, record["person_name"], record["new_title"], record["new_employer"],
             record.get("previous_employer_or_role"), record.get("start_date"),
             record["tier"], record.get("context_summary", ""),
             1 if record.get("ambiguous_title_flag") else 0,
             source_url, source_name, json.dumps([]), ts, ts),
        )
        logger.info("  NEW: %s | %s at %s [Tier %d]",
                    record["person_name"], record["new_title"], record["new_employer"], record["tier"])
        return True

    # Already known — corroborating coverage. Keep the richer article as primary.
    existing_id, existing_summary, other_sources_json = row
    other_sources = json.loads(other_sources_json or "[]")
    if source_url not in other_sources:
        other_sources.append(source_url)
    if len(record.get("context_summary", "")) > len(existing_summary or ""):
        conn.execute(
            """UPDATE appointments SET context_summary = ?, primary_source_url = ?,
               primary_source_name = ?, other_sources = ?, last_updated = ? WHERE id = ?""",
            (record.get("context_summary", ""), source_url, source_name,
             json.dumps(other_sources), ts, existing_id),
        )
    else:
        conn.execute(
            "UPDATE appointments SET other_sources = ?, last_updated = ? WHERE id = ?",
            (json.dumps(other_sources), ts, existing_id),
        )
    logger.info("  CORROBORATING: %s at %s also covered by %s",
                record["person_name"], record["new_employer"], source_name)
    return False


# ---------------------------------------------------------------------------
# RSS INGESTION
# ---------------------------------------------------------------------------
def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)


def matches_prefilter(title: str, text: str) -> bool:
    haystack = f"{title} {text}".lower()
    has_title_kw = (
        any(kw in haystack for kw in TITLE_KEYWORDS_PHRASES)
        or _TITLE_ACRONYM_RE.search(haystack) is not None
    )
    has_verb = any(v in haystack for v in APPOINTMENT_VERBS)
    return has_title_kw and has_verb


def fetch_rss_candidates(logger: logging.Logger) -> List[Dict]:
    candidates = []
    for source_name, cfg in RSS_SOURCES.items():
        try:
            resp = requests.get(
                cfg["url"], headers={"User-Agent": RSS_USER_AGENT}, timeout=RSS_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("  RSS fetch failed for %s: %s", source_name, exc)
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            logger.warning("  RSS parse failed for %s: %s", source_name, parsed.bozo_exception)
            continue

        logger.info("  %s: %d entries", source_name, len(parsed.entries))
        for entry in parsed.entries:
            url = entry.get("link", "")
            if not url:
                continue
            title = entry.get("title", "")
            if "content" in entry and entry["content"]:
                body = entry["content"][0].get("value", "")
            else:
                body = entry.get("summary", "")
            text = strip_html(body)
            if not matches_prefilter(title, text):
                continue
            published = entry.get("published", "") or entry.get("updated", "")
            candidates.append({
                "url": url, "source": source_name, "title": title,
                "text": text, "published": published,
                "is_uk_focused": cfg["is_uk_focused"],
            })
    return candidates


# ---------------------------------------------------------------------------
# NEWSAPI INGESTION
# ---------------------------------------------------------------------------
def fetch_newsapi_candidates(api_key: str, logger: logging.Logger) -> List[Dict]:
    candidates = []
    from_date = (date.today() - timedelta(days=NEWS_API_LOOKBACK_DAYS)).isoformat()
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    seen_urls = set()

    for group in NEWS_API_PHRASE_GROUPS:
        query = " OR ".join(group)
        page = 1
        while page <= NEWS_API_MAX_PAGES:
            time.sleep(NEWS_API_SLEEP_SECONDS)
            try:
                resp = session.get(
                    NEWS_API_BASE,
                    params={
                        "apiKey": api_key, "q": query, "language": "en",
                        "from": from_date, "sortBy": "publishedAt",
                        "pageSize": NEWS_API_PAGE_SIZE, "page": page,
                    },
                    timeout=(5, 20),
                )
            except requests.RequestException as exc:
                logger.error("  NewsAPI request failed for group %r: %s", query, exc)
                break

            if resp.status_code == 426:
                logger.error("  NewsAPI: free-tier upgrade required (426) — skipping remaining groups")
                return candidates
            if resp.status_code == 429:
                logger.warning("  NewsAPI 429 — sleeping 60s")
                time.sleep(60)
                continue
            if resp.status_code != 200:
                logger.error("  NewsAPI HTTP %d for group %r", resp.status_code, query)
                break

            data = resp.json()
            if data.get("status") != "ok":
                logger.error("  NewsAPI error: %s", data.get("message", "unknown"))
                break

            articles = data.get("articles", [])
            for art in articles:
                url = art.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = art.get("title") or ""
                desc = art.get("description") or ""
                content = art.get("content") or ""
                text = " ".join(filter(None, [desc, content]))
                if not matches_prefilter(title, text):
                    continue
                candidates.append({
                    "url": url,
                    "source": (art.get("source") or {}).get("name", "NewsAPI"),
                    "title": title, "text": text,
                    "published": art.get("publishedAt", ""),
                    "is_uk_focused": False,
                })

            total_results = data.get("totalResults", 0)
            fetched_so_far = page * NEWS_API_PAGE_SIZE
            if not articles or fetched_so_far >= total_results:
                break
            page += 1

    logger.info("  NewsAPI: %d prefiltered candidate(s) across %d phrase group(s)",
                len(candidates), len(NEWS_API_PHRASE_GROUPS))
    return candidates


# ---------------------------------------------------------------------------
# LLM EXTRACTION
# ---------------------------------------------------------------------------
EXTRACTION_TOOL = {
    "name": "extract_appointment",
    "description": "Extract structured details about a senior technology leadership appointment from an article, or flag it as not relevant.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_appointment": {
                "type": "boolean",
                "description": "True only if this article reports a specific named person newly appointed to a senior technology leadership role.",
            },
            "uk_ireland_relevant": {
                "type": "boolean",
                "description": "True if the employer is a UK or Ireland business (or the role is UK/Ireland-based). False for purely non-UK/Ireland stories.",
            },
            "person_name": {"type": "string"},
            "new_title": {"type": "string", "description": "Verbatim as reported in the article."},
            "new_employer": {"type": "string"},
            "previous_employer_or_role": {"type": "string", "description": "Empty string if not mentioned."},
            "start_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) if stated, else empty string."},
            "tier": {
                "type": "integer", "enum": [1, 2],
                "description": "1 = clear C-suite technology leadership (CIO, CDIO, CTO, CDO, Group CIO/CTO). 2 = Head of IT, VP Technology, Technology Director, or similar role with likely budget/buying authority but no C-suite title.",
            },
            "context_summary": {
                "type": "string",
                "description": "One line, e.g. 'replaces retiring CIO', 'newly created role', 'part of wider leadership restructure'.",
            },
            "ambiguous_title_flag": {
                "type": "boolean",
                "description": "True if the title is ambiguous between a technology role and a non-technology role (e.g. 'Digital Director' at a marketing-led company) and you had to make a judgment call.",
            },
        },
        "required": ["is_appointment", "uk_ireland_relevant"],
    },
}

EXTRACTION_SYSTEM_PROMPT = """You screen UK/Ireland enterprise tech trade press for newly appointed senior \
technology leaders, for a peer advisor doing outreach in their first 100 days.

Tier 1 = clear C-suite technology titles: CIO, CDIO, CTO, CDO, Group CIO, Group CTO.
Tier 2 = Head of IT, VP Technology, Technology Director, and similar roles that carry \
real budget and buying authority even without a C-suite label — include these, don't \
screen them out just for lacking a C-suite title.

Titles like "Digital Director" or "Digital Transformation Director" are ambiguous: they \
can mean marketing/e-commerce (out of scope) or genuine technology/IT leadership (in scope). \
Decide from the article's description of actual responsibilities. If the role clearly owns \
technology infrastructure, IT systems, engineering, or data platforms, include it as Tier 2 \
and set ambiguous_title_flag=true so a human can sanity-check. If the responsibilities described \
are purely marketing, brand, content, or customer experience with no technology/IT ownership, \
set is_appointment=false.

Only set is_appointment=true for a genuinely NEW appointment of a NAMED individual — not a \
promotion rumour, a resignation-only story, an opinion piece merely mentioning a CIO, or a \
listicle/awards piece. If the article is about something else entirely (product launch, \
funding round, etc.) with no personnel appointment, set is_appointment=false.

Judge uk_ireland_relevant based on where the employer is headquartered or where the role is \
based, not the article's outlet."""


def call_extraction(client: anthropic.Anthropic, article: Dict, logger: logging.Logger) -> Optional[Dict]:
    user_content = (
        f"Source: {article['source']}\n"
        f"Published: {article['published']}\n"
        f"Title: {article['title']}\n\n"
        f"Article text:\n{article['text'][:6000]}"
    )
    try:
        msg = client.messages.create(
            model=LLM_MODEL,
            max_tokens=500,
            system=EXTRACTION_SYSTEM_PROMPT,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "extract_appointment"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:
        logger.warning("  LLM extraction failed for %s: %s", article["url"], exc)
        return None

    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    return None


# ---------------------------------------------------------------------------
# DIGEST OUTPUT
# ---------------------------------------------------------------------------
def format_entry(row: sqlite3.Row) -> str:
    lines = [f"### {row['person_name']} — {row['new_title']} at {row['new_employer']}"]
    detail = []
    if row["previous_role"]:
        detail.append(f"Previously: {row['previous_role']}")
    if row["start_date"]:
        detail.append(f"Start date: {row['start_date']}")
    if detail:
        lines.append(" · ".join(detail))
    if row["context_summary"]:
        lines.append(row["context_summary"])
    if row["ambiguous_title"]:
        lines.append("_Flagged: ambiguous title, sanity-check before reaching out._")
    lines.append(f"[{row['primary_source_name']}]({row['primary_source_url']})")
    other = json.loads(row["other_sources"] or "[]")
    if other:
        lines.append(f"Also covered by {len(other)} other outlet(s) — a stronger newsworthiness signal.")
    return "\n\n".join(lines)


def write_digest(conn: sqlite3.Connection, new_dedup_keys: List[str], logger: logging.Logger) -> None:
    if not new_dedup_keys:
        rows = []
    else:
        placeholders = ",".join("?" * len(new_dedup_keys))
        rows = conn.execute(
            f"SELECT * FROM appointments WHERE dedup_key IN ({placeholders})",
            new_dedup_keys,
        ).fetchall()

    tier1 = sorted([r for r in rows if r["tier"] == 1],
                   key=lambda r: r["start_date"] or "", reverse=True)
    tier2 = sorted([r for r in rows if r["tier"] == 2],
                   key=lambda r: r["start_date"] or "", reverse=True)

    lines = [f"# CIO/CDIO Appointment Digest — {TODAY}", ""]
    if not rows:
        lines.append("No new appointments detected this week.")
    else:
        lines.append(f"{len(rows)} new appointment(s): {len(tier1)} Tier 1, {len(tier2)} Tier 2.")
        lines.append("")
        lines.append("## Tier 1 — C-suite technology leadership")
        lines.append("")
        if tier1:
            for row in tier1:
                lines.append(format_entry(row))
                lines.append("")
        else:
            lines.append("None this week.")
            lines.append("")

        lines.append("## Tier 2 — Senior technology roles with likely buying authority")
        lines.append("")
        if tier2:
            for row in tier2:
                lines.append(format_entry(row))
                lines.append("")
        else:
            lines.append("None this week.")
            lines.append("")

    OUTPUT_DIR.mkdir(exist_ok=True)
    text = "\n".join(lines)
    DIGEST_PATH.write_text(text, encoding="utf-8")
    LATEST_PATH.write_text(text, encoding="utf-8")
    logger.info("Digest written -> %s", DIGEST_PATH.name)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("CIO/CDIO Appointment Monitor v%s", VERSION)
    logger.info("=" * 60)

    load_dotenv()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    news_api_key  = os.getenv("NEWS_API_KEY", "").strip()

    if not anthropic_key:
        logger.error("ANTHROPIC_API_KEY not set. Exiting.")
        sys.exit(1)

    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    logger.info("Fetching RSS sources...")
    candidates = fetch_rss_candidates(logger)

    if news_api_key:
        logger.info("Running NewsAPI broad sweep...")
        candidates.extend(fetch_newsapi_candidates(news_api_key, logger))
    else:
        logger.info("NEWS_API_KEY not set — skipping NewsAPI sweep (RSS-only run)")

    ts = datetime.utcnow().isoformat()
    unseen = []
    for c in candidates:
        if article_already_seen(conn, c["url"]):
            continue
        unseen.append(c)
        mark_article_seen(conn, c["url"], c["source"], ts)
    conn.commit()

    logger.info("%d candidate article(s), %d not previously seen", len(candidates), len(unseen))

    client = anthropic.Anthropic(api_key=anthropic_key)
    new_dedup_keys = []
    extracted = 0
    skipped_not_appointment = 0
    skipped_not_uk = 0

    for c in unseen:
        result = call_extraction(client, c, logger)
        if result is None:
            continue
        extracted += 1
        if not result.get("is_appointment"):
            skipped_not_appointment += 1
            continue
        if not result.get("uk_ireland_relevant"):
            skipped_not_uk += 1
            continue
        required = ("person_name", "new_title", "new_employer", "tier")
        if not all(result.get(f) for f in required):
            logger.warning("  Incomplete extraction for %s — skipping", c["url"])
            continue

        is_new = upsert_appointment(conn, result, c["source"], c["url"], ts, logger)
        conn.commit()
        if is_new:
            new_dedup_keys.append(make_dedup_key(result["person_name"], result["new_employer"]))

    write_digest(conn, new_dedup_keys, logger)

    logger.info("")
    logger.info("=" * 60)
    logger.info("Summary")
    logger.info("=" * 60)
    logger.info("Candidates prefiltered   : %d", len(candidates))
    logger.info("Newly fetched articles   : %d", len(unseen))
    logger.info("LLM extractions run      : %d", extracted)
    logger.info("Not an appointment       : %d", skipped_not_appointment)
    logger.info("Not UK/Ireland relevant  : %d", skipped_not_uk)
    logger.info("New appointments in digest: %d", len(new_dedup_keys))
    logger.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
