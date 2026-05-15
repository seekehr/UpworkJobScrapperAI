"""Run the Upwork feed scraper (`scrapping.py`), then the new-job monitor (`scrape_new_jobs.py`)."""

import asyncio

import scrape_new_jobs
import scrapping


def main() -> None:
    asyncio.run(scrapping.init())
    asyncio.run(scrape_new_jobs.init(skip_ready_prompt=True))


if __name__ == "__main__":
    main()
