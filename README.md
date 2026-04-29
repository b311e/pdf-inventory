# PDF Inventory

A small local web app that crawls websites, finds every PDF they link to, extracts each PDF's metadata (title, author, page count, file size, etc.), and shows it all in a sortable, searchable table. Each website gets its own SQLite database file.

Everything runs on your computer. Nothing is uploaded anywhere.

---

## One-time setup

You only need to do this once.

### 1. Install Python

Open PowerShell or Command Prompt and run:

```
python --version
```

- If you see `Python 3.10.x` or newer, you're good.
- If you see `Python 3.9` or older, or "command not found", install the latest Python from <https://www.python.org/downloads/>. **During install, check the box "Add Python to PATH".**

### 2. Open a terminal in the project folder

```
cd c:\code\pdf-inventory
```

### 3. Create an isolated Python environment

This keeps the app's libraries from interfering with anything else on your machine.

```
python -m venv .venv
```

This creates a `.venv` folder. You'll never need to touch what's inside it.

### 4. Turn the environment on

```
.venv\Scripts\activate
```

Your terminal prompt should now start with `(.venv)`. That means you're inside the environment.

### 5. Install the libraries the app needs

```
pip install -r requirements.txt
```

Wait for it to finish (takes 30–60 seconds the first time).

### 6. Install the headless browser (for JavaScript-heavy sites)

Some sites (like XSP/Domino-based government databases) only show their content after JavaScript runs. To handle those, run:

```
python -m playwright install chromium
```

This downloads Chromium (~150 MB, one time only). When it's done, setup is complete. Skip this step if you'll only ever scrape simple HTML sites — the app still works without it for those, you just won't be able to tick the "Use browser" box.

---

## Running the app

Every time you want to use it:

### 1. Open a terminal in the project folder

```
cd c:\code\pdf-inventory
```

### 2. Turn the environment on

```
.venv\Scripts\activate
```

(Your prompt should show `(.venv)`.)

### 3. Start the server

```
uvicorn app:app --reload
```

You'll see something like:

```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

**Leave this terminal window open.** The app is running inside it. Closing it stops the app.

### 4. Open the app in your browser

Go to <http://127.0.0.1:8000>

### 5. Use it

In the text box, paste one or more URLs, **one per line**. Optionally give a site a custom name with a pipe:

```
Colorado Ballot History | https://www.leg.state.co.us/lcs/ballothistory.nsf/
Senate Records | https://www.leg.state.co.us/inethsr.nsf/
https://example.com/just-a-bare-url-also-works
```

Click **Scrape**. Each URL becomes its own job; up to 4 run in parallel. Watch the "Sites" section for progress (`running · pages 5/12 · pdfs 3/8`). When a site says `ok`, click its row to view just its PDFs, or click "All sites" at the top for the combined view.

### 6. Stop the app when you're done

Click into the terminal running the server and press `Ctrl+C`. The server stops.

To run it again later, repeat steps 1–4 — you do NOT need to repeat the one-time setup.

---

## Where your data lives

```
c:\code\pdf-inventory\
  registry.db         <- index of all sites you've scraped
  data\
    example_com.db    <- one file per site, contains all its PDFs
    docs_python_org.db
    ...
```

- To **back up** a single site, copy its `.db` file from `data\`.
- To **inspect** a site's data outside the app, open its `.db` file in [DB Browser for SQLite](https://sqlitebrowser.org/) — there's just one table, `pdfs`.
- **Deleting a site from the UI** also deletes its `.db` file from `data\`.

---

## Things to know

- **Be patient on big sites.** A site with hundreds of PDFs takes a few minutes the first time — each PDF has to be downloaded once to read its metadata. Re-scraping is faster: only changed PDFs are updated.
- **The `pages` and `depth` settings are safety limits.** `pages 100` stops after visiting 100 pages on a site. `depth 3` follows links up to 3 clicks deep from the start URL. Raise them for wider crawls; lower them for quick peeks.
- **The crawler only finds PDFs linked with regular `<a href="...">` tags.** PDFs hidden behind login pages, JavaScript buttons, or "Download" forms won't show up.
- **Be polite.** If you crawl someone else's site with `pages 5000` and `depth 10`, you'll hit them with thousands of requests. Stick close to the defaults unless you own the site.
- **Same domain, different paths = separate sites.** `example.com/docs` and `example.com/blog` get their own DBs.

---

## Troubleshooting

**"Error: Failed to fetch" in the browser.**
The server has stopped. Look at the terminal running uvicorn:
- If it shows `Ctrl+C` or your prompt back, restart it: `uvicorn app:app --reload`
- If it shows a Python traceback, the app crashed — read the last few lines for the cause.

**The terminal won't accept commands / says `(.venv)` is missing.**
You need to re-activate the environment. Run `.venv\Scripts\activate` again.

**A scrape is stuck on "running" forever.**
Click the **Cancel** button next to that site. If you've restarted the server, any half-finished jobs automatically get marked `interrupted` on the next launch.

**A site shows status `error` with a `!` icon.**
Hover the `!` to see the message. Common causes: the URL is wrong, the site is down, or the site blocks automated requests.

**I want to start completely fresh.**
Stop the server. Delete `registry.db` and the `data\` folder. Start the server again — it'll create empty ones.
