import asyncio
import concurrent.futures
import io
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

USER_AGENT = "Mozilla/5.0 (compatible; pdf-inventory/1.0)"
TIMEOUT = 30.0
MAX_PDF_BYTES = 50 * 1024 * 1024
SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "#")

# Some frameworks (XSP/Domino especially) bootstrap their session via a JS redirect
# like `window.location.href = "...?SessionID=..."`. We can't run JS, but if the
# static page yields nothing useful, follow the redirect URL as a fallback.
JS_REDIRECT_RE = re.compile(
    r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']'
)

# Playwright lives entirely on a dedicated worker thread that uses the *sync* API.
# This avoids Python 3.14's asyncio-subprocess gap on Windows: the sync API manages
# its own internal event loop, so we don't depend on asyncio supporting subprocesses.
# All browser operations run sequentially on that one thread (slower but reliable).
_browser_state: dict = {"playwright": None, "browser": None, "context": None}
_browser_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_browser_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _browser_executor
    if _browser_executor is None:
        _browser_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )
    return _browser_executor


def _init_browser_sync():
    if _browser_state["context"] is not None:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright"
        ) from e
    pw = sync_playwright().start()
    _browser_state["playwright"] = pw
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception as e:
        msg = str(e).lower()
        if "executable doesn't exist" in msg or "playwright install" in msg:
            raise RuntimeError(
                "Chromium is not installed for Playwright. "
                "Run: python -m playwright install chromium"
            ) from e
        raise
    _browser_state["browser"] = browser
    _browser_state["context"] = browser.new_context(user_agent=USER_AGENT)
    _log("browser launched")


_EXPAND_BUTTON_TEXTS = ("Expand All", "Show All", "View All", "Load More", "Show More")
_NEXT_PAGE_SELECTORS = (
    '.xspPagerNav.xspNext a:not(.xspDisabled)',  # Domino/XSP pagers
    'a[title*="next page" i]',
    'a[aria-label*="next" i]',
    'a:has-text("Next ›")',
    'a:has-text("Next »")',
)
MAX_PAGINATION_CLICKS = 200  # safety: stops after this many "Next" clicks


def _fetch_with_browser_sync(url: str, timeout_ms: int = 30000):
    _init_browser_sync()
    ctx = _browser_state["context"]
    page = ctx.new_page()
    try:
        resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # Some frameworks only render data after a click on "Expand All" or similar.
        for label in _EXPAND_BUTTON_TEXTS:
            try:
                btn = page.locator(f'button:has-text("{label}")').first
                if btn.count() and btn.is_visible(timeout=500):
                    btn.click(timeout=2000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    _log(f"    browser: clicked '{label}' button")
                    break
            except Exception:
                pass
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Walk through pagination by repeatedly clicking "Next", accumulating each
        # page's HTML. We then concatenate everything so all links from every page
        # are visible to the link extractor.
        chunks = [page.content()]
        for i in range(MAX_PAGINATION_CLICKS):
            next_link = None
            for sel in _NEXT_PAGE_SELECTORS:
                try:
                    cand = page.locator(sel).first
                    if cand.count() and cand.is_visible(timeout=300):
                        next_link = cand
                        break
                except Exception:
                    continue
            if next_link is None:
                break
            try:
                next_link.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=8000)
                page.wait_for_timeout(400)
                new_html = page.content()
            except Exception as e:
                _log(f"    browser: pagination stopped after {i+1} clicks: {e}")
                break
            # Detect when "Next" no-ops (same content twice in a row) — last page.
            if new_html == chunks[-1]:
                break
            chunks.append(new_html)
        if len(chunks) > 1:
            _log(f"    browser: paginated through {len(chunks)} pages")

        body = "\n".join(chunks)
        final_url = page.url
        status = resp.status if resp else 0
        ct = (resp.headers.get("content-type", "text/html") if resp else "text/html").lower()
        return final_url, status, body, ct
    finally:
        page.close()


def _shutdown_browser_sync():
    if _browser_state["context"] is None:
        return
    for key, closer in (
        ("context", "close"),
        ("browser", "close"),
        ("playwright", "stop"),
    ):
        obj = _browser_state[key]
        if obj is None:
            continue
        try:
            getattr(obj, closer)()
        except Exception:
            pass
        _browser_state[key] = None
    _log("browser shut down")


async def _fetch_page_with_browser(url: str, timeout_ms: int = 30000):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _get_browser_executor(), _fetch_with_browser_sync, url, timeout_ms
    )


async def shutdown_browser():
    global _browser_executor
    if _browser_executor is None:
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_browser_executor, _shutdown_browser_sync)
    except Exception:
        pass
    _browser_executor.shutdown(wait=False)
    _browser_executor = None


def _normalize(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()


def _is_pdf_path(path: str) -> bool:
    return path.lower().endswith(".pdf")


# Files we don't want to navigate to with a browser (or treat as crawlable pages).
# Keeps non-PDF binary downloads from triggering "Page.goto: Download is starting" errors.
DOWNLOAD_EXTENSIONS = (
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf", ".wpd",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".m4a", ".ogg", ".flac",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".tiff", ".ico",
    ".exe", ".dmg", ".iso", ".bin", ".msi", ".apk",
    ".csv", ".tsv",
)


def _is_download_path(path: str) -> bool:
    return path.lower().endswith(DOWNLOAD_EXTENSIONS)


def _log(msg: str):
    print(f"[crawl] {msg}", flush=True)


def _collect_links(soup) -> list[str]:
    """Pull every URL the crawler should consider from a parsed HTML document.
    Covers <a>, <area>, <frame>, <iframe> (so framesets work), and embedded PDFs."""
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        out.append(a["href"])
    for area in soup.find_all("area", href=True):
        out.append(area["href"])
    for tag in soup.find_all(["frame", "iframe"]):
        src = tag.get("src")
        if src:
            out.append(src)
    for tag in soup.find_all(["embed", "object"]):
        src = tag.get("src") or tag.get("data")
        if src:
            out.append(src)
    return out


def _clean(val):
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _parse_pdf_bytes(content: bytes) -> dict:
    meta: dict = {"size_bytes": len(content)}
    if len(content) > MAX_PDF_BYTES:
        meta["fetch_error"] = f"PDF larger than {MAX_PDF_BYTES // (1024*1024)} MB; skipped parse"
        return meta
    try:
        reader = PdfReader(io.BytesIO(content))
        info = reader.metadata or {}
        meta["title"] = _clean(info.get("/Title"))
        meta["author"] = _clean(info.get("/Author"))
        meta["subject"] = _clean(info.get("/Subject"))
        meta["creator"] = _clean(info.get("/Creator"))
        meta["producer"] = _clean(info.get("/Producer"))
        meta["creation_date"] = _clean(info.get("/CreationDate"))
        meta["mod_date"] = _clean(info.get("/ModDate"))
        meta["page_count"] = len(reader.pages)
        meta["tagged"] = _tagged_flag(reader)
        meta["pdf_ua"] = _pdf_ua_flag(reader)
    except Exception as e:
        meta["fetch_error"] = f"parse error: {e}"
    return meta


def _tagged_flag(reader) -> int | None:
    """1 if the PDF declares MarkInfo/Marked = true, 0 if explicitly false, None if unknown."""
    try:
        if hasattr(reader, "is_tagged"):
            return 1 if reader.is_tagged else 0
        root = reader.trailer["/Root"]
        if hasattr(root, "get_object"):
            root = root.get_object()
        mark_info = root.get("/MarkInfo") if hasattr(root, "get") else None
        if mark_info is None:
            return 0
        if hasattr(mark_info, "get_object"):
            mark_info = mark_info.get_object()
        marked = mark_info.get("/Marked") if hasattr(mark_info, "get") else None
        return 1 if marked else 0
    except Exception:
        return None


def _pdf_ua_flag(reader) -> int | None:
    """1 if the XMP metadata declares any pdfuaid:part value, 0 if XMP exists without it, None on error."""
    try:
        xmp = reader.xmp_metadata
        if xmp is None:
            return 0
        data = None
        stream = getattr(xmp, "stream", None)
        if stream is not None:
            if hasattr(stream, "get_data"):
                data = stream.get_data()
            elif isinstance(stream, (bytes, bytearray)):
                data = bytes(stream)
        if data is None:
            return 0
        text = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data)
        return 1 if "pdfuaid" in text.lower() else 0
    except Exception:
        return None


async def crawl_site(
    start_url: str,
    *,
    crawl: bool = True,
    max_pages: int = 100,
    max_depth: int = 3,
    concurrency: int = 6,
    use_browser: bool = False,
    on_pdf=None,
    on_progress=None,
    cancel_check=None,
    seen_pages_init: set | None = None,
    seen_pdfs_init: set | None = None,
    seed_pages: list | None = None,
    on_page_queued=None,
    on_page_visited=None,
) -> dict:
    """Crawl pages and extract PDF metadata concurrently.

    on_pdf(url, meta) is awaited as soon as a PDF is fully fetched + parsed.
    on_progress(state) is awaited periodically; state has pages_visited,
    pages_total, pdfs_found, pdfs_done.
    cancel_check() returns True to stop the job early.
    """
    start = urlparse(start_url)
    if start.scheme not in ("http", "https"):
        raise ValueError("URL must be http(s)")
    base_host = start.netloc.lower()
    start_norm = _normalize(start_url)

    state = {"pages_visited": 0, "pages_total": 0, "pdfs_found": 0, "pdfs_done": 0}
    seen_pages: set[str] = set(seen_pages_init) if seen_pages_init else set()
    seen_pdfs: set[str] = set(seen_pdfs_init) if seen_pdfs_init else set()
    pages_scheduled = 0  # this run only — limits how many new visits this run does
    in_flight: set[asyncio.Task] = set()
    errors: list[dict] = []
    page_sem = asyncio.Semaphore(concurrency)
    pdf_sem = asyncio.Semaphore(concurrency)
    host_resolved = False  # flips after the first successful fetch (to absorb redirects)

    async def emit_progress():
        if on_progress:
            await on_progress(state)

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
    }
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )

    async with httpx.AsyncClient(
        headers=headers, timeout=TIMEOUT, follow_redirects=True, limits=limits
    ) as client:

        def schedule(coro) -> asyncio.Task:
            t = asyncio.create_task(coro)
            in_flight.add(t)
            t.add_done_callback(in_flight.discard)
            return t

        async def handle_pdf(pdf_url: str, found_on: str):
            if cancelled():
                return
            async with pdf_sem:
                try:
                    resp = await client.get(pdf_url)
                    resp.raise_for_status()
                    content = resp.content
                except Exception as e:
                    meta = {"size_bytes": None, "fetch_error": str(e)}
                    _log(f"pdf-err  {pdf_url} -- {e}")
                else:
                    meta = await asyncio.to_thread(_parse_pdf_bytes, content)
                    if meta.get("fetch_error"):
                        _log(f"pdf-warn {pdf_url} -- {meta['fetch_error']}")
                    else:
                        _log(f"pdf      {pdf_url} -- {meta.get('size_bytes', 0)} bytes, "
                             f"{meta.get('page_count', '?')} pages")
            meta["found_on"] = found_on
            state["pdfs_done"] += 1
            if on_pdf:
                await on_pdf(pdf_url, meta)
            await emit_progress()

        async def handle_page(url: str, depth: int):
            nonlocal base_host, host_resolved, pages_scheduled
            if cancelled():
                return
            async with page_sem:
                try:
                    if use_browser:
                        final_url, status, body, ct = await _fetch_page_with_browser(url)
                        if status and status >= 400:
                            raise RuntimeError(f"HTTP {status}")
                    else:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        final_url = str(resp.url)
                        body = resp.text
                        ct = resp.headers.get("content-type", "").lower()
                except Exception as e:
                    errors.append({"url": url, "error": str(e)})
                    _log(f"error d={depth} {url} -- {e}")
                    state["pages_visited"] += 1
                    await emit_progress()
                    return

                # If the start URL redirected to a different host, treat that as the new base.
                final_host = urlparse(final_url).netloc.lower()
                if not host_resolved:
                    host_resolved = True
                    if final_host and final_host != base_host:
                        _log(f"redir start host {base_host} -> {final_host}")
                        base_host = final_host

                if "html" not in ct:
                    _log(f"skip  d={depth} {url} -- non-html ({ct or 'no content-type'})")
                    state["pages_visited"] += 1
                    await emit_progress()
                    return

                try:
                    soup = BeautifulSoup(body, "html.parser")
                except Exception as e:
                    _log(f"parse-error d={depth} {url} -- {e}")
                    state["pages_visited"] += 1
                    await emit_progress()
                    return

                base_url = final_url
                hrefs = _collect_links(soup)
                breakdown = {"pdf+": 0, "page+": 0, "off_host": [], "depth": 0,
                             "seen": 0, "skip_prefix": 0, "bad_scheme": 0,
                             "max_pages": 0, "single_page_mode": 0,
                             "download_ext": 0}
                drop_examples: list[str] = []  # populated when nothing useful was queued

                for raw in hrefs:
                    href = (raw or "").strip()
                    if not href:
                        continue
                    if href.startswith(SKIP_PREFIXES):
                        breakdown["skip_prefix"] += 1
                        drop_examples.append(f"skip-prefix: {href}")
                        continue
                    full = urljoin(base_url, href)
                    p = urlparse(full)
                    if p.scheme not in ("http", "https"):
                        breakdown["bad_scheme"] += 1
                        drop_examples.append(f"bad-scheme: {full}")
                        continue
                    norm = _normalize(full)
                    if _is_pdf_path(p.path):
                        if norm in seen_pdfs:
                            breakdown["seen"] += 1
                        else:
                            seen_pdfs.add(norm)
                            state["pdfs_found"] += 1
                            breakdown["pdf+"] += 1
                            schedule(handle_pdf(norm, base_url))
                    elif _is_download_path(p.path):
                        breakdown["download_ext"] += 1
                        drop_examples.append(f"download-ext: {full}")
                    elif not crawl:
                        breakdown["single_page_mode"] += 1
                        drop_examples.append(f"single-page-mode: {full}")
                    elif p.netloc.lower() != base_host:
                        breakdown["off_host"].append(p.netloc.lower())
                        drop_examples.append(f"off-host ({p.netloc}): {full}")
                    elif depth >= max_depth:
                        breakdown["depth"] += 1
                        drop_examples.append(f"depth-limit (d={depth}>={max_depth}): {full}")
                    elif norm in seen_pages:
                        breakdown["seen"] += 1
                    else:
                        seen_pages.add(norm)
                        state["pages_total"] += 1
                        breakdown["page+"] += 1
                        if on_page_queued:
                            await on_page_queued(norm, depth + 1)
                        if pages_scheduled < max_pages:
                            pages_scheduled += 1
                            schedule(handle_page(norm, depth + 1))
                        else:
                            # Frontier — saved to DB so a Continue can pick it up.
                            breakdown["max_pages"] += 1

                # Build a compact summary, omitting zero-count categories.
                parts = [f"{len(hrefs)} links"]
                for k in ("pdf+", "page+"):
                    if breakdown[k]:
                        parts.append(f"{breakdown[k]} {k}")
                if breakdown["off_host"]:
                    hosts = sorted(set(breakdown["off_host"]))
                    parts.append(f"{len(breakdown['off_host'])} off-host({', '.join(hosts[:3])})")
                for k in ("depth", "seen", "skip_prefix", "bad_scheme", "max_pages", "single_page_mode", "download_ext"):
                    if breakdown[k]:
                        parts.append(f"{breakdown[k]} {k}")
                _log(f"page  d={depth} {url} -- {', '.join(parts)}")

                # When a page is a dead end, look for a JS-based session redirect
                # (XSP/Domino style) and queue it at the same depth as a fallback.
                if breakdown["pdf+"] == 0 and breakdown["page+"] == 0:
                    js_followed = False
                    for script in soup.find_all("script"):
                        js_text = (script.string or "")
                        m = JS_REDIRECT_RE.search(js_text)
                        if not m:
                            continue
                        target = urljoin(base_url, m.group(1).strip())
                        tp = urlparse(target)
                        if tp.scheme not in ("http", "https"):
                            continue
                        if tp.netloc.lower() != base_host:
                            continue
                        norm = _normalize(target)
                        if norm in seen_pages:
                            continue
                        seen_pages.add(norm)
                        state["pages_total"] += 1
                        if on_page_queued:
                            await on_page_queued(norm, depth)
                        _log(f"    js-redirect fallback -> {target}")
                        if pages_scheduled < max_pages:
                            pages_scheduled += 1
                            schedule(handle_page(norm, depth))  # same depth, not deeper
                        js_followed = True
                        break

                    if not js_followed:
                        a_total = len(soup.find_all("a"))
                        body_len = len(body)
                        _log(f"    diagnostics: {a_total} <a> tags total, {body_len} bytes, "
                             f"final url = {final_url}")
                        sample = body[:400].replace("\n", " ").replace("\r", "")
                        _log(f"    sample: {sample!r}")
                        for line in drop_examples[:10]:
                            _log(f"    {line}")
                        if len(drop_examples) > 10:
                            _log(f"    ...and {len(drop_examples) - 10} more")

                state["pages_visited"] += 1
                if on_page_visited:
                    await on_page_visited(url)
                await emit_progress()

        # Seed. Three cases:
        # 1) seed_pages provided (Continue mode) — visit the saved frontier.
        # 2) start URL is itself a PDF — fetch it directly.
        # 3) start URL is a page — BFS from it.
        if seed_pages:
            for seed_url, seed_depth in seed_pages:
                if seed_url not in seen_pages:
                    seen_pages.add(seed_url)
                    state["pages_total"] += 1
                if pages_scheduled < max_pages:
                    pages_scheduled += 1
                    schedule(handle_page(seed_url, seed_depth))
                # else: stays in DB-frontier for a future Continue
        elif _is_pdf_path(start.path):
            seen_pdfs.add(start_norm)
            state["pdfs_found"] = 1
            schedule(handle_pdf(start_norm, start_norm))
        else:
            if start_norm not in seen_pages:
                seen_pages.add(start_norm)
                state["pages_total"] = 1
                if on_page_queued:
                    await on_page_queued(start_norm, 0)
            pages_scheduled += 1
            schedule(handle_page(start_norm, 0))

        # Drain — tasks may add new tasks while we wait.
        while in_flight:
            await asyncio.wait(list(in_flight))

    return {
        "pages_visited": state["pages_visited"],
        "pdfs_found": state["pdfs_found"],
        "pdfs_done": state["pdfs_done"],
        "errors": errors,
        "cancelled": cancelled(),
    }
