#!/usr/bin/env python3
"""
Salone del Mobile 2026 — Scraper (Single Browser, Reliable)
============================================================
- One browser, one page at a time — no rate limiting issues
- Retries with backoff on any timeout
- Saves progress after every page — safe to resume anytime
- Output: salone_exhibitors_2026.csv  (name, country, website)

INSTALL:
    pip install playwright
    playwright install chromium

RUN:
    python Ghost-Fetch.py            # all 109 pages
    python Ghost-Fetch.py --pages 2  # test 2 pages
    python Ghost-Fetch.py --resume   # resume after any stop
"""

import asyncio, csv, json, argparse, sys, random
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("Run:  pip install playwright && playwright install chromium")

# ── Config ────────────────────────────────────────────────────────────────────
TOTAL_PAGES   = 109
OUTPUT_CSV    = "salone_exhibitors_2026.csv"
PROGRESS_FILE = "scraper_progress.json"
BASE_URL      = "PUT YOUR WEB URL HERE TO SCRAPE"
MAX_RETRIES   = 4
RETRY_WAIT_S  = 10

# ── CSS Selectors ─────────────────────────────────────────────────────────────
SEL_NAME    = "p.css-15v3ejs"
SEL_COUNTRY = "p.css-1ncgp7i"
SEL_WEBSITE = "span.css-1dwin23"
SEL_COOKIE  = "#onetrust-accept-btn-handler"

# ── CSV ───────────────────────────────────────────────────────────────────────
def append_rows(rows: List[Dict], write_header: bool) -> None:
    with open(OUTPUT_CSV, "w" if write_header else "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "country", "website"])
        if write_header:
            w.writeheader()
        w.writerows(rows)

# ── Progress ──────────────────────────────────────────────────────────────────
def save_progress(last_page: int) -> None:
    Path(PROGRESS_FILE).write_text(
        json.dumps({"last_page": last_page, "at": datetime.now().isoformat()})
    )

def load_progress() -> int:
    if Path(PROGRESS_FILE).exists():
        return json.loads(Path(PROGRESS_FILE).read_text()).get("last_page", 0)
    return 0

# ── Dismiss cookie popup ──────────────────────────────────────────────────────
async def dismiss_cookies(page: Any) -> None:
    try:
        btn = page.locator(SEL_COOKIE)
        if await btn.is_visible(timeout=2000):
            await btn.click()
            await asyncio.sleep(0.5)
            return
    except Exception:
        pass
    try:
        await page.evaluate("""() => {
            document.getElementById('onetrust-consent-sdk')?.remove();
            document.querySelector('.onetrust-pc-dark-filter')?.remove();
        }""")
    except Exception:
        pass

# ── Load a page reliably with retries ────────────────────────────────────────
async def load_listing(page: Any, url: str, page_num: int) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector(SEL_NAME, timeout=15000)
            return True
        except Exception as e:
            wait = RETRY_WAIT_S * attempt + random.uniform(2, 5)
            print(f"  [P{page_num}] Load attempt {attempt}/{MAX_RETRIES} failed — waiting {wait:.0f}s")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)
    print(f"  [P{page_num}] Could not load after {MAX_RETRIES} attempts, skipping page.")
    return False

# ── Scrape one full listing page ──────────────────────────────────────────────
async def scrape_page(page: Any, page_num: int) -> List[Dict]:
    url = BASE_URL.format(p=page_num)
    print(f"\n{'─'*55}")
    print(f"  Page {page_num}/{TOTAL_PAGES}")
    print(f"{'─'*55}")

    if not await load_listing(page, url, page_num):
        return []

    await dismiss_cookies(page)

    # Read all names + countries in a single JS call
    companies = await page.evaluate(f"""() => {{
        const names     = [...document.querySelectorAll('{SEL_NAME}')];
        const countries = [...document.querySelectorAll('{SEL_COUNTRY}')];
        return names.map((el, i) => ({{
            name:    el.innerText.trim(),
            country: countries[i] ? countries[i].innerText.trim() : ''
        }}));
    }}""")

    print(f"  Found {len(companies)} companies\n")
    rows = []

    for idx, company in enumerate(companies):
        name    = company["name"]
        country = company["country"]
        website = "no websites"

        print(f"  [{idx+1}/{len(companies)}] {name}")

        # ── Verify listing is still alive ─────────────────────────────────────
        alive = await page.query_selector_all(SEL_NAME)
        if not alive:
            print(f"    Page went stale — reloading...")
            if not await load_listing(page, url, page_num):
                rows.append({"name": name, "country": country, "website": website})
                continue
            await dismiss_cookies(page)

        # ── Click company name ────────────────────────────────────────────────
        clicked = False
        try:
            await dismiss_cookies(page)
            for el in await page.query_selector_all(SEL_NAME):
                if (await el.inner_text()).strip() == name:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.3, 0.6))
                    await el.click()
                    clicked = True
                    break
        except Exception as e:
            print(f"    Click error: {e}")

        if not clicked:
            print(f"    Could not click — skipping")
            rows.append({"name": name, "country": country, "website": website})
            await load_listing(page, url, page_num)
            continue

        # ── Wait for detail page ──────────────────────────────────────────────
        await asyncio.sleep(random.uniform(2.0, 3.0))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        # ── Grab website ──────────────────────────────────────────────────────
        try:
            await page.wait_for_selector(SEL_WEBSITE, timeout=7000)
            for span in await page.query_selector_all(SEL_WEBSITE):
                text = (await span.inner_text()).strip()
                if "www." in text or "http" in text:
                    website = text
                    break
        except Exception:
            pass

        print(f"    → {website}")
        rows.append({"name": name, "country": country, "website": website})

        # ── Return to listing ─────────────────────────────────────────────────
        await asyncio.sleep(random.uniform(1.5, 2.5))
        if not await load_listing(page, url, page_num):
            print(f"    Could not return to listing — stopping page early")
            break
        await dismiss_cookies(page)
        await asyncio.sleep(random.uniform(1.0, 1.8))

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
async def run(start: int, end: int) -> None:
    write_header = not Path(OUTPUT_CSV).exists()
    total = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await ctx.new_page()

        for pnum in range(start, end + 1):
            rows = await scrape_page(page, pnum)

            if rows:
                append_rows(rows, write_header)
                write_header = False
                total += len(rows)

            save_progress(pnum)

            done_pct = round((pnum - start + 1) / (end - start + 1) * 100)
            print(f"\n  ✓ Page {pnum} saved | Total so far: {total} | {done_pct}% done")

            # Polite pause between pages
            if pnum < end:
                wait = random.uniform(3.0, 6.0)
                print(f"  Waiting {wait:.1f}s before next page...")
                await asyncio.sleep(wait)

        await browser.close()

    print(f"\n{'='*55}")
    print(f"✅  Done! {total} companies saved to '{OUTPUT_CSV}'")
    print(f"{'='*55}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages",  type=int, default=TOTAL_PAGES, help="Max pages to scrape")
    ap.add_argument("--start",  type=int, default=1,           help="Start page")
    ap.add_argument("--resume", action="store_true",           help="Resume from last saved page")
    args = ap.parse_args()

    start = args.start
    if args.resume:
        start = load_progress() + 1
        print(f"Resuming from page {start}")

    end = min(start + args.pages - 1, TOTAL_PAGES)

    if start > end:
        sys.exit("✅ Already complete!")

    print(f"Salone del Mobile 2026 — Single Browser Scraper")
    print(f"Pages  : {start} → {end}")
    print(f"Output : {OUTPUT_CSV}\n")

    asyncio.run(run(start, end))
