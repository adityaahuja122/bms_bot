import json
import os
import subprocess
import sys
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN      = os.getenv("TG_TOKEN", "8666314563:AAFXDLrKjlkWz41rLo9BLdkutJj4h1Y8JKA")
CHAT_IDS_RAW = os.getenv("TG_CHAT_IDS", "924367933,1707720927")
CHAT_IDS   = [int(x.strip()) for x in CHAT_IDS_RAW.split(",") if x.strip()]

EVENT_FILE  = "events.json"
STATUS_FILE = "process_status.json"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def auth(update: Update) -> bool:
    return update.effective_chat.id in CHAT_IDS


# ─── Storage ──────────────────────────────────────────────────────────────────

def load_events():
    if not os.path.exists(EVENT_FILE):
        return []
    try:
        with open(EVENT_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_events(events):
    with open(EVENT_FILE, "w") as f:
        json.dump(events, f, indent=2)

def read_monitor_status():
    """Read process_status.json written by monitor.py"""
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except:
        return None


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "🎫 <b>BMS Monitor Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/addevent <code>&lt;link&gt;</code> — Add event URL\n"
        "/removeevent <code>&lt;link&gt;</code> — Remove URL\n"
        "/listevents — Show all tracked links\n"
        "/clearevents — Remove all links\n"
        "/monitor — Check all tracked events NOW\n"
        "/monitor <code>&lt;link&gt;</code> — Check one specific URL NOW\n"
        "/status — Bot &amp; monitor status\n"
        "/help — Show this menu\n\n"
        "💡 When booking opens or tickets fill fast, you'll get an alert with a <b>one-tap BOOK NOW button</b>.",
        parse_mode="HTML"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def addevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if not context.args:
        await update.message.reply_text("❌ Usage: /addevent &lt;link&gt;", parse_mode="HTML")
        return
    link = context.args[0].strip()
    if not link.startswith("http"):
        await update.message.reply_text("❌ Invalid URL — must start with <code>http</code>", parse_mode="HTML")
        return
    events = load_events()
    if link in events:
        await update.message.reply_text("⚠️ Already monitoring this link")
        return
    events.append(link)
    save_events(events)
    await update.message.reply_text(
        f"✅ <b>Added!</b>\n\n"
        f"🔗 <code>{link}</code>\n\n"
        f"📋 Total tracked: <b>{len(events)}</b>",
        parse_mode="HTML"
    )


async def removeevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if not context.args:
        await update.message.reply_text("❌ Usage: /removeevent &lt;link&gt;", parse_mode="HTML")
        return
    link = context.args[0].strip()
    events = load_events()
    if link in events:
        events.remove(link)
        save_events(events)
        await update.message.reply_text(f"🗑 Removed\n\nRemaining: <b>{len(events)}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Link not found in list")


async def listevents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    events = load_events()
    if not events:
        await update.message.reply_text(
            "📋 No events monitored.\n\nUse /addevent &lt;link&gt; to add one.",
            parse_mode="HTML"
        )
        return
    lines = [f"📋 <b>Monitored Events ({len(events)})</b>\n"]
    for i, url in enumerate(events, 1):
        lines.append(f"{i}. <code>{url}</code>")
    keyboard = [[InlineKeyboardButton(f"🔗 Open #{i}", url=url)] for i, url in enumerate(events, 1)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=reply_markup
    )


async def clearevents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    save_events([])
    await update.message.reply_text("🗑 All events cleared!")


# ─── /monitor ─────────────────────────────────────────────────────────────────

async def monitor_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return

    python       = sys.executable
    monitor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.py")

    if context.args:
        url = context.args[0].strip()
        if not url.startswith("http"):
            await update.message.reply_text(
                "❌ Invalid URL — must start with <code>http</code>",
                parse_mode="HTML"
            )
            return
        await update.message.reply_text(
            f"🔍 <b>Checking single event...</b>\n\n"
            f"🔗 <code>{url}</code>\n\n"
            f"Alert will arrive shortly.",
            parse_mode="HTML"
        )
        try:
            subprocess.Popen(
                [python, monitor_path, "--once", "--url", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not start monitor: {e}")
        return

    events = load_events()
    if not events:
        await update.message.reply_text(
            "❌ No events to check.\n\n"
            "Add one with /addevent &lt;link&gt;\n"
            "Or check a specific URL: /monitor &lt;link&gt;",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(
        f"🔍 <b>Manual check started!</b>\n\n"
        f"Checking <b>{len(events)}</b> event(s) now...\n"
        f"Results will arrive as alerts shortly.",
        parse_mode="HTML"
    )
    try:
        subprocess.Popen(
            [python, monitor_path, "--once"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not start monitor: {e}")


# ─── /status — reads process_status.json written by monitor.py ───────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return

    events  = load_events()
    ms      = read_monitor_status()

    if ms:
        # Determine if the monitor process is actually alive
        pid = ms.get("pid")
        alive = False
        try:
            if pid:
                os.kill(pid, 0)   # signal 0 = just check existence
                alive = True
        except (OSError, ProcessLookupError):
            alive = False

        state_str  = ms.get("state", "unknown")
        started_at = ms.get("started_at", "—")
        last_check = ms.get("last_check", "—")
        uptime_s   = ms.get("uptime_s", 0)
        next_in    = ms.get("next_check_in", "—")
        tracked    = ms.get("events_tracked", len(events))

        h, rem = divmod(int(uptime_s), 3600)
        m, s   = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

        if alive:
            mon_line = f"🟢 Running  <i>({state_str})</i>"
        else:
            mon_line = "🔴 Stopped  <i>(stale status file)</i>"

        await update.message.reply_text(
            f"📊 <b>System Status</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot        : 🟢 Online\n"
            f"📡 Monitor    : {mon_line}\n"
            f"🆙 Uptime     : <b>{uptime_str}</b>\n"
            f"🕐 Started    : {started_at}\n"
            f"🔍 Last check : {last_check}\n"
            f"⏳ Next check : in <b>{next_in}s</b>\n"
            f"🎫 Tracking   : <b>{tracked}</b> event(s)",
            parse_mode="HTML"
        )
    else:
        # Fallback — try pgrep
        try:
            result     = subprocess.run(["pgrep", "-f", "monitor.py"], capture_output=True, text=True)
            monitor_on = bool(result.stdout.strip())
            monitor_str = "🟢 Running  <i>(no detail available)</i>" if monitor_on else "🔴 Not running"
        except:
            monitor_str = "❓ Unknown"

        await update.message.reply_text(
            f"📊 <b>System Status</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot      : 🟢 Online\n"
            f"📡 Monitor  : {monitor_str}\n"
            f"🎫 Tracking : <b>{len(events)}</b> event(s)",
            parse_mode="HTML"
        )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("❓ Unknown command. Use /help")


# ─── Run ──────────────────────────────────────────────────────────────────────

def main():
    print("Bot starting...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("help",         help_cmd))
    app.add_handler(CommandHandler("addevent",     addevent))
    app.add_handler(CommandHandler("removeevent",  removeevent))
    app.add_handler(CommandHandler("listevents",   listevents))
    app.add_handler(CommandHandler("clearevents",  clearevents))
    app.add_handler(CommandHandler("monitor",      monitor_now))
    app.add_handler(CommandHandler("status",       status))
    print("Bot ready. Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()