"""
Upwork Job Scraper
==================
Connects to YOUR real Chrome (via remote debugging) so Upwork
cannot detect any automation.

HOW TO USE
----------
1. Close every Chrome window on your PC.

2. Open a Command Prompt and paste this (one line):
       "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222

   (macOS: open -a "Google Chrome" --args --remote-debugging-port=9222)

3. In that Chrome window go to https://www.upwork.com and log in normally.

4. In a second terminal run:
       python scraping.py

The script will connect to your running Chrome, navigate to the jobs
feed, scrape every tile, visit each detail page for hire-rate & rating,
and save upwork_jobs.json + upwork_jobs.csv next to the script.
"""

import asyncio
import csv
import json
import re
import sys
from datetime import datetime

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ── Config ────────────────────────────────────────────────────────────────────

JOBS_URL  = "https://www.upwork.com/nx/find-work/most-recent?nav_dir=pop"
CDP_URL   = "http://localhost:9222"   # must match --remote-debugging-port

OUTPUT_JSON = "upwork_jobs.json"
OUTPUT_CSV  = "upwork_jobs.csv"

DETAIL_TIMEOUT = 10_000   # ms to wait for hire-rate element on detail pages
REQUEST_DELAY  = 1.5      # seconds between detail-page requests


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_text(element) -> str:
    return element.get_text(strip=True) if element else ""


def parse_rating_from_foreground(fg_div) -> str:
    """
    Upwork encodes star ratings in a foreground div's inline width:
        width_px / 78 * 5 = rating
    Prefers the hidden <span class="sr-only"> when available.
    """
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


# ── Detail-page scraping ──────────────────────────────────────────────────────

async def scrape_detail(page, url: str) -> dict:
    """Navigate to a job detail page and return client_rating + client_hire_rate."""
    result = {"client_rating": "", "client_hire_rate": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("text=/hire rate/i", timeout=DETAIL_TIMEOUT)
    except PlaywrightTimeoutError:
        print(f"  WARNING  Timeout on: {url}", file=sys.stderr)
        return result
    except Exception as exc:
        print(f"  WARNING  Error ({exc}) on: {url}", file=sys.stderr)
        return result

    soup = BeautifulSoup(await page.content(), "html.parser")

    # Rating — prefer the screen-reader span, then the visible value div
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

    # Hire rate — looks like "39% hire rate, 12 open jobs"
    hire_text = soup.find(string=re.compile(r"\d+%\s+hire rate", re.I))
    if hire_text:
        result["client_hire_rate"] = hire_text.strip()

    return result


# ── Feed-page tile parsing ────────────────────────────────────────────────────

def parse_job_tile(section) -> dict:
    job: dict = {}

    title_a = section.find("a", {"data-ev-label": "link"})
    job["title"] = safe_text(title_a)
    job["url"]   = ("https://www.upwork.com" + title_a["href"]) if title_a else ""

    desc = section.find("span", {"data-test": "job-description-text"})
    job["description"] = safe_text(desc)

    job_type = section.find("strong", {"data-test": "job-type"})
    job["rate"] = safe_text(job_type)           # e.g. "Hourly: $25-$80"

    budget_el = section.find("span", {"data-test": "budget"})
    job["estimated_budget"] = safe_text(budget_el)  # present for fixed-price

    proposals = section.find("span", {"data-test": "proposals-tier"})
    job["proposals"] = safe_text(proposals)

    posted = section.find("span", {"data-test": "posted-on"})
    job["posted"] = safe_text(posted)

    spent = section.find("span", {"data-test": "formatted-amount"})
    job["client_money_spent"] = safe_text(spent)

    pv = section.find("strong", {"data-test": "payment-verification-status"})
    job["payment_verified"] = safe_text(pv)

    country = section.find("small", {"data-test": "client-country"})
    raw_country = safe_text(country) if country else ""
    job["client_country"] = re.sub(r"\s+", " ", raw_country).strip()

    # Tile-level rating (often absent; overwritten by detail page)
    sr_rating = section.find("span", class_="sr-only",
                               string=re.compile(r"Rating is", re.I))
    if sr_rating:
        m = re.search(r"([\d.]+)\s+out of", safe_text(sr_rating))
        job["client_rating"] = m.group(1) if m else ""
    else:
        fg = section.find("div", class_="air3-rating-foreground")
        job["client_rating"] = parse_rating_from_foreground(fg)

    job["client_hire_rate"] = ""   # filled later from detail page
    return job


# ── Main ──────────────────────────────────────────────────────────────────────

async def init():
    print(__doc__)
    input("Press Enter once Chrome is open and you are logged into Upwork... ")

    async with async_playwright() as pw:
        # ── Connect to the user's real Chrome over CDP ────────────────────────
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:
            print(
                f"\nERROR: Could not connect to Chrome at {CDP_URL}\n"
                f"Details: {exc}\n\n"
                "Make sure you:\n"
                "  1. Closed ALL Chrome windows first.\n"
                "  2. Launched Chrome with --remote-debugging-port=9222\n"
                "  3. Are running this script AFTER Chrome is open.",
                file=sys.stderr,
            )
            return

        print(f"Connected to Chrome ({len(browser.contexts)} context(s) found).")

        # Reuse the existing browser context so our session is already logged in
        context = browser.contexts[0]
        pages   = context.pages
        page    = pages[0] if pages else await context.new_page()

        # ── Navigate to jobs feed ─────────────────────────────────────────────
        print(f"\nNavigating to: {JOBS_URL}")
        await page.goto(JOBS_URL, wait_until="networkidle", timeout=30_000)

        try:
            await page.wait_for_selector("section.air3-card-section", timeout=15_000)
        except PlaywrightTimeoutError:
            print(
                "ERROR: Job tiles did not appear. Are you logged in to Upwork?",
                file=sys.stderr,
            )
            return

        # ── Parse feed tiles ──────────────────────────────────────────────────
        soup  = BeautifulSoup(await page.content(), "html.parser")
        tiles = soup.find_all("section", class_="air3-card-section")
        print(f"Found {len(tiles)} job tile(s). Parsing...\n")

        jobs = []
        for t in tiles:
            j = parse_job_tile(t)
            if j.get("title"):
                jobs.append(j)

        # ── Visit detail pages ────────────────────────────────────────────────
        detail_page = await context.new_page()
        for i, job in enumerate(jobs, 1):
            if not job["url"]:
                continue
            print(f"  [{i}/{len(jobs)}] {job['title'][:65]}")
            detail = await scrape_detail(detail_page, job["url"])
            if detail["client_rating"]:
                job["client_rating"] = detail["client_rating"]
            job["client_hire_rate"] = detail["client_hire_rate"]
            await asyncio.sleep(REQUEST_DELAY)
        await detail_page.close()

        # Don't close the browser — it's the user's real Chrome!
        print("\nScraping complete. Chrome left open.")

    # ── Save ──────────────────────────────────────────────────────────────────
    if not jobs:
        print("No jobs were scraped. Exiting without saving.")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for job in jobs:
        job["scraped_at"] = ts

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    print(f"  Saved {OUTPUT_JSON}  ({len(jobs)} jobs)")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(jobs[0].keys()))
        writer.writeheader()
        writer.writerows(jobs)
    print(f"  Saved {OUTPUT_CSV}  ({len(jobs)} jobs)")
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())