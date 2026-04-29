import sys

# On Windows, force the Proactor event loop policy so asyncio subprocesses work.
# Playwright needs to spawn a Chromium subprocess; SelectorEventLoop on Windows
# raises NotImplementedError when you try. Must run before any event loop is created.
if sys.platform == "win32":
    import asyncio as _asyncio_init
    _asyncio_init.set_event_loop_policy(_asyncio_init.WindowsProactorEventLoopPolicy())

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from db import (
    delete_site_files,
    get_registry,
    get_site_db,
    init,
    init_site_db,
    reset_interrupted_jobs,
    site_db_path,
    slug_for,
    unique_slug,
)
from scraper import crawl_site, shutdown_browser

BASE_DIR = Path(__file__).parent

# Strong refs so background tasks aren't GC'd; cancel flags keyed by site id.
_active_jobs: set[asyncio.Task] = set()
_cancel_flags: dict[int, bool] = {}

MAX_PARALLEL_JOBS = 4
_job_slots = asyncio.Semaphore(MAX_PARALLEL_JOBS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init()
    n = reset_interrupted_jobs()
    if n:
        print(f"[startup] reset {n} interrupted job(s)")
    yield
    for sid in list(_cancel_flags):
        _cancel_flags[sid] = True
    if _active_jobs:
        await asyncio.gather(*_active_jobs, return_exceptions=True)
    await shutdown_browser()


app = FastAPI(title="PDF Inventory", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/sites")
def list_sites():
    with get_registry() as conn:
        rows = conn.execute(
            "SELECT * FROM sites ORDER BY COALESCE(last_scraped,'') DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/pdfs")
def list_pdfs(site_id: int | None = None):
    """Return PDFs from one site, or aggregated from all sites if site_id is omitted."""
    with get_registry() as conn:
        if site_id is None:
            sites = conn.execute("SELECT id, url, name, db_path FROM sites").fetchall()
        else:
            sites = conn.execute(
                "SELECT id, url, name, db_path FROM sites WHERE id = ?", (site_id,)
            ).fetchall()
            if not sites:
                raise HTTPException(404, "Site not found")

    out: list[dict] = []
    for s in sites:
        if not Path(s["db_path"]).exists():
            continue
        with get_site_db(s["db_path"]) as conn:
            rows = conn.execute("SELECT * FROM pdfs").fetchall()
        for p in rows:
            row = dict(p)
            row["source_url"] = s["url"]
            row["site_id"] = s["id"]
            row["site_name"] = s["name"]
            out.append(row)
    out.sort(key=lambda r: r.get("scraped_at") or "", reverse=True)
    return out


@app.post("/api/scrape")
async def scrape(
    url: str = Form(...),
    name: str = Form(""),
    mode: str = Form("crawl"),
    max_pages: int = Form(100),
    max_depth: int = Form(3),
    use_browser: bool = Form(False),
):
    return _enqueue_one(
        url, mode, max_pages, max_depth,
        name=name.strip() or None, use_browser=use_browser,
    )


@app.post("/api/scrape/bulk")
async def scrape_bulk(
    urls: str = Form(...),
    mode: str = Form("crawl"),
    max_pages: int = Form(100),
    max_depth: int = Form(3),
    use_browser: bool = Form(False),
):
    targets: list[tuple[str, str | None]] = []
    for line in urls.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            left, right = line.split("|", 1)
            # Accept "Name | URL" or "URL | Name" — whichever side looks like a URL is the URL.
            left, right = left.strip(), right.strip()
            if right.startswith(("http://", "https://")):
                targets.append((right, left or None))
            else:
                targets.append((left, right or None))
        else:
            targets.append((line, None))
    if not targets:
        raise HTTPException(400, "No URLs provided")
    results: list[dict] = []
    errors: list[dict] = []
    for u, n in targets:
        try:
            results.append(_enqueue_one(
                u, mode, max_pages, max_depth, name=n, use_browser=use_browser,
            ))
        except HTTPException as e:
            errors.append({"url": u, "error": e.detail})
    return {"enqueued": len(results), "jobs": results, "errors": errors}


def _enqueue_one(
    url: str, mode: str, max_pages: int, max_depth: int,
    *, name: str | None = None, use_browser: bool = False,
) -> dict:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, f"URL must start with http:// or https:// — got {url!r}")
    if mode not in ("page", "crawl"):
        raise HTTPException(400, "mode must be 'page' or 'crawl'")
    max_pages = max(1, min(max_pages, 5000))
    max_depth = max(1, min(max_depth, 10))

    with get_registry() as conn:
        existing = conn.execute(
            "SELECT id, db_path FROM sites WHERE url = ?", (url,)
        ).fetchone()
        if existing:
            site_id = existing["id"]
            db_path = existing["db_path"]
            if name is not None:
                conn.execute("UPDATE sites SET name=? WHERE id=?", (name, site_id))
            conn.execute(
                """UPDATE sites SET status='pending', error=NULL, mode=?, use_browser=?,
                       pages_visited=0, pages_total=0, pdfs_total=0 WHERE id=?""",
                (mode, int(use_browser), site_id),
            )
        else:
            base = slug_for(url)
            slug = unique_slug(conn, base)
            db_path = site_db_path(slug)
            cur = conn.execute(
                """INSERT INTO sites (url, name, slug, db_path, status, mode, use_browser)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (url, name, slug, db_path, mode, int(use_browser)),
            )
            site_id = cur.lastrowid

    init_site_db(db_path)  # idempotent
    _cancel_flags[site_id] = False
    _start_job(_run_scrape(site_id, db_path, url, mode, max_pages, max_depth, use_browser))
    return {"site_id": site_id, "url": url, "status": "pending"}


@app.patch("/api/sites/{site_id}")
async def update_site(site_id: int, name: str = Form("")):
    """Update a site's custom display name. Empty string clears it."""
    cleaned = name.strip() or None
    with get_registry() as conn:
        cur = conn.execute(
            "UPDATE sites SET name = ? WHERE id = ?", (cleaned, site_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Site not found")
    return {"ok": True, "name": cleaned}


@app.post("/api/sites/{site_id}/cancel")
def cancel_site(site_id: int):
    if site_id not in _cancel_flags:
        raise HTTPException(404, "No active job for that site")
    _cancel_flags[site_id] = True
    return {"ok": True}


@app.post("/api/sites/{site_id}/continue")
async def continue_site(
    site_id: int,
    max_pages: int = Form(100),
    max_depth: int = Form(3),
):
    """Resume crawling a site from its saved frontier (URLs queued but not visited)."""
    with get_registry() as conn:
        row = conn.execute(
            "SELECT url, mode, db_path, use_browser FROM sites WHERE id = ?", (site_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Site not found")

    max_pages = max(1, min(max_pages, 5000))
    max_depth = max(1, min(max_depth, 10))

    with get_registry() as conn:
        conn.execute(
            """UPDATE sites SET status='pending', error=NULL,
                   pages_visited=0, pages_total=0, pdfs_total=0 WHERE id=?""",
            (site_id,),
        )

    _cancel_flags[site_id] = False
    _start_job(_run_scrape(
        site_id, row["db_path"], row["url"], row["mode"] or "crawl",
        max_pages, max_depth, bool(row["use_browser"]),
        continue_mode=True,
    ))
    return {"site_id": site_id, "status": "pending"}


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: int):
    _cancel_flags[site_id] = True  # stop in-flight work first
    with get_registry() as conn:
        site = conn.execute(
            "SELECT db_path FROM sites WHERE id = ?", (site_id,)
        ).fetchone()
        conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    if site:
        delete_site_files(site["db_path"])
    return {"ok": True}


@app.delete("/api/sites/{site_id}/pdfs/{pdf_id}")
def delete_pdf(site_id: int, pdf_id: int):
    with get_registry() as conn:
        site = conn.execute(
            "SELECT db_path FROM sites WHERE id = ?", (site_id,)
        ).fetchone()
    if not site:
        raise HTTPException(404, "Site not found")
    with get_site_db(site["db_path"]) as conn:
        conn.execute("DELETE FROM pdfs WHERE id = ?", (pdf_id,))
    return {"ok": True}


def _start_job(coro):
    t = asyncio.create_task(coro)
    _active_jobs.add(t)
    t.add_done_callback(_active_jobs.discard)


async def _run_scrape(
    site_id, db_path, url, mode, max_pages, max_depth, use_browser,
    *, continue_mode: bool = False,
):
    last_progress = 0.0

    async def on_progress(state):
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress < 0.4:
            return
        last_progress = now
        await asyncio.to_thread(_update_progress, site_id, db_path, state)

    async def on_pdf(pdf_url, meta):
        await asyncio.to_thread(_insert_pdf, db_path, pdf_url, meta)

    async def on_page_queued(u, d):
        await asyncio.to_thread(_record_queued, db_path, u, d)

    async def on_page_visited(u):
        await asyncio.to_thread(_record_visited, db_path, u)

    def cancel_check() -> bool:
        return _cancel_flags.get(site_id, False)

    async with _job_slots:
        if cancel_check():
            await asyncio.to_thread(_finish, site_id, db_path, "cancelled", "Cancelled before start")
            _cancel_flags.pop(site_id, None)
            return
        await asyncio.to_thread(_set_status, site_id, "running")

        # Continue mode: load prior crawl state from DB; otherwise start clean.
        seen_pages_init = None
        seen_pdfs_init = None
        seed_pages = None
        if continue_mode:
            seen_pages_init, seed_pages, seen_pdfs_init = await asyncio.to_thread(
                _load_crawl_state, db_path
            )
        else:
            await asyncio.to_thread(_clear_crawl_state, db_path)

        try:
            result = await crawl_site(
                url,
                crawl=(mode == "crawl"),
                max_pages=max_pages,
                max_depth=max_depth,
                use_browser=use_browser,
                on_pdf=on_pdf,
                on_progress=on_progress,
                cancel_check=cancel_check,
                seen_pages_init=seen_pages_init,
                seen_pdfs_init=seen_pdfs_init,
                seed_pages=seed_pages,
                on_page_queued=on_page_queued,
                on_page_visited=on_page_visited,
            )
        except Exception as e:
            await asyncio.to_thread(_finish, site_id, db_path, "error", str(e))
            _cancel_flags.pop(site_id, None)
            return

        await asyncio.to_thread(
            _update_progress,
            site_id,
            db_path,
            {
                "pages_visited": result["pages_visited"],
                "pages_total": result["pages_visited"],
                "pdfs_found": result["pdfs_found"],
                "pdfs_done": result["pdfs_done"],
            },
        )
        if result.get("cancelled"):
            await asyncio.to_thread(_finish, site_id, db_path, "cancelled", "Cancelled by user")
        else:
            await asyncio.to_thread(_finish, site_id, db_path, "ok", None)
    _cancel_flags.pop(site_id, None)


def _set_status(site_id, status):
    with get_registry() as conn:
        conn.execute("UPDATE sites SET status=? WHERE id=?", (status, site_id))


def _update_progress(site_id, db_path, state):
    pdf_count = _count_pdfs(db_path)
    with get_registry() as conn:
        conn.execute(
            """UPDATE sites
               SET pages_visited=?, pages_total=?, pdfs_total=?, pdf_count=?
               WHERE id=?""",
            (
                state.get("pages_visited", 0),
                state.get("pages_total", 0),
                state.get("pdfs_found", 0),
                pdf_count,
                site_id,
            ),
        )


def _finish(site_id, db_path, status, error):
    pdf_count = _count_pdfs(db_path)
    with get_registry() as conn:
        conn.execute(
            "UPDATE sites SET status=?, last_scraped=?, error=?, pdf_count=? WHERE id=?",
            (status, _now(), error, pdf_count, site_id),
        )


def _count_pdfs(db_path) -> int:
    if not Path(db_path).exists():
        return 0
    with get_site_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM pdfs").fetchone()[0]


def _load_crawl_state(db_path):
    """Return (visited_urls, frontier_pairs, seen_pdf_urls) from a site's DB."""
    visited_urls: set[str] = set()
    frontier: list[tuple[str, int]] = []
    seen_pdf_urls: set[str] = set()
    if not Path(db_path).exists():
        return visited_urls, frontier, seen_pdf_urls
    with get_site_db(db_path) as conn:
        for row in conn.execute("SELECT url, depth, visited FROM crawl_state"):
            visited_urls.add(row["url"])
            if not row["visited"]:
                frontier.append((row["url"], row["depth"]))
        for row in conn.execute("SELECT url FROM pdfs"):
            seen_pdf_urls.add(row["url"])
    return visited_urls, frontier, seen_pdf_urls


def _clear_crawl_state(db_path):
    if not Path(db_path).exists():
        return
    with get_site_db(db_path) as conn:
        conn.execute("DELETE FROM crawl_state")


def _record_queued(db_path, url, depth):
    with get_site_db(db_path) as conn:
        conn.execute(
            """INSERT INTO crawl_state (url, depth, visited, last_seen)
               VALUES (?, ?, 0, ?)
               ON CONFLICT(url) DO NOTHING""",
            (url, depth, _now()),
        )


def _record_visited(db_path, url):
    with get_site_db(db_path) as conn:
        conn.execute(
            """INSERT INTO crawl_state (url, depth, visited, last_seen)
               VALUES (?, 0, 1, ?)
               ON CONFLICT(url) DO UPDATE SET
                   visited = 1,
                   last_seen = excluded.last_seen""",
            (url, _now()),
        )


def _insert_pdf(db_path, pdf_url, meta):
    with get_site_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pdfs (url, title, author, subject, creator, producer,
                              creation_date, mod_date, page_count, size_bytes,
                              tagged, pdf_ua, found_on, fetch_error, scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                author = excluded.author,
                subject = excluded.subject,
                creator = excluded.creator,
                producer = excluded.producer,
                creation_date = excluded.creation_date,
                mod_date = excluded.mod_date,
                page_count = excluded.page_count,
                size_bytes = excluded.size_bytes,
                tagged = excluded.tagged,
                pdf_ua = excluded.pdf_ua,
                found_on = excluded.found_on,
                fetch_error = excluded.fetch_error,
                scraped_at = excluded.scraped_at
            """,
            (
                pdf_url,
                meta.get("title"),
                meta.get("author"),
                meta.get("subject"),
                meta.get("creator"),
                meta.get("producer"),
                meta.get("creation_date"),
                meta.get("mod_date"),
                meta.get("page_count"),
                meta.get("size_bytes"),
                meta.get("tagged"),
                meta.get("pdf_ua"),
                meta.get("found_on"),
                meta.get("fetch_error"),
                _now(),
            ),
        )
