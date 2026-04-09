#!/usr/bin/env python3
"""
Salone del Mobile 2026 — 2-Browser Safe Scraper
================================================
- 2 headless browsers, staggered 15s apart so server never sees a spike
- Human-like random delays between every request
- Smart retry with backoff on any timeout
- Stale-page check before every company click
- Output: salone_exhibitors_2026.csv  (name, country, website)
- Resume support via scraper_progress.json

INSTALL:
    pip install playwright
    playwright install chromium

RUN:
    python salone_scraper.py              # all 109 pages
    python salone_scraper.py --pages 4   # test 4 pages (2 per worker)
    python salone_scraper.py --resume    # resume after stop
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
TOTAL_PAGES      = 109
OUTPUT_CSV       = "salone_exhibitors_2026.csv"
PROGRESS_FILE    = "scraper_progress.json"
BASE_URL         = "PUT YOUR WEB URL HERE TO SCRAPE"
WORKERS          = 2
WORKER_STAGGER_S = 15     # Worker 2 waits 15s before starting
MAX_RETRIES      = 4      # retries per page load
RETRY_WAIT_S     = 12     # base wait between retries (multiplied by attempt)

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

# ── Progress ──────────────────────────────────────────────────────────────────
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

# ── Human-like random delay ───────────────────────────────────────────────────
async def human_delay(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))

# ── Cookie dismiss ────────────────────────────────────────────────────────────
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
            const sdk = document.getElementById('onetrust-consent-sdk');
            if (sdk) sdk.remove();
            const filter = document.querySelector('.onetrust-pc-dark-filter');
            if (filter) filter.remove();
        }""")
    except Exception:
        pass

# ── Goto with retry + exponential backoff ────────────────────────────────────
async def goto_with_retry(page: Any, url: str, tag: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector(SEL_NAME, timeout=15000)
            return True
        except Exception as e:
            wait = RETRY_WAIT_S * attempt + random.uniform(2, 6)
            print(f"{tag} Attempt {attempt}/{MAX_RETRIES} failed ({e.__class__.__name__})")
            if attempt < MAX_RETRIES:
                print(f"{tag} Waiting {wait:.1f}s before retry...")
                await asyncio.sleep(wait)
    print(f"{tag} GIVING UP on this page after {MAX_RETRIES} attempts.")
    return False

# ── Check if listing page is still alive (not stale/blank) ───────────────────
async def is_listing_alive(page: Any) -> bool:
    try:
        els = await page.query_selector_all(SEL_NAME)
        return len(els) > 0
    except Exception:
        return False

# ── Scrape a single listing page ──────────────────────────────────────────────
async def scrape_page(page: Any, page_num: int, worker_id: int) -> List[Dict]:
    url = BASE_URL.format(p=page_num)
    tag = f"[W{worker_id}|P{page_num}]"

    # Load listing with retry
    ok = await goto_with_retry(page, url, tag)
    if not ok:
        return []

    await dismiss_cookies(page)

    # Read all names + countries in one JS call
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

        # ── Ensure listing page is alive before clicking ──────────────────────
        if not await is_listing_alive(page):
            print(f"{tag} Page went stale before [{idx+1}], reloading...")
            ok = await goto_with_retry(page, url, tag)
            if not ok:
                rows.append({"name": name, "country": country, "website": website})
                continue
            await dismiss_cookies(page)

        # ── Click company name ────────────────────────────────────────────────
        try:
            await dismiss_cookies(page)
            els = await page.query_selector_all(SEL_NAME)
            clicked = False
            for el in els:
                if (await el.inner_text()).strip() == name:
                    await el.scroll_into_view_if_needed()
                    await human_delay(0.3, 0.7)   # feel human before click
                    await el.click()
                    clicked = True
                    break

            if not clicked:
                print(f"{tag} [{idx+1}] Not clickable: {name} — reloading listing")
                await goto_with_retry(page, url, tag)
                rows.append({"name": name, "country": country, "website": website})
                continue

        except Exception as e:
            print(f"{tag} [{idx+1}] Click error: {e}")
            rows.append({"name": name, "country": country, "website": website})
            continue

        # ── Wait for detail page ──────────────────────────────────────────────
        await human_delay(1.8, 3.0)   # variable wait = harder to detect as bot
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        # ── Grab website ──────────────────────────────────────────────────────
        try:
            await page.wait_for_selector(SEL_WEBSITE, timeout=6000)
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

        # ── Return to listing with retry ──────────────────────────────────────
        await human_delay(1.0, 2.0)   # pause before navigating back
        ok = await goto_with_retry(page, url, tag)
        if not ok:
            print(f"{tag} Could not return to listing — stopping this page early")
            break
        await dismiss_cookies(page)
        await human_delay(0.8, 1.5)   # settle before next company

    return rows


# ── Worker: owns one browser, processes its page slice ───────────────────────
async def worker(pw, page_range: List[int], worker_id: int,
                 completed: set, header_written: asyncio.Event) -> int:

    # Stagger worker 2 so both don't hammer server at startup
    if worker_id == 2:
        print(f"[W2] Waiting {WORKER_STAGGER_S}s before starting (stagger)...")
        await asyncio.sleep(WORKER_STAGGER_S)

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
        print(f"[W{worker_id}|P{pnum}] ✓ done | subtotal: {total}")

        # ── Delay between pages (not between companies) ───────────────────────
        await human_delay(3.0, 6.0)

    await browser.close()
    return total


# ── Split pages evenly ────────────────────────────────────────────────────────
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

    print(f"\nPage split across {WORKERS} workers:")
    for i, chunk in enumerate(splits, 1):
        if chunk:
            print(f"  Worker {i}: pages {chunk[0]}–{chunk[-1]} ({len(chunk)} pages)")
    print(f"  Worker 2 starts {WORKER_STAGGER_S}s after Worker 1\n")

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

    print(f"Salone del Mobile 2026 — Safe 2-Browser Scraper")
    print(f"Pages   : {start} → {end}  ({len(remaining)} remaining)")
    print(f"Workers : {WORKERS} browsers (staggered {WORKER_STAGGER_S}s apart)")
    print(f"Output  : {OUTPUT_CSV}")

    asyncio.run(run(remaining, completed))