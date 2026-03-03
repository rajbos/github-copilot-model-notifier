#!/usr/bin/env python3
"""Check GitHub Copilot models for changes and update pages.

Scrapes https://docs.github.com/en/copilot/reference/ai-models/supported-models,
compares with stored data, and generates GitHub Pages content (HTML + RSS feed)
when changes are detected.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import formatdate

from playwright.sync_api import sync_playwright

# Number of columns in the model table (used for the empty-state row)
_MODEL_TABLE_COLS = 5

DOCS_URL = "https://docs.github.com/en/copilot/reference/ai-models/supported-models"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.join(_SCRIPT_DIR, "..")

DATA_DIR = os.path.join(_REPO_ROOT, "data")
DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
MODELS_FILE = os.path.join(DATA_DIR, "models.json")
CHANGES_FILE = os.path.join(DATA_DIR, "changes.json")
INDEX_FILE = os.path.join(DOCS_DIR, "index.html")
FEED_FILE = os.path.join(DOCS_DIR, "feed.xml")

REPO = os.environ.get("GITHUB_REPOSITORY", "rajbos/github-copilot-model-notifier")
SERVER_URL = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
REPO_URL = f"{SERVER_URL}/{REPO}"
OWNER = REPO.split("/")[0]
REPO_NAME = REPO.split("/")[1]
PAGES_URL = f"https://{OWNER}.github.io/{REPO_NAME}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Normalise whitespace and strip leading/trailing spaces."""
    return re.sub(r"\s+", " ", text).strip()


def load_json(path: str, default=None):
    """Load a JSON file; return *default* when the file is missing or invalid."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: str, data) -> None:
    """Persist *data* as a JSON file (creates parent directories as needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def set_output(name: str, value: str) -> None:
    """Write a step output variable to GITHUB_OUTPUT (no-op outside Actions)."""
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if not github_output:
        return
    with open(github_output, "a") as fh:
        if "\n" in str(value):
            delimiter = "EOF"
            fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            fh.write(f"{name}={value}\n")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_models() -> dict:
    """Use Playwright to render the docs page and extract model data.

    Returns a dict keyed by model name, each value containing provider,
    release_status, multiplier_paid, and multiplier_free fields.
    """
    print("Scraping GitHub Docs for model information…")
    models: dict = {}
    multipliers: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(DOCS_URL, wait_until="networkidle", timeout=60_000)

        # Wait up to 15 s for at least one table cell to be non-empty
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('article table tbody tr td').length > 0",
                timeout=15_000,
            )
        except Exception:
            print("Warning: tables may not have fully loaded within the timeout")

        tables = page.locator("article table").all()
        print(f"Found {len(tables)} table(s)")

        for table in tables:
            # Collect header labels
            header_els = table.locator("thead th, thead td").all()
            if not header_els:
                header_els = table.locator("tr:first-child th, tr:first-child td").all()
            headers = [clean_text(h.inner_text()) for h in header_els]
            if not headers:
                continue

            print(f"  Table headers: {headers}")

            rows = table.locator("tbody tr").all()
            for row in rows:
                cell_els = row.locator("td, th").all()
                cells = [clean_text(c.inner_text()) for c in cell_els]
                if not any(cells):
                    continue
                # Pad cells so we can zip safely
                while len(cells) < len(headers):
                    cells.append("")
                row_data = dict(zip(headers, cells))

                # ---- Supported AI models table (Model name | Provider | …) ----
                if "Model name" in row_data:
                    name = row_data.get("Model name", "").strip()
                    if name:
                        models[name] = {
                            "provider": row_data.get("Provider", ""),
                            "release_status": row_data.get("Release status", ""),
                        }

                # ---- Model multipliers table (Model | Multiplier for … | …) ----
                elif "Model" in row_data and any(
                    "multiplier" in k.lower() for k in row_data
                ):
                    name = row_data.get("Model", "").strip()
                    if name:
                        paid_key = next(
                            (k for k in row_data if "paid" in k.lower() and "multiplier" in k.lower()),
                            "",
                        )
                        free_key = next(
                            (k for k in row_data if "free" in k.lower() and "multiplier" in k.lower()),
                            "",
                        )
                        multipliers[name] = {
                            "multiplier_paid": row_data.get(paid_key, ""),
                            "multiplier_free": row_data.get(free_key, ""),
                        }

        browser.close()

    # Merge the two sources
    result: dict = {}
    for name in sorted(set(list(models) + list(multipliers))):
        result[name] = {
            "name": name,
            "provider": models.get(name, {}).get("provider", ""),
            "release_status": models.get(name, {}).get("release_status", ""),
            "multiplier_paid": multipliers.get(name, {}).get("multiplier_paid", ""),
            "multiplier_free": multipliers.get(name, {}).get("multiplier_free", ""),
        }

    print(f"Extracted {len(result)} model(s)")
    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_models(old: dict, new: dict) -> list:
    """Return a human-readable list of changes between *old* and *new* data."""
    changes = []
    old_names = set(old)
    new_names = set(new)

    for name in sorted(new_names - old_names):
        m = new[name]
        desc = f"**New model added**: {name}"
        if m.get("provider"):
            desc += f" (Provider: {m['provider']})"
        if m.get("multiplier_paid"):
            desc += f", Multiplier: {m['multiplier_paid']}"
        changes.append(desc)

    for name in sorted(old_names - new_names):
        changes.append(f"**Model removed**: {name}")

    for name in sorted(old_names & new_names):
        o, n = old[name], new[name]
        field_changes = []
        if o.get("provider") != n.get("provider"):
            field_changes.append(
                f"Provider changed from '{o.get('provider')}' to '{n.get('provider')}'"
            )
        if o.get("multiplier_paid") != n.get("multiplier_paid"):
            field_changes.append(
                f"Multiplier (paid) changed from '{o.get('multiplier_paid')}' to '{n.get('multiplier_paid')}'"
            )
        if o.get("multiplier_free") != n.get("multiplier_free"):
            field_changes.append(
                f"Multiplier (free) changed from '{o.get('multiplier_free')}' to '{n.get('multiplier_free')}'"
            )
        if o.get("release_status") != n.get("release_status"):
            field_changes.append(
                f"Release status changed from '{o.get('release_status')}' to '{n.get('release_status')}'"
            )
        if field_changes:
            changes.append(f"**{name}**: " + "; ".join(field_changes))

    return changes


# ---------------------------------------------------------------------------
# Page generation
# ---------------------------------------------------------------------------

def generate_html(models: dict, changes_history: list) -> str:
    """Return the full HTML for docs/index.html."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    model_rows = []
    for name, info in sorted(models.items()):
        model_rows.append(
            f"      <tr>\n"
            f"        <td>{name}</td>\n"
            f"        <td>{info.get('provider', '')}</td>\n"
            f"        <td>{info.get('release_status', '')}</td>\n"
            f"        <td>{info.get('multiplier_paid', '')}</td>\n"
            f"        <td>{info.get('multiplier_free', '')}</td>\n"
            f"      </tr>"
        )

    change_items = []
    for change in reversed(changes_history[-20:]):
        items_html = "".join(f"<li>{c}</li>" for c in change.get("items", []))
        tag = change.get("tag", "")
        release_link = f"{REPO_URL}/releases/tag/{tag}" if tag else REPO_URL
        change_items.append(
            f'  <div class="change-item">\n'
            f'    <h3><a href="{release_link}">{change.get("title", "Update")}</a></h3>\n'
            f'    <p class="date">{change.get("date", "")}</p>\n'
            f"    <ul>{items_html}</ul>\n"
            f"  </div>"
        )

    model_rows_html = (
        "\n".join(model_rows)
        if model_rows
        else f'      <tr><td colspan="{_MODEL_TABLE_COLS}">No models found yet. The first workflow run will populate this page.</td></tr>'
    )
    change_items_html = (
        "\n".join(change_items) if change_items else "  <p>No changes recorded yet.</p>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GitHub Copilot Model Updates</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; color: #24292f; }}
    h1 {{ border-bottom: 1px solid #d0d7de; padding-bottom: 10px; }}
    h2 {{ margin-top: 30px; color: #24292f; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
    th, td {{ padding: 8px 12px; text-align: left; border: 1px solid #d0d7de; }}
    th {{ background-color: #f6f8fa; font-weight: 600; }}
    tr:hover td {{ background-color: #f6f8fa; }}
    .subscribe {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; margin: 20px 0; }}
    .subscribe a {{ color: #0969da; }}
    .rss-icon {{ color: #f26522; }}
    .change-item {{ border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; margin: 10px 0; }}
    .date {{ color: #656d76; font-size: 0.9em; margin: 4px 0; }}
    .updated {{ color: #656d76; font-size: 0.85em; }}
    footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #d0d7de; color: #656d76; font-size: 0.85em; }}
  </style>
</head>
<body>
  <h1>&#x1F916; GitHub Copilot Model Updates</h1>
  <p>Automatically tracks changes to <a href="{DOCS_URL}">GitHub Copilot's supported AI models</a>,
  including new models, retired models, and multiplier changes.</p>

  <div class="subscribe">
    <strong>&#x1F4EC; Subscribe to updates:</strong>
    <ul>
      <li><span class="rss-icon">&#x1F4E1;</span> <a href="feed.xml">RSS Feed</a> &ndash; Subscribe with your RSS reader</li>
      <li>&#x1F514; <a href="{REPO_URL}/releases">GitHub Releases</a> &ndash; Watch this repository and select <em>Releases only</em></li>
    </ul>
  </div>

  <p class="updated">Last updated: {now} &nbsp;|&nbsp; Source: <a href="{DOCS_URL}">GitHub Docs</a></p>

  <h2>Current Models ({len(models)})</h2>
  <table>
    <thead>
      <tr>
        <th>Model Name</th>
        <th>Provider</th>
        <th>Release Status</th>
        <th>Multiplier (Paid Plans)</th>
        <th>Multiplier (Copilot Free)</th>
      </tr>
    </thead>
    <tbody>
{model_rows_html}
    </tbody>
  </table>

  <h2>Recent Changes</h2>
{change_items_html}

  <footer>
    <p>This page is automatically updated by a
    <a href="{REPO_URL}">GitHub Actions workflow</a>.
    Data sourced from <a href="{DOCS_URL}">GitHub Docs</a>.</p>
  </footer>
</body>
</html>
"""


def generate_rss(changes_history: list) -> str:
    """Return the XML for docs/feed.xml."""
    now_rfc = formatdate(usegmt=True)

    items = []
    for change in reversed(changes_history[-50:]):
        items_html = "".join(
            f"&lt;li&gt;{c}&lt;/li&gt;" for c in change.get("items", [])
        )
        tag = change.get("tag", "")
        link = f"{REPO_URL}/releases/tag/{tag}" if tag else REPO_URL
        items.append(
            f"    <item>\n"
            f"      <title>{change.get('title', 'Model Update')}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <description>&lt;ul&gt;{items_html}&lt;/ul&gt;</description>\n"
            f"      <pubDate>{change.get('pub_date', now_rfc)}</pubDate>\n"
            f"      <guid isPermaLink=\"{'true' if tag else 'false'}\">{link}</guid>\n"
            f"    </item>"
        )

    items_xml = "\n".join(items)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>GitHub Copilot Model Updates</title>
    <link>{PAGES_URL}</link>
    <description>Notifications about changes to GitHub Copilot models including new models, removed models, and multiplier changes.</description>
    <language>en-us</language>
    <atom:link href="{PAGES_URL}/feed.xml" rel="self" type="application/rss+xml"/>
    <lastBuildDate>{now_rfc}</lastBuildDate>
{items_xml}
  </channel>
</rss>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)

    # 1. Scrape current models from the docs page
    new_models = scrape_models()
    if not new_models:
        print("ERROR: No models were found. The scraper may have failed.")
        sys.exit(1)

    # 2. Load stored models for comparison
    old_models = load_json(MODELS_FILE, {})
    print(f"Stored models: {len(old_models)}  |  Scraped models: {len(new_models)}")

    # 3. Determine what changed
    changes = compare_models(old_models, new_models)

    # 4. Load the full change history
    changes_history = load_json(CHANGES_FILE, [])

    if changes:
        print(f"{len(changes)} change(s) detected:")
        for c in changes:
            print(f"  - {c}")

        now = datetime.now(timezone.utc)
        tag = f"models-{now.strftime('%Y-%m-%d-%H%M%S')}"
        title = f"Model Update: {now.strftime('%Y-%m-%d')}"

        change_entry = {
            "date": now.strftime("%Y-%m-%d %H:%M UTC"),
            "pub_date": formatdate(timeval=now.timestamp(), usegmt=True),
            "title": title,
            "tag": tag,
            "items": changes,
        }
        changes_history.append(change_entry)

        # Persist updated data
        save_json(MODELS_FILE, new_models)
        save_json(CHANGES_FILE, changes_history)

        # Regenerate static pages
        with open(INDEX_FILE, "w") as fh:
            fh.write(generate_html(new_models, changes_history))
        with open(FEED_FILE, "w") as fh:
            fh.write(generate_rss(changes_history))
        print(f"Pages written to {DOCS_DIR}")

        # Build release notes
        release_notes = (
            f"## GitHub Copilot Model Changes\n\n"
            f"Source: [{DOCS_URL}]({DOCS_URL})\n\n"
            f"### Changes\n\n"
        )
        for change in changes:
            release_notes += f"- {change}\n"
        release_notes += "\n### Current Models\n\n"
        release_notes += "| Model | Provider | Multiplier (Paid) |\n"
        release_notes += "| ----- | -------- | ----------------- |\n"
        for name, info in sorted(new_models.items()):
            release_notes += (
                f"| {name} | {info.get('provider', '')} | {info.get('multiplier_paid', '')} |\n"
            )

        # Write release notes to a temp file for the workflow to consume
        release_notes_path = os.path.join(tempfile.gettempdir(), "release_notes.md")
        with open(release_notes_path, "w") as fh:
            fh.write(release_notes)

        set_output("changed", "true")
        set_output("tag", tag)
        set_output("title", title)
    else:
        print("No changes detected – nothing to do.")
        set_output("changed", "false")


if __name__ == "__main__":
    main()
