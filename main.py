"""Run the Upwork feed scraper (`scrapping.py`), optional keyword summaries, then the new-job monitor (`scrape_new_jobs.py`)."""

import asyncio
import json
from pathlib import Path

import yaml

import scrape_new_jobs
import scrapping

KEYWORDS_PATH = Path(__file__).resolve().parent / "keywords.yaml"


def load_keywords(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    raw = data.get("keywords") if isinstance(data, dict) else None
    if not raw:
        return []
    return [str(k).strip() for k in raw if str(k).strip()]


def job_matches(title: str, description: str, keywords: list[str]) -> tuple[bool, list[str]]:
    blob = f"{title}\n{description}".lower()
    matched = [kw for kw in keywords if kw.lower() in blob]
    return (bool(matched), matched)


def _print_keyword_summary() -> None:
    keywords = load_keywords(KEYWORDS_PATH)
    if not keywords:
        return

    out_path = Path(__file__).resolve().parent / scrapping.OUTPUT_JSON
    if not out_path.is_file():
        return

    with open(out_path, encoding="utf-8") as f:
        jobs = json.load(f)

    print("\n--- Keyword matches (title + feed snippet vs keywords.yaml) ---")
    hits = 0
    for job in jobs:
        title = job.get("title") or ""
        description = job.get("description") or ""
        ok, matched = job_matches(title, description, keywords)
        if ok:
            hits += 1
            preview = title[:72] + ("…" if len(title) > 72 else "")
            print(f"  • {preview}")
            print(f"    matched: {', '.join(matched)}")
    print(f"\nKeywords: {keywords} | jobs with any match: {hits} / {len(jobs)}")


def main() -> None:
    asyncio.run(scrapping.init())
    _print_keyword_summary()
    asyncio.run(scrape_new_jobs.init(skip_ready_prompt=True))


if __name__ == "__main__":
    main()
