#!/usr/bin/env python3
"""Check GitHub Copilot models for changes and update pages.

Fetches model information from the GitHub Docs API (model-hosting and
model-comparison pages), compares with stored data, and generates GitHub Pages
content (HTML + RSS feed) when changes are detected.
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
_MODEL_TABLE_COLS = 5

DOCS_URL = "https://docs.github.com/en/copilot/reference/ai-models/supported-models"
_HOSTING_API_URL = (
    "https://docs.github.com/api/article/body"
    "?pathname=/en/copilot/reference/ai-models/model-hosting"
)
_COMPARISON_API_URL = (
    "https://docs.github.com/api/article/body"
    "?pathname=/en/copilot/reference/ai-models/model-comparison"
)
_PRICING_API_URL = (
    "https://docs.github.com/api/article/body"
    "?pathname=/en/copilot/reference/copilot-billing/models-and-pricing"
)
_SUPPORTED_MODELS_HTML_URL = DOCS_URL

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


def _parse_hosting_page(md: str) -> dict:
    """Extract model names and providers from the model-hosting markdown.

    The page groups models under h2 headings such as ``## OpenAI models``
    and lists them as a bullet list after a ``Used for:`` label.
    For the xAI section the model name appears in prose instead of a list.
    """
    models: dict = {}

    # Order matters: more-specific patterns must come before shorter ones.
    provider_sections = [
        ("OpenAI models fine-tuned by Microsoft", "Microsoft"),
        ("OpenAI models", "OpenAI"),
        ("Anthropic models", "Anthropic"),
        ("Google models", "Google"),
        ("xAI models", "xAI"),
    ]

    current_provider: str | None = None
    in_used_for = False

    for line in md.splitlines():
        stripped = line.strip()

        # Detect a provider section header (e.g. "## OpenAI models")
        new_provider = None
        for section, provider in provider_sections:
            if stripped == f"## {section}":
                new_provider = provider
                break

        if new_provider is not None:
            current_provider = new_provider
            in_used_for = False
            continue

        # Any other h2 resets the current provider context
        if stripped.startswith("## "):
            current_provider = None
            in_used_for = False
            continue

        # "Used for:" starts the bullet-list of model names
        if stripped == "Used for:":
            in_used_for = True
            continue

        # Non-blank, non-list content ends the bullet-list section
        if in_used_for and stripped and not stripped.startswith("* "):
            in_used_for = False

        # Extract model names from the bullet list
        if in_used_for and current_provider and stripped.startswith("* "):
            name = stripped[2:].strip()
            if name and name not in models:
                models[name] = {
                    "name": name,
                    "provider": current_provider,
                    "release_status": "",
                    "multiplier_paid": "",
                    "multiplier_free": "",
                }

    # The xAI section uses prose ("xAI operates MODEL in GitHub Copilot")
    # instead of a bullet list, so we extract those names with a regex.
    # The pattern matches a model name that starts and ends with an alphanumeric
    # character and may contain letters, digits, spaces, dots, and hyphens in
    # the middle (e.g. "Grok Code Fast 1").
    for m in re.finditer(
        r"xAI operates ([A-Za-z0-9][A-Za-z0-9 .\-]*[A-Za-z0-9]) in GitHub Copilot",
        md,
    ):
        name = m.group(1).strip()
        if name and name not in models:
            models[name] = {
                "name": name,
                "provider": "xAI",
                "release_status": "",
                "multiplier_paid": "",
                "multiplier_free": "",
            }

    return models


def _parse_comparison_page(md: str, existing: dict) -> dict:
    """Supplement *existing* models with any found in the model-comparison page.

    The comparison page contains two-column markdown tables whose first column
    holds the model name (e.g. ``| GPT-5.1-Codex | Delivers… |``).
    Header rows, separator rows, and known non-model strings are skipped.
    """
    _SKIP_NAMES = {
        "model",
        "available models in chat",
        "model name",
    }

    models = dict(existing)

    for line in md.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|")[1:-1]]
        if not cells:
            continue
        # Strip footnote markers (e.g. "MAI-Code-1-Flash[^note]") so names
        # dedupe against the footnote-free names from the hosting page.
        name = _strip_footnotes(cells[0])
        # Skip header rows, separator rows, and empty/known-non-model cells
        if not name or name.startswith("-") or name.lower() in _SKIP_NAMES:
            continue
        # Model names start with a capital letter; this filters out separator
        # rows (e.g. "---") and lower-case prose text that may appear in tables.
        if name[0].isupper() and name not in models:
            models[name] = {
                "name": name,
                "provider": "",
                "release_status": "",
                "multiplier_paid": "",
                "multiplier_free": "",
            }

    return models


def _parse_html_tables(html: str) -> list[list[list[str]]]:
    """Extract all ``<table>`` elements from *html* as lists of rows.

    Each table is a list of rows, and each row is a list of cell texts
    (from ``<th>`` and ``<td>`` elements).  HTML tags inside cells are
    stripped; ``aria-label`` attributes on SVG icons are preserved as the
    cell text (e.g. ``"Included"`` / ``"Not included"``).
    """
    tables: list[list[list[str]]] = []
    for table_m in re.finditer(r"<table.*?</table>", html, re.DOTALL):
        rows: list[list[str]] = []
        for row_m in re.finditer(r"<tr.*?>(.*?)</tr>", table_m.group(), re.DOTALL):
            cells: list[str] = []
            for cell_m in re.finditer(
                r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row_m.group(1), re.DOTALL
            ):
                cell_html = cell_m.group(1)
                # If the cell contains an SVG icon, use its aria-label
                aria = re.search(r'aria-label="([^"]+)"', cell_html)
                if aria:
                    cells.append(aria.group(1))
                else:
                    # Strip remaining HTML tags and normalise whitespace
                    cells.append(clean_text(re.sub(r"<[^>]+>", "", cell_html)))
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _enrich_from_supported_models_html(html: str, models: dict) -> None:
    """Update *models* in-place with release status and multiplier data.

    Parses the rendered HTML of the supported-models page which contains
    tables with release status and multiplier information that are not
    available from the markdown API endpoints.
    """
    tables = _parse_html_tables(html)

    for table in tables:
        if not table:
            continue
        header = [h.lower() for h in table[0]]

        # Release-status table: "model name | provider | release status | …"
        if "release status" in header:
            status_idx = header.index("release status")
            for row in table[1:]:
                name = row[0] if row else ""
                if name in models and status_idx < len(row):
                    models[name]["release_status"] = row[status_idx]

        # Multiplier table: "model | multiplier for paid plans | multiplier for copilot free"
        paid_col = next(
            (i for i, h in enumerate(header) if "paid" in h), None
        )
        free_col = next(
            (i for i, h in enumerate(header) if "free" in h), None
        )
        if paid_col is not None or free_col is not None:
            for row in table[1:]:
                name = row[0] if row else ""
                if name in models:
                    if paid_col is not None and paid_col < len(row):
                        models[name]["multiplier_paid"] = row[paid_col]
                    if free_col is not None and free_col < len(row):
                        models[name]["multiplier_free"] = row[free_col]


def _strip_footnotes(name: str) -> str:
    """Remove Markdown footnote markers from a model name.

    e.g. ``"Claude Sonnet 5[^promo]"`` -> ``"Claude Sonnet 5"``.
    """
    return re.sub(r"\[\^[^\]]*\]", "", name).strip()


def _parse_price(raw: str) -> float | None:
    """Parse a price cell such as ``$1.00`` / ``$0.025`` into a float.

    Returns ``None`` for empty cells or non-numeric placeholders like
    ``Not applicable``.
    """
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned.lower() == "not applicable":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iter_markdown_tables(md: str):
    """Yield ``(header, rows)`` for each pipe-delimited table in *md*.

    ``header`` is a list of lowercased column names; ``rows`` is a list of
    cell-lists (raw cell text).  Separator rows (``| --- |``) and fully-blank
    spacer rows are skipped.  A blank line ends the current table.
    """
    header: list[str] | None = None
    rows: list[list[str]] = []

    def _cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().split("|")[1:-1]]

    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _cells(stripped)
            # Separator row, e.g. "| --- | --- |"
            if cells and all(set(c) <= {"-", ":"} and c for c in cells):
                continue
            if header is None:
                header = [c.lower() for c in cells]
                continue
            # Skip fully-blank spacer rows used between data rows.
            if not any(cells):
                continue
            rows.append(cells)
        else:
            if header is not None:
                yield header, rows
            header, rows = None, []

    if header is not None:
        yield header, rows


def _parse_pricing_markdown(md: str) -> dict:
    """Parse the Copilot models-and-pricing page markdown into per-model data.

    Returns a dict keyed by model name.  Each value is a ``pricing`` block
    shaped to match the consumer's ``copilotPricing`` schema::

        {
            "category": "Lightweight",        # model class, when present
            "releaseStatus": "GA",            # when present
            "inputCostPerMillion": 1.0,       # Default tier
            "cachedInputCostPerMillion": 0.1,
            "outputCostPerMillion": 6.0,
            "cacheCreationCostPerMillion": 1.25,   # only when a cache-write column exists
            "longContext": {                        # only when a Long-context row exists
                "threshold": "> 200K",
                "inputCostPerMillion": 2.0,
                "cachedInputCostPerMillion": 0.2,
                "outputCostPerMillion": 9.0
            }
        }
    """
    pricing_by_model: dict = {}

    for header, rows in _iter_markdown_tables(md):
        # Identify a pricing table by the presence of an Input + Output column.
        try:
            input_idx = header.index("input")
            output_idx = header.index("output")
        except ValueError:
            continue

        name_idx = header.index("model") if "model" in header else 0
        cached_idx = next(
            (i for i, h in enumerate(header) if "cached input" in h), None
        )
        cachewrite_idx = next(
            (i for i, h in enumerate(header) if "cache write" in h), None
        )
        status_idx = next(
            (i for i, h in enumerate(header) if "release status" in h), None
        )
        category_idx = next(
            (i for i, h in enumerate(header) if "category" in h), None
        )
        tier_idx = header.index("tier") if "tier" in header else None
        threshold_idx = next(
            (i for i, h in enumerate(header) if "threshold" in h), None
        )

        def cell(row: list[str], idx: int | None) -> str:
            return row[idx] if idx is not None and idx < len(row) else ""

        for row in rows:
            name = _strip_footnotes(cell(row, name_idx))
            if not name:
                continue

            in_price = _parse_price(cell(row, input_idx))
            out_price = _parse_price(cell(row, output_idx))
            if in_price is None and out_price is None:
                continue

            tier_value = cell(row, tier_idx).lower()
            is_long_context = tier_value == "long context"

            block = pricing_by_model.setdefault(name, {})

            # Metadata (release status / category) taken from the Default row.
            if not is_long_context:
                status = cell(row, status_idx)
                category = cell(row, category_idx)
                if status and "releaseStatus" not in block:
                    block["releaseStatus"] = status
                if category and "category" not in block:
                    block["category"] = category

            tier_data = {
                "inputCostPerMillion": in_price,
                "cachedInputCostPerMillion": _parse_price(cell(row, cached_idx)),
                "outputCostPerMillion": out_price,
            }
            cache_write = _parse_price(cell(row, cachewrite_idx))
            if cache_write is not None:
                tier_data["cacheCreationCostPerMillion"] = cache_write

            # Drop keys that couldn't be parsed to keep the block clean.
            tier_data = {k: v for k, v in tier_data.items() if v is not None}

            if is_long_context:
                threshold = cell(row, threshold_idx)
                lc: dict[str, object] = dict(tier_data)
                if threshold and threshold.lower() != "not applicable":
                    lc["threshold"] = threshold
                block["longContext"] = lc
            else:
                block.update(tier_data)
                threshold = cell(row, threshold_idx)
                if threshold and threshold.lower() != "not applicable":
                    block["threshold"] = threshold

    return pricing_by_model


def _enrich_from_pricing(md: str, models: dict) -> None:
    """Attach a ``pricing`` block to each model found on the pricing page.

    Matches by exact model name first, then case-insensitively.  Additive:
    models without a pricing-page entry are left untouched.
    """
    pricing_by_model = _parse_pricing_markdown(md)
    if not pricing_by_model:
        return

    # Case-insensitive lookup of existing model keys, ignoring footnote markers
    # that may appear in scraped model names (e.g. "MAI-Code-1-Flash[^note]").
    lower_index = {_strip_footnotes(name).lower(): name for name in models}

    for price_name, block in pricing_by_model.items():
        target = None
        if price_name in models:
            target = price_name
        else:
            target = lower_index.get(price_name.lower())
        if target is not None and block:
            models[target]["pricing"] = block


def scrape_models() -> dict:
    """Fetch model information from GitHub Docs API endpoints.

    Uses the model-hosting page as the primary source for providers, and the
    model-comparison page to fill in any models not listed there.  The
    rendered supported-models HTML page is used to obtain release status
    and multiplier data.  The models-and-pricing page supplies per-token
    pricing (Default and optional Long-context tiers).  Returns a dict keyed
    by model name.
    """
    print("Fetching GitHub Docs model information…")

    hosting_md = _fetch_text(_HOSTING_API_URL)
    models = _parse_hosting_page(hosting_md)

    comparison_md = _fetch_text(_COMPARISON_API_URL)
    models = _parse_comparison_page(comparison_md, models)

    supported_html = _fetch_text(_SUPPORTED_MODELS_HTML_URL)
    _enrich_from_supported_models_html(supported_html, models)

    try:
        pricing_md = _fetch_text(_PRICING_API_URL)
        _enrich_from_pricing(pricing_md, models)
    except Exception as e:  # pricing is additive; don't fail the whole run
        print(f"Warning: could not fetch/parse pricing page: {e}")

    print(f"Extracted {len(models)} model(s)")
    return models


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _format_price(value) -> str:
    if value is None:
        return ""
    try:
        return f"${float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _format_pricing_summary(pricing: dict | None) -> str:
    """Return a short human-readable summary of a model's Default-tier pricing."""
    if not pricing:
        return ""
    in_price = _format_price(pricing.get("inputCostPerMillion"))
    out_price = _format_price(pricing.get("outputCostPerMillion"))
    if not in_price and not out_price:
        return ""
    return f"{in_price} in / {out_price} out per 1M tokens"


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
        pricing_summary = _format_pricing_summary(m.get("pricing"))
        if pricing_summary:
            desc += f", Pricing: {pricing_summary}"
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
        if o.get("pricing") != n.get("pricing"):
            old_summary = _format_pricing_summary(o.get("pricing"))
            new_summary = _format_pricing_summary(n.get("pricing"))
            if old_summary and new_summary and old_summary != new_summary:
                field_changes.append(f"Pricing changed from '{old_summary}' to '{new_summary}'")
            elif new_summary and not old_summary:
                field_changes.append(f"Pricing added: {new_summary}")
            elif old_summary and not new_summary:
                field_changes.append("Pricing removed")
            else:
                field_changes.append("Pricing updated")
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
        pricing = info.get("pricing") or {}
        in_price = _format_price(pricing.get("inputCostPerMillion"))
        out_price = _format_price(pricing.get("outputCostPerMillion"))
        model_rows.append(
            f"      <tr>\n"
            f"        <td>{name}</td>\n"
            f"        <td>{info.get('provider', '')}</td>\n"
            f"        <td>{info.get('release_status', '')}</td>\n"
            f"        <td>{in_price}</td>\n"
            f"        <td>{out_price}</td>\n"
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
        <th>Input Price (per 1M tokens)</th>
        <th>Output Price (per 1M tokens)</th>
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
        release_notes += "| Model | Provider | Input Price (per 1M) | Output Price (per 1M) |\n"
        release_notes += "| ----- | -------- | --------------------- | ----------------------- |\n"
        for name, info in sorted(new_models.items()):
            pricing = info.get("pricing") or {}
            in_price = _format_price(pricing.get("inputCostPerMillion"))
            out_price = _format_price(pricing.get("outputCostPerMillion"))
            release_notes += (
                f"| {name} | {info.get('provider', '')} | {in_price} | {out_price} |\n"
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
