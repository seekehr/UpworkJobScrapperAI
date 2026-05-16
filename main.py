"""Run the Upwork feed scraper (`scrapping.py`), then the new-job monitor (`scrape_new_jobs.py`)."""

import asyncio

import groq_client
import scrape_new_jobs
import scrapping


def _ask_loop() -> None:
    """Let the user ask questions about scraped jobs until they type 'exit'."""
    print("\n--- Ask questions about the scraped jobs (type 'exit' to move on) ---\n")
    while True:
        question = input("You: ").strip()
        if not question:
            continue
        if question.lower() == "exit":
            break
        groq_client.ask(question)
        print()


def main() -> None:
    print("\nSelect mode:")
    print("1. Listen for new jobs only (skip initial scrape)")
    print("2. Scrape all jobs first, then listen for new jobs")
    choice = input("\nEnter choice (1 or 2): ").strip()

    if choice == "1":
        asyncio.run(scrape_new_jobs.init())
    else:
        asyncio.run(scrapping.init())
        _ask_loop()
        asyncio.run(scrape_new_jobs.init(skip_ready_prompt=True))


if __name__ == "__main__":
    main()
