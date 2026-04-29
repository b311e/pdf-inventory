import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent
REGISTRY_PATH = BASE_DIR / "registry.db"
DATA_DIR = BASE_DIR / "data"
LEGACY_DB = BASE_DIR / "pdfs.db"

BUSY_STATUSES = ("pending", "running", "crawling", "scanning", "extracting")

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    name TEXT,
    slug TEXT UNIQUE NOT NULL,
    db_path TEXT NOT NULL,
    status TEXT,
    error TEXT,
    mode TEXT DEFAULT 'page',
    pages_visited INTEGER DEFAULT 0,
    pages_total INTEGER DEFAULT 0,
    pdfs_total INTEGER DEFAULT 0,
    pdf_count INTEGER DEFAULT 0,
    use_browser INTEGER DEFAULT 0,
    last_scraped TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdfs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT,
    author TEXT,
    subject TEXT,
    creator TEXT,
    producer TEXT,
    creation_date TEXT,
    mod_date TEXT,
    page_count INTEGER,
    size_bytes INTEGER,
    tagged INTEGER,
    pdf_ua INTEGER,
    found_on TEXT,
    fetch_error TEXT,
    scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pdfs_url ON pdfs(url);
CREATE TABLE IF NOT EXISTS crawl_state (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL,
    visited INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_state_visited ON crawl_state(visited);
"""

# Columns to add to per-site DBs created before they existed.
PDF_MIGRATIONS = [
    ("mod_date", "TEXT"),
    ("tagged", "INTEGER"),
    ("pdf_ua", "INTEGER"),
    ("found_on", "TEXT"),
]


@contextmanager
def get_registry():
    conn = sqlite3.connect(REGISTRY_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_site_db(db_path):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    DATA_DIR.mkdir(exist_ok=True)
    with get_registry() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(REGISTRY_SCHEMA)
        # Lightweight column migration for registries created before `name` existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sites)").fetchall()}
        if "name" not in cols:
            conn.execute("ALTER TABLE sites ADD COLUMN name TEXT")
        if "use_browser" not in cols:
            conn.execute("ALTER TABLE sites ADD COLUMN use_browser INTEGER DEFAULT 0")
    _migrate_legacy_if_needed()
    _migrate_all_site_dbs()


def _migrate_all_site_dbs():
    """Apply pending column migrations to every existing per-site DB at startup."""
    with get_registry() as conn:
        rows = conn.execute("SELECT db_path FROM sites").fetchall()
    for r in rows:
        if not Path(r["db_path"]).exists():
            continue
        try:
            init_site_db(r["db_path"])
        except Exception as e:
            print(f"[migrate] {r['db_path']}: {e}")


def init_site_db(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with get_site_db(p) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SITE_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pdfs)").fetchall()}
        for col, ddl in PDF_MIGRATIONS:
            if col not in cols:
                conn.execute(f"ALTER TABLE pdfs ADD COLUMN {col} {ddl}")


def slug_for(url: str) -> str:
    """Build a filesystem-safe, human-readable slug from a URL."""
    p = urlparse(url)
    parts = [p.netloc.lower()]
    if p.path and p.path.strip("/"):
        path_clean = re.sub(r"[^a-z0-9]+", "_", p.path.lower()).strip("_")
        if path_clean:
            parts.append(path_clean[:30])
    raw = "_".join(parts)
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_") or "site"
    return safe[:80]


def unique_slug(conn, base: str) -> str:
    slug = base
    n = 1
    while conn.execute("SELECT 1 FROM sites WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}_{n}"
    return slug


def site_db_path(slug: str) -> str:
    return str(DATA_DIR / f"{slug}.db")


def reset_interrupted_jobs() -> int:
    placeholders = ",".join("?" for _ in BUSY_STATUSES)
    with get_registry() as conn:
        cur = conn.execute(
            f"""UPDATE sites
                SET status = 'interrupted',
                    error = COALESCE(error, 'Interrupted by app restart')
                WHERE status IN ({placeholders})""",
            BUSY_STATUSES,
        )
        return cur.rowcount


def delete_site_files(db_path: str):
    """Remove a site's DB file plus its WAL/SHM siblings."""
    p = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass


def _migrate_legacy_if_needed():
    """One-time move from monolithic pdfs.db (sources+pdfs) to registry + data/<slug>.db files."""
    if not LEGACY_DB.exists():
        return
    legacy = sqlite3.connect(LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sources" not in tables or "pdfs" not in tables:
            return
        cols = {r[1] for r in legacy.execute("PRAGMA table_info(pdfs)").fetchall()}
        if "source_id" not in cols:
            return  # already migrated or unknown layout

        sources = legacy.execute("SELECT * FROM sources").fetchall()
        print(f"[migrate] moving {len(sources)} site(s) from pdfs.db into per-site DBs...")
        with get_registry() as reg:
            for s in sources:
                base = slug_for(s["url"])
                slug = unique_slug(reg, base)
                db_path = site_db_path(slug)
                init_site_db(db_path)

                pdfs = legacy.execute(
                    "SELECT * FROM pdfs WHERE source_id = ?", (s["id"],)
                ).fetchall()
                with get_site_db(db_path) as site:
                    for p in pdfs:
                        site.execute(
                            """
                            INSERT OR IGNORE INTO pdfs
                              (url, title, author, subject, creator, producer,
                               creation_date, page_count, size_bytes, fetch_error, scraped_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                p["url"], p["title"], p["author"], p["subject"],
                                p["creator"], p["producer"], p["creation_date"],
                                p["page_count"], p["size_bytes"],
                                p["fetch_error"], p["scraped_at"],
                            ),
                        )

                reg.execute(
                    """
                    INSERT INTO sites (url, slug, db_path, status, error, mode,
                                       pages_visited, pages_total, pdfs_total, pdf_count,
                                       last_scraped)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        s["url"], slug, db_path, s["status"], s["error"],
                        s["mode"] or "page",
                        s["pages_visited"] or 0, s["pages_total"] or 0,
                        s["pdfs_total"] or 0, len(pdfs),
                        s["last_scraped"],
                    ),
                )

        backup = LEGACY_DB.with_suffix(".db.legacy")
        LEGACY_DB.rename(backup)
        print(f"[migrate] done. old DB moved to {backup.name}")
    finally:
        legacy.close()
