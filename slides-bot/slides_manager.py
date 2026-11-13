#!/usr/bin/env python3
"""Manage a static Marp/HTML slides directory, decks.json manifest, and index.html cards."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("SLIDES_TZ", "UTC"))
SLIDES_ROOT = Path(os.environ.get("SLIDES_ROOT", "/var/www/slides"))
DECKS_FILE = SLIDES_ROOT / "decks.json"
INDEX_FILE = SLIDES_ROOT / "index.html"
MARP_BIN = os.environ.get("MARP_BIN", "marp")
SLIDES_BASE_URL = os.environ.get("SLIDES_BASE_URL", "https://slides.example.com").rstrip("/")

RESERVED_SLUGS = {"fonts", "index", "assets", ".git", "api", "favicon", "robots", "sitemap"}
SUPPORTED_EXTS = {".html", ".htm", ".md", ".markdown", ".zip"}
COVER_THEMES = ("forest", "tram", "sand", "sky", "slate")

COUNT_START = "<!-- SLIDES:COUNT_START -->"
COUNT_END = "<!-- SLIDES:COUNT_END -->"
LIST_START = "<!-- SLIDES:LIST_START -->"
LIST_END = "<!-- SLIDES:LIST_END -->"

MAX_ZIP_FILES = 500
MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024  # 100 MiB

SLIDES_LOCK = threading.Lock()

EXTRA_THEME_CSS = """    .cover[data-theme="tram"] { border-color: #365c4b; background: linear-gradient(135deg, #1a4d3a, #0e2a1f); color: #e4eadf; }
    .cover[data-theme="tram"] .cover-subtitle { color: #b7cbbd; }
    .cover[data-theme="tram"] .cover-number { color: #f2c230; }
    .cover[data-theme="sand"] { border-color: #ddd0b8; background: linear-gradient(125deg, #f7f1e3, #fffdf8); color: #5c4524; }
    .cover[data-theme="sand"] .cover-subtitle { color: #7e6746; }
    .cover[data-theme="sand"] .cover-number { color: #9c8665; }
    .cover[data-theme="sky"] { border-color: #bfd4e8; background: linear-gradient(125deg, #e8f2fb, #ffffff); color: #1b4d7a; }
    .cover[data-theme="sky"] .cover-subtitle { color: #3f6d94; }
    .cover[data-theme="sky"] .cover-number { color: #678cad; }
    .cover[data-theme="slate"] { border-color: #3d4b56; background: linear-gradient(135deg, #253038, #141a1f); color: #e8eef2; }
    .cover[data-theme="slate"] .cover-subtitle { color: #a7b8c4; }
    .cover[data-theme="slate"] .cover-number { color: #95abc0; }"""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _split_leading_emoji(text: str) -> tuple[str, str]:
    """Split a leading emoji/symbol cluster from a title if present."""
    s = (text or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^([\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+)\s*(.+)$", s)
    if m:
        return m.group(1), m.group(2).strip()
    return "", s


def sanitize_slug(raw: str, fallback_prefix: str = "deck") -> str:
    stem = Path(raw).stem if "." in raw else raw
    slug = re.sub(r"[^a-z0-9_-]+", "-", stem.strip().lower()).strip("-_")
    if not slug or slug in RESERVED_SLUGS:
        ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        slug = f"{fallback_prefix}-{ts}"
    return slug[:40].strip("-_")


def is_valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", slug)) and slug not in RESERVED_SLUGS


def parse_marp_markdown(md_text: str) -> dict[str, Any]:
    """Extract frontmatter, slide count, title, subtitle, and description from Marp Markdown."""
    lines = md_text.splitlines()
    idx = 0
    frontmatter: dict[str, str] = {}

    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            line = lines[idx]
            if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
                k, v = line.split(":", 1)
                frontmatter[k.strip().lower()] = v.strip().strip("'\"")
            idx += 1
        if idx < len(lines) and lines[idx].strip() == "---":
            idx += 1

    slides: list[list[str]] = [[]]
    in_fence = False
    fence_char = ""
    for line in lines[idx:]:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            ch = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_char = ch
            elif ch == fence_char:
                in_fence = False
        if not in_fence and stripped == "---":
            slides.append([])
        else:
            slides[-1].append(line)

    non_empty_slides = [s for s in slides if any(ln.strip() and not ln.strip().startswith("<!--") for ln in s)]
    pages = max(1, len(non_empty_slides) if non_empty_slides else len(slides))

    title = frontmatter.get("title", "")
    subtitle = ""
    description = frontmatter.get("description", "")

    first_slide = non_empty_slides[0] if non_empty_slides else (slides[0] if slides else [])
    for line in first_slide:
        st = line.strip()
        if not st or st.startswith("<!--") or st.startswith("!["):
            continue
        if st.startswith("# ") and not title:
            title = st[2:].strip()
        elif st.startswith("## ") and not subtitle:
            subtitle = st[3:].strip()
        elif re.match(r"^#{1,3}\s+", st) and not title:
            title = re.sub(r"^#{1,3}\s+", "", st).strip()

    icon, clean_title = _split_leading_emoji(title)
    return {
        "title": clean_title or title,
        "subtitle": subtitle,
        "icon": icon,
        "pages": pages,
        "description": description,
    }


def parse_html_metadata(html_text: str) -> dict[str, Any]:
    """Extract title, description, icon, and slide page count from rendered slide HTML."""
    title = ""
    m_title = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if m_title:
        title = html.unescape(re.sub(r"\s+", " ", m_title.group(1))).strip()

    description = ""
    m_desc = re.search(
        r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
        html_text,
        flags=re.IGNORECASE,
    )
    if not m_desc:
        m_desc = re.search(
            r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
            html_text,
            flags=re.IGNORECASE,
        )
    if m_desc:
        description = html.unescape(m_desc.group(1)).strip()

    marp_ids = re.findall(r'<section\b[^>]*\bid=["\'](\d+)["\']', html_text, flags=re.IGNORECASE)
    marp_svgs = re.findall(r"<svg\b[^>]*data-marpit-svg", html_text, flags=re.IGNORECASE)
    if marp_ids:
        pages = len(set(marp_ids))
    elif marp_svgs:
        pages = len(marp_svgs)
    else:
        sections = re.findall(r"<section\b", html_text, flags=re.IGNORECASE)
        pages = len(sections) if sections else 1

    icon, clean_title = _split_leading_emoji(title)
    return {
        "title": clean_title or title,
        "icon": icon,
        "pages": max(1, pages),
        "description": description,
    }


def parse_slides_args(raw_args: str) -> dict[str, str]:
    """Parse user-supplied metadata overrides from key=value or pipe-separated syntax."""
    text = (raw_args or "").strip()
    if not text:
        return {}

    key_map = {
        "slug": "slug",
        "id": "slug",
        "name": "slug",
        "url": "slug",
        "title": "title",
        "t": "title",
        "sub": "subtitle",
        "subtitle": "subtitle",
        "cat": "category",
        "category": "category",
        "tag": "category",
        "desc": "description",
        "description": "description",
        "icon": "icon",
        "emoji": "icon",
        "eyebrow": "eyebrow",
        "label": "eyebrow",
        "pages": "pages",
        "page": "pages",
        "p": "pages",
        "theme": "theme",
    }

    if "|" in text and "=" not in text.split("|", 1)[0]:
        parts = [p.strip() for p in text.split("|")]
        out: dict[str, str] = {}
        if parts and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", parts[0].lower()):
            out["slug"] = parts[0].lower()
            parts = parts[1:]
        if len(parts) >= 1 and parts[0]:
            out["title"] = parts[0]
        if len(parts) == 2 and parts[1]:
            out["category"] = parts[1]
        elif len(parts) == 3:
            if parts[1]:
                out["category"] = parts[1]
            if parts[2]:
                out["description"] = parts[2]
        elif len(parts) >= 4:
            if parts[1]:
                out["subtitle"] = parts[1]
            if parts[2]:
                out["category"] = parts[2]
            if parts[3]:
                out["description"] = parts[3]
        return out

    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()

    out = {}
    bare_tokens: list[str] = []
    for tok in tokens:
        m = re.match(r"^([a-zA-Z_]+)[:=](.*)$", tok)
        if m and m.group(1).lower() in key_map:
            out[key_map[m.group(1).lower()]] = m.group(2).strip()
        else:
            bare_tokens.append(tok)

    if bare_tokens:
        if "slug" not in out and len(bare_tokens) == 1 and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", bare_tokens[0]):
            out["slug"] = bare_tokens[0].lower()
        elif "title" not in out:
            out["title"] = " ".join(bare_tokens)

    return out


def load_decks() -> list[dict[str, Any]]:
    if not DECKS_FILE.exists():
        save_decks([])
        return []
    raw = json.loads(DECKS_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("decks", [])
    return list(raw)


def save_decks(decks: list[dict[str, Any]]) -> None:
    _atomic_write_text(DECKS_FILE, json.dumps(decks, ensure_ascii=False, indent=2) + "\n")


def _render_deck_li(index_num: int, deck: dict[str, Any]) -> str:
    num_str = f"{index_num:02d}"
    slug = deck["slug"]
    href = deck["href"]
    eyebrow = (deck.get("eyebrow") or slug.replace("-", " ").replace("_", " ")).upper()
    icon = deck.get("icon") or "🎞️"
    title = deck.get("title") or slug
    subtitle = deck.get("subtitle") or ""
    category = deck.get("category") or "演示与分享"
    pages = int(deck.get("pages") or 1)
    description = deck.get("description") or "在线幻灯片与演示文稿。"
    theme = deck.get("theme") if deck.get("theme") in COVER_THEMES else "forest"

    title_id = f"deck-{slug}-title"
    desc_id = f"deck-{slug}-description"
    theme_attr = f' data-theme="{html.escape(theme, quote=True)}"' if theme != "forest" else ""

    sub_line = (
        f'\n                <span class="cover-subtitle">{html.escape(subtitle)}</span>'
        if subtitle
        else ""
    )

    return (
        f"          <li>\n"
        f'            <a class="deck" href="{html.escape(href, quote=True)}" aria-labelledby="{html.escape(title_id, quote=True)}" aria-describedby="{html.escape(desc_id, quote=True)}">\n'
        f'              <div class="cover"{theme_attr} aria-hidden="true">\n'
        f'                <span class="cover-number">{num_str} / {html.escape(eyebrow)}</span>\n'
        f'                <span class="cover-icon">{html.escape(icon)}</span>\n'
        f'                <span class="cover-title">{html.escape(title)}</span>{sub_line}\n'
        f"              </div>\n"
        f'              <div class="meta"><span>{html.escape(category)}</span><span class="pages">{pages} PAGES</span></div>\n'
        f'              <div class="title-row"><h3 id="{html.escape(title_id, quote=True)}">{html.escape(title)}</h3><span class="open">打开演示 <span aria-hidden="true">↗</span></span></div>\n'
        f'              <p class="description" id="{html.escape(desc_id, quote=True)}">{html.escape(description)}</p>\n'
        f"            </a>\n"
        f"          </li>"
    )


def _inject_markers_if_needed(src: str) -> str:
    if '.cover[data-theme="tram"]' not in src and "</style>" in src:
        src = src.replace("  </style>", EXTRA_THEME_CSS + "\n  </style>", 1)

    if COUNT_START not in src:
        src, n_count = re.subn(
            r'(<span class="count">)(.*?)(</span>)',
            rf"\1{COUNT_START}\2{COUNT_END}\3",
            src,
            count=1,
            flags=re.DOTALL,
        )
        if n_count == 0:
            raise RuntimeError("index.html 中未找到 .count 容器")

    if LIST_START not in src:
        src, n_list = re.subn(
            r'(<ul class="collection">)(.*?)(</ul>)',
            rf"\1\n{LIST_START}\2{LIST_END}\n        \3",
            src,
            count=1,
            flags=re.DOTALL,
        )
        if n_list == 0:
            raise RuntimeError("index.html 中未找到 .collection 容器")

    return src


def render_index(decks: list[dict[str, Any]]) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"缺少目录页文件: {INDEX_FILE}")

    src = _inject_markers_if_needed(INDEX_FILE.read_text(encoding="utf-8"))
    count_label = f"{len(decks):02d} / {'SLIDE DECK' if len(decks) == 1 else 'SLIDE DECKS'}"
    items_html = "\n".join(_render_deck_li(i + 1, d) for i, d in enumerate(decks))

    src = re.sub(
        re.escape(COUNT_START) + r".*?" + re.escape(COUNT_END),
        f"{COUNT_START}{count_label}{COUNT_END}",
        src,
        count=1,
        flags=re.DOTALL,
    )
    src = re.sub(
        re.escape(LIST_START) + r".*?" + re.escape(LIST_END),
        f"{LIST_START}\n{items_html}\n        {LIST_END}",
        src,
        count=1,
        flags=re.DOTALL,
    )
    _atomic_write_text(INDEX_FILE, src)


def ensure_initialized() -> list[dict[str, Any]]:
    with SLIDES_LOCK:
        decks = load_decks()
        if INDEX_FILE.exists():
            render_index(decks)
        return decks


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Safely extract zip_path into dest_dir, unwrapping a single top-level directory if present."""
    total_size = 0
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            norm = info.filename.replace("\\", "/")
            if norm.startswith("/") or ".." in Path(norm).parts:
                raise ValueError(f"压缩包包含不安全路径: {info.filename}")
            if any(part == "__MACOSX" for part in Path(norm).parts):
                continue
            if Path(norm).name == ".DS_Store":
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"压缩包不允许包含符号链接: {info.filename}")
            count += 1
            if count > MAX_ZIP_FILES:
                raise ValueError(f"压缩包文件数量超过上限 ({MAX_ZIP_FILES})")
            total_size += info.file_size
            if total_size > MAX_ZIP_UNCOMPRESSED:
                raise ValueError("压缩包解压后体积超过 100 MiB 上限")

        for info in zf.infolist():
            norm = info.filename.replace("\\", "/")
            parts = [p for p in Path(norm).parts if p not in ("", ".")]
            if not parts or "__MACOSX" in parts or parts[-1] == ".DS_Store":
                continue
            target = (dest_dir / Path(*parts)).resolve()
            if not str(target).startswith(str(dest_dir.resolve())):
                raise ValueError(f"路径越界: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_f, open(target, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)

    entries = [p for p in dest_dir.iterdir() if p.name not in ("__MACOSX", ".DS_Store")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_dir


def _resolve_marp_bin() -> str:
    resolved = shutil.which(MARP_BIN)
    if resolved:
        return resolved
    if Path(MARP_BIN).exists():
        return MARP_BIN
    raise RuntimeError(f"未找到 marp-cli ({MARP_BIN})，请先安装 @marp-team/marp-cli")


def _compile_marp(md_path: Path, out_html: Path) -> None:
    marp_exe = _resolve_marp_bin()
    env = dict(os.environ)
    env["HOME"] = "/tmp"
    env["npm_config_cache"] = "/tmp"
    cmd = [
        marp_exe,
        str(md_path),
        "--html",
        "--allow-local-files",
        "--no-stdin",
        "-o",
        str(out_html),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, cwd=str(md_path.parent))
    if proc.returncode != 0 or not out_html.exists():
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Marp 编译失败 (rc={proc.returncode}): {err[-600:]}")


def publish_slide_file(
    uploaded_path: Path,
    original_filename: str,
    raw_args: str = "",
) -> dict[str, Any]:
    """Process an uploaded .html, .md, or .zip into SLIDES_ROOT/<slug>/ and update index.html."""
    ext = Path(original_filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型 `{ext}`，请发送 .html、.md 或 .zip。")

    overrides = parse_slides_args(raw_args)
    raw_slug = overrides.get("slug") or original_filename
    slug = sanitize_slug(raw_slug)
    if not is_valid_slug(slug):
        raise ValueError(f"无效或保留的 slug: {slug}")

    with tempfile.TemporaryDirectory(prefix="slides_stage_") as tmp_str:
        tmp_dir = Path(tmp_str)
        stage_dir = tmp_dir / "stage"
        stage_dir.mkdir(parents=True, exist_ok=True)
        extracted_meta: dict[str, Any] = {}
        warnings: list[str] = []

        if ext in (".html", ".htm"):
            dst_html = stage_dir / "index.html"
            shutil.copy2(uploaded_path, dst_html)
            extracted_meta = parse_html_metadata(dst_html.read_text(encoding="utf-8", errors="replace"))

        elif ext in (".md", ".markdown"):
            md_text = uploaded_path.read_text(encoding="utf-8", errors="replace")
            extracted_meta = parse_marp_markdown(md_text)
            rel_imgs = re.findall(r"!\[[^\]]*\]\((?!https?://|data:)([^)]+)\)", md_text)
            if rel_imgs:
                warnings.append("检测到本地图片引用（如 " + ", ".join(rel_imgs[:3]) + "），若缺图请将 .md 与图片打包成 .zip 上传。")
            md_copy = stage_dir / f"{slug}.md"
            md_copy.write_text(md_text, encoding="utf-8")
            _compile_marp(md_copy, stage_dir / "index.html")
            html_meta = parse_html_metadata((stage_dir / "index.html").read_text(encoding="utf-8", errors="replace"))
            if not extracted_meta.get("title") and html_meta.get("title"):
                extracted_meta["title"] = html_meta["title"]
            if html_meta.get("pages"):
                extracted_meta["pages"] = html_meta["pages"]

        elif ext == ".zip":
            extract_dir = tmp_dir / "unzipped"
            extract_dir.mkdir(parents=True, exist_ok=True)
            content_root = _safe_extract_zip(uploaded_path, extract_dir)
            shutil.copytree(content_root, stage_dir, dirs_exist_ok=True)

            index_html = stage_dir / "index.html"
            html_files = sorted(stage_dir.glob("*.html")) + sorted(stage_dir.glob("*.htm"))
            md_files = sorted(stage_dir.glob("*.md")) + sorted(stage_dir.glob("*.markdown"))

            if index_html.exists():
                extracted_meta = parse_html_metadata(index_html.read_text(encoding="utf-8", errors="replace"))
            elif html_files:
                shutil.copy2(html_files[0], index_html)
                extracted_meta = parse_html_metadata(index_html.read_text(encoding="utf-8", errors="replace"))
            elif md_files:
                preferred = next((p for p in md_files if p.stem.lower() in (slug, "slides", "index", "readme")), md_files[0])
                md_text = preferred.read_text(encoding="utf-8", errors="replace")
                extracted_meta = parse_marp_markdown(md_text)
                _compile_marp(preferred, index_html)
                html_meta = parse_html_metadata(index_html.read_text(encoding="utf-8", errors="replace"))
                if html_meta.get("pages"):
                    extracted_meta["pages"] = html_meta["pages"]
            else:
                raise ValueError("压缩包根目录未找到 .html 或 .md 演示文件。")

        for root_dir, dirs, files in os.walk(stage_dir):
            os.chmod(root_dir, 0o755)
            for d in dirs:
                os.chmod(os.path.join(root_dir, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root_dir, f), 0o644)

        now_iso = datetime.now(TZ).isoformat(timespec="seconds")

        with SLIDES_LOCK:
            decks = load_decks()
            existing = next((d for d in decks if d["slug"] == slug), None)
            if existing and not existing.get("managed", True):
                raise ValueError(f"`{slug}` 是保留的根路径演示，请换用其他 slug。")

            dest_dir = SLIDES_ROOT / slug
            backup_dir = None
            if dest_dir.exists():
                backup_dir = SLIDES_ROOT / f".{slug}.bak"
                if backup_dir.exists():
                    shutil.rmtree(backup_dir)
                dest_dir.rename(backup_dir)

            try:
                shutil.copytree(stage_dir, dest_dir)
                if backup_dir and backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir, ignore_errors=True)
                if backup_dir and backup_dir.exists():
                    backup_dir.rename(dest_dir)
                raise

            base = dict(existing or {})
            pages_val = extracted_meta.get("pages") or base.get("pages") or 1
            if overrides.get("pages") and overrides["pages"].isdigit():
                pages_val = max(1, int(overrides["pages"]))

            theme = overrides.get("theme") or base.get("theme") or "tram"
            if theme not in COVER_THEMES:
                theme = "tram"

            deck: dict[str, Any] = {
                "slug": slug,
                "href": f"{slug}/",
                "managed": True,
                "title": overrides.get("title") or extracted_meta.get("title") or base.get("title") or slug,
                "subtitle": overrides.get("subtitle") if "subtitle" in overrides else (extracted_meta.get("subtitle") or base.get("subtitle") or ""),
                "eyebrow": overrides.get("eyebrow") or base.get("eyebrow") or slug.replace("-", " ").replace("_", " ").upper(),
                "icon": overrides.get("icon") or extracted_meta.get("icon") or base.get("icon") or "🎞️",
                "category": overrides.get("category") or base.get("category") or "演示与分享",
                "pages": int(pages_val),
                "description": overrides.get("description") or extracted_meta.get("description") or base.get("description") or f"{overrides.get('title') or extracted_meta.get('title') or slug} 在线幻灯片演示。",
                "theme": theme,
                "created_at": base.get("created_at") or now_iso,
                "updated_at": now_iso,
            }

            if existing:
                decks = [deck if d["slug"] == slug else d for d in decks]
                action = "updated"
            else:
                decks.append(deck)
                action = "created"

            save_decks(decks)
            render_index(decks)

    return {
        "action": action,
        "deck": deck,
        "url": f"{SLIDES_BASE_URL}/{deck['href']}",
        "index_url": f"{SLIDES_BASE_URL}/",
        "warnings": warnings,
    }


def update_deck_metadata(slug: str, raw_args: str) -> dict[str, Any]:
    overrides = parse_slides_args(raw_args)
    if not overrides:
        raise ValueError("请提供要修改的字段，例如：`/slides edit slug title=新标题 cat=分类 desc=简介`")

    with SLIDES_LOCK:
        decks = load_decks()
        target = next((d for d in decks if d["slug"] == slug), None)
        if not target:
            raise ValueError(f"未找到幻灯片 `{slug}`。")

        for field in ("title", "subtitle", "eyebrow", "icon", "category", "description"):
            if field in overrides:
                target[field] = overrides[field]
        if "pages" in overrides and overrides["pages"].isdigit():
            target["pages"] = max(1, int(overrides["pages"]))
        if "theme" in overrides and overrides["theme"] in COVER_THEMES:
            target["theme"] = overrides["theme"]
        target["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")

        save_decks(decks)
        render_index(decks)
        return dict(target)


def delete_deck(slug: str) -> dict[str, Any]:
    with SLIDES_LOCK:
        decks = load_decks()
        target = next((d for d in decks if d["slug"] == slug), None)
        if not target:
            raise ValueError(f"未找到幻灯片 `{slug}`。")

        decks = [d for d in decks if d["slug"] != slug]
        save_decks(decks)
        render_index(decks)

        if target.get("managed"):
            deck_dir = SLIDES_ROOT / slug
            if deck_dir.exists() and deck_dir.is_dir():
                shutil.rmtree(deck_dir)
        return dict(target)
