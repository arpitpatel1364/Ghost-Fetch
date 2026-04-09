#!/usr/bin/env python3
"""
Salone del Mobile 2026 — Fast Parallel Scraper (4 Workers)
===========================================================
- 4 headless browser instances running in parallel (optimised for i5 CPU)
- Pages split evenly across all 4 workers
- Output: salone_exhibitors_2026.csv  (name, country, website)
- Resume support via scraper_progress.json

INSTALL:
    pip install playwright
    playwright install chromium

RUN:
    python salone_scraper_fast.py              # all 109 pages
    python salone_scraper_fast.py --pages 4   # test 4 pages
    python salone_scraper_fast.py --resume     # resume after stop
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
WORKERS       = 4

# ── CSS Selectors ─────────────────────────────────────────────────────────────
SEL_NAME    = "p.css-15v3ejs"
SEL_COUNTRY = "p.css-1ncgp7i"
SEL_WEBSITE = "span.css-1dwin23"
SEL_COOKIE  = "#onetrust-accept-btn-handler"

# ── Thread-safe CSV writer ────────────────────────────────────────────────────
csv_lock = asyncio.Lock()

async def write_rows(rows: List[Dict], write_header: bool) -> None:
    async with csv_lock:
        with open(OUTPUT_CSV, "w" if write_header else "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "country", "website"])
            if write_header:
                w.writeheader()
            w.writerows(rows)

# ── Progress tracking (shared across workers) ─────────────────────────────────
progress_lock = asyncio.Lock()

async def save_progress(completed: set) -> None:
    async with progress_lock:
        Path(PROGRESS_FILE).write_text(
            json.dumps({"completed_pages": sorted(completed), "at": datetime.now().isoformat()})
        )

def load_progress() -> set:
    if Path(PROGRESS_FILE).exists():
        data = json.loads(Path(PROGRESS_FILE).read_text())
        return set(data.get("completed_pages", []))
    return set()

# ── Cookie dismiss ────────────────────────────────────────────────────────────
async def dismiss_cookies(page: Any) -> None:
    try:
        btn = page.locator(SEL_COOKIE)
        if await btn.is_visible(timeout=1500):
            await btn.click()
            await asyncio.sleep(0.4)
    except Exception:
        try:
            await page.evaluate("""() => {
                const sdk = document.getElementById('onetrust-consent-sdk');
                if (sdk) sdk.remove();
            }""")
        except Exception:
            pass

# ── Scrape a single listing page ──────────────────────────────────────────────
async def scrape_page(page: Any, page_num: int, worker_id: int) -> List[Dict]:
    url = BASE_URL.format(p=page_num)
    tag = f"[W{worker_id}|P{page_num}]"

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector(SEL_NAME, timeout=15000)
    except Exception as e:
        print(f"{tag} ERROR loading page: {e}")
        return []

    await dismiss_cookies(page)

    # Grab all names + countries in one JS call
    companies = await page.evaluate(f"""() => {{
        const names     = [...document.querySelectorAll('{SEL_NAME}')];
        const countries = [...document.querySelectorAll('{SEL_COUNTRY}')];
        return names.map((el, i) => ({{
            name:    el.innerText.trim(),
            country: countries[i] ? countries[i].innerText.trim() : ''
        }}));
    }}""")

    print(f"{tag} {len(companies)} companies found")
    rows = []

    for idx, company in enumerate(companies):
        name    = company["name"]
        country = company["country"]
        website = "no websites"

        # Click the company name
        try:
            await dismiss_cookies(page)
            els = await page.query_selector_all(SEL_NAME)
            clicked = False
            for el in els:
                if (await el.inner_text()).strip() == name:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    clicked = True
                    break

            if not clicked:
                print(f"{tag} [{idx+1}] SKIP (not found): {name}")
                rows.append({"name": name, "country": country, "website": website})
                continue

        except Exception as e:
            print(f"{tag} [{idx+1}] ERROR clicking {name}: {e}")
            rows.append({"name": name, "country": country, "website": website})
            continue

        # Wait for detail page to load
        await asyncio.sleep(1.2)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

        # Grab website URL
        try:
            await page.wait_for_selector(SEL_WEBSITE, timeout=5000)
            spans = await page.query_selector_all(SEL_WEBSITE)
            for span in spans:
                text = (await span.inner_text()).strip()
                if "www." in text or "http" in text:
                    website = text
                    break
        except Exception:
            pass  # stays "no websites"

        print(f"{tag} [{idx+1}/{len(companies)}] {name} → {website}")
        rows.append({"name": name, "country": country, "website": website})

        # Return to listing page
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector(SEL_NAME, timeout=12000)
            await asyncio.sleep(random.uniform(0.5, 1.0))  # polite delay
        except Exception as e:
            print(f"{tag} WARNING returning to listing: {e}")
            try:
                await page.goto(url, wait_until="load", timeout=60000)
                await page.wait_for_selector(SEL_NAME, timeout=12000)
            except Exception as e2:
                print(f"{tag} ERROR could not recover listing: {e2}")

    return rows


# ── Worker: owns one browser, processes its page slice ───────────────────────
async def worker(pw, page_range: List[int], worker_id: int,
                 completed: set, header_written: asyncio.Event) -> int:

    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page  = await ctx.new_page()
    total = 0

    for pnum in page_range:
        if pnum in completed:
            print(f"[W{worker_id}|P{pnum}] Already done, skipping.")
            continue

        rows = await scrape_page(page, pnum, worker_id)

        if rows:
            need_header = not header_written.is_set() and not Path(OUTPUT_CSV).exists()
            if need_header:
                header_written.set()
            await write_rows(rows, need_header)
            total += len(rows)

        completed.add(pnum)
        await save_progress(completed)
        print(f"[W{worker_id}|P{pnum}] ✓ complete | worker subtotal: {total}")

    await browser.close()
    return total


# ── Split pages as evenly as possible across N workers ───────────────────────
def split_pages(pages: List[int], n: int) -> List[List[int]]:
    k, rem = divmod(len(pages), n)
    chunks, start = [], 0
    for i in range(n):
        size = k + (1 if i < rem else 0)
        chunks.append(pages[start:start + size])
        start += size
    return chunks


# ── Main ──────────────────────────────────────────────────────────────────────
async def run(pages_to_scrape: List[int], completed: set) -> None:
    splits = split_pages(pages_to_scrape, WORKERS)

    header_written = asyncio.Event()
    if Path(OUTPUT_CSV).exists():
        header_written.set()

    print(f"\nPage distribution across {WORKERS} workers:")
    for i, chunk in enumerate(splits, 1):
        if chunk:
            print(f"  Worker {i}: pages {chunk[0]}–{chunk[-1]} ({len(chunk)} pages)")
    print()

    async with async_playwright() as pw:
        results = await asyncio.gather(*[
            worker(pw, splits[i], worker_id=i+1,
                   completed=completed, header_written=header_written)
            for i in range(WORKERS)
        ])

    total = sum(results)
    print(f"\n{'='*60}")
    print(f"✅  DONE! {total} companies saved to '{OUTPUT_CSV}'")
    print(f"{'='*60}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages",  type=int, default=TOTAL_PAGES, help="Number of pages to scrape")
    ap.add_argument("--start",  type=int, default=1,           help="Start page number")
    ap.add_argument("--resume", action="store_true",           help="Skip already-completed pages")
    args = ap.parse_args()

    completed = load_progress() if args.resume else set()

    start     = args.start
    end       = min(start + args.pages - 1, TOTAL_PAGES)
    all_pages = list(range(start, end + 1))
    remaining = [p for p in all_pages if p not in completed]

    if not remaining:
        sys.exit("✅ All pages already completed!")

    print(f"Salone del Mobile 2026 — Fast Scraper")
    print(f"Pages   : {start} → {end}  ({len(remaining)} remaining)")
    print(f"Workers : {WORKERS} parallel browsers (i5 optimised)")
    print(f"Output  : {OUTPUT_CSV}")

    asyncio.run(run(remaining, completed))
