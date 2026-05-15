"""
Upwork Job Scraper
==================
Connects to YOUR real Chrome (via remote debugging) so Upwork cannot
detect automation.  Scrolls the feed, loading more jobs automatically,
until every visible job was posted within MAX_AGE_DAYS days.  Then visits
each detail page to collect client rating and hire rate.

HOW TO USE
----------
1. Kill every Chrome process and launch Chrome with remote debugging:

   PowerShell:
       Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
       & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
           --remote-debugging-port=9222 `
           --user-data-dir="C:\\Users\\Hp\\chrome-debug-profile"

2. Log into Upwork in that Chrome window.

3. In a second terminal:
       python scraping.py

SETTINGS (edit below)
---------------------
MAX_AGE_DAYS   – only collect jobs posted within this many days  (default 2)
REQUEST_DELAY  – seconds to wait between detail-page visits      (default 1.5)
DETAIL_TIMEOUT – ms to wait for "hire rate" text on detail pages (default 10000)
"""

import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ════════════════════════════════════════════════════════════════
#  SETTINGS  — edit these
# ════════════════════════════════════════════════════════════════

MAX_AGE_DAYS   = 1       # stop scrolling when jobs are older than this
REQUEST_DELAY  = 1.5     # seconds between detail-page requests
DETAIL_TIMEOUT = 10_000  # ms

CDP_URL      = "http://localhost:9222"
JOBS_URL     = "https://www.upwork.com/nx/find-work/most-recent?nav_dir=pop"
OUTPUT_JSON  = "upwork_jobs.json"
OUTPUT_CSV   = "upwork_jobs.csv"

# ════════════════════════════════════════════════════════════════
#  "Posted N ago" → timedelta parser
# ════════════════════════════════════════════════════════════════

# Maps unit words → number of seconds
_UNIT_SECONDS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour":   3600, "hours": 3600,
    "day":    86400, "days": 86400,
    "week":   604800, "weeks": 604800,
    "month":  2_592_000, "months": 2_592_000,  # ~30 days
    "year":   31_536_000, "years": 31_536_000,
}

def parse_posted_age(text: str) -> timedelta | None:
    """
    Parse strings like:
      "50 minutes ago", "2 hours ago", "1 day ago",
      "3 days ago", "just now", "yesterday"
    Returns a timedelta representing how long ago the job was posted,
    or None if the text cannot be parsed.
    """
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


def is_too_old(posted_text: str, max_age_days: int) -> bool:
    """Return True if the job's posted-age exceeds max_age_days."""
    age = parse_posted_age(posted_text)
    if age is None:
        return False   # can't tell → keep it
    return age > timedelta(days=max_age_days)


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


# ════════════════════════════════════════════════════════════════
#  Feed tile parser
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
        print(f"  [WARN] Timeout on detail page: {url}", file=sys.stderr)
        return result
    except Exception as exc:
        print(f"  [WARN] Error on detail page: {exc}", file=sys.stderr)
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
#  Captcha / block detector
# ════════════════════════════════════════════════════════════════

async def wait_for_captcha_if_needed(page, timeout_minutes: int = 10) -> None:
    """
    If Upwork shows a CAPTCHA or challenge page, print a warning and
    wait for the user to solve it (up to timeout_minutes minutes).
    Detection: page URL contains 'captcha' or 'challenge', or the
    page title contains 'verify' or 'robot'.
    """
    url   = page.url.lower()
    title = (await page.title()).lower()
    if any(kw in url or kw in title
           for kw in ("captcha", "challenge", "verify", "robot", "blocked")):
        print(
            "\n⚠  CAPTCHA / challenge detected!\n"
            "   Please solve it in the Chrome window, then press Enter here...",
            flush=True,
        )
        input()
        # After solving, give the page a moment to redirect
        await asyncio.sleep(3)


# ════════════════════════════════════════════════════════════════
#  Feed scroller  — loads tiles until cutoff age is hit
# ════════════════════════════════════════════════════════════════

async def scroll_and_collect(page, max_age_days: int) -> list[dict]:
    """
    Scroll the feed, parse new tiles after each scroll, and stop when
    the oldest visible job exceeds max_age_days.  Returns a deduplicated
    list of job dicts (title is used as the dedup key).
    """
    seen_urls: set[str] = set()
    all_jobs:  list[dict] = []
    cutoff = timedelta(days=max_age_days)

    scroll_pause   = 1.8   # seconds after each scroll before re-reading the DOM
    no_new_streak  = 0     # how many scrolls in a row yielded zero new tiles
    MAX_NO_NEW     = 5     # give up after this many barren scrolls

    print(f"Scrolling feed (cutoff: jobs posted ≤ {max_age_days} day(s) ago)...\n")

    while True:
        await wait_for_captcha_if_needed(page)

        soup  = BeautifulSoup(await page.content(), "html.parser")
        tiles = soup.find_all("section", class_="air3-card-section")

        new_this_round = 0
        reached_cutoff = False

        for tile in tiles:
            job = parse_job_tile(tile)
            if not job.get("title") or job["url"] in seen_urls:
                continue

            # Check age
            if job["posted"] and is_too_old(job["posted"], max_age_days):
                print(
                    f"  [STOP] Reached cutoff — job posted '{job['posted']}' "
                    f"exceeds {max_age_days} day(s). Stopping scroll."
                )
                reached_cutoff = True
                break

            seen_urls.add(job["url"])
            all_jobs.append(job)
            new_this_round += 1
            print(f"  + [{len(all_jobs):>4}] {job['posted']:>20}  {job['title'][:55]}")

        if reached_cutoff:
            break

        if new_this_round == 0:
            no_new_streak += 1
            if no_new_streak >= MAX_NO_NEW:
                print("  [STOP] No new jobs loaded after several scrolls — feed exhausted.")
                break
        else:
            no_new_streak = 0

        # Scroll to the bottom of the page to trigger lazy-loading
        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(scroll_pause)

        # If the page didn't grow, the feed may be fully loaded
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            # Try pressing End key as a fallback trigger
            await page.keyboard.press("End")
            await asyncio.sleep(scroll_pause)
            final_height = await page.evaluate("document.body.scrollHeight")
            if final_height == prev_height:
                no_new_streak += 1

    print(f"\nCollected {len(all_jobs)} job(s) within the {max_age_days}-day window.")
    return all_jobs


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

async def init():
    print(__doc__)

    # ── Ask for cutoff ───────────────────────────────────────────
    try:
        days_input = input(
            f"How many days back should we collect? "
            f"[default: {MAX_AGE_DAYS}]: "
        ).strip()
        max_age = int(days_input) if days_input else MAX_AGE_DAYS
    except ValueError:
        max_age = MAX_AGE_DAYS
    print(f"  → Collecting jobs posted within the last {max_age} day(s).\n")

    input("Press Enter once Chrome is open and you are logged into Upwork... ")

    async with async_playwright() as pw:
        # ── Connect to user's real Chrome ────────────────────────────────────
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            print(
                f"\nERROR: Could not connect to Chrome at {CDP_URL}\n"
                f"Details: {exc}\n\n"
                "Make sure you:\n"
                "  1. Killed ALL Chrome processes first.\n"
                "  2. Launched Chrome with --remote-debugging-port=9222\n"
                "  3. Ran this script AFTER Chrome opened.",
                file=sys.stderr,
            )
            return

        print(f"Connected to Chrome  ({len(browser.contexts)} context(s))\n")
        context = browser.contexts[0]
        pages   = context.pages
        page    = pages[0] if pages else await context.new_page()

        # ── Navigate to feed ─────────────────────────────────────────────────
        print(f"Navigating to: {JOBS_URL}")
        await page.goto(JOBS_URL, wait_until="networkidle", timeout=30_000)
        await wait_for_captcha_if_needed(page)

        try:
            await page.wait_for_selector("section.air3-card-section", timeout=15_000)
        except PlaywrightTimeoutError:
            print("ERROR: Job tiles did not appear. Are you logged in?", file=sys.stderr)
            return

        # ── Scroll & collect ─────────────────────────────────────────────────
        jobs = await scroll_and_collect(page, max_age)

        if not jobs:
            print("No jobs collected. Exiting.")
            return

        # ── Visit detail pages ────────────────────────────────────────────────
        print(f"\nFetching detail pages for {len(jobs)} job(s)...\n")
        detail_page = await context.new_page()

        for i, job in enumerate(jobs, 1):
            if not job["url"]:
                continue
            print(f"  [{i}/{len(jobs)}] {job['title'][:65]}")

            # Check for captcha on the feed page too (background tab)
            await wait_for_captcha_if_needed(detail_page)

            detail = await scrape_detail(detail_page, job["url"])

            # After each detail page, check again
            await wait_for_captcha_if_needed(detail_page)

            if detail["client_rating"]:
                job["client_rating"] = detail["client_rating"]
            job["client_hire_rate"] = detail["client_hire_rate"]
            await asyncio.sleep(REQUEST_DELAY)

        await detail_page.close()
        print("\nScraping complete. Chrome left open.")

    # ── Save results ──────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for job in jobs:
        job["scraped_at"] = ts

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {OUTPUT_JSON}  ({len(jobs)} jobs)")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(jobs[0].keys()))
        writer.writeheader()
        writer.writerows(jobs)
    print(f"  Saved {OUTPUT_CSV}  ({len(jobs)} jobs)")
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())