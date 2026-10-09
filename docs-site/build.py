#!/usr/bin/env python3
"""Static site generator for the new_DRUID / New_sage documentation site.

Usage:
    python3 build.py            # run from ~/workspace/druid-sage-docs/

Reads Markdown from src/, writes HTML to site/, copies assets/style.css,
then runs the internal link checker.

Missing src files are warned about and skipped (not fatal), so the
generator can be tested before all pages exist. The sidebar nav is built
from the pages that were actually generated, in PAGES order.
"""

import html
import os
import re
import shutil
import sys

import markdown

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
SITE_DIR = os.path.join(BASE_DIR, "site")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

# (md_filename, title, group) — exact order is the nav order.
PAGES = [
    ("index.md", "Home", "Home"),
    ("sage-overview.md", "Overview", "New_sage"),
    ("sage-architecture.md", "Architecture & data flow", "New_sage"),
    ("sage-mcp-tools.md", "MCP tools reference", "New_sage"),
    ("sage-ingestion-freshness.md", "Ingestion & freshness", "New_sage"),
    ("sage-deployment.md", "Deployment", "New_sage"),
    ("sage-configuration.md", "Configuration reference", "New_sage"),
    ("druid-overview.md", "Overview", "new_DRUID"),
    ("druid-architecture.md", "Architecture & services map", "new_DRUID"),
    ("druid-mcp-tools.md", "MCP tools reference", "new_DRUID"),
    ("druid-web-ui.md", "Web UI user guide", "new_DRUID"),
    ("druid-api-reference.md", "Document & Admin API reference", "new_DRUID"),
    ("druid-deployment.md", "Deployment runbook", "new_DRUID"),
    ("druid-operations.md", "Operations", "new_DRUID"),
    ("druid-troubleshooting.md", "Troubleshooting", "new_DRUID"),
    ("druid-configuration.md", "Configuration reference", "new_DRUID"),
    ("shared-artifact-policy.md", "Artifact & model policy", "Shared"),
    ("shared-offline-ops.md", "Offline-first operations", "Shared"),
]

MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists"]

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &middot; DRUID &amp; SAGE docs</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
{nav}
<main class="content" id="content">
{body}
</main>
</body>
</html>
"""


def toc_slugify(value):
    """Replicate markdown's ``toc`` extension slugify.

    Lowercase, spaces -> hyphens, remove anything that is not
    alphanumeric, a hyphen, or an underscore, then collapse and trim
    hyphens. Header ``id`` attributes in the generated HTML follow this.
    """
    value = value.lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9\-_]", "", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def html_filename(md_filename):
    base = os.path.splitext(os.path.basename(md_filename))[0]
    return base + ".html"


def build_nav(generated, current_md):
    """Sidebar nav: Home link at top, then New_sage / new_DRUID / Shared groups."""
    home_links = []
    groups = {}  # group -> list of (md, title), in PAGES order
    for md_name, title, group in generated:
        if group == "Home":
            home_links.append((md_name, title))
        else:
            groups.setdefault(group, []).append((md_name, title))

    parts = ['<nav class="sidebar" aria-label="Documentation">']
    parts.append('<div class="brand"><a href="index.html">DRUID &amp; SAGE</a>'
                 '<span class="brand-sub">docs</span></div>')
    for md_name, title in home_links:
        cls = ' class="home-link active"' if md_name == current_md else ' class="home-link"'
        cur = ' aria-current="page"' if md_name == current_md else ""
        parts.append(f'<a{cls}{cur} href="{html_filename(md_name)}">{html.escape(title)}</a>')
    for group, items in groups.items():
        parts.append('<section class="nav-group">')
        parts.append(f"<h2>{html.escape(group)}</h2>")
        parts.append("<ul>")
        for md_name, title in items:
            cls = ' class="active"' if md_name == current_md else ""
            cur = ' aria-current="page"' if md_name == current_md else ""
            parts.append(
                f'<li><a{cls}{cur} href="{html_filename(md_name)}">'
                f"{html.escape(title)}</a></li>"
            )
        parts.append("</ul>")
        parts.append("</section>")
    parts.append("</nav>")
    return "\n".join(parts)


def render_page(md_name, title, group, body, generated):
    nav = build_nav(generated, md_name)
    return PAGE_TEMPLATE.format(
        title=html.escape(title), nav=nav, body=body
    )


# ---------------------------------------------------------------------------
# Link checker
# ---------------------------------------------------------------------------

HREF_RE = re.compile(r'href="([^"]+)"')
ID_RE = re.compile(r'\sid="([^"]+)"')
EXTERNAL_PREFIXES = (
    "http://", "https://", "//", "mailto:", "data:", "tel:", "javascript:",
)


def collect_ids(html_text):
    return set(ID_RE.findall(html_text))


def check_links(site_dir, html_paths):
    """Verify every internal link in the generated HTML.

    Checks hrefs that point at .html targets (relative, optional #anchor)
    and same-page #anchors. Returns True if everything passed.
    """
    ids_by_file = {}
    for path in html_paths:
        with open(path, encoding="utf-8") as fh:
            ids_by_file[path] = collect_ids(fh.read())

    failures = []
    checked = 0
    warned = 0

    def report(ok, message):
        print(("PASS: " if ok else "FAIL: ") + message)
        if not ok:
            failures.append(message)

    for path in sorted(html_paths):
        rel = os.path.relpath(path, site_dir)
        with open(path, encoding="utf-8") as fh:
            hrefs = HREF_RE.findall(fh.read())
        for href in hrefs:
            if not href or href.startswith(EXTERNAL_PREFIXES):
                continue
            if href.startswith("#"):
                # Same-page anchor.
                anchor = href[1:]
                if not anchor:
                    continue
                checked += 1
                if anchor in ids_by_file[path]:
                    report(True, f"{rel} #{anchor} -> same page")
                else:
                    hint = toc_slugify(anchor)
                    extra = (
                        f" (hint: toc would slugify this as '{hint}')"
                        if hint != anchor and hint in ids_by_file[path]
                        else ""
                    )
                    report(False, f"{rel} #{anchor} -> anchor not found on page{extra}")
                continue
            target_path, _, anchor = href.partition("#")
            if target_path.endswith(".html"):
                # Internal page link.
                target = os.path.normpath(os.path.join(os.path.dirname(path), target_path))
                checked += 1
                if not os.path.isfile(target):
                    report(False, f"{rel} -> {target_path} (target file missing)")
                elif anchor and anchor not in ids_by_file.get(target, set()):
                    ids = ids_by_file.get(target, set())
                    hint = toc_slugify(anchor)
                    extra = (
                        f" (hint: toc would slugify this as '{hint}')"
                        if hint != anchor and hint in ids
                        else ""
                    )
                    report(
                        False,
                        f"{rel} -> {target_path}#{anchor} (anchor not found){extra}",
                    )
                else:
                    suffix = f"#{anchor}" if anchor else ""
                    report(True, f"{rel} -> {target_path}{suffix}")
            elif target_path:
                # Other relative resource (e.g. style.css): warn, don't fail.
                target = os.path.normpath(os.path.join(os.path.dirname(path), target_path))
                if not os.path.isfile(target):
                    print(f"WARN: {rel} -> {target_path} (asset missing)")
                    warned += 1

    print(f"\nLink check: {checked} link(s) checked, "
          f"{len(failures)} failure(s), {warned} warning(s).")
    return not failures


def main():
    generated = []  # (md_name, title, group)
    bodies = {}
    for md_name, title, group in PAGES:
        src = os.path.join(SRC_DIR, md_name)
        if not os.path.isfile(src):
            print(f"WARN: missing src/{md_name} -- skipping", file=sys.stderr)
            continue
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        bodies[md_name] = markdown.markdown(text, extensions=MD_EXTENSIONS)
        generated.append((md_name, title, group))

    os.makedirs(SITE_DIR, exist_ok=True)
    # Clear stale pages so removed/renamed sources don't linger.
    for entry in os.listdir(SITE_DIR):
        if entry.endswith(".html"):
            os.remove(os.path.join(SITE_DIR, entry))

    for md_name, title, group in generated:
        page = render_page(md_name, title, group, bodies[md_name], generated)
        out = os.path.join(SITE_DIR, html_filename(md_name))
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(page)
        print(f"wrote site/{html_filename(md_name)}")

    css_src = os.path.join(ASSETS_DIR, "style.css")
    if os.path.isfile(css_src):
        shutil.copy2(css_src, os.path.join(SITE_DIR, "style.css"))
        print("wrote site/style.css")
    else:
        print("WARN: assets/style.css not found -- skipping copy", file=sys.stderr)

    html_paths = [
        os.path.join(SITE_DIR, html_filename(md_name))
        for md_name, _, _ in generated
    ]
    print("\n--- link check ---")
    ok = check_links(SITE_DIR, html_paths)
    if not ok:
        print("\nLink check FAILED.", file=sys.stderr)
        sys.exit(1)
    print("\nLink check passed.")


if __name__ == "__main__":
    main()
