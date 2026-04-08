# Ghost-Fetch

> **GhostFetch** is a silent, headless web automation tool built with Playwright and Python. It navigates paginated directories, simulates human interactions like scrolling and clicking, extracts structured data, and exports clean CSV output. Features smart retry logic, resume support, and rate-limit-safe delays for reliable long-run scraping.

---

## Output

| Column | Description |
|--------|-------------|
| `name` | Company / exhibitor name |
| `country` | Country of origin |
| `website` | Official website (`no websites` if none listed) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        GhostFetch                               │
│                                                                 │
│   ┌─────────────┐                                               │
│   │ Entry Point │  python ghostfetch.py [--pages] [--resume]    │
│   └──────┬──────┘                                               │
│          │                                                      │
│          ▼                                                      │
│   ┌─────────────────────────────────────────────┐               │
│   │             Progress Manager                │               │
│   │   Load scraper_progress.json (if --resume)  │               │
│   │   Track completed pages → save after each   │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│                      ▼                                          │
│   ┌─────────────────────────────────────────────┐               │
│   │           Playwright Browser                │               │
│   │   • Headless Chromium                       │               │
│   │   • Windows User-Agent header               │               │
│   │   • en-US locale + Accept-Language          │               │
│   │   • 1280×800 viewport                       │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│          ┌───────────▼───────────┐                              │
│          │    For each page      │ ◄─────────────┐              │
│          └───────────┬───────────┘               │              │
│                      │                           │              │
│                      ▼                           │              │
│   ┌─────────────────────────────────────────────┤               │
│   │           goto_with_retry()                 │               │
│   │   • Load listing URL                        │               │
│   │   • Retry up to 4× with exponential backoff │               │
│   │   • Wait for SEL_NAME selector              │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│                      ▼                                          │
│   ┌─────────────────────────────────────────────┐               │
│   │           dismiss_cookies()                 │               │
│   │   • Click accept button (OneTrust)          │               │
│   │   • Fallback: remove overlay via JS         │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│                      ▼                                          │
│   ┌─────────────────────────────────────────────┐               │
│   │         Bulk JS Read (one call)             │               │
│   │   • querySelectorAll(SEL_NAME)              │               │
│   │   • querySelectorAll(SEL_COUNTRY)           │               │
│   │   • Returns list of {name, country}         │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│          ┌───────────▼────────────┐                             │
│          │  For each company      │ ◄──────────────┐            │
│          └───────────┬────────────┘                │            │
│                      │                             │            │
│                      ▼                             │            │
│   ┌─────────────────────────────────────────────┐  │            │
│   │         is_listing_alive() check            │  │            │
│   │   • Query SEL_NAME elements                 │  │            │
│   │   • If stale → reload listing               │  │            │
│   └──────────────────┬──────────────────────────┘  │            │
│                      │                             │            │
│                      ▼                             │            │
│   ┌─────────────────────────────────────────────┐  │            │
│   │      Click company name (human-like)        │  │            │
│   │   • scroll_into_view_if_needed()            │  │            │
│   │   • Random delay 0.3–0.6s before click      │  │            │
│   │   • Match by innerText                      │  │            │
│   └──────────────────┬──────────────────────────┘  │            │
│                      │                             │            │
│                      ▼                             │            │
│   ┌─────────────────────────────────────────────┐  │            │
│   │         Detail page loaded                  │  │            │
│   │   • Random wait 2.0–3.0s                    │  │            │
│   │   • wait_for_load_state(domcontentloaded)   │  │            │
│   │   • wait_for_selector(SEL_WEBSITE)          │  │            │
│   │   • Extract website text (www. / http)      │  │            │
│   └──────────────────┬──────────────────────────┘  │            │
│                      │                             │            │
│                      ▼                             │            │
│   ┌─────────────────────────────────────────────┐  │            │
│   │       Append row  {name, country, website}  │  │            │
│   └──────────────────┬──────────────────────────┘  │            │
│                      │                             │            │
│                      ▼                             │            │
│    ┌─────────────────────────────────────────────┐ │            │
│    │         goto_with_retry() → back            │ │            │
│    │   • Reload listing URL                      │ │            │
│    │   • Random delay 1.0–2.5s after return      ├─┘            │
│    └──────────────────┬──────────────────────────┘              │
│                       │  (all companies done)                   │
│                      ▼                                          │
│   ┌─────────────────────────────────────────────┐               │
│   │             Write rows → CSV                │               │
│   │       salone_exhibitors_2026.csv            │               │
│   └──────────────────┬──────────────────────────┘               │
│                      │                                          │
│                      ▼                                          │
│   ┌─────────────────────────────────────────────┐               │
│   │      Save progress → JSON checkpoint        │               │
│   │   scraper_progress.json                     │               │
│   │   Random delay 3.0–6.0s before next page    ├───────────────►│
│   └─────────────────────────────────────────────┘  (next page)  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## & Retry & Resilience Flow

```
  go to(url)
      │
      ▼
  Success? ──YES──► continue
      │
      NO
      ▼
  attempt × RETRY_WAIT_S + jitter
      │
      ▼
  attempt < MAX_RETRIES? ──YES──► retry
      │
      NO
      ▼
  Skip page, log error, move on
```

---

##Configuration

All settings are at the top of the script — no config file needed:

```python
TOTAL_PAGES   = 109       # total pages to scrape
MAX_RETRIES   = 4         # retries on timeout
RETRY_WAIT_S  = 10        # base wait between retries (× attempt number)

# CSS selectors
SEL_NAME    = "p.css-15v3ejs"
SEL_COUNTRY = "p.css-1ncgp7i"
SEL_WEBSITE = "span.css-1dwin23"
```

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/GhostFetch.git
cd GhostFetch

# 2. Install dependencies
pip install playwright

# 3. Install browser engine
playwright install chromium
```

---

## 🖥️ Usage

```bash
# Run all pages
python ghostfetch.py

# Test with 2 pages first
python ghostfetch.py --pages 2

# Start from a specific page
python ghostfetch.py --start 10

# Resume after a stop or crash
python ghostfetch.py --resume
```

---

## File Structure

```
GhostFetch/
├── ghostfetch.py              # main scraper script
├── salone_exhibitors_2026.csv # output data (auto-created)
├── scraper_progress.json      # checkpoint file (auto-created)
└── README.md
```

---

## Anti-Detection Measures

| Technique | Detail |
|-----------|--------|
| Realistic User-Agent | Windows Chrome 124 string |
| Human-like delays | Random waits between every action |
| Page-level cooldown | 3–6s random pause between pages |
| Cookie handling | Auto-dismisses OneTrust popups |
| Single browser | One request at a time, no parallel hammering |
| Stale page detection | Reloads listing if DOM goes blank mid-run |

---

## Requirements

- Python 3.8+
- `playwright` (`pip install playwright`)
- Chromium (`playwright install chromium`)

---

## License

MIT — use freely, modify as needed.

---

*Built with [Playwright](https://playwright.dev/) · Python 3*
