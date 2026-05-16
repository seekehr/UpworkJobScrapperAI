"""Groq API client for querying scraped Upwork job data."""

import json
import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

OUTPUT_JSON = "upwork_jobs.json"
PROMPT_FILE = "prompt.txt"


def _load_jobs() -> str:
    if not os.path.exists(OUTPUT_JSON):
        return "No job data available yet."
    with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    if not jobs:
        return "No jobs found in the data file."
    return json.dumps(jobs, indent=2, ensure_ascii=False)


def _load_prompt() -> str:
    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def ask(question: str) -> None:
    """Stream a Groq response about the scraped jobs."""
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    jobs_context = _load_jobs()
    system_prompt = f"{_load_prompt()}\n\n{jobs_context}"

    stream = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        stream=True,
    )

    for chunk in stream:
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
    print()
