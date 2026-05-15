"""Post Upwork jobs to Discord via webhook; dedupe using sent_jobs_discord.json.

Set DISCORD_WEBHOOK_URL in a `.env` file next to this module (see `.env.example`).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

# Jobs successfully posted to Discord (full snapshots + discord_sent_at); used to skip resends.
SENT_JOBS_JSON = _ROOT / "sent_jobs_discord.json"


def _resolved_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _truncate(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _load_sent_entries() -> list[dict[str, Any]]:
    path = SENT_JOBS_JSON
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _sent_urls(entries: list[dict[str, Any]]) -> set[str]:
    return {(e.get("url") or "").strip() for e in entries if (e.get("url") or "").strip()}


def _save_sent_entries(entries: list[dict[str, Any]]) -> None:
    path = SENT_JOBS_JSON
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _job_embed(job: dict[str, Any]) -> dict[str, Any]:
    title = _truncate(job.get("title") or "New Upwork job", 256)
    url = (job.get("url") or "").strip()
    description = _truncate(job.get("description") or "", 3500)

    fields: list[dict[str, Any]] = []
    for name, key in (
        ("Posted", "posted"),
        ("Rate", "rate"),
        ("Budget", "estimated_budget"),
        ("Proposals", "proposals"),
        ("Spent", "client_money_spent"),
        ("Country", "client_country"),
        ("Payment", "payment_verified"),
        ("Rating", "client_rating"),
        ("Hire rate", "client_hire_rate"),
        ("First seen", "first_seen_at"),
        ("Scraped", "scraped_at"),
    ):
        val = str(job.get(key) or "").strip()
        if val:
            fields.append({"name": name, "value": _truncate(val, 1024), "inline": True})

    embed: dict[str, Any] = {
        "title": title,
        "description": description or None,
        "color": 0x3498DB,
        "fields": fields[:25],
    }
    if url:
        embed["url"] = url
    return {k: v for k, v in embed.items() if v is not None}


def _post_job_embed(job: dict[str, Any]) -> bool:
    hook = _resolved_url()
    if not hook:
        return False

    payload = {"embeds": [_job_embed(job)]}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        hook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                print(
                    f"  [WARN] Discord webhook HTTP {resp.status}",
                    file=sys.stderr,
                )
                return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        print(
            f"  [WARN] Discord webhook failed ({exc.code}): {body}",
            file=sys.stderr,
        )
        return False
    except OSError as exc:
        print(f"  [WARN] Discord webhook failed: {exc}", file=sys.stderr)
        return False
    return True


def is_enabled() -> bool:
    return bool(_resolved_url())


def send_new_job(job: dict[str, Any]) -> bool:
    """Post job to Discord if configured and this URL is not already in sent_jobs_discord.json."""
    if not _resolved_url():
        return False

    url = (job.get("url") or "").strip()
    if not url:
        return False

    entries = _load_sent_entries()
    if url in _sent_urls(entries):
        return False

    if not _post_job_embed(job):
        return False

    snapshot = dict(job)
    snapshot["discord_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries.append(snapshot)
    try:
        _save_sent_entries(entries)
    except OSError as exc:
        print(
            f"  [WARN] Could not save {SENT_JOBS_JSON.name}: {exc}",
            file=sys.stderr,
        )
    return True
