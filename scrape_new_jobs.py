"""
Upwork New Job Monitor
======================
Runs AFTER scraping.py.  Reloads the Upwork feed every 30-35 seconds
(random interval to appear human), compares against all previously seen
job URLs, and saves any brand-new jobs to new_jobs.json / new_jobs.csv.
Also fetches each new job's detail page for client rating + hire rate.

HOW TO USE
----------
1. Run scraping.py first (Chrome must already be open with --remote-debugging-port=9222).
2. In the same terminal (or a second one):
       python scrape_new_jobs.py

Press Ctrl+C at any time to stop monitoring.

SETTINGS (edit below)
---------------------
RELOAD_MIN     – minimum seconds between reloads   (default 30)
RELOAD_MAX     – maximum seconds between reloads   (default 35)
REQUEST_DELAY  – seconds between detail-page hits  (default 1.5)
DETAIL_TIMEOUT – ms to wait for hire-rate element  (default 10000)
KNOWN_JSON     – output file from scraping.py to seed known URLs
"""

import asyncio
import csv
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

import discord_webhook


# ════════════════════════════════════════════════════════════════
#  SETTINGS
# ════════════════════════════════════════════════════════════════

RELOAD_MIN     = 30
RELOAD_MAX     = 60
REQUEST_DELAY  = 1.5
DETAIL_TIMEOUT = 10_000

CDP_URL      = "http://localhost:9222"
JOBS_URL     = "https://www.upwork.com/nx/find-work/most-recent?nav_dir=pop"
KNOWN_JSON   = "upwork_jobs.json"    # seeded from scraping.py run
OUTPUT_JSON  = "new_jobs.json"
OUTPUT_CSV   = "new_jobs.csv"


# ════════════════════════════════════════════════════════════════
#  "Posted N ago" → timedelta  (shared with scraping.py)
# ════════════════════════════════════════════════════════════════

_UNIT_SECONDS = {
    "second": 1,         "seconds": 1,
    "minute": 60,        "minutes": 60,
    "hour":   3600,      "hours":   3600,
    "day":    86400,     "days":    86400,
    "week":   604800,    "weeks":   604800,
    "month":  2_592_000, "months":  2_592_000,
    "year":   31_536_000,"years":   31_536_000,
}

def parse_posted_age(text: str) -> timedelta | None:
    text = text.strip().lower()
    if text in ("just now", "moments ago"):
        return timedelta(seconds=0)
    if text == "yesterday":
        return timedelta(days=1)
    m = re.search(r"(\d+)\s+(\w+)\s+ago", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = _UNIT_SECONDS.get(unit)
        if secs:
            return timedelta(seconds=n * secs)
    return None


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════

def safe_text(el) -> str:
    return el.get_text(strip=True) if el else ""


def parse_rating_from_foreground(fg_div) -> str:
    if fg_div is None:
        return ""
    parent = fg_div.find_parent()
    sr = parent.find("span", class_="sr-only") if parent else None
    if sr:
        m = re.search(r"[\d.]+", safe_text(sr))
        return m.group() if m else ""
    style = fg_div.get("style", "")
    m = re.search(r"width:\s*([\d.]+)px", style)
    if m:
        try:
            return str(round(float(m.group(1)) / 78 * 5, 1))
        except ValueError:
            pass
    return ""


async def wait_for_captcha_if_needed(page) -> None:
    url   = page.url.lower()
    title = (await page.title()).lower()
    if any(kw in url or kw in title
           for kw in ("captcha", "challenge", "verify", "robot", "blocked")):
        print(
            "\n⚠  CAPTCHA detected! Solve it in Chrome, then press Enter...",
            flush=True,
        )
        input()
        await asyncio.sleep(3)


# ════════════════════════════════════════════════════════════════
#  Feed tile parser  (identical logic to scraping.py)
# ════════════════════════════════════════════════════════════════

def parse_job_tile(section) -> dict:
    job: dict = {}

    title_a = section.find("a", {"data-ev-label": "link"})
    job["title"] = safe_text(title_a)
    job["url"]   = ("https://www.upwork.com" + title_a["href"]) if title_a else ""

    desc = section.find("span", {"data-test": "job-description-text"})
    job["description"] = safe_text(desc)

    job_type = section.find("strong", {"data-test": "job-type"})
    job["rate"] = safe_text(job_type)

    budget_el = section.find("span", {"data-test": "budget"})
    job["estimated_budget"] = safe_text(budget_el)

    proposals = section.find("span", {"data-test": "proposals-tier"})
    job["proposals"] = safe_text(proposals)

    posted = section.find("span", {"data-test": "posted-on"})
    job["posted"] = safe_text(posted)

    spent = section.find("span", {"data-test": "formatted-amount"})
    job["client_money_spent"] = safe_text(spent)

    pv = section.find("strong", {"data-test": "payment-verification-status"})
    job["payment_verified"] = safe_text(pv)

    country = section.find("small", {"data-test": "client-country"})
    job["client_country"] = re.sub(r"\s+", " ", safe_text(country)).strip()

    sr_rating = section.find("span", class_="sr-only",
                               string=re.compile(r"Rating is", re.I))
    if sr_rating:
        m = re.search(r"([\d.]+)\s+out of", safe_text(sr_rating))
        job["client_rating"] = m.group(1) if m else ""
    else:
        fg = section.find("div", class_="air3-rating-foreground")
        job["client_rating"] = parse_rating_from_foreground(fg)

    job["client_hire_rate"] = ""
    return job


# ════════════════════════════════════════════════════════════════
#  Detail-page scraper
# ════════════════════════════════════════════════════════════════

async def scrape_detail(page, url: str) -> dict:
    result = {"client_rating": "", "client_hire_rate": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("text=/hire rate/i", timeout=DETAIL_TIMEOUT)
    except PlaywrightTimeoutError:
        print(f"  [WARN] Timeout: {url}", file=sys.stderr)
        return result
    except Exception as exc:
        print(f"  [WARN] Error ({exc}): {url}", file=sys.stderr)
        return result

    soup = BeautifulSoup(await page.content(), "html.parser")

    sr_rating = soup.find("span", class_="sr-only",
                           string=re.compile(r"Rating is", re.I))
    if sr_rating:
        m = re.search(r"([\d.]+)\s+out of", safe_text(sr_rating))
        result["client_rating"] = m.group(1) if m else ""
    if not result["client_rating"]:
        rv = soup.find("div", class_="air3-rating-value-text")
        result["client_rating"] = safe_text(rv)
    if not result["client_rating"]:
        fg = soup.find("div", class_="air3-rating-foreground")
        result["client_rating"] = parse_rating_from_foreground(fg)

    hire_text = soup.find(string=re.compile(r"\d+%\s+hire rate", re.I))
    if hire_text:
        result["client_hire_rate"] = hire_text.strip()

    return result


# ════════════════════════════════════════════════════════════════
#  Output helpers — append to JSON array + CSV without rewriting
# ════════════════════════════════════════════════════════════════

def load_existing_output() -> list[dict]:
    """Load new_jobs.json if it exists, so we can append to it."""
    if os.path.exists(OUTPUT_JSON):
        try:
            with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def save_output(all_new_jobs: list[dict]) -> None:
    """Overwrite new_jobs.json and new_jobs.csv with the full accumulated list."""
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_new_jobs, f, indent=2, ensure_ascii=False)

    if all_new_jobs:
        fieldnames = list(all_new_jobs[0].keys())
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_new_jobs)


def seed_known_urls() -> set[str]:
    """
    Read upwork_jobs.json (output of scraping.py) plus any existing
    new_jobs.json so we never re-report a job we already know about.
    """
    known: set[str] = set()
    for path in (KNOWN_JSON, OUTPUT_JSON):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for job in json.load(f):
                        if job.get("url"):
                            known.add(job["url"])
                print(f"  Seeded {len(known)} known URL(s) from {path}")
            except (json.JSONDecodeError, IOError):
                pass
    return known


# ════════════════════════════════════════════════════════════════
#  One reload cycle — returns list of brand-new jobs found
# ════════════════════════════════════════════════════════════════

async def check_for_new_jobs(page, detail_page, known_urls: set[str]) -> list[dict]:
    """
    Reload the feed page, parse the first screen of tiles (no Load More —
    new jobs are always at the top), return any whose URL is not in known_urls.
    """
    await page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    await wait_for_captcha_if_needed(page)

    # Wait for tiles
    found = False
    for sel in ("section.air3-card-section", "[data-test='job-tile']", ".job-tile-title"):
        try:
            await page.wait_for_selector(sel, timeout=10_000)
            found = True
            break
        except PlaywrightTimeoutError:
            continue

    if not found:
        print("  [WARN] Feed tiles not found on this reload — skipping cycle.",
              file=sys.stderr)
        return []

    await asyncio.sleep(1.5)  # let lazy content settle

    soup  = BeautifulSoup(await page.content(), "html.parser")
    tiles = soup.find_all("section", class_="air3-card-section")

    new_jobs: list[dict] = []
    for tile in tiles:
        job = parse_job_tile(tile)
        if not job.get("title") or not job.get("url"):
            continue
        if job["url"] in known_urls:
            continue  # already seen

        # It's new — fetch detail page
        print(f"  ★ NEW  {job['posted']:<20}  {job['title'][:55]}")
        await wait_for_captcha_if_needed(detail_page)
        detail = await scrape_detail(detail_page, job["url"])
        await wait_for_captcha_if_needed(detail_page)
        if detail["client_rating"]:
            job["client_rating"] = detail["client_rating"]
        job["client_hire_rate"] = detail["client_hire_rate"]
        job["first_seen_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        known_urls.add(job["url"])
        new_jobs.append(job)
        await asyncio.sleep(REQUEST_DELAY)

    return new_jobs


# ════════════════════════════════════════════════════════════════
#  Main loop
# ════════════════════════════════════════════════════════════════

async def init(*, skip_ready_prompt: bool = False):
    if skip_ready_prompt:
        print("\n--- New job monitor (after initial scrape; Ctrl+C to stop) ---\n")
    else:
        print(__doc__)
        input("Press Enter once Chrome is open and you are logged into Upwork... ")

    # ── Connect to Chrome ─────────────────────────────────────────────────────
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            print(
                f"\nERROR: Could not connect to Chrome at {CDP_URL}\n{exc}\n\n"
                "Make sure Chrome was launched with --remote-debugging-port=9222.",
                file=sys.stderr,
            )
            return

        print(f"Connected to Chrome  ({len(browser.contexts)} context(s))\n")
        context     = browser.contexts[0]
        pages       = context.pages
        feed_page   = pages[0] if pages else await context.new_page()
        detail_page = await context.new_page()

        # ── Seed known URLs ───────────────────────────────────────────────────
        known_urls = seed_known_urls()
        print(f"\nMonitoring for new jobs. Reloading every {RELOAD_MIN}–{RELOAD_MAX}s.")
        print("Press Ctrl+C to stop.\n")

        # ── Load existing new_jobs output so we can append ────────────────────
        accumulated = load_existing_output()

        cycle = 0
        try:
            while True:
                cycle += 1
                wait_secs = random.uniform(RELOAD_MIN, RELOAD_MAX)
                next_time = datetime.now() + timedelta(seconds=wait_secs)
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"Cycle {cycle} — next reload in {wait_secs:.1f}s "
                    f"(~{next_time.strftime('%H:%M:%S')})",
                    flush=True,
                )
                await asyncio.sleep(wait_secs)

                print(f"[{datetime.now().strftime('%H:%M:%S')}] Reloading feed...")
                try:
                    new_jobs = await check_for_new_jobs(feed_page, detail_page, known_urls)
                except Exception as exc:
                    print(f"  [ERROR] Cycle {cycle} failed: {exc}", file=sys.stderr)
                    continue

                if new_jobs:
                    accumulated.extend(new_jobs)
                    save_output(accumulated)
                    for job in reversed(new_jobs):
                        await asyncio.to_thread(discord_webhook.send_new_job, job)
                    print(
                        f"  → {len(new_jobs)} new job(s) found. "
                        f"Total saved: {len(accumulated)}. "
                        f"Files: {OUTPUT_JSON}, {OUTPUT_CSV}"
                    )
                else:
                    print(f"  → No new jobs this cycle.")

        except KeyboardInterrupt:
            print(f"\n\nStopped by user after {cycle} cycle(s).")
            print(f"Total new jobs collected: {len(accumulated)}")
            if accumulated:
                save_output(accumulated)
                print(f"Final save → {OUTPUT_JSON}, {OUTPUT_CSV}")

        await detail_page.close()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(init())