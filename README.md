# Upwork Scrapper

UpworkScrapper is a Python toolset that plugs into your own Chrome (via remote debugging on port 9222) and uses Playwright to read Upwork’s “most recent” freelancer feed. It collects job listings into JSON and CSV—title, link, description, rate/budget, proposals, posted time, client spend, payment verification, country, and enriched client rating and hire rate from each job’s detail page—with pauses between requests and prompts if a captcha or challenge appears so you can solve it in the real browser session.

A second script monitors the feed on a timer (about 30–35 seconds between reloads, randomized), compares against URLs you already know, and appends truly new jobs to new_jobs.json / new_jobs.csv while optionally posting Discord embeds through a webhook; successful posts are deduped in a local file so the same job is not announced twice. Running main.py chains the full scrape and then starts that monitor in one flow.

## Google Chrome Run Command 

Start and login then press enter in the terminal.

```& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
 --remote-debugging-port=9222 `
 --remote-debugging-address=0.0.0.0 `
 --user-data-dir="C:\temp\chrome-debug-profile"```