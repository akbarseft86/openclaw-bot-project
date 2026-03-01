#!/usr/bin/env python3
"""
prompt_db_v2.py — Prompt Database Manager v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Manages prompt storage with Prompt Pack support.
YAML-frontmatter blocks in per-category .md files.

CLI:
  python3 prompt_db_v2.py save "<title>" "<prompt_text>"
  python3 prompt_db_v2.py save-pack "<raw_text>"
  python3 prompt_db_v2.py search "<query>"
  python3 prompt_db_v2.py search-packs "<query>"
  python3 prompt_db_v2.py list-all
  python3 prompt_db_v2.py list-category "<category>"
  python3 prompt_db_v2.py get "<slug>"
  python3 prompt_db_v2.py remove "<slug>"
"""

import os
import re
import sys
import json
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

PROMPTS_DIR = os.path.expanduser("~/.openclaw/workspace/prompts_v2")

CATEGORY_KEYWORDS = {
    "Landing Page & Website": [
        "landing page", "hero", "section", "wireframe", "layout",
        "above the fold", "website", "web page", "homepage",
    ],
    "Marketing & Ads": [
        "ads", "iklan", "campaign", "creative", "conversion", "funnel",
        "meta ads", "google ads", "retargeting", "targeting",
    ],
    "Content & Social Media": [
        "caption", "content", "reels", "tiktok", "thread", "threads",
        "script", "hook", "instagram", "social media", "konten",
    ],
    "Email & CRM": [
        "email", "newsletter", "sequence", "crm", "follow up",
        "autoresponder", "drip",
    ],
    "Business & Offer": [
        "offer", "tripwire", "pricing", "productized", "client",
        "proposal", "business", "bisnis", "penawaran",
    ],
    "Coding & Automation": [
        "code", "api", "script", "python", "deploy", "automation",
        "coding", "programming", "bot", "developer",
    ],
}

# Map category names to safe filenames
CATEGORY_FILES = {
    "Landing Page & Website": "Landing_Page_And_Website.md",
    "Marketing & Ads": "Marketing_And_Ads.md",
    "Content & Social Media": "Content_And_Social_Media.md",
    "Email & CRM": "Email_And_CRM.md",
    "Business & Offer": "Business_And_Offer.md",
    "Coding & Automation": "Coding_And_Automation.md",
}

DEFAULT_CATEGORY = "Content & Social Media"


# ══════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    s = text.lower().strip()
    s = re.sub(r'[^\w\s-]', '', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    s = s.strip('-')
    return s[:80] if s else "untitled"


def ensure_structure():
    """Create prompts_v2 directory and category files if missing."""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    for cat_name, filename in CATEGORY_FILES.items():
        filepath = os.path.join(PROMPTS_DIR, filename)
        if not os.path.exists(filepath):
            with open(filepath, "w") as f:
                f.write(f"# {cat_name}\n\n")
            os.chmod(filepath, 0o644)


def auto_classify(prompt_text: str, title: str = "") -> str:
    """Auto-classify prompt into a category based on keywords."""
    combined = (title + " " + prompt_text).lower()
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > 0:
            scores[cat] = score
    if not scores:
        return DEFAULT_CATEGORY
    return max(scores, key=scores.get)


def category_filepath(category: str) -> str:
    """Get filepath for a category."""
    filename = CATEGORY_FILES.get(category)
    if not filename:
        filename = CATEGORY_FILES.get(DEFAULT_CATEGORY)
    return os.path.join(PROMPTS_DIR, filename)


# ══════════════════════════════════════════════════════════════════════
# YAML BLOCK PARSING
# ══════════════════════════════════════════════════════════════════════

BLOCK_PATTERN = re.compile(
    r'^---\n(.*?)\n---\n',
    re.MULTILINE | re.DOTALL
)


def parse_yaml_block(block_text: str) -> dict:
    """Parse a simple YAML frontmatter block into a dict."""
    result = {}
    current_key = None
    multiline_buf = []

    for line in block_text.split('\n'):
        # Check for key: value pattern
        m = re.match(r'^(\w[\w_]*)\s*:\s*(.*)', line)
        if m:
            # Save previous multiline if any
            if current_key and multiline_buf:
                result[current_key] = '\n'.join(multiline_buf).strip()
                multiline_buf = []

            key = m.group(1)
            val = m.group(2).strip()

            if val == '|':
                current_key = key
                multiline_buf = []
            elif val.startswith('[') and val.endswith(']'):
                # Parse simple list
                items = [x.strip().strip('"').strip("'")
                         for x in val[1:-1].split(',') if x.strip()]
                result[key] = items
            else:
                result[key] = val
                current_key = None
        elif current_key is not None:
            multiline_buf.append(line)

    if current_key and multiline_buf:
        result[current_key] = '\n'.join(multiline_buf).strip()

    return result


def serialize_yaml_block(entry: dict) -> str:
    """Serialize a dict into a YAML frontmatter block."""
    lines = ["---"]
    for key, val in entry.items():
        if isinstance(val, list):
            lines.append(f"{key}: [{', '.join(val)}]")
        elif isinstance(val, str) and '\n' in val:
            lines.append(f"{key}: |")
            for vline in val.split('\n'):
                lines.append(f"  {vline}")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return '\n'.join(lines) + '\n'


def read_all_entries() -> list[dict]:
    """Read all prompt entries from all category files."""
    ensure_structure()
    entries = []
    for filename in CATEGORY_FILES.values():
        filepath = os.path.join(PROMPTS_DIR, filename)
        if not os.path.exists(filepath):
            continue
        with open(filepath, 'r') as f:
            content = f.read()

        for match in BLOCK_PATTERN.finditer(content):
            block = match.group(1)
            entry = parse_yaml_block(block)
            if entry.get('slug'):
                entries.append(entry)

    return entries


# ══════════════════════════════════════════════════════════════════════
# CORE OPERATIONS
# ══════════════════════════════════════════════════════════════════════

def remove_prompt_by_slug(slug: str) -> bool:
    """Remove all entries with given slug from all category files."""
    ensure_structure()
    removed = False
    for filename in CATEGORY_FILES.values():
        filepath = os.path.join(PROMPTS_DIR, filename)
        if not os.path.exists(filepath):
            continue
        with open(filepath, 'r') as f:
            content = f.read()

        new_content = content
        for match in BLOCK_PATTERN.finditer(content):
            block = match.group(1)
            entry = parse_yaml_block(block)
            if entry.get('slug') == slug:
                new_content = new_content.replace(match.group(0), '')
                removed = True

        if removed:
            # Clean up extra blank lines
            new_content = re.sub(r'\n{3,}', '\n\n', new_content)
            with open(filepath, 'w') as f:
                f.write(new_content)

    return removed


def save_prompt(title: str, prompt_text: str,
                hint_category: str | None = None,
                pack_slug: str | None = None,
                tags: list | None = None) -> dict:
    """Save a single prompt entry."""
    ensure_structure()
    slug = slugify(title)

    # Remove existing entry with same slug (upsert)
    remove_prompt_by_slug(slug)

    category = hint_category or auto_classify(prompt_text, title)
    now = datetime.now().strftime("%Y-%m-%d")

    # Auto-generate tags if not provided
    if not tags:
        tags = [w for w in slug.split('-') if len(w) > 2][:6]

    # Build keywords from title
    keywords = ', '.join(title.lower().split()[:8])

    entry = {
        "title": title,
        "slug": slug,
        "category": category,
        "tags": tags,
        "keywords": keywords,
        "level": "starter",
        "commercial": "public",
        "created_at": now,
        "prompt": prompt_text,
    }

    if pack_slug:
        entry["pack_slug"] = pack_slug

    # Append to category file
    filepath = category_filepath(category)
    with open(filepath, 'a') as f:
        f.write('\n' + serialize_yaml_block(entry))

    return {"slug": slug, "title": title, "category": category, "pack_slug": pack_slug}


# ══════════════════════════════════════════════════════════════════════
# PROMPT PACK PARSING
# ══════════════════════════════════════════════════════════════════════

def parse_prompt_pack(raw_text: str) -> dict:
    """
    Parse a prompt pack from raw user input.
    First non-empty line = pack title.
    Sub-prompts start with numbered pattern: 1) or 1.
    """
    lines = raw_text.strip().split('\n')
    pack_title = ""
    sub_prompts = []

    # Find pack title (first non-empty line)
    start_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            pack_title = stripped
            start_idx = i + 1
            break

    if not pack_title:
        return {"pack_title": "", "pack_slug": "", "sub_prompts": []}

    # Parse sub-prompts
    current_sub_title = ""
    current_sub_lines = []
    num_pattern = re.compile(r'^\s*(\d+)\s*[).]\s*(.*)')

    for line in lines[start_idx:]:
        m = num_pattern.match(line)
        if m:
            # Save previous sub-prompt if exists
            if current_sub_title:
                sub_prompts.append({
                    "title": current_sub_title,
                    "prompt": '\n'.join(current_sub_lines).strip()
                })
            current_sub_title = m.group(2).strip()
            current_sub_lines = []
        else:
            current_sub_lines.append(line)

    # Save last sub-prompt
    if current_sub_title:
        sub_prompts.append({
            "title": current_sub_title,
            "prompt": '\n'.join(current_sub_lines).strip()
        })

    pack_slug = slugify(pack_title)

    return {
        "pack_title": pack_title,
        "pack_slug": pack_slug,
        "sub_prompts": sub_prompts,
    }


def save_prompt_pack(raw_text: str) -> list[dict]:
    """Parse and save a prompt pack."""
    pack = parse_prompt_pack(raw_text)
    if not pack["sub_prompts"]:
        return []

    # Classify pack based on all content
    all_text = pack["pack_title"] + " " + " ".join(
        sp["title"] + " " + sp["prompt"] for sp in pack["sub_prompts"]
    )
    category = auto_classify(all_text, pack["pack_title"])

    results = []
    for sp in pack["sub_prompts"]:
        sub_slug_part = slugify(sp["title"])
        full_title = sp["title"]

        result = save_prompt(
            title=full_title,
            prompt_text=sp["prompt"],
            hint_category=category,
            pack_slug=pack["pack_slug"],
        )
        results.append(result)

    return results


# ══════════════════════════════════════════════════════════════════════
# SEARCH & LISTING
# ══════════════════════════════════════════════════════════════════════

def search_prompts(query: str) -> list[dict]:
    """Search prompts by query in title, tags, keywords, category."""
    entries = read_all_entries()
    q = query.lower()
    results = []
    for e in entries:
        searchable = ' '.join([
            str(e.get('title', '')),
            str(e.get('category', '')),
            str(e.get('keywords', '')),
            ' '.join(e.get('tags', [])) if isinstance(e.get('tags'), list) else str(e.get('tags', '')),
            str(e.get('pack_slug', '')),
        ]).lower()
        if q in searchable:
            results.append(e)
    return results


def search_packs_by_topic(query: str) -> list[dict]:
    """Search and group results by pack_slug."""
    results = search_prompts(query)
    packs = {}
    singles = []

    for r in results:
        ps = r.get('pack_slug', '')
        if ps:
            if ps not in packs:
                packs[ps] = {
                    "pack_slug": ps,
                    "category": r.get('category', ''),
                    "count": 0,
                    "sample_titles": [],
                }
            packs[ps]["count"] += 1
            if len(packs[ps]["sample_titles"]) < 3:
                packs[ps]["sample_titles"].append(r.get('title', ''))
        else:
            singles.append(r)

    return {"packs": list(packs.values()), "singles": singles}


def get_prompt_by_slug(slug: str) -> dict | None:
    """Get a single prompt by slug."""
    entries = read_all_entries()
    for e in entries:
        if e.get('slug') == slug:
            return e
    return None


def list_all() -> dict:
    """Return summary of all prompts grouped by category."""
    entries = read_all_entries()
    by_category = {}
    for e in entries:
        cat = e.get('category', DEFAULT_CATEGORY)
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append({
            "slug": e.get('slug', ''),
            "title": e.get('title', ''),
            "level": e.get('level', ''),
            "pack_slug": e.get('pack_slug', ''),
        })
    return {"total": len(entries), "by_category": by_category}


def list_category(category_query: str) -> list[dict]:
    """List prompts in a specific category (fuzzy match)."""
    entries = read_all_entries()
    q = category_query.lower()
    results = []
    for e in entries:
        cat = e.get('category', '').lower()
        if q in cat:
            results.append({
                "slug": e.get('slug', ''),
                "title": e.get('title', ''),
                "level": e.get('level', ''),
                "pack_slug": e.get('pack_slug', ''),
            })
    return results


# ══════════════════════════════════════════════════════════════════════
# FORMAT OUTPUT (Telegram-friendly, hemat token)
# ══════════════════════════════════════════════════════════════════════

def format_search_results(query: str, data: dict) -> str:
    """Format search results for Telegram output."""
    packs = data.get("packs", [])
    singles = data.get("singles", [])

    lines = []

    if packs:
        lines.append(f"\U0001F4E6 Paket ({len(packs)}):")
        for i, p in enumerate(packs[:5], 1):
            samples = ', '.join(p['sample_titles'][:3])
            lines.append(f"{i}) {p['pack_slug']} \u2013 {p['count']} prompt ({samples})")

    if singles:
        lines.append(f"\n\U0001F4DD Tunggal ({len(singles)}):")
        for i, s in enumerate(singles[:5], 1):
            title = s.get('title', '')[:50]
            lines.append(f"{i}) {s.get('slug', '')} \u2013 {title}")

    if not packs and not singles:
        return f"\u274C Tidak ada prompt untuk '{query}'."

    lines.append(f"\nKetik: lihat paket: [slug] atau pakai: [slug]")
    return '\n'.join(lines)


def format_list_overview(data: dict) -> str:
    """Format list-all output."""
    lines = [f"\U0001F4CA Database Prompt: {data['total']} total"]
    for cat, items in data["by_category"].items():
        lines.append(f"\u2022 {cat}: {len(items)} prompt")
        for item in items[:3]:
            slug = item['slug']
            title = item['title'][:40]
            ps = f" [{item['pack_slug']}]" if item.get('pack_slug') else ""
            lines.append(f"  {slug}: {title}{ps}")
        if len(items) > 3:
            lines.append(f"  ... +{len(items) - 3} lagi")
    lines.append("Ketik: List [kategori] untuk detail, atau prompt [topik] untuk cari.")
    return '\n'.join(lines)


def format_category_list(category: str, items: list) -> str:
    """Format list-category output."""
    if not items:
        return f"\u274C Tidak ada prompt di kategori '{category}'."
    lines = [f"\U0001F4C2 {category} ({len(items)} prompt):"]
    for i, item in enumerate(items[:10], 1):
        title = item['title'][:45]
        ps = f" [{item['pack_slug']}]" if item.get('pack_slug') else ""
        lines.append(f"{i}) {item['slug']} \u2013 {title}{ps}")
    if len(items) > 10:
        lines.append(f"... +{len(items) - 10} lagi")
    return '\n'.join(lines)


def format_pack_detail(pack_slug: str) -> str:
    """Format detail view of a prompt pack."""
    entries = read_all_entries()
    pack_items = [e for e in entries if e.get('pack_slug') == pack_slug]
    if not pack_items:
        return f"\u274C Pack '{pack_slug}' tidak ditemukan."
    lines = [f"\U0001F4E6 {pack_slug} ({len(pack_items)} prompt):"]
    for i, item in enumerate(pack_items, 1):
        title = item.get('title', '')[:45]
        lines.append(f"{i}) {item.get('slug', '')} \u2013 {title}")
    lines.append(f"\nKetik: pakai: [slug] untuk pakai salah satu.")
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ══════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: prompt_db_v2.py <command> [args]")
        print("Commands: save, save-pack, search, search-packs, list-all, list-category, get, remove, pack-detail")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "save":
        if len(sys.argv) < 4:
            print("Usage: save <title> <prompt_text>")
            sys.exit(1)
        title = sys.argv[2]
        prompt_text = sys.argv[3]
        result = save_prompt(title, prompt_text)
        print(json.dumps(result, ensure_ascii=False))

    elif cmd == "save-pack":
        if len(sys.argv) < 3:
            print("Usage: save-pack <raw_text>")
            sys.exit(1)
        raw = sys.argv[2]
        results = save_prompt_pack(raw)
        if results:
            pack_slug = results[0].get("pack_slug", "")
            slugs = [r["slug"] for r in results]
            print(f"\U0001F3AF Paket disimpan. slug: {pack_slug}")
            print(f"Sub-prompt: {', '.join(slugs[:7])}")
        else:
            print("\u274C Gagal parse prompt pack.")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: search <query>")
            sys.exit(1)
        query = sys.argv[2]
        results = search_prompts(query)
        for r in results[:10]:
            print(f"{r.get('slug', '')} \u2013 {r.get('title', '')[:50]}")
        if not results:
            print(f"\u274C Tidak ada hasil untuk '{query}'.")

    elif cmd == "search-packs":
        if len(sys.argv) < 3:
            print("Usage: search-packs <query>")
            sys.exit(1)
        query = sys.argv[2]
        data = search_packs_by_topic(query)
        print(format_search_results(query, data))

    elif cmd == "list-all":
        data = list_all()
        print(format_list_overview(data))

    elif cmd == "list-category":
        if len(sys.argv) < 3:
            print("Usage: list-category <category>")
            sys.exit(1)
        cat = sys.argv[2]
        items = list_category(cat)
        print(format_category_list(cat, items))

    elif cmd == "get":
        if len(sys.argv) < 3:
            print("Usage: get <slug>")
            sys.exit(1)
        slug = sys.argv[2]
        entry = get_prompt_by_slug(slug)
        if entry:
            print(entry.get('prompt', ''))
        else:
            print(f"\u274C Prompt dengan slug '{slug}' tidak ditemukan.")

    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: remove <slug>")
            sys.exit(1)
        slug = sys.argv[2]
        if remove_prompt_by_slug(slug):
            print(f"\u2705 Prompt '{slug}' dihapus.")
        else:
            print(f"\u274C Slug '{slug}' tidak ditemukan.")

    elif cmd == "pack-detail":
        if len(sys.argv) < 3:
            print("Usage: pack-detail <pack_slug>")
            sys.exit(1)
        ps = sys.argv[2]
        print(format_pack_detail(ps))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
