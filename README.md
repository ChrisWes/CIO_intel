# CIO/CDIO Appointment Tracker

Weekly scan of UK/Ireland enterprise tech trade press for newly appointed CIOs, CDIOs,
CTOs, CDOs and equivalent senior technology leaders, so Chris can reach out during the
high-value first-100-days window. Lead generation for advisory conversations, not
marketing automation.

Pattern reused from `../insure` (SQLite baseline for dedup, GitHub Actions on a schedule,
Claude for extraction, commit results back to the repo).

## How it works

1. **Ingest** — pulls the latest items from named trade press RSS feeds, plus an optional
   NewsAPI.org broad sweep across senior-tech-leadership title phrases.
2. **Pre-filter** — cheap keyword screen (a senior-tech-title keyword AND an appointment
   verb) before spending an LLM call. Deliberately wide; false positives are expected and
   filtered at the next step.
3. **Extract & classify** — Claude (`claude-haiku-4-5`) reads each candidate article and
   decides: is this actually a new appointment, is it UK/Ireland relevant, and what tier.
4. **Dedupe** — on person name + normalised employer name, against a rolling SQLite
   baseline (`data/appointments_baseline.db`). The richest article becomes the primary
   source; other outlets covering the same appointment are noted as corroboration.
5. **Digest** — `output/digest_<date>.md` (and `output/digest_latest.md`), grouped Tier 1
   / Tier 2, newest first within each tier. Only *newly detected* appointments appear in
   a given week's digest — the baseline DB and digest are committed back to the repo by
   the GitHub Action, no email step in v1.

## Setup

1. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` (required). `NEWS_API_KEY`
   is optional — leave blank to run RSS-only.
2. `pip install -r requirements.txt`
3. `python appointment_monitor.py`
4. For the scheduled GitHub Action (`.github/workflows/appointment_monitor.yml`, runs
   Monday 06:00 UTC), add `ANTHROPIC_API_KEY` and `NEWS_API_KEY` as repo secrets.

## Open decisions flagged back

These were called out in the brief as needing a judgment call — here's what v1 does and
why, so they can be revisited if the assumption turns out wrong:

**News API / rate limits.** NewsAPI.org's free "Developer" plan is licensed for
dev/testing only, not production use — running this weekly for real lead-gen is outside
its terms. It's wired up and works (verified against live feeds), but treat it as a
placeholder: either upgrade to a paid NewsAPI plan, or swap in a commercially-licensed
provider (e.g. Bing News/Azure Cognitive Search News, NewsData.io's paid tier, GNews).
Because this only needs to run weekly, request volume is trivial either way — the
18 title phrases are batched into 5 grouped queries, well inside any plan's rate limits.
RSS-only (no NewsAPI key) is a fully functional fallback and has no such restriction.

**RSS feeds — full text or fetch-and-parse?** Checked live: UKTN, Digit.fyi, The Stack,
diginomica, and ComputerWeekly all include the full article body in the RSS
`content:encoded` field — no separate fetch-and-parse step needed for these. Two
practical wrinkles found and handled:
- `computerweekly.com/rss` is an HTML index page, not a feed — the actual feed is
  `/rss/All-Computer-Weekly-content.xml` (used here).
- Tech Monitor sits behind Akamai bot protection and may 403 intermittently even with
  browser-like headers; this is a soft failure (logged, skipped, doesn't stop the run).
- Computing.co.uk has no discoverable working RSS feed as of 2026-07 (all standard paths
  404) — omitted from v1. Worth another look if this matters, or ask them directly for
  a feed URL.
- CIO.com's feed is global/US-weighted (no separate UK edition — `cio.co.uk` just
  redirects to `cio.com`), so its articles go through the same UK/Ireland relevance
  check as NewsAPI results rather than being trusted by default like the UK-only sources.

**Tier classification for ambiguous titles.** Titles like "Digital Director" can mean
marketing/e-commerce (out of scope) or genuine technology leadership (in scope). The
extraction prompt tells Claude to decide from the article's description of actual
responsibilities: technology/IT/data/engineering ownership → include as Tier 2 and set
`ambiguous_title_flag`, which surfaces a note in the digest to sanity-check before
reaching out; purely marketing/brand/content responsibilities with no technology
ownership → excluded entirely. This errs toward inclusion (per the brief's "wider net is
deliberate") while still flagging the judgment call rather than hiding it.

## Known limitations / not in scope for v1

Matches the brief's deferred list: no HubSpot CRM matching, no LinkedIn sourcing, no
company size/sector enrichment, no automatic removal of actioned entries. The baseline DB
exists specifically to make dedup-against-history and future CRM matching possible later.
