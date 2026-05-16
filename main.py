"""Run the Upwork feed scraper (`scrapping.py`), then the new-job monitor (`scrape_new_jobs.py`)."""

import asyncio

import scrape_new_jobs
import scrapping


def main() -> None:
    print("\nSelect mode:")
    print("1. Listen for new jobs only (skip initial scrape)")
    print("2. Scrape all jobs first, then listen for new jobs")
    choice = input("\nEnter choice (1 or 2): ").strip()

    if choice == "1":
        asyncio.run(scrape_new_jobs.init())
    else:
        asyncio.run(scrapping.init())
        asyncio.run(scrape_new_jobs.init(skip_ready_prompt=True))


if __name__ == "__main__":
    main()
