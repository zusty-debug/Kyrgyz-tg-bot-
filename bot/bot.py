"""
Telegram bot for the Kyrgyz data search API.

Standalone service — talks to the API over HTTPS, never touches the
database of the API directly. Uses its own tables (bot_users,
bot_daily_usage) on the same Postgres instance.

Roles:
  owner    : Telegram ID configured via OWNER_TELEGRAM_ID env var.
             Unlimited searches. Can promote/demote admins.
  admin    : Promoted by owner. Unlimited searches. Can grant/revoke premium.
  premium  : Granted by owner/admin. Unlimited searches.
  regular  : 5 searches per 24-hour window (resets at midnight UTC).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime

import requests
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Integer, String, create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# --- Configuration ---------------------------------------------------------

API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
OWNER_TELEGRAM_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0"))
DEV_HANDLE = os.environ.get("DEV_HANDLE", "@xzusty").strip() or "@xzusty"
DAILY_QUOTA = int(os.environ.get("DAILY_QUOTA", "5"))
PER_PAGE = int(os.environ.get("PER_PAGE", "5"))
MAX_RESPONSE_CHARS = 3500  # leave headroom under Telegram's 4096 limit

_missing = [n for n, v in (
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("API_KEY", API_KEY),
    ("OWNER_TELEGRAM_ID", OWNER_TELEGRAM_ID),
    ("DATABASE_URL", DATABASE_URL),
) if not v]
if _missing:
    raise RuntimeError(
        "Missing required env vars: "
        + ", ".join(_missing)
        + ". Set them in Render dashboard for the worker service."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# --- Database --------------------------------------------------------------

Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class BotUser(Base):
    __tablename__ = "bot_users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String(64))
    full_name = Column(String(255))
    role = Column(String(16), default="regular", index=True)
    added_by = Column(BigInteger)
    added_at = Column(DateTime, default=datetime.utcnow)


class DailyUsage(Base):
    __tablename__ = "bot_daily_usage"
    user_id = Column(BigInteger, primary_key=True)
    usage_date = Column(Date, primary_key=True, default=date.today)
    count = Column(Integer, default=0)
    last_query = Column(String(500))
    last_page = Column(Integer, default=1)


Base.metadata.create_all(engine)
log.info("Database tables ready (bot_users, bot_daily_usage).")


def _today() -> date:
    return date.today()


def _full_name(tg_user) -> str:
    return (
        (tg_user.first_name or "")
        + (" " + tg_user.last_name if tg_user.last_name else "")
    ).strip()


def get_or_create_user(s, tg_user) -> BotUser:
    u = s.query(BotUser).filter_by(user_id=tg_user.id).first()
    if u is None:
        u = BotUser(
            user_id=tg_user.id,
            username=tg_user.username or "",
            full_name=_full_name(tg_user),
            role="regular",
        )
        s.add(u)
    else:
        new_name = _full_name(tg_user)
        if u.full_name != new_name:
            u.full_name = new_name
        if tg_user.username and u.username != tg_user.username:
            u.username = tg_user.username
    # Bootstrap owner on first sight
    if tg_user.id == OWNER_TELEGRAM_ID and u.role != "owner":
        u.role = "owner"
    s.commit()
    return u


def get_user(s, user_id: int):
    return s.query(BotUser).filter_by(user_id=user_id).first()


def get_usage(s, user_id: int):
    return s.query(DailyUsage).filter_by(
        user_id=user_id, usage_date=_today()
    ).first()


def set_last_query(s, user_id, query, page):
    row = get_usage(s, user_id)
    if row is None:
        row = DailyUsage(
            user_id=user_id, usage_date=_today(),
            count=0, last_query=query, last_page=page,
        )
        s.add(row)
    else:
        row.last_query = query
        row.last_page = page
    s.commit()


def increment_usage(s, user_id):
    row = get_usage(s, user_id)
    if row is None:
        row = DailyUsage(user_id=user_id, usage_date=_today(), count=1)
        s.add(row)
    else:
        row.count = (row.count or 0) + 1
    s.commit()


def is_unlimited(role: str) -> bool:
    return role in ("owner", "admin", "premium")


ROLE_LABEL = {
    "owner": "👑 Owner",
    "admin": "🛡️ Admin",
    "premium": "⭐ Premium",
    "regular": "👤 Regular",
}

# --- Query parsing ---------------------------------------------------------

KEYMAP = {
    "name": "name", "n": "name",
    "id": "person_id", "iin": "person_id", "pid": "person_id",
    "region": "region", "r": "region", "reg": "region",
    "city": "city", "c": "city",
    "address": "address", "addr": "address", "a": "address",
    "dob": "dob", "d": "dob", "date": "dob",
}


def parse_query(qs: str) -> dict:
    parts = qs.split()
    if not parts:
        return {}
    if not any("=" in p for p in parts):
        return {"name": qs}
    params = {}
    for part in parts:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in KEYMAP:
            params[KEYMAP[k]] = v
    return params


# --- API client ------------------------------------------------------------

def call_search(params: dict, page: int = 1, limit: int = PER_PAGE) -> dict:
    headers = {"X-API-Key": API_KEY}
    qp = {**params, "page": page, "limit": limit}
    r = requests.get(
        f"{API_URL}/api/search", params=qp, headers=headers, timeout=20
    )
    if r.status_code != 200:
        raise RuntimeError(f"API returned {r.status_code}: {r.text[:200]}")
    return r.json()


# --- Strings ---------------------------------------------------------------

WELCOME = (
    "👋 Welcome to *Kyrgyzstan Residents Search* 🔎\n\n"
    "Search the database by name, region, city, address, DOB or ID.\n\n"
    "Try `/help` for examples.\n\n"
    f"Developer - {DEV_HANDLE}"
)

HELP = (
    "📘 *How to search*\n\n"
    "Free-text name search:\n"
    "`/search Капинос`\n"
    "`/search Кайрат`\n\n"
    "Filter by field:\n"
    "`/search city=Бишкек`\n"
    "`/search region=Чуйская`\n"
    "`/search address=Советская`\n"
    "`/search id=20306195500450`\n"
    "`/search dob=1986-03-12`\n\n"
    "Combine:\n"
    "`/search name=Кайрат region=Чуйская`\n\n"
    "Commands:\n"
    "  `/search <query>` — search\n"
    "  `/quota`         — searches left today\n"
    "  `/myinfo`        — your role & usage\n"
    "  `/help`          — this help\n\n"
    "Results are JSON, paginated — tap `Next 5 →` to keep going (free within same search).\n\n"
    f"Developer - {DEV_HANDLE}"
)

# --- Handlers --------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        u = get_or_create_user(s, update.effective_user)
        extras = ""
        if u.role == "owner": extras = "\n\n👑 You are the *OWNER*."
        elif u.role == "admin": extras = "\n\n🛡️ You are an *ADMIN* (unlimited)."
        elif u.role == "premium": extras = "\n\n⭐ You are *PREMIUM* (unlimited)."
        await update.message.reply_text(WELCOME + extras, parse_mode=ParseMode.MARKDOWN)
    finally:
        s.close()


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def cmd_myinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        u = get_or_create_user(s, update.effective_user)
        usage = get_usage(s, u.user_id)
        used = usage.count if usage else 0
        unlimited = is_unlimited(u.role)
        if unlimited:
            quota_text = "♾️ Unlimited"
            used_text = "∞"
        else:
            rem = max(0, DAILY_QUOTA - used)
            quota_text = f"{rem} of {DAILY_QUOTA} left today"
            used_text = str(used)
        msg = (
            "👤 *Your profile*\n\n"
            f"  Telegram ID : `{u.user_id}`\n"
            f"  Name        : {u.full_name or '–'}\n"
            f"  Username    : @{u.username or '–'}\n"
            f"  Role        : {ROLE_LABEL.get(u.role, u.role)}\n"
            f"  Searches today: {used_text}\n"
            f"  Quota       : {quota_text}\n\n"
            f"Developer - {DEV_HANDLE}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    finally:
        s.close()


async def cmd_quota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        u = get_or_create_user(s, update.effective_user)
        if is_unlimited(u.role):
            await update.message.reply_text(
                f"♾️ Unlimited searches (role: `{u.role}`).\n\n"
                f"Developer - {DEV_HANDLE}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        usage = get_usage(s, u.user_id)
        used = usage.count if usage else 0
        rem = max(0, DAILY_QUOTA - used)
        await update.message.reply_text(
            f"📊 *Today's quota*\n\n"
            f"  used: {used}\n"
            f"  remaining: {rem} of {DAILY_QUOTA}\n\n"
            "Resets at midnight UTC.\n\n"
            f"Developer - {DEV_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s.close()


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        tg_user = update.effective_user
        user = get_or_create_user(s, tg_user)
        qs = " ".join(ctx.args or []).strip()
        if not qs:
            await update.message.reply_text(
                "❌ Empty query.\nTry: `/search Капинос` or `/search city=Бишкек`\n\n"
                f"Developer - {DEV_HANDLE}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not is_unlimited(user.role):
            usage = get_usage(s, tg_user.id)
            used = usage.count if usage else 0
            if used >= DAILY_QUOTA:
                await update.message.reply_text(
                    f"⚠️ You've used all *{DAILY_QUOTA}* searches today.\n"
                    f"Try again tomorrow (resets midnight UTC).\n\n"
                    f"Developer - {DEV_HANDLE}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
        params = parse_query(qs)
        if not params:
            await update.message.reply_text(
                "❌ Couldn't parse query.\nTry: `/search Капинос` or `/search city=Бишкек`\n\n"
                f"Developer - {DEV_HANDLE}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        set_last_query(s, tg_user.id, qs, 1)
        if not is_unlimited(user.role):
            increment_usage(s, tg_user.id)
        await _send_results(s, qs, params, page=1, user_obj=user, target=update.message)
    finally:
        s.close()


async def _send_results(s, qs, params, page, user_obj, target):
    """Send search results as JSON code block + pagination buttons."""
    try:
        data = call_search(params, page=page, limit=PER_PAGE)
    except Exception as e:
        text = f"❌ Search failed: `{e}`\n\nDeveloper - {DEV_HANDLE}"
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await target.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    count = data.get("count", 0)
    results = data.get("results", [])
    payload = {
        "query": qs,
        "page": page,
        "per_page": PER_PAGE,
        "matches": count,
        "shown": len(results),
        "results": results,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if len(text) > MAX_RESPONSE_CHARS:
        truncated = {
            "query": qs,
            "page": page,
            "per_page": PER_PAGE,
            "matches": count,
            "shown": 3,
            "results": results[:3],
            "_note": "Output truncated. Refine your search to see more.",
        }
        text = json.dumps(truncated, indent=2, ensure_ascii=False, default=str)
        if len(text) > MAX_RESPONSE_CHARS:
            text = text[:MAX_RESPONSE_CHARS] + "\n... (truncated)"

    total_pages = max(1, (count + PER_PAGE - 1) // PER_PAGE)
    kb = []
    if page > 1:
        kb.append(InlineKeyboardButton("← Prev 5", callback_data=f"pg:{page - 1}"))
    if page < total_pages:
        kb.append(InlineKeyboardButton(
            f"Next 5 → (page {page + 1}/{total_pages})",
            callback_data=f"pg:{page + 1}",
        ))
    reply_markup = InlineKeyboardMarkup([kb]) if kb else None

    body = f"```json\n{text}\n```\n\nDeveloper - {DEV_HANDLE}"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(
            body, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )
    else:
        await target.reply_text(
            body, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )


async def on_paginate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        q = update.callback_query
        await q.answer()
        tg_user = q.from_user
        user = get_or_create_user(s, tg_user)
        data = q.data or ""
        if not data.startswith("pg:"):
            return
        try:
            new_page = int(data.split(":", 1)[1])
        except ValueError:
            return
        usage = get_usage(s, tg_user.id)
        if usage is None or not usage.last_query:
            await q.edit_message_text(
                "⚠️ No recent search found. Send a new /search first.\n\n"
                f"Developer - {DEV_HANDLE}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        qs = usage.last_query
        params = parse_query(qs)
        set_last_query(s, tg_user.id, qs, new_page)
        await _send_results(s, qs, params, page=new_page, user_obj=user, target=q)
    finally:
        s.close()


# --- Admin commands --------------------------------------------------------

def _parse_target_id(ctx):
    if not ctx.args:
        return None
    arg = ctx.args[0].lstrip("@")
    return int(arg) if arg.isdigit() else None


async def cmd_grant_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        me = get_or_create_user(s, update.effective_user)
        if me.role != "owner":
            await update.message.reply_text("⛔ Only the owner can grant admin.")
            return
        tid = _parse_target_id(ctx)
        if tid is None:
            await update.message.reply_text("Usage: `/grant_admin <user_id>`")
            return
        if tid == OWNER_TELEGRAM_ID:
            await update.message.reply_text("🤔 That's you (the owner).")
            return
        u = get_user(s, tid)
        if u is None:
            await update.message.reply_text(
                f"❌ No bot user with ID `{tid}`. They must `/start` the bot first."
            )
            return
        prev = u.role
        u.role = "admin"
        u.added_by = me.user_id
        u.added_at = datetime.utcnow()
        s.commit()
        await update.message.reply_text(
            f"✅ User `{tid}` promoted: {prev} → 🛡️ *admin* (unlimited).\n\n"
            f"Developer - {DEV_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s.close()


async def cmd_revoke_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        me = get_or_create_user(s, update.effective_user)
        if me.role != "owner":
            await update.message.reply_text("⛔ Only the owner can revoke admin.")
            return
        tid = _parse_target_id(ctx)
        if tid is None:
            await update.message.reply_text("Usage: `/revoke_admin <user_id>`")
            return
        u = get_user(s, tid)
        if u is None or u.role != "admin":
            await update.message.reply_text(f"❌ No admin with ID `{tid}`.")
            return
        u.role = "regular"
        u.added_by = me.user_id
        u.added_at = datetime.utcnow()
        s.commit()
        await update.message.reply_text(
            f"✅ User `{tid}` demoted: 🛡️ admin → 👤 regular.\n\n"
            f"Developer - {DEV_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s.close()


async def cmd_grant_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        me = get_or_create_user(s, update.effective_user)
        if me.role not in ("owner", "admin"):
            await update.message.reply_text("⛔ Only owner or admin can grant premium.")
            return
        tid = _parse_target_id(ctx)
        if tid is None:
            await update.message.reply_text("Usage: `/grant_premium <user_id>`")
            return
        u = get_user(s, tid)
        if u is None:
            await update.message.reply_text(
                f"❌ No bot user with ID `{tid}`. They must `/start` the bot first."
            )
            return
        u.role = "premium"
        u.added_by = me.user_id
        u.added_at = datetime.utcnow()
        s.commit()
        await update.message.reply_text(
            f"⭐ User `{tid}` granted *premium* (unlimited searches).\n\n"
            f"Developer - {DEV_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s.close()


async def cmd_revoke_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        me = get_or_create_user(s, update.effective_user)
        if me.role not in ("owner", "admin"):
            await update.message.reply_text("⛔ Only owner or admin can revoke premium.")
            return
        tid = _parse_target_id(ctx)
        if tid is None:
            await update.message.reply_text("Usage: `/revoke_premium <user_id>`")
            return
        u = get_user(s, tid)
        if u is None or u.role != "premium":
            await update.message.reply_text(f"❌ No premium user with ID `{tid}`.")
            return
        u.role = "regular"
        u.added_by = me.user_id
        u.added_at = datetime.utcnow()
        s.commit()
        await update.message.reply_text(
            f"✅ User `{tid}` premium revoked → 👤 regular.\n\n"
            f"Developer - {DEV_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        s.close()


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SessionLocal()
    try:
        me = get_or_create_user(s, update.effective_user)
        if me.role not in ("owner", "admin"):
            await update.message.reply_text("⛔ Only owner or admin can list users.")
            return
        rows = s.query(BotUser).order_by(BotUser.role, BotUser.user_id).all()
        if not rows:
            await update.message.reply_text("No users yet.")
            return
        by_role = {"owner": [], "admin": [], "premium": [], "regular": []}
        for u in rows:
            by_role.setdefault(u.role, []).append(u)
        lines = ["📋 *Users by role*\n"]
        for role, key in [
            ("owner", "👑 Owners"),
            ("admin", "🛡️ Admins"),
            ("premium", "⭐ Premium"),
            ("regular", "👤 Regular"),
        ]:
            users = by_role.get(role, [])
            lines.append(f"\n{key}: *{len(users)}*")
            for u in users[:10]:
                uname = f"@{u.username}" if u.username else "–"
                lines.append(f"  • `{u.user_id}` {u.full_name or '–'} ({uname})")
            if len(users) > 10:
                lines.append(f"  … and {len(users) - 10} more")
        lines.append(f"\nDeveloper - {DEV_HANDLE}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    finally:
        s.close()


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Unknown command. Try `/help`.\n\n"
        f"Developer - {DEV_HANDLE}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def post_init(app: Application):
    s = SessionLocal()
    try:
        u = s.query(BotUser).filter_by(user_id=OWNER_TELEGRAM_ID).first()
        if u is None:
            u = BotUser(
                user_id=OWNER_TELEGRAM_ID,
                username="",
                full_name="Owner",
                role="owner",
            )
            s.add(u)
        else:
            u.role = "owner"
        s.commit()
        log.info("Bot ready. Owner=%s. Calling API: %s", OWNER_TELEGRAM_ID, API_URL)
    finally:
        s.close()


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("quota", cmd_quota))
    app.add_handler(CommandHandler("myinfo", cmd_myinfo))
    app.add_handler(CommandHandler("grant_admin", cmd_grant_admin))
    app.add_handler(CommandHandler("revoke_admin", cmd_revoke_admin))
    app.add_handler(CommandHandler("grant_premium", cmd_grant_premium))
    app.add_handler(CommandHandler("revoke_premium", cmd_revoke_premium))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_paginate, pattern=r"^pg:"))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    log.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
