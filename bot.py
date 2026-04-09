"""
╔══════════════════════════════════════════════════════╗
║       FREE UNLIMITED TELEGRAM AUTO FORWARDER         ║
║   No follower limit · No restrictions · 100% Free   ║
╚══════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)

# ── Read environment variables ────────────────────────────────────────────────
API_ID         = int(os.environ.get("API_ID", 0))
API_HASH       = os.environ.get("API_HASH", "")
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", 0))
SESSION_STRING = os.environ.get("SESSION_STRING", "")
CONFIG_FILE    = Path("config.json")

# ── Validate ──────────────────────────────────────────────────────────────────
missing = [k for k, v in {
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "BOT_TOKEN": BOT_TOKEN,
    "ADMIN_ID": ADMIN_ID,
    "SESSION_STRING": SESSION_STRING,
}.items() if not v]

if missing:
    raise SystemExit(f"❌ Missing environment variables: {', '.join(missing)}")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("forwarder")

# ── Config helpers ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "pairs": [],
    "affiliate_url": "",
    "affiliate_key": "",
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ── Link conversion ───────────────────────────────────────────────────────────
async def convert_link(url: str, cfg: dict, session: aiohttp.ClientSession) -> str:
    api_url = cfg.get("affiliate_url", "")
    api_key = cfg.get("affiliate_key", "")
    if not api_url:
        return url
    try:
        async with session.post(
            api_url, json={"url": url, "api_key": api_key},
            timeout=aiohttp.ClientTimeout(total=6)
        ) as r:
            if r.status == 200:
                data = await r.json()
                converted = data.get("converted_url") or data.get("short_url") or data.get("url")
                if converted:
                    return converted
    except Exception as e:
        log.warning("Link convert failed: %s", e)
    return url

URL_RE = re.compile(r'https?://[^\s\]\)>\"\']+')

async def rewrite_links(text: str, cfg: dict, session: aiohttp.ClientSession) -> str:
    urls = URL_RE.findall(text)
    for url in set(urls):
        converted = await convert_link(url, cfg, session)
        text = text.replace(url, converted)
    return text

# ── Keyword filter ────────────────────────────────────────────────────────────
def passes_filter(text: str, pair: dict) -> bool:
    kws = [k.strip().lower() for k in pair.get("filters", "").split(",") if k.strip()]
    if not kws:
        return True
    return any(kw in text.lower() for kw in kws)

# ── Telethon user client (uses StringSession — no login prompt!) ──────────────
user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

async def start_user_client():
    await user_client.connect()
    if not await user_client.is_user_authorized():
        raise SystemExit("❌ Session string is invalid or expired. Generate a new one.")
    me = await user_client.get_me()
    log.info("✅ Userbot logged in as %s (@%s)", me.first_name, me.username)

# ── Forward handler ───────────────────────────────────────────────────────────
async def handle_new_message(event):
    cfg = load_config()
    msg = event.message
    src = str(event.chat_id)

    matching = [p for p in cfg["pairs"] if p["active"] and (
        p["source"] == src or p["source"].lstrip("@") == src.lstrip("@")
    )]
    if not matching:
        return

    raw_text = msg.raw_text or ""

    async with aiohttp.ClientSession() as session:
        for pair in matching:
            if not passes_filter(raw_text, pair):
                continue

            delay = int(pair.get("delay", 0))
            if delay:
                await asyncio.sleep(delay)

            text = await rewrite_links(raw_text, cfg, session)

            prefix = pair.get("prefix", "")
            suffix = pair.get("suffix", "")
            if prefix:
                text = prefix + "\n" + text
            if suffix:
                text = text + "\n" + suffix

            dest = pair["dest"]
            try:
                if msg.media and not raw_text:
                    await user_client.send_file(dest, msg.media, caption=text or None)
                elif msg.media:
                    await user_client.send_file(dest, msg.media, caption=text)
                else:
                    await user_client.send_message(dest, text, link_preview=True)
                log.info("✅ Forwarded [pair %s] to %s", pair["id"], dest)
            except Exception as e:
                log.error("❌ Forward failed [pair %s]: %s", pair["id"], e)

# ── Conversation states ───────────────────────────────────────────────────────
(
    ASK_SOURCE, ASK_DEST, ASK_FILTER, ASK_DELAY,
    ASK_PREFIX, ASK_SUFFIX, EDIT_PAIR,
    ASK_AFF_URL, ASK_AFF_KEY,
) = range(9)

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, ctx)
    return wrapper

# ── /start ────────────────────────────────────────────────────────────────────
@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Auto Forwarder Bot* — Free & Unlimited\n\n"
        "Commands:\n"
        "/addpair — Add a new source→dest forwarding pair\n"
        "/pairs — List & manage all pairs\n"
        "/affiliate — Set affiliate link converter\n"
        "/status — Show bot status\n"
        "/help — Show this message",
        parse_mode="Markdown"
    )

# ── /addpair ──────────────────────────────────────────────────────────────────
@admin_only
async def cmd_addpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 *Step 1/4* — Send the *source* channel username or ID.\n"
        "Example: `@dealsource` or `-1001234567890`\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown"
    )
    return ASK_SOURCE

async def got_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_source"] = update.message.text.strip()
    await update.message.reply_text(
        "📤 *Step 2/4* — Send the *destination* channel username or ID.\n"
        "Example: `@mychannel` or `-1009876543210`",
        parse_mode="Markdown"
    )
    return ASK_DEST

async def got_dest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_dest"] = update.message.text.strip()
    await update.message.reply_text(
        "🔍 *Step 3/4* — Keyword filter (optional).\n"
        "Comma-separated words. Only messages containing these will be forwarded.\n"
        "Send `-` to skip (forward everything).",
        parse_mode="Markdown"
    )
    return ASK_FILTER

async def got_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    ctx.user_data["new_filter"] = "" if val == "-" else val
    await update.message.reply_text(
        "⏱ *Step 4/4* — Forward delay in seconds.\n"
        "Send `0` for instant forwarding.",
        parse_mode="Markdown"
    )
    return ASK_DELAY

async def got_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
    except ValueError:
        delay = 0
    cfg = load_config()
    new_id = str(len(cfg["pairs"]) + 1)
    pair = {
        "id": new_id,
        "source": ctx.user_data["new_source"],
        "dest": ctx.user_data["new_dest"],
        "filters": ctx.user_data.get("new_filter", ""),
        "delay": delay,
        "prefix": "",
        "suffix": "",
        "active": True,
    }
    cfg["pairs"].append(pair)
    save_config(cfg)
    register_listener(pair["source"])
    await update.message.reply_text(
        f"✅ *Pair #{new_id} created!*\n\n"
        f"Source: `{pair['source']}`\n"
        f"Dest: `{pair['dest']}`\n"
        f"Filter: `{pair['filters'] or 'None (all messages)'}`\n"
        f"Delay: `{delay}s`\n\n"
        "Forwarding is now *active* 🟢",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ── /pairs ────────────────────────────────────────────────────────────────────
@admin_only
async def cmd_pairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg["pairs"]:
        await update.message.reply_text("No pairs configured. Use /addpair to add one.")
        return
    for pair in cfg["pairs"]:
        status = "🟢 Active" if pair["active"] else "🔴 Paused"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "⏸ Pause" if pair["active"] else "▶️ Resume",
                callback_data=f"toggle_{pair['id']}"
            ),
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_{pair['id']}"),
        ]])
        await update.message.reply_text(
            f"*Pair #{pair['id']}* — {status}\n"
            f"📥 `{pair['source']}`\n"
            f"📤 `{pair['dest']}`\n"
            f"🔍 Filter: `{pair['filters'] or 'all'}`\n"
            f"⏱ Delay: `{pair.get('delay', 0)}s`",
            parse_mode="Markdown", reply_markup=kb
        )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg = load_config()
    if data.startswith("toggle_"):
        pid = data.split("_")[1]
        for p in cfg["pairs"]:
            if p["id"] == pid:
                p["active"] = not p["active"]
                save_config(cfg)
                state = "🟢 Active" if p["active"] else "🔴 Paused"
                await query.edit_message_text(f"Pair #{pid} is now *{state}*", parse_mode="Markdown")
                return
    if data.startswith("delete_"):
        pid = data.split("_")[1]
        cfg["pairs"] = [p for p in cfg["pairs"] if p["id"] != pid]
        save_config(cfg)
        await query.edit_message_text(f"🗑 Pair #{pid} deleted.")

# ── /affiliate ────────────────────────────────────────────────────────────────
@admin_only
async def cmd_affiliate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    current = cfg.get("affiliate_url", "") or "Not set"
    await update.message.reply_text(
        f"🔗 *Affiliate Link Converter*\n\nCurrent API: `{current}`\n\n"
        "Send the API endpoint URL, or `-` to disable.",
        parse_mode="Markdown"
    )
    return ASK_AFF_URL

async def got_aff_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    cfg = load_config()
    cfg["affiliate_url"] = "" if val == "-" else val
    save_config(cfg)
    if val == "-":
        await update.message.reply_text("✅ Affiliate converter disabled.")
        return ConversationHandler.END
    await update.message.reply_text("Send your API key (or `-` if not needed):")
    return ASK_AFF_KEY

async def got_aff_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    cfg = load_config()
    cfg["affiliate_key"] = "" if val == "-" else val
    save_config(cfg)
    await update.message.reply_text("✅ Affiliate converter configured!")
    return ConversationHandler.END

# ── /status ───────────────────────────────────────────────────────────────────
@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    total  = len(cfg["pairs"])
    active = sum(1 for p in cfg["pairs"] if p["active"])
    aff    = cfg.get("affiliate_url") or "None"
    await update.message.reply_text(
        f"📊 *Bot Status*\n\n"
        f"Total pairs: `{total}`\n"
        f"Active: `{active}`\n"
        f"Paused: `{total - active}`\n"
        f"Affiliate API: `{aff}`",
        parse_mode="Markdown"
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ── Telethon listeners ────────────────────────────────────────────────────────
_registered_sources: set = set()

def register_listener(source: str):
    if source in _registered_sources:
        return
    _registered_sources.add(source)
    user_client.add_event_handler(handle_new_message, events.NewMessage(chats=source))
    log.info("Registered listener for %s", source)

def register_all_listeners():
    cfg = load_config()
    for pair in cfg["pairs"]:
        if pair["active"]:
            register_listener(pair["source"])

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Starting Auto Forwarder Bot...")
    await start_user_client()
    register_all_listeners()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addpair", cmd_addpair)],
        states={
            ASK_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_source)],
            ASK_DEST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_dest)],
            ASK_FILTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_filter)],
            ASK_DELAY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_delay)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("affiliate", cmd_affiliate)],
        states={
            ASK_AFF_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_aff_url)],
            ASK_AFF_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_aff_key)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    ))

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("pairs",  cmd_pairs))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_handler))

    log.info("✅ Bot is running! Open Telegram and send /start to your bot.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await user_client.run_until_disconnected()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
