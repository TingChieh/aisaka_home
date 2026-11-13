#!/usr/bin/env python3
"""Standalone Telegram bot for managing a static slides directory via /slides."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import slides_manager

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/slides-bot"))
OFFSET_FILE = STATE_DIR / "offset"
TG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
CONFIRM_TTL_SECONDS = 60

STOP_EVENT = threading.Event()
PENDING_LOCK = threading.Lock()
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}


def sanitize_filename(name: str) -> str:
    cleaned = os.path.basename(name.replace("\\", "/")).strip()
    cleaned = re.sub(r"[\x00-\x1f/]", "_", cleaned)
    return cleaned or f"slides_{int(time.time())}"


def fmt_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


def register_pending(action: str, target: str) -> str:
    now = time.time()
    token = secrets.token_hex(4)
    with PENDING_LOCK:
        expired = [k for k, v in PENDING_ACTIONS.items() if now - v["ts"] > CONFIRM_TTL_SECONDS]
        for k in expired:
            PENDING_ACTIONS.pop(k, None)
        PENDING_ACTIONS[token] = {"action": action, "target": target, "ts": now}
    return token


def pop_pending(token: str) -> dict[str, Any] | None:
    with PENDING_LOCK:
        item = PENDING_ACTIONS.pop(token, None)
    if not item or time.time() - item["ts"] > CONFIRM_TTL_SECONDS:
        return None
    return item


class TgClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
        url = f"{self.base}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "slides-bot/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API {method} HTTP {exc.code}: {body[:200]}") from exc

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        return self.call("sendMessage", payload, timeout=15)

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            self.call("editMessageText", payload, timeout=15)
        except RuntimeError as exc:
            if "message is not modified" not in str(exc):
                raise

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:180]
        if alert:
            payload["show_alert"] = True
        try:
            self.call("answerCallbackQuery", payload, timeout=10)
        except Exception:
            pass

    def download_file_to(self, file_id: str, dest_path: Path) -> int:
        info = self.call("getFile", {"file_id": file_id}, timeout=20)
        file_path = (info.get("result") or {}).get("file_path")
        if not file_path:
            raise RuntimeError("getFile 未返回文件路径")
        dl_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        req = urllib.request.Request(dl_url, headers={"User-Agent": "slides-bot/1.0"})
        total = 0
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        return total


def extract_document(msg: dict[str, Any]) -> dict[str, Any] | None:
    doc = msg.get("document")
    if not doc:
        return None
    uid = doc.get("file_unique_id", "doc")[-6:]
    fname = sanitize_filename(doc.get("file_name") or f"slides_{uid}")
    return {
        "file_id": doc["file_id"],
        "file_size": doc.get("file_size", 0),
        "filename": fname,
    }


def build_slides_view(notice: str = "") -> tuple[str, dict[str, Any]]:
    decks = slides_manager.ensure_initialized()
    lines = ["<b>🎞️ 幻灯片目录</b>"]
    if notice:
        lines.append(notice)
    lines.append(f"当前共收录 <b>{len(decks)}</b> 份演示：\n")

    deck_buttons: list[dict[str, str]] = []
    for idx, d in enumerate(decks, start=1):
        icon = d.get("icon") or "🎞️"
        title = d.get("title") or d["slug"]
        slug = d["slug"]
        cat = d.get("category") or "演示与分享"
        pages = int(d.get("pages") or 1)
        url = f"{slides_manager.SLIDES_BASE_URL}/{d['href']}"
        lines.append(
            f'{idx:02d}. {html.escape(icon)} <a href="{html.escape(url, quote=True)}"><b>{html.escape(title)}</b></a>'
            f" · <code>{html.escape(slug)}</code> · {html.escape(cat)} · {pages}P"
        )
        deck_buttons.append({"text": f"{icon} {slug}", "callback_data": f"slides:d:{slug}"})

    lines.append("\n💡 发送 <code>.html</code> / <code>.md</code> / <code>.zip</code> 并附带说明 <code>/slides</code> 即可发布新演示。")

    rows = [deck_buttons[i : i + 2] for i in range(0, len(deck_buttons), 2)]
    rows.append([
        {"text": "📖 用法说明", "callback_data": "slides:help"},
        {"text": "🌐 打开目录页", "url": f"{slides_manager.SLIDES_BASE_URL}/"},
    ])
    rows.append([{"text": "🔄 刷新", "callback_data": "cmd:slides"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_slides_help_view() -> tuple[str, dict[str, Any]]:
    text = (
        "<b>📖 /slides 使用说明</b>\n\n"
        "<b>1. 添加 / 更新幻灯片</b>\n"
        "• 发送 <code>.html</code>、Marp <code>.md</code> 或含图片资源的 <code>.zip</code>，在文件说明中写 <code>/slides [参数]</code>；\n"
        "• 或<b>回复</b>一条已发过的文件消息，发送 <code>/slides [参数]</code>。\n\n"
        "<b>2. 参数写法（均可选，不填则自动提取标题与页数）</b>\n"
        "• <code>/slides my-talk</code>\n"
        "• <code>/slides my-talk | 标题 | 分类 | 简介</code>\n"
        "• <code>/slides slug=my-talk title=\"AI 分享\" cat=技术 sub=副标题 icon=🤖 theme=tram</code>\n"
        "• 配色 <code>theme</code>：<code>forest</code> · <code>tram</code> · <code>sand</code> · <code>sky</code> · <code>slate</code>\n\n"
        "<b>3. 管理命令</b>\n"
        "• <code>/slides</code> — 查看目录与管理按钮\n"
        "• <code>/slides edit &lt;slug&gt; title=新标题 cat=分类</code> — 修改卡片信息\n"
        "• <code>/slides del &lt;slug&gt;</code> — 删除演示（需二次确认）\n"
        "• <code>/slides rebuild</code> — 刷新并重建目录页"
    )
    return text, {"inline_keyboard": [[{"text": "⬅️ 返回列表", "callback_data": "cmd:slides"}]]}


def build_slides_deck_view(slug: str, notice: str = "") -> tuple[str, dict[str, Any]]:
    decks = slides_manager.ensure_initialized()
    deck = next((d for d in decks if d["slug"] == slug), None)
    if not deck:
        return build_slides_view(f"⚠️ 未找到幻灯片 <code>{html.escape(slug)}</code>")

    url = f"{slides_manager.SLIDES_BASE_URL}/{deck['href']}"
    theme = deck.get("theme") or "forest"
    lines = [f"<b>{html.escape(deck.get('icon') or '🎞️')} {html.escape(deck.get('title') or slug)}</b>"]
    if notice:
        lines.append(notice)
    if deck.get("subtitle"):
        lines.append(f"副标题：{html.escape(deck['subtitle'])}")
    lines.extend([
        f"• 路径：<code>{html.escape(slug)}</code>（{html.escape(deck['href'])}）",
        f"• 分类：{html.escape(deck.get('category') or '演示与分享')} · {int(deck.get('pages') or 1)} 页",
        f"• 配色：<code>{html.escape(theme)}</code>",
        f"• 简介：{html.escape(deck.get('description') or '')}",
        f'• 链接：<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>',
    ])

    theme_labels = [("forest", "🌲绿纸"), ("tram", "🚋叮叮"), ("sand", "🏜暖沙"), ("sky", "🌤晴蓝"), ("slate", "🌑石板")]
    theme_row = [
        {"text": f"{'✓ ' if th == theme else ''}{lbl}", "callback_data": f"slides:t:{slug}:{th}"}
        for th, lbl in theme_labels
    ]
    kb = {
        "inline_keyboard": [
            theme_row[:3],
            theme_row[3:],
            [{"text": "🌐 打开演示", "url": url}, {"text": "🗑 删除", "callback_data": f"ask_slides_del:{slug}"}],
            [{"text": "⬅️ 返回列表", "callback_data": "cmd:slides"}],
        ]
    }
    return "\n".join(lines), kb


def handle_slides_upload(tg: TgClient, chat_id: int, reply_msg_id: int, media: dict[str, Any], raw_args: str) -> None:
    fname = media["filename"]
    ext = Path(fname).suffix.lower()
    if ext not in slides_manager.SUPPORTED_EXTS:
        tg.send(
            chat_id,
            f"⚠️ 暂不支持 <code>{html.escape(fname)}</code>，请发送 <code>.html</code>、<code>.md</code> 或 <code>.zip</code>。",
            reply_to_message_id=reply_msg_id,
        )
        return

    fsize = media.get("file_size") or 0
    if fsize > TG_MAX_DOWNLOAD_BYTES:
        tg.send(
            chat_id,
            f"❌ 文件过大 (<code>{fmt_bytes(fsize)}</code>)，Telegram 下载上限为 20 MiB。",
            reply_to_message_id=reply_msg_id,
        )
        return

    status_msg = tg.send(chat_id, f"⏳ 正在处理幻灯片 <code>{html.escape(fname)}</code> ...", reply_to_message_id=reply_msg_id)
    status_id = (status_msg.get("result") or {}).get("message_id")

    try:
        with tempfile.TemporaryDirectory(prefix="tg_slides_") as tmp_dir:
            local_path = Path(tmp_dir) / fname
            dl_bytes = tg.download_file_to(media["file_id"], local_path)
            result = slides_manager.publish_slide_file(local_path, fname, raw_args)
    except Exception as exc:
        err_text = f"❌ 发布失败：<code>{html.escape(str(exc))}</code>"
        if status_id:
            tg.edit(chat_id, status_id, err_text)
        else:
            tg.send(chat_id, err_text, reply_to_message_id=reply_msg_id)
        return

    deck = result["deck"]
    action_label = "已更新" if result["action"] == "updated" else "已发布"
    lines = [
        f"✅ <b>{action_label}幻灯片</b>（{fmt_bytes(dl_bytes)}）",
        f"• 标题：{html.escape(deck.get('icon') or '🎞️')} <b>{html.escape(deck['title'])}</b>",
        f"• 分类：{html.escape(deck['category'])} · {deck['pages']} 页",
        f'• 地址：<a href="{html.escape(result["url"], quote=True)}">{html.escape(result["url"])}</a>',
    ]
    for w in result.get("warnings") or []:
        lines.append(f"⚠️ {html.escape(w)}")

    kb = {
        "inline_keyboard": [
            [{"text": "🌐 打开演示", "url": result["url"]}, {"text": "⚙️ 管理此卡片", "callback_data": f"slides:d:{deck['slug']}"}],
            [{"text": "🎞️ 查看全部", "callback_data": "cmd:slides"}],
        ]
    }
    if status_id:
        tg.edit(chat_id, status_id, "\n".join(lines), kb)
    else:
        tg.send(chat_id, "\n".join(lines), kb, reply_to_message_id=reply_msg_id)


def handle_message(tg: TgClient, msg: dict[str, Any]) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    if not chat_id or not msg_id:
        return

    media = extract_document(msg)
    if media:
        caption = (msg.get("caption") or "").strip()
        m_slides = re.match(r"^/slides(?:@\S+)?(?:\s+(.*))?$", caption, flags=re.IGNORECASE | re.DOTALL)
        raw_args = (m_slides.group(1) or "").strip() if m_slides else caption
        handle_slides_upload(tg, chat_id, msg_id, media, raw_args)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        body, kb = build_slides_help_view()
        tg.send(chat_id, body, kb, reply_to_message_id=msg_id)
        return

    if cmd != "/slides":
        tg.send(chat_id, "发送 /slides 查看幻灯片目录，或发送 .html / .md / .zip 文件发布演示。")
        return

    raw_tail = re.sub(r"^/slides(?:@\S+)?\s*", "", text, count=1, flags=re.IGNORECASE)
    tail_parts = raw_tail.split() if raw_tail else []
    sub = tail_parts[0].lower() if tail_parts else ""
    replied = msg.get("reply_to_message") or {}
    rep_media = extract_document(replied)

    if sub in ("", "list", "ls") and not rep_media:
        body, kb = build_slides_view()
        tg.send(chat_id, body, kb, reply_to_message_id=msg_id)
    elif sub in ("help", "h", "?"):
        body, kb = build_slides_help_view()
        tg.send(chat_id, body, kb, reply_to_message_id=msg_id)
    elif sub == "rebuild":
        decks = slides_manager.ensure_initialized()
        body, kb = build_slides_view(f"✅ 已重新渲染目录页（共 {len(decks)} 份）")
        tg.send(chat_id, body, kb, reply_to_message_id=msg_id)
    elif sub in ("del", "rm", "delete"):
        if len(tail_parts) < 2:
            tg.send(chat_id, "用法：<code>/slides del &lt;slug&gt;</code>", reply_to_message_id=msg_id)
            return
        slug = tail_parts[1].lower()
        decks = slides_manager.ensure_initialized()
        target = next((d for d in decks if d["slug"] == slug), None)
        if not target:
            tg.send(chat_id, f"⚠️ 未找到幻灯片 <code>{html.escape(slug)}</code>。", reply_to_message_id=msg_id)
            return
        tok = register_pending("slides_del", slug)
        tg.send(
            chat_id,
            f"⚠️ 确认删除幻灯片 <b>{html.escape(target['title'])}</b> (<code>{html.escape(slug)}</code>)？\n（{CONFIRM_TTL_SECONDS}s 内有效）",
            {"inline_keyboard": [[{"text": "✅ 确认删除", "callback_data": f"confirm:{tok}"}, {"text": "❌ 取消", "callback_data": f"cancel:{tok}"}]]},
            reply_to_message_id=msg_id,
        )
    elif sub == "edit":
        if len(tail_parts) < 3:
            tg.send(chat_id, "用法：<code>/slides edit &lt;slug&gt; title=新标题 cat=分类</code>", reply_to_message_id=msg_id)
            return
        slug = tail_parts[1].lower()
        edit_tail = re.sub(r"^\s*edit\s+\S+\s*", "", raw_tail, count=1, flags=re.IGNORECASE)
        try:
            slides_manager.update_deck_metadata(slug, edit_tail)
        except Exception as exc:
            tg.send(chat_id, f"❌ 修改失败：<code>{html.escape(str(exc))}</code>", reply_to_message_id=msg_id)
            return
        body, kb = build_slides_deck_view(slug, "✅ 已更新卡片信息")
        tg.send(chat_id, body, kb, reply_to_message_id=msg_id)
    elif rep_media:
        add_tail = re.sub(r"^\s*add(?:\s+|$)", "", raw_tail, count=1, flags=re.IGNORECASE)
        handle_slides_upload(tg, chat_id, replied.get("message_id", msg_id), rep_media, add_tail)
    else:
        body, kb = build_slides_help_view()
        tg.send(chat_id, "💡 请发送 .html / .md / .zip 文件或回复含文件的消息使用 /slides。\n\n" + body, kb, reply_to_message_id=msg_id)


def handle_callback(tg: TgClient, cb: dict[str, Any]) -> None:
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    if not chat_id or not msg_id:
        tg.answer_callback(cb_id)
        return

    if data == "cmd:slides":
        tg.answer_callback(cb_id)
        body, kb = build_slides_view()
        tg.edit(chat_id, msg_id, body, kb)
    elif data == "slides:help":
        tg.answer_callback(cb_id)
        body, kb = build_slides_help_view()
        tg.edit(chat_id, msg_id, body, kb)
    elif data.startswith("slides:d:"):
        slug = data.split(":", 2)[2]
        tg.answer_callback(cb_id)
        body, kb = build_slides_deck_view(slug)
        tg.edit(chat_id, msg_id, body, kb)
    elif data.startswith("slides:t:"):
        _, _, rest = data.partition("slides:t:")
        if ":" in rest:
            slug, theme = rest.rsplit(":", 1)
            slides_manager.update_deck_metadata(slug, f"theme={theme}")
            tg.answer_callback(cb_id, f"已切换配色: {theme}")
            body, kb = build_slides_deck_view(slug, f"✅ 配色已切换为 <code>{html.escape(theme)}</code>")
            tg.edit(chat_id, msg_id, body, kb)
    elif data.startswith("ask_slides_del:"):
        slug = data.split(":", 1)[1]
        decks = slides_manager.ensure_initialized()
        target = next((d for d in decks if d["slug"] == slug), None)
        if not target:
            tg.answer_callback(cb_id, "未找到该幻灯片", alert=True)
            return
        tok = register_pending("slides_del", slug)
        tg.answer_callback(cb_id)
        tg.edit(
            chat_id,
            msg_id,
            f"⚠️ 确认删除幻灯片 <b>{html.escape(target['title'])}</b> (<code>{html.escape(slug)}</code>)？",
            {"inline_keyboard": [[{"text": "✅ 确认删除", "callback_data": f"confirm:{tok}"}, {"text": "❌ 取消", "callback_data": f"cancel:{tok}"}]]},
        )
    elif data.startswith("cancel:"):
        pop_pending(data.split(":", 1)[1])
        tg.answer_callback(cb_id, "已取消")
        body, kb = build_slides_view()
        tg.edit(chat_id, msg_id, body, kb)
    elif data.startswith("confirm:"):
        item = pop_pending(data.split(":", 1)[1])
        if not item:
            tg.answer_callback(cb_id, "确认已过期，请重新发起", alert=True)
            return
        if item["action"] == "slides_del":
            removed = slides_manager.delete_deck(item["target"])
            tg.answer_callback(cb_id, f"已删除 {item['target']}")
            body, kb = build_slides_view(f"🗑 已删除：<b>{html.escape(removed['title'])}</b>")
            tg.edit(chat_id, msg_id, body, kb)
    else:
        tg.answer_callback(cb_id)


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset), encoding="utf-8")


def main() -> int:
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    owner_raw = os.environ.get("TG_OWNER_ID", "").strip()
    if not token or not owner_raw.lstrip("-").isdigit():
        print("Error: TG_BOT_TOKEN and numeric TG_OWNER_ID are required.", file=sys.stderr)
        return 1
    owner_id = int(owner_raw)
    tg = TgClient(token)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP_EVENT.set())

    try:
        tg.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "slides", "description": "管理幻灯片目录与发布"},
                    {"command": "help", "description": "显示使用说明"},
                ]
            },
            timeout=10,
        )
    except Exception as exc:
        print(f"warn: setMyCommands failed: {exc}", file=sys.stderr, flush=True)

    offset = load_offset()
    backoff = 2
    while not STOP_EVENT.is_set():
        try:
            resp = tg.call(
                "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                timeout=40,
            )
            backoff = 2
        except Exception as exc:
            print(f"getUpdates error: {exc}", file=sys.stderr, flush=True)
            STOP_EVENT.wait(backoff)
            backoff = min(30, backoff * 2)
            continue

        for upd in resp.get("result") or []:
            offset = upd["update_id"] + 1
            save_offset(offset)

            msg = upd.get("message")
            if msg:
                cid = (msg.get("chat") or {}).get("id")
                fid = (msg.get("from") or {}).get("id")
                if cid == owner_id and fid == owner_id:
                    threading.Thread(target=handle_message, args=(tg, msg), daemon=True).start()
                continue

            cb = upd.get("callback_query")
            if cb:
                fid = (cb.get("from") or {}).get("id")
                cid = ((cb.get("message") or {}).get("chat") or {}).get("id")
                if cid == owner_id and fid == owner_id:
                    threading.Thread(target=handle_callback, args=(tg, cb), daemon=True).start()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
