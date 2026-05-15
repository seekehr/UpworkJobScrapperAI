"""
Upwork Job Scraper
==================
Connects to YOUR real Chrome (via remote debugging) so Upwork cannot
detect automation.  Scrolls the feed incrementally, waiting for new tiles
to appear in the DOM after each scroll, until every visible job exceeds
the age cutoff.  Then visits each detail page for rating and hire rate.

HOW TO USE
----------
1. Kill every Chrome process and launch Chrome with remote debugging:

   PowerShell (run as one block):
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
SCROLL_STEP    – pixels to scroll per step                       (default 600)
SCROLL_PAUSE   – seconds to wait after each scroll step          (default 2.0)
NEW_TILE_WAIT  – ms to wait for at least one new tile to appear  (default 5000)
MAX_SAME_COUNT – stop after this many scrolls with no new tiles  (default 8)
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
#  SETTINGS
# ════════════════════════════════════════════════════════════════

MAX_AGE_DAYS   = 2
REQUEST_DELAY  = 1.5
DETAIL_TIMEOUT = 10_000
# (scrolling replaced by "Load More Jobs" button — no scroll settings needed)

CDP_URL     = "http://localhost:9222"
JOBS_URL    = "https://www.upwork.com/nx/find-work/most-recent?nav_dir=pop"
OUTPUT_JSON = "upwork_jobs.json"
OUTPUT_CSV  = "upwork_jobs.csv"


# ════════════════════════════════════════════════════════════════
#  "Posted N ago" → timedelta
# ════════════════════════════════════════════════════════════════

_UNIT_SECONDS = {
    "second": 1,        "seconds": 1,
    "minute": 60,       "minutes": 60,
    "hour":   3600,     "hours":   3600,
    "day":    86400,    "days":    86400,
    "week":   604800,   "weeks":   604800,
    "month":  2_592_000,"months":  2_592_000,
    "year":   31_536_000,"years":  31_536_000,
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

def is_too_old(posted_text: str, max_age_days: int) -> bool:
    age = parse_posted_age(posted_text)
    if age is None:
        return False
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

async def wait_for_captcha_if_needed(page) -> None:
    url   = page.url.lower()
    title = (await page.title()).lower()
    if any(kw in url or kw in title
           for kw in ("captcha", "challenge", "verify", "robot", "blocked")):
        print(
            "\n⚠  CAPTCHA / challenge detected!\n"
            "   Solve it in the Chrome window, then press Enter here...",
            flush=True,
        )
        input()
        await asyncio.sleep(3)


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
#  Incremental scroller
# ════════════════════════════════════════════════════════════════

async def scroll_and_collect(page, max_age_days: int) -> list[dict]:
    """
    Parse visible tiles, then click "Load More Jobs" repeatedly until:
      - A tile older than max_age_days is found, or
      - The button disappears (feed exhausted).
    """
    seen_urls: set[str] = set()
    all_jobs:  list[dict] = []

    LOAD_MORE_SEL  = "[data-test='load-more-button']"
    LOAD_MORE_WAIT = 15_000   # ms to wait for button + new tiles after clicking

    print(f"Collecting jobs posted ≤ {max_age_days} day(s) ago...\n")

    async def parse_current_tiles() -> bool:
        """Parse all tiles on the page. Returns True if cutoff was reached."""
        soup  = BeautifulSoup(await page.content(), "html.parser")
        tiles = soup.find_all("section", class_="air3-card-section")
        for tile in tiles:
            job = parse_job_tile(tile)
            if not job.get("title") or job["url"] in seen_urls:
                continue
            if job["posted"] and is_too_old(job["posted"], max_age_days):
                print(
                    f"  [STOP] Cutoff — '{job['posted']}' exceeds {max_age_days} day(s)."
                )
                return True
            seen_urls.add(job["url"])
            all_jobs.append(job)
            print(f"  + [{len(all_jobs):>4}]  {job['posted']:<22}  {job['title'][:55]}")
        return False

    # Parse whatever is already on screen
    if await parse_current_tiles():
        print(f"\nCollected {len(all_jobs)} job(s).")
        return all_jobs

    page_num = 1
    while True:
        await wait_for_captcha_if_needed(page)

        # Scroll the button into view so it's clickable
        btn = page.locator(LOAD_MORE_SEL)
        btn_count = await btn.count()

        if btn_count == 0:
            print("  [STOP] No 'Load More Jobs' button — feed exhausted.")
            break

        print(f"  [page {page_num}] Clicking 'Load More Jobs'...")
        tiles_before = await page.locator("section.air3-card-section").count()

        await btn.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)
        await btn.click()
        page_num += 1

        # Wait for new tiles to appear in the DOM
        try:
            await page.wait_for_function(
                f"document.querySelectorAll('section.air3-card-section').length > {tiles_before}",
                timeout=LOAD_MORE_WAIT,
            )
        except PlaywrightTimeoutError:
            print("  [WARN] Timed out waiting for new tiles after clicking Load More.")
            # Button may have triggered a captcha — check
            await wait_for_captcha_if_needed(page)
            # Try once more
            try:
                await page.wait_for_function(
                    f"document.querySelectorAll('section.air3-card-section').length > {tiles_before}",
                    timeout=LOAD_MORE_WAIT,
                )
            except PlaywrightTimeoutError:
                print("  [STOP] Still no new tiles — giving up.")
                break

        await asyncio.sleep(1.5)  # let content settle

        if await parse_current_tiles():
            break

    print(f"\nCollected {len(all_jobs)} job(s) within the {max_age_days}-day window.")
    return all_jobs


# ════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════

async def init():
    print(__doc__)

    try:
        days_input = input(
            f"How many days back to collect? [default {MAX_AGE_DAYS}]: "
        ).strip()
        max_age = int(days_input) if days_input else MAX_AGE_DAYS
    except ValueError:
        max_age = MAX_AGE_DAYS
    print(f"  → Cutoff: jobs posted within the last {max_age} day(s).\n")

    input("Press Enter once Chrome is open and you are logged into Upwork... ")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            print(
                f"\nERROR: Could not connect to Chrome at {CDP_URL}\n{exc}\n\n"
                "Steps:\n"
                "  1. Stop-Process -Name chrome -Force\n"
                "  2. Launch Chrome with --remote-debugging-port=9222\n"
                "  3. Log into Upwork, then re-run this script.",
                file=sys.stderr,
            )
            return

        print(f"Connected to Chrome  ({len(browser.contexts)} context(s))\n")
        context = browser.contexts[0]
        pages   = context.pages
        page    = pages[0] if pages else await context.new_page()

        print(f"Navigating to: {JOBS_URL}")
        # domcontentloaded is more reliable than networkidle on Vue/React SPAs
        await page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        await wait_for_captcha_if_needed(page)

        # Wait for tiles with retries — Upwork hydrates the DOM asynchronously
        print("Waiting for job feed to load...")
        found = False
        selectors = [
            "section.air3-card-section",
            "[data-test='job-tile']",
            ".job-tile-title",
        ]
        for attempt in range(3):
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=10_000)
                    print(f"  Tiles found (selector: {sel})")
                    found = True
                    break
                except PlaywrightTimeoutError:
                    continue
            if found:
                break
            print(f"  Attempt {attempt + 1}: not visible yet, nudging SPA...")
            await page.evaluate("window.scrollTo(0, 400)")
            await asyncio.sleep(3)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(2)

        if not found:
            cur_url   = page.url
            cur_title = await page.title()
            snippet   = (await page.content())[:3000]
            print(
                f"\nERROR: Job tiles not found after 3 attempts.\n"
                f"  URL   : {cur_url}\n"
                f"  Title : {cur_title}\n"
                f"  HTML snippet:\n{snippet}",
                file=sys.stderr,
            )
            return

        await asyncio.sleep(2)  # let lazy content settle

        # Initial parse before any scrolling
        soup  = BeautifulSoup(await page.content(), "html.parser")
        tiles = soup.find_all("section", class_="air3-card-section")
        seen_urls: set[str] = set()
        initial_jobs: list[dict] = []
        for tile in tiles:
            job = parse_job_tile(tile)
            if job.get("title"):
                seen_urls.add(job["url"])
                if not is_too_old(job["posted"], max_age):
                    initial_jobs.append(job)
                    print(f"  + [{len(initial_jobs):>4}]  {job['posted']:<22}  {job['title'][:55]}")

        # Now scroll for more
        scroll_jobs = await scroll_and_collect(page, max_age)

        # Merge (scroll_and_collect has its own seen_urls, so dedup by url again)
        all_urls = {j["url"] for j in initial_jobs}
        jobs = initial_jobs[:]
        for j in scroll_jobs:
            if j["url"] not in all_urls:
                all_urls.add(j["url"])
                jobs.append(j)

        if not jobs:
            print("No jobs collected within the cutoff window.")
            return

        print(f"\nTotal unique jobs: {len(jobs)}")

        # ── Detail pages ─────────────────────────────────────────────────────
        print(f"\nFetching detail pages...\n")
        detail_page = await context.new_page()

        for i, job in enumerate(jobs, 1):
            if not job["url"]:
                continue
            print(f"  [{i}/{len(jobs)}] {job['title'][:65]}")
            await wait_for_captcha_if_needed(detail_page)
            detail = await scrape_detail(detail_page, job["url"])
            await wait_for_captcha_if_needed(detail_page)
            if detail["client_rating"]:
                job["client_rating"] = detail["client_rating"]
            job["client_hire_rate"] = detail["client_hire_rate"]
            await asyncio.sleep(REQUEST_DELAY)

        await detail_page.close()
        print("\nScraping complete. Chrome left open.")

    # ── Save ──────────────────────────────────────────────────────────────────
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
    asyncio.run(init())