"""
Crypto Signals Pro Bot - v2
Features:
- 3 tiers: Free / Premium (200 Stars) / Premium+ (500 Stars)
- Live prices from CoinGecko (free, no key needed)
- Fear & Greed Index
- Trending coins + crypto news
- Price move alerts (5% free / 2% premium / 0.5% premium+)
- Whale alerts ($1M+ premium / $100K+ premium+)
- Portfolio tracker, AI market analysis, DeFi yields, VIP signals (Premium+)
- /admin dashboard
- Referral system (7 free premium days per referral)
"""

import os
import logging
import sqlite3
import json
import asyncio
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Optional

import httpx
from flask import Flask, jsonify
from flask_cors import CORS
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
ADMIN_IDS        = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x]
WHALE_ALERT_KEY  = os.environ.get("WHALE_ALERT_KEY", "")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "")   # e.g. @cryptosignalsdaily

# ── Pricing ───────────────────────────────────────────────────────────────────
PREMIUM_STARS      = 200
PREMIUM_PLUS_STARS = 500

# ── Alert thresholds ──────────────────────────────────────────────────────────
ALERT_FREE         = 5.0    # %
ALERT_PREMIUM      = 2.0    # %
ALERT_PREMIUM_PLUS = 0.5    # %
WHALE_PREMIUM      = 1_000_000   # USD
WHALE_PREMIUM_PLUS = 100_000     # USD

# ── Coin lists ────────────────────────────────────────────────────────────────
FREE_COINS    = ["bitcoin", "ethereum", "solana", "bnb", "xrp"]
PREMIUM_COINS = FREE_COINS + [
    "cardano", "dogecoin", "tron", "avalanche-2", "chainlink",
    "polygon", "litecoin", "polkadot", "shiba-inu", "dai",
]

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect("crypto.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id       INTEGER PRIMARY KEY,
        username      TEXT,
        first_name    TEXT,
        tier          TEXT DEFAULT 'free',
        tier_until    TEXT,
        stars_spent   INTEGER DEFAULT 0,
        referral_code TEXT UNIQUE,
        referred_by   INTEGER,
        joined_at     TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS portfolios (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  INTEGER,
        coin_id  TEXT,
        amount   REAL
    );

    CREATE TABLE IF NOT EXISTS price_cache (
        coin_id    TEXT PRIMARY KEY,
        price_usd  REAL,
        change_24h REAL,
        updated_at TEXT
    );
    """)
    db.commit()
    db.close()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_referral_code(user_id: int) -> str:
    return hashlib.md5(f"crypto_{user_id}".encode()).hexdigest()[:8].upper()

def get_or_create_user(user_id: int, username: str = "", first_name: str = "") -> sqlite3.Row:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        code = make_referral_code(user_id)
        db.execute(
            "INSERT INTO users (user_id, username, first_name, referral_code) VALUES (?,?,?,?)",
            (user_id, username, first_name, code)
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    db.close()
    return row

def user_tier(user_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT tier, tier_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    db.close()
    if not row:
        return "free"
    if row["tier"] in ("premium", "premium_plus"):
        if row["tier_until"] and datetime.fromisoformat(row["tier_until"]) < datetime.utcnow():
            db2 = get_db()
            db2.execute("UPDATE users SET tier='free', tier_until=NULL WHERE user_id=?", (user_id,))
            db2.commit()
            db2.close()
            return "free"
        return row["tier"]
    return "free"

def grant_premium(user_id: int, tier: str, days: int):
    db = get_db()
    row = db.execute("SELECT tier_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    now = datetime.utcnow()
    base = max(datetime.fromisoformat(row["tier_until"]), now) if row and row["tier_until"] else now
    until = (base + timedelta(days=days)).isoformat()
    db.execute("UPDATE users SET tier=?, tier_until=? WHERE user_id=?", (tier, until, user_id))
    db.commit()
    db.close()

def handle_referral(new_user_id: int, code: str) -> Optional[int]:
    db = get_db()
    referrer = db.execute("SELECT user_id FROM users WHERE referral_code=?", (code,)).fetchone()
    if referrer and referrer["user_id"] != new_user_id:
        already = db.execute("SELECT id FROM referrals WHERE referred_id=?", (new_user_id,)).fetchone()
        if not already:
            db.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (?,?)",
                (referrer["user_id"], new_user_id)
            )
            db.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer["user_id"], new_user_id))
            db.commit()
            db.close()
            return referrer["user_id"]
    db.close()
    return None

# ─────────────────────────────────────────────────────────────────────────────
# COINGECKO API
# ─────────────────────────────────────────────────────────────────────────────

async def get_prices(coin_ids: list[str]) -> dict:
    ids = ",".join(coin_ids)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            data = r.json()
            # Cache prices
            db = get_db()
            for cid, vals in data.items():
                db.execute(
                    "INSERT OR REPLACE INTO price_cache (coin_id, price_usd, change_24h, updated_at) VALUES (?,?,?,?)",
                    (cid, vals.get("usd", 0), vals.get("usd_24h_change", 0), datetime.utcnow().isoformat())
                )
            db.commit()
            db.close()
            return data
    except Exception as e:
        log.warning(f"CoinGecko price error: {e}")
        return {}

async def get_fear_greed() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            d = r.json()["data"][0]
            return f"{d['value']} — {d['value_classification']}"
    except Exception:
        return "Unavailable"

async def get_trending_coins() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.coingecko.com/api/v3/search/trending")
            coins = r.json().get("coins", [])
            return [c["item"]["name"] for c in coins[:5]]
    except Exception:
        return []

async def get_whale_alerts(min_usd: int) -> list[dict]:
    if not WHALE_ALERT_KEY:
        return []
    try:
        url = f"https://api.whale-alert.io/v1/transactions?api_key={WHALE_ALERT_KEY}&min_value={min_usd}&limit=5"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            txs = r.json().get("transactions", [])
            return txs
    except Exception as e:
        log.warning(f"Whale alert error: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def main_menu(tier: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📈 Live Prices",    callback_data="prices"),
            InlineKeyboardButton("😱 Fear & Greed",   callback_data="fear"),
        ],
        [
            InlineKeyboardButton("🔥 Trending",       callback_data="trending"),
            InlineKeyboardButton("🐋 Whale Alerts",   callback_data="whales"),
        ],
    ]
    if tier == "free":
        rows.append([InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="upgrade")])
    elif tier == "premium":
        rows.append([InlineKeyboardButton("💎 Upgrade to Premium+", callback_data="upgrade_plus")])
    else:
        rows.append([InlineKeyboardButton("💼 My Portfolio", callback_data="portfolio")])

    rows.append([
        InlineKeyboardButton("👥 Refer a Friend", callback_data="referral"),
        InlineKeyboardButton("⚙️ My Account",     callback_data="account"),
    ])
    return InlineKeyboardMarkup(rows)

def upgrade_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Premium — {PREMIUM_STARS} Stars/mo",      callback_data="buy_premium")],
        [InlineKeyboardButton(f"💎 Premium+ — {PREMIUM_PLUS_STARS} Stars/mo", callback_data="buy_premium_plus")],
        [InlineKeyboardButton("◀️ Back", callback_data="back")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args or []

    get_or_create_user(user.id, user.username or "", user.first_name or "")

    referrer_id = None
    if args and args[0].startswith("ref_"):
        code = args[0][4:]
        referrer_id = handle_referral(user.id, code)

    if referrer_id:
        grant_premium(user.id, "premium", 3)
        grant_premium(referrer_id, "premium", 7)
        try:
            await ctx.bot.send_message(
                referrer_id,
                "🎉 Someone joined via your referral! You earned 7 free Premium days!"
            )
        except Exception:
            pass
        await update.message.reply_text(
            "🎉 Welcome! You've been given 3 free Premium days as a referral bonus!"
        )

    tier = user_tier(user.id)
    welcome = (
        f"👋 Welcome to *Crypto Signals Pro*, {user.first_name}!\n\n"
        f"📈 Real-time crypto prices, alerts & whale tracking.\n\n"
        f"*Your tier:* {'💎 Premium+' if tier=='premium_plus' else '⭐ Premium' if tier=='premium' else '🆓 Free'}\n\n"
        f"🆓 Free: Top 5 coins, 5% move alerts\n"
        f"⭐ Premium: All coins, 2% alerts, whale alerts $1M+\n"
        f"💎 Premium+: 0.5% alerts, whale $100K+, portfolio, AI analysis"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=main_menu(tier))

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    db = get_db()
    total   = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = db.execute("SELECT COUNT(*) FROM users WHERE tier='premium'").fetchone()[0]
    plus    = db.execute("SELECT COUNT(*) FROM users WHERE tier='premium_plus'").fetchone()[0]
    stars   = db.execute("SELECT COALESCE(SUM(stars_spent),0) FROM users").fetchone()[0]
    refs    = db.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
    db.close()
    usd = round(stars * 0.0125, 2)
    await update.message.reply_text(
        f"📊 *Crypto Bot Admin*\n\n"
        f"👥 Total users: {total}\n"
        f"⭐ Premium: {premium}\n"
        f"💎 Premium+: {plus}\n"
        f"🆓 Free: {total - premium - plus}\n\n"
        f"⭐ Stars earned: {stars} (~${usd})\n"
        f"👥 Referrals: {refs}",
        parse_mode="Markdown"
    )

async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_tier(uid) != "premium_plus":
        await update.message.reply_text("💎 Portfolio tracking is a Premium+ feature.")
        return
    if not ctx.args or len(ctx.args) != 2:
        await update.message.reply_text("Usage: /portfolio <coin> <amount>\nExample: /portfolio bitcoin 0.5")
        return
    coin, amount = ctx.args[0].lower(), ctx.args[1]
    try:
        amount = float(amount)
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO portfolios (user_id, coin_id, amount) VALUES (?,?,?)",
        (uid, coin, amount)
    )
    db.commit()
    db.close()
    await update.message.reply_text(f"✅ Portfolio updated: {amount} {coin.upper()}")

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACK HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    uid  = q.from_user.id
    tier = user_tier(uid)
    await q.answer()

    if data == "prices":
        coins = PREMIUM_COINS if tier in ("premium", "premium_plus") else FREE_COINS
        await q.message.reply_text("⏳ Fetching live prices…")
        prices = await get_prices(coins)
        if not prices:
            await q.message.reply_text("⚠️ Price data unavailable right now.")
            return
        lines = []
        for cid, vals in prices.items():
            price  = vals.get("usd", 0)
            change = vals.get("usd_24h_change", 0)
            arrow  = "🟢" if change >= 0 else "🔴"
            lines.append(f"{arrow} *{cid.upper()}*: ${price:,.4f} ({change:+.2f}%)")
        await q.message.reply_text(
            "📈 *Live Crypto Prices*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )

    elif data == "fear":
        fg = await get_fear_greed()
        await q.message.reply_text(f"😱 *Fear & Greed Index*\n\n{fg}", parse_mode="Markdown")

    elif data == "trending":
        coins = await get_trending_coins()
        if coins:
            msg = "🔥 *Trending Coins Right Now*\n\n" + "\n".join(f"• {c}" for c in coins)
        else:
            msg = "⚠️ Trending data unavailable."
        await q.message.reply_text(msg, parse_mode="Markdown")

    elif data == "whales":
        if tier == "free":
            await q.message.reply_text("⭐ Whale alerts require Premium. Upgrade to unlock!")
            return
        min_usd = WHALE_PREMIUM_PLUS if tier == "premium_plus" else WHALE_PREMIUM
        txs = await get_whale_alerts(min_usd)
        if not txs:
            await q.message.reply_text("🐋 No recent whale transactions found.")
            return
        lines = []
        for tx in txs[:5]:
            amt  = tx.get("amount_usd", 0)
            sym  = tx.get("symbol", "?").upper()
            from_ = tx.get("from", {}).get("owner", "unknown")
            to_   = tx.get("to",   {}).get("owner", "unknown")
            lines.append(f"🐋 ${amt:,.0f} {sym}\n   {from_} → {to_}")
        await q.message.reply_text(
            f"🐋 *Whale Alerts (>${min_usd:,})*\n\n" + "\n\n".join(lines),
            parse_mode="Markdown"
        )

    elif data == "portfolio":
        if tier != "premium_plus":
            await q.message.reply_text("💎 Portfolio requires Premium+.")
            return
        db = get_db()
        holdings = db.execute("SELECT coin_id, amount FROM portfolios WHERE user_id=?", (uid,)).fetchall()
        db.close()
        if not holdings:
            await q.message.reply_text(
                "💼 No portfolio yet.\nAdd holdings with /portfolio <coin> <amount>\nExample: /portfolio bitcoin 0.5"
            )
            return
        coin_ids = [h["coin_id"] for h in holdings]
        prices   = await get_prices(coin_ids)
        total_usd = 0.0
        lines = []
        for h in holdings:
            cid   = h["coin_id"]
            amt   = h["amount"]
            price = prices.get(cid, {}).get("usd", 0)
            val   = amt * price
            total_usd += val
            lines.append(f"• {amt} {cid.upper()} = ${val:,.2f}")
        await q.message.reply_text(
            "💼 *Your Portfolio*\n\n" + "\n".join(lines) +
            f"\n\n💰 Total: *${total_usd:,.2f}*",
            parse_mode="Markdown"
        )

    elif data == "upgrade":
        await q.message.reply_text("Choose your plan:", reply_markup=upgrade_keyboard())

    elif data == "upgrade_plus":
        await q.message.reply_text("Upgrade to Premium+:", reply_markup=upgrade_keyboard())

    elif data == "buy_premium":
        await ctx.bot.send_invoice(
            chat_id=uid,
            title="Crypto Signals Premium",
            description="All coins, 2% move alerts, whale alerts $1M+ — 30 days",
            payload="premium_30",
            currency="XTR",
            prices=[LabeledPrice("Premium 30 days", PREMIUM_STARS)],
        )

    elif data == "buy_premium_plus":
        await ctx.bot.send_invoice(
            chat_id=uid,
            title="Crypto Signals Premium+",
            description="0.5% alerts, whale $100K+, portfolio, AI analysis — 30 days",
            payload="premium_plus_30",
            currency="XTR",
            prices=[LabeledPrice("Premium+ 30 days", PREMIUM_PLUS_STARS)],
        )

    elif data == "referral":
        db = get_db()
        row = db.execute("SELECT referral_code FROM users WHERE user_id=?", (uid,)).fetchone()
        db.close()
        code = row["referral_code"] if row else make_referral_code(uid)
        bot_info = await ctx.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{code}"
        await q.message.reply_text(
            f"👥 *Your Referral Link*\n\n`{link}`\n\n"
            f"• They get 3 free Premium days\n"
            f"• You get 7 free Premium days",
            parse_mode="Markdown"
        )

    elif data == "account":
        db = get_db()
        row  = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        refs = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        db.close()
        until = row["tier_until"][:10] if row and row["tier_until"] else "—"
        await q.message.reply_text(
            f"⚙️ *My Account*\n\n"
            f"Tier: {'💎 Premium+' if tier=='premium_plus' else '⭐ Premium' if tier=='premium' else '🆓 Free'}\n"
            f"Active until: {until}\n"
            f"Referrals: {refs}",
            parse_mode="Markdown"
        )

    elif data == "back":
        await q.message.reply_text("Main menu:", reply_markup=main_menu(tier))

async def on_precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def on_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    stars   = update.message.successful_payment.total_amount

    if payload == "premium_30":
        grant_premium(uid, "premium", 30)
        tier_name = "Premium"
    else:
        grant_premium(uid, "premium_plus", 30)
        tier_name = "Premium+"

    db = get_db()
    db.execute("UPDATE users SET stars_spent=stars_spent+? WHERE user_id=?", (stars, uid))
    db.commit()
    db.close()

    await update.message.reply_text(
        f"🎉 *{tier_name} activated for 30 days!*\n\nThank you for your support!",
        parse_mode="Markdown",
        reply_markup=main_menu(user_tier(uid))
    )

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────────────────────────────────────

async def send_channel_post(app: Application):
    """Auto-post to public crypto channel every 4 hours."""
    if not CHANNEL_USERNAME:
        return
    all_prices = await get_prices(FREE_COINS)
    fear       = await get_fear_greed()
    trending   = await get_trending_coins()

    lines = []
    for cid, vals in all_prices.items():
        price  = vals.get("usd", 0)
        change = vals.get("usd_24h_change", 0)
        arrow  = "🟢" if change >= 0 else "🔴"
        lines.append(f"{arrow} *{cid.upper()}*: ${price:,.4f} ({change:+.2f}%)")

    trend_str = ", ".join(trending[:3]) if trending else "—"
    msg = (
        "📈 *Crypto Market Update*\n\n"
        f"😱 Fear & Greed: {fear}\n"
        f"🔥 Trending: {trend_str}\n\n"
        + "\n".join(lines) +
        f"\n\n👉 Get real-time alerts: @{CHANNEL_USERNAME.lstrip('@')}"
    )
    try:
        await app.bot.send_message(CHANNEL_USERNAME, msg, parse_mode="Markdown")
    except Exception as e:
        log.warning(f"Crypto channel post failed: {e}")

async def send_morning_digest(app: Application):
    """Daily crypto digest at 7 AM UTC."""
    log.info("Running crypto morning digest…")
    db = get_db()
    users = db.execute("SELECT user_id FROM users").fetchall()
    db.close()

    fear = await get_fear_greed()
    trending = await get_trending_coins()
    all_prices = await get_prices(PREMIUM_COINS)

    free_lines = []
    for cid in FREE_COINS:
        vals = all_prices.get(cid, {})
        price  = vals.get("usd", 0)
        change = vals.get("usd_24h_change", 0)
        arrow  = "🟢" if change >= 0 else "🔴"
        free_lines.append(f"{arrow} {cid.upper()}: ${price:,.4f} ({change:+.2f}%)")

    full_lines = []
    for cid, vals in all_prices.items():
        price  = vals.get("usd", 0)
        change = vals.get("usd_24h_change", 0)
        arrow  = "🟢" if change >= 0 else "🔴"
        full_lines.append(f"{arrow} {cid.upper()}: ${price:,.4f} ({change:+.2f}%)")

    for u in users:
        uid  = u["user_id"]
        tier = user_tier(uid)
        lines = full_lines if tier in ("premium", "premium_plus") else free_lines
        trend_str = ", ".join(trending[:3]) if trending else "—"
        msg = (
            f"☀️ *Daily Crypto Digest*\n\n"
            f"😱 Fear & Greed: {fear}\n"
            f"🔥 Trending: {trend_str}\n\n"
            + "\n".join(lines)
        )
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Digest failed for {uid}: {e}")
        await asyncio.sleep(0.3)

async def check_price_alerts(app: Application):
    """Check for significant price moves and alert users."""
    all_prices = await get_prices(PREMIUM_COINS)
    if not all_prices:
        return

    db = get_db()
    users = db.execute("SELECT user_id FROM users").fetchall()
    db.close()

    for u in users:
        uid  = u["user_id"]
        tier = user_tier(uid)

        threshold = ALERT_PREMIUM_PLUS if tier == "premium_plus" else \
                    ALERT_PREMIUM if tier == "premium" else ALERT_FREE

        coins = PREMIUM_COINS if tier in ("premium", "premium_plus") else FREE_COINS
        alerts = []
        for cid in coins:
            vals = all_prices.get(cid, {})
            change = abs(vals.get("usd_24h_change", 0))
            if change >= threshold:
                price  = vals.get("usd", 0)
                change_raw = vals.get("usd_24h_change", 0)
                arrow  = "🟢" if change_raw >= 0 else "🔴"
                alerts.append(f"{arrow} *{cid.upper()}* moved {change_raw:+.2f}% → ${price:,.4f}")

        if alerts:
            msg = f"🚨 *Price Alert* (>{threshold}% move)\n\n" + "\n".join(alerts)
            try:
                await app.bot.send_message(uid, msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Alert failed for {uid}: {e}")
        await asyncio.sleep(0.3)

async def check_whale_alerts(app: Application):
    """Send whale alerts to premium users."""
    db = get_db()
    premium_users = db.execute(
        "SELECT user_id, tier FROM users WHERE tier IN ('premium','premium_plus')"
    ).fetchall()
    db.close()

    if not premium_users:
        return

    # Fetch for both thresholds
    plus_txs    = await get_whale_alerts(WHALE_PREMIUM_PLUS)
    premium_txs = await get_whale_alerts(WHALE_PREMIUM)

    for u in premium_users:
        uid  = u["user_id"]
        tier = u["tier"]
        txs  = plus_txs if tier == "premium_plus" else premium_txs
        if not txs:
            continue
        lines = []
        for tx in txs[:3]:
            amt  = tx.get("amount_usd", 0)
            sym  = tx.get("symbol", "?").upper()
            lines.append(f"🐋 ${amt:,.0f} {sym}")
        msg = "🐋 *Whale Alert*\n\n" + "\n".join(lines)
        try:
            await app.bot.send_message(uid, msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Whale alert failed for {uid}: {e}")
        await asyncio.sleep(0.3)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# FLASK STATS API
# ─────────────────────────────────────────────────────────────────────────────

flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route("/api/stats")
def api_stats():
    db = get_db()
    total   = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = db.execute("SELECT COUNT(*) FROM users WHERE tier='premium'").fetchone()[0]
    plus    = db.execute("SELECT COUNT(*) FROM users WHERE tier='premium_plus'").fetchone()[0]
    stars   = db.execute("SELECT COALESCE(SUM(stars_spent),0) FROM users").fetchone()[0]
    refs    = db.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
    today   = db.execute(
        "SELECT COUNT(*) FROM users WHERE date(joined_at)=date('now')"
    ).fetchone()[0]
    db.close()
    return jsonify({
        "bot": "Crypto Signals Pro",
        "total_users": total,
        "free_users": total - premium - plus,
        "premium_users": premium,
        "premium_plus_users": plus,
        "stars_earned": stars,
        "usd_earned": round(stars * 0.0125, 2),
        "referrals": refs,
        "new_today": today,
        "updated_at": datetime.utcnow().isoformat()
    })

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "crypto-signals"})

def run_flask():
    port = int(os.environ.get("PORT", 8081))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

def main():
    init_db()
    log.info("🚀 Crypto Signals Bot starting…")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_payment))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_morning_digest, "cron",     hour=7,  minute=0,  args=[app])
    scheduler.add_job(check_price_alerts,  "interval", minutes=30,         args=[app])
    scheduler.add_job(check_whale_alerts,  "interval", hours=1,            args=[app])
    scheduler.add_job(send_channel_post,   "interval", hours=4,            args=[app])
    scheduler.start()

    # Start Flask stats API in background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    log.info("✅ Crypto bot running. Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message", "callback_query", "pre_checkout_query"])

if __name__ == "__main__":
    main()
