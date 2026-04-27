#!/usr/bin/env python3
"""Check GitHub Copilot models for changes and update pages.

Fetches model information from the GitHub Copilot billing/pricing docs page,
compares with stored data, and generates GitHub Pages content (HTML + RSS feed)
when changes are detected.
"""

import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from email.utils import formatdate

# Number of columns in the model table (used for the empty-state row)
_MODEL_TABLE_COLS = 8

DOCS_URL = "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
_BILLING_HTML_URL = DOCS_URL

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

def _fetch_text(url: str) -> str:
    """Fetch *url* and return the response body as decoded text."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "github-copilot-model-notifier/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def _parse_billing_page(html: str) -> dict:
    """Parse the billing/models-and-pricing HTML page to extract model data.

    Iterates through h2/h3 headings and table elements in document order.
    Provider names are taken from h3 headings in the Pricing tables section.
    Pricing data is extracted from tables that contain an ``Input`` column.
    """
    _PROVIDER_MAP = {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "google": "Google",
        "xai": "xAI",
        "fine-tuned (github)": "GitHub",
    }

    def _cell_text(cell_html: str) -> str:
        """Return plain text for a table cell, stripping footnote superscripts."""
        cell_html = re.sub(
            r"<sup[^>]*>.*?</sup>", "", cell_html, flags=re.DOTALL | re.IGNORECASE
        )
        return clean_text(re.sub(r"<[^>]+>", "", cell_html))

    def _col_idx(header: list, name: str) -> int | None:
        """Return the index of *name* in *header*, or None if absent."""
        try:
            return header.index(name)
        except ValueError:
            return None

    def _col_containing(header: list, fragment: str) -> int | None:
        """Return the first header index whose text contains *fragment*, or None."""
        return next((i for i, h in enumerate(header) if fragment in h), None)

    def _cell(row: list, idx: int | None) -> str:
        """Return the cell value at *idx* in *row*, or an empty string."""
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    def _price(row: list, idx: int | None) -> str:
        """Return a price cell value stripped of its leading dollar sign."""
        return _cell(row, idx).lstrip("$")

    models: dict = {}
    current_provider: str | None = None

    for m in re.finditer(
        r"(<h[23][^>]*>.*?</h[23]>|<table[^>]*>.*?</table>)",
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        elem = m.group()
        level_m = re.match(r"<(h[23])", elem, re.IGNORECASE)

        if level_m:
            level = level_m.group(1).lower()
            text = clean_text(re.sub(r"<[^>]+>", "", elem)).lower()
            if level == "h2":
                # h2 boundaries reset the provider context
                current_provider = None
            else:
                # h3: set provider if the heading text matches a known provider
                current_provider = _PROVIDER_MAP.get(text)
            continue

        if not re.match(r"<table", elem, re.IGNORECASE):
            continue
        if current_provider is None:
            continue

        # Parse this table into rows of cell texts
        rows: list[list[str]] = []
        for row_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", elem, re.DOTALL | re.IGNORECASE):
            cells = [
                _cell_text(c.group(1))
                for c in re.finditer(
                    r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>",
                    row_m.group(1),
                    re.DOTALL | re.IGNORECASE,
                )
            ]
            if cells:
                rows.append(cells)

        if len(rows) < 2:
            continue

        header = [h.lower() for h in rows[0]]

        # Skip tables that don't have an Input pricing column
        if "input" not in header:
            continue

        input_idx = _col_idx(header, "input")
        cached_idx = _col_idx(header, "cached input")
        write_idx = _col_containing(header, "cache write")
        output_idx = _col_idx(header, "output")
        status_idx = _col_idx(header, "release status")
        category_idx = _col_idx(header, "category")

        for row in rows[1:]:
            name = row[0].strip() if row else ""
            if not name or name.startswith("-") or name.lower() == "model":
                continue

            models[name] = {
                "name": name,
                "provider": current_provider,
                "release_status": _cell(row, status_idx),
                "category": _cell(row, category_idx),
                "input_price": _price(row, input_idx),
                "cached_input_price": _price(row, cached_idx),
                "cache_write_price": _price(row, write_idx),
                "output_price": _price(row, output_idx),
            }

    return models


def scrape_models() -> dict:
    """Fetch model pricing information from the GitHub Copilot billing page.

    Parses the rendered billing/models-and-pricing HTML page to extract model
    names, providers, release statuses, categories, and per-token pricing.
    Returns a dict keyed by model name.
    """
    print("Fetching GitHub Copilot billing/pricing information…")
    html = _fetch_text(_BILLING_HTML_URL)
    models = _parse_billing_page(html)
    print(f"Extracted {len(models)} model(s)")
    return models


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
        if m.get("category"):
            desc += f", Category: {m['category']}"
        if m.get("input_price"):
            desc += f", Input: ${m['input_price']}/1M tokens"
        if m.get("output_price"):
            desc += f", Output: ${m['output_price']}/1M tokens"
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
        if o.get("release_status") != n.get("release_status"):
            field_changes.append(
                f"Release status changed from '{o.get('release_status')}' to '{n.get('release_status')}'"
            )
        if o.get("category") != n.get("category"):
            field_changes.append(
                f"Category changed from '{o.get('category')}' to '{n.get('category')}'"
            )
        for price_field, label in (
            ("input_price", "Input price"),
            ("cached_input_price", "Cached input price"),
            ("cache_write_price", "Cache write price"),
            ("output_price", "Output price"),
        ):
            if o.get(price_field) != n.get(price_field):
                field_changes.append(
                    f"{label} changed from"
                    f" '${o.get(price_field, '')}' to '${n.get(price_field, '')}' per 1M tokens"
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

    def _price(val: str) -> str:
        return f"${val}" if val else "N/A"

    model_rows = []
    for name, info in sorted(models.items()):
        model_rows.append(
            f"      <tr>\n"
            f"        <td>{name}</td>\n"
            f"        <td>{info.get('provider', '')}</td>\n"
            f"        <td>{info.get('release_status', '')}</td>\n"
            f"        <td>{info.get('category', '')}</td>\n"
            f"        <td>{_price(info.get('input_price', ''))}</td>\n"
            f"        <td>{_price(info.get('cached_input_price', ''))}</td>\n"
            f"        <td>{_price(info.get('cache_write_price', ''))}</td>\n"
            f"        <td>{_price(info.get('output_price', ''))}</td>\n"
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
    @media (prefers-color-scheme: dark) {{
      body {{ background-color: #0d1117; color: #e6edf3; }}
      h1 {{ border-bottom-color: #30363d; }}
      h2 {{ color: #e6edf3; }}
      a {{ color: #58a6ff; }}
      th, td {{ border-color: #30363d; }}
      th {{ background-color: #161b22; }}
      tr:hover td {{ background-color: #161b22; }}
      .subscribe {{ background: #161b22; border-color: #30363d; }}
      .subscribe a {{ color: #58a6ff; }}
      .change-item {{ border-color: #30363d; }}
      .change-item a {{ color: #58a6ff; }}
      .date {{ color: #8b949e; }}
      .updated {{ color: #8b949e; }}
      .updated a {{ color: #58a6ff; }}
      footer {{ border-top-color: #30363d; color: #8b949e; }}
      footer a {{ color: #58a6ff; }}
    }}
  </style>
</head>
<body>
  <h1>&#x1F916; GitHub Copilot Model Updates</h1>
  <p>Automatically tracks changes to <a href="{DOCS_URL}">GitHub Copilot's AI model pricing</a>,
  including new models, retired models, and pricing changes. All prices are per 1 million tokens.</p>

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
        <th>Category</th>
        <th>Input ($/1M)</th>
        <th>Cached Input ($/1M)</th>
        <th>Cache Write ($/1M)</th>
        <th>Output ($/1M)</th>
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
    <description>Notifications about changes to GitHub Copilot models including new models, removed models, and pricing changes.</description>
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
        release_notes += "| Model | Provider | Category | Input ($/1M) | Cached Input ($/1M) | Cache Write ($/1M) | Output ($/1M) |\n"
        release_notes += "| ----- | -------- | -------- | ------------ | ------------------- | ------------------ | ------------- |\n"
        for name, info in sorted(new_models.items()):
            def _p(field: str) -> str:
                v = info.get(field, "")
                return f"${v}" if v else "N/A"
            release_notes += (
                f"| {name} | {info.get('provider', '')} | {info.get('category', '')} |"
                f" {_p('input_price')} | {_p('cached_input_price')} |"
                f" {_p('cache_write_price')} | {_p('output_price')} |\n"
            )

        # Write release notes to a temp file for the workflow to consume
        release_notes_path = os.path.join(tempfile.gettempdir(), "release_notes.md")
        with open(release_notes_path, "w") as fh:
            fh.write(release_notes)

        set_output("release_notes_path", release_notes_path)
        set_output("changed", "true")
        set_output("tag", tag)
        set_output("title", title)
    else:
        print("No changes detected – nothing to do.")
        set_output("changed", "false")


if __name__ == "__main__":
    main()
