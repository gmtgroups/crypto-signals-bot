"""
╔══════════════════════════════════════════════════════╗
║         CRYPTO SIGNALS BOT — Full Production         ║
║   Price alerts · trending tokens · whale moves       ║
║   Free: top 5 coins | Premium: full signals suite    ║
╚══════════════════════════════════════════════════════╝

SETUP (one-time, ~20 min):
1. Message @BotFather on Telegram → /newbot → copy TOKEN
2. Sign up at railway.app (free tier)
3. Set environment variables (see .env.example)
4. Deploy — bot runs forever, fully automated.

DATA SOURCES (all free APIs):
- CoinGecko: prices, market cap, trending
- CryptoCompare: news feed
- Alternative.me: Fear & Greed Index
- Whale Alert public API (optional, needs free key)
"""

import os
import asyncio
import logging
import sqlite3
import httpx
from datetime import datetime, timedelta
from typing import Optional
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, MessageHandler, filters,
    ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────
BOT_TOKEN            = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WHALE_ALERT_API_KEY  = os.getenv("WHALE_ALERT_KEY", "")       # Free at whale-alert.io
ADMIN_IDS            = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",")]

PREMIUM_PRICE_STARS  = 500           # ~$6.25 USD / month
PREMIUM_DAYS         = 30

DB_PATH = "crypto_signals.db"

# ── API Endpoints ─────────────────────────────────────
COINGECKO_BASE    = "https://api.coingecko.com/api/v3"
FEAR_GREED_API    = "https://api.alternative.me/fng/"
CRYPTOCOMPARE_NEWS= "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&categories=BTC,ETH,Altcoin,Trading"
WHALE_ALERT_API   = "https://api.whale-alert.io/v1/transactions"

# Top coins to track
TOP_COINS = [
    "bitcoin", "ethereum", "solana", "binancecoin",
    "ripple", "cardano", "avalanche-2", "polkadot",
    "chainlink", "dogecoin", "shiba-inu", "pepe"
]

# Alert thresholds
PRICE_CHANGE_THRESHOLD_FREE    = 5.0   # % — free users get alerted at 5%
PRICE_CHANGE_THRESHOLD_PREMIUM = 2.0   # % — premium users get alerted at 2%
WHALE_THRESHOLD_USD            = 1_000_000  # $1M+ transactions

# ── Database ──────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT,
            first_name      TEXT,
            is_premium      INTEGER DEFAULT 0,
            premium_until   TEXT,
            watchlist       TEXT DEFAULT '',
            alert_threshold REAL DEFAULT 5.0,
            joined_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS price_cache (
            coin_id     TEXT PRIMARY KEY,
            price_usd   REAL,
            change_24h  REAL,
            market_cap  REAL,
            volume_24h  REAL,
            updated_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            coin_id     TEXT,
            direction   TEXT,
            sent_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            amount      INTEGER,
            currency    TEXT,
            paid_at     TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def upsert_user(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user_id, username, first_name))
    conn.commit()
    conn.close()

def set_premium(user_id: int, days: int = PREMIUM_DAYS):
    until = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_premium=1, premium_until=?, alert_threshold=? WHERE user_id=?",
              (until, PRICE_CHANGE_THRESHOLD_PREMIUM, user_id))
    conn.commit()
    conn.close()

def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user["is_premium"]:
        return False
    if user["premium_until"]:
        return datetime.fromisoformat(user["premium_until"]) > datetime.now()
    return False

def get_watchlist(user_id: int) -> list[str]:
    user = get_user(user_id)
    if not user or not user["watchlist"]:
        return TOP_COINS[:5]
    return [c for c in user["watchlist"].split(",") if c]

def set_watchlist(user_id: int, coins: list[str]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET watchlist=? WHERE user_id=?", (",".join(coins), user_id))
    conn.commit()
    conn.close()

def get_all_users() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("SELECT user_id, is_premium, alert_threshold FROM users").fetchall()
    conn.close()
    return rows

def log_payment(user_id: int, amount: int, currency: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, amount, currency) VALUES (?,?,?)", (user_id, amount, currency))
    conn.commit()
    conn.close()

def alert_already_sent(user_id: int, coin_id: str, direction: str) -> bool:
    """Prevent duplicate alerts within 1 hour."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
    row = c.execute("""
        SELECT 1 FROM alerts_sent
        WHERE user_id=? AND coin_id=? AND direction=? AND sent_at > ?
    """, (user_id, coin_id, direction, cutoff)).fetchone()
    conn.close()
    return row is not None

def log_alert(user_id: int, coin_id: str, direction: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO alerts_sent (user_id, coin_id, direction) VALUES (?,?,?)",
              (user_id, coin_id, direction))
    conn.commit()
    conn.close()

# ── Data Fetchers ─────────────────────────────────────
async def fetch_prices(coin_ids: list[str]) -> dict:
    """Fetch prices from CoinGecko (free API)."""
    ids = ",".join(coin_ids)
    url = (
        f"{COINGECKO_BASE}/coins/markets"
        f"?vs_currency=usd&ids={ids}"
        f"&order=market_cap_desc&per_page=50&page=1"
        f"&sparkline=false&price_change_percentage=1h,24h,7d"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()
            result = {}
            for coin in data:
                result[coin["id"]] = {
                    "name":       coin["name"],
                    "symbol":     coin["symbol"].upper(),
                    "price":      coin["current_price"],
                    "change_1h":  coin.get("price_change_percentage_1h_in_currency", 0) or 0,
                    "change_24h": coin.get("price_change_percentage_24h", 0) or 0,
                    "change_7d":  coin.get("price_change_percentage_7d_in_currency", 0) or 0,
                    "market_cap": coin.get("market_cap", 0) or 0,
                    "volume":     coin.get("total_volume", 0) or 0,
                    "ath":        coin.get("ath", 0) or 0,
                    "ath_change": coin.get("ath_change_percentage", 0) or 0,
                }
            return result
    except Exception as e:
        log.error(f"CoinGecko error: {e}")
        return {}

async def fetch_fear_greed() -> dict:
    """Fetch Fear & Greed Index."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(FEAR_GREED_API)
            data = resp.json()
            fg = data["data"][0]
            return {
                "value":       int(fg["value"]),
                "label":       fg["value_classification"],
                "timestamp":   fg["timestamp"],
            }
    except Exception as e:
        log.error(f"Fear/Greed error: {e}")
        return {"value": 50, "label": "Neutral", "timestamp": ""}

async def fetch_trending() -> list[dict]:
    """Fetch trending coins from CoinGecko."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{COINGECKO_BASE}/search/trending")
            data = resp.json()
            return [
                {
                    "name":   c["item"]["name"],
                    "symbol": c["item"]["symbol"],
                    "rank":   c["item"]["market_cap_rank"],
                }
                for c in data.get("coins", [])[:7]
            ]
    except Exception as e:
        log.error(f"Trending error: {e}")
        return []

async def fetch_crypto_news() -> list[dict]:
    """Fetch latest crypto news."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(CRYPTOCOMPARE_NEWS)
            data = resp.json()
            articles = []
            for item in data.get("Data", [])[:8]:
                articles.append({
                    "title":  item["title"],
                    "url":    item["url"],
                    "source": item["source"],
                    "body":   item["body"][:200],
                })
            return articles
    except Exception as e:
        log.error(f"Crypto news error: {e}")
        return []

# ── Message Builders ──────────────────────────────────
def emoji_for_change(pct: float) -> str:
    if pct >= 10:  return "🚀"
    if pct >= 5:   return "📈"
    if pct >= 0:   return "🟢"
    if pct >= -5:  return "🔴"
    if pct >= -10: return "📉"
    return "💀"

def format_price(price: float) -> str:
    if price >= 1000: return f"${price:,.2f}"
    if price >= 1:    return f"${price:.4f}"
    return f"${price:.8f}"

def format_large(n: float) -> str:
    if n >= 1e9:  return f"${n/1e9:.2f}B"
    if n >= 1e6:  return f"${n/1e6:.2f}M"
    return f"${n:,.0f}"

def build_price_card(coin_id: str, data: dict) -> str:
    e = emoji_for_change(data["change_24h"])
    return (
        f"{e} *{data['name']} ({data['symbol']})*\n"
        f"💵 Price: {format_price(data['price'])}\n"
        f"⏱ 1h: {data['change_1h']:+.2f}%  "
        f"📅 24h: {data['change_24h']:+.2f}%  "
        f"📆 7d: {data['change_7d']:+.2f}%\n"
        f"📊 Vol: {format_large(data['volume'])}  "
        f"Mktcap: {format_large(data['market_cap'])}\n"
    )

def build_fear_greed_bar(value: int) -> str:
    filled = round(value / 10)
    bar = "█" * filled + "░" * (10 - filled)
    if value >= 75:   label = "🤑 Extreme Greed"
    elif value >= 55: label = "😊 Greed"
    elif value >= 45: label = "😐 Neutral"
    elif value >= 25: label = "😨 Fear"
    else:             label = "😱 Extreme Fear"
    return f"[{bar}] {value}/100 — {label}"

# ── Keyboards ─────────────────────────────────────────
def premium_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="buy_premium")
    ]])

def main_menu_keyboard(premium: bool = False):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Price Check",     callback_data="prices"),
         InlineKeyboardButton("🔥 Trending",        callback_data="trending")],
        [InlineKeyboardButton("😱 Fear & Greed",    callback_data="fear_greed"),
         InlineKeyboardButton("📰 Crypto News",     callback_data="news")],
        [InlineKeyboardButton("🔔 My Alerts",       callback_data="my_alerts"),
         InlineKeyboardButton("📋 My Watchlist",    callback_data="watchlist")],
        [] if premium else [InlineKeyboardButton("⭐ Premium — 500 Stars/mo", callback_data="buy_premium")],
    ])

# ── Command Handlers ──────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username or "", user.first_name or "")
    premium = is_premium(user.id)

    fg = await fetch_fear_greed()
    bar = build_fear_greed_bar(fg["value"])

    welcome = (
        f"🚨 *Welcome, {user.first_name}!*\n\n"
        f"I'm your *Crypto Signals Bot* — I track prices, whale moves, trending tokens and market sentiment 24/7.\n\n"
        f"📊 *Market Mood Right Now:*\n{bar}\n\n"
        f"🆓 *Free:* Top 5 coins · Daily digest · 5% move alerts\n"
        f"⭐ *Premium (500 Stars/mo):* All coins · Whale alerts · 2% alerts · Real-time signals\n\n"
        f"What do you want to check?"
    )
    await update.message.reply_text(
        welcome, parse_mode="Markdown",
        reply_markup=main_menu_keyboard(premium)
    )

async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Get prices for watchlist coins."""
    user_id = update.effective_user.id
    premium = is_premium(user_id)
    watchlist = get_watchlist(user_id) if premium else TOP_COINS[:5]

    await update.message.reply_text("⏳ Fetching live prices...")
    prices = await fetch_prices(watchlist)

    if not prices:
        await update.message.reply_text("⚠️ Price data unavailable. Try again shortly.")
        return

    msg = f"📊 *Live Prices — {datetime.utcnow().strftime('%H:%M UTC')}*\n\n"
    for coin_id, data in prices.items():
        msg += build_price_card(coin_id, data) + "\n"

    if not premium:
        msg += "\n⭐ *Premium:* Track all coins + get 2% move alerts!"

    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=None if premium else premium_keyboard()
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    premium = is_premium(user_id)
    until = (user or {}).get("premium_until", "N/A")

    await update.message.reply_text(
        f"📋 *Your Status*\n\n"
        f"🏷 Tier: {'⭐ Premium' if premium else '🆓 Free'}\n"
        + (f"📅 Until: {until[:10]}\n" if premium and until else "")
        + f"🔔 Alert threshold: {PRICE_CHANGE_THRESHOLD_PREMIUM if premium else PRICE_CHANGE_THRESHOLD_FREE}%\n"
        f"📋 Watchlist: {len(get_watchlist(user_id))} coins",
        parse_mode="Markdown",
        reply_markup=None if premium else premium_keyboard()
    )

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    total   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = c.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
    revenue = c.execute("SELECT SUM(amount) FROM payments").fetchone()[0] or 0
    alerts  = c.execute("SELECT COUNT(*) FROM alerts_sent WHERE sent_at > datetime('now','-24 hours')").fetchone()[0]
    conn.close()

    await update.message.reply_text(
        f"📊 *Admin Dashboard*\n\n"
        f"👥 Total users: {total}\n"
        f"⭐ Premium: {premium}\n"
        f"💰 Stars earned: {revenue}\n"
        f"🔔 Alerts sent (24h): {alerts}\n"
        f"📈 Conversion: {round(premium/max(total,1)*100,1)}%",
        parse_mode="Markdown"
    )

# ── Callback Handlers ─────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    premium = is_premium(user_id)
    data = query.data

    if data == "prices":
        watchlist = get_watchlist(user_id) if premium else TOP_COINS[:5]
        await query.message.reply_text("⏳ Fetching prices...")
        prices = await fetch_prices(watchlist)
        msg = f"📊 *Live Prices — {datetime.utcnow().strftime('%H:%M UTC')}*\n\n"
        for _, coin_data in prices.items():
            msg += build_price_card(_, coin_data) + "\n"
        if not premium:
            msg += "\n⭐ *Premium:* All coins + 2% move alerts!"
        await query.message.reply_text(msg, parse_mode="Markdown",
                                       reply_markup=None if premium else premium_keyboard())

    elif data == "trending":
        trending = await fetch_trending()
        if not trending:
            await query.message.reply_text("⚠️ Trending data unavailable.")
            return
        msg = "🔥 *Trending Coins Right Now*\n\n"
        for i, c in enumerate(trending, 1):
            rank = f"#{c['rank']}" if c['rank'] else "unranked"
            msg += f"{i}. *{c['name']}* ({c['symbol']}) — Market cap rank {rank}\n"
        if not premium:
            msg += "\n⭐ Premium: Set alerts on trending coins instantly!"
        await query.message.reply_text(msg, parse_mode="Markdown",
                                       reply_markup=None if premium else premium_keyboard())

    elif data == "fear_greed":
        fg = await fetch_fear_greed()
        bar = build_fear_greed_bar(fg["value"])
        interpretation = {
            range(0, 25):   "🔑 *Buying opportunity?* Extreme fear often precedes reversals.",
            range(25, 45):  "😨 Market is fearful. Proceed cautiously.",
            range(45, 55):  "😐 Neutral sentiment. Watch for a directional move.",
            range(55, 75):  "😊 Greed building. Consider taking some profits.",
            range(75, 101): "⚠️ *Extreme greed.* Historically a correction risk zone.",
        }
        tip = next((v for k, v in interpretation.items() if fg["value"] in k), "")
        msg = (
            f"😱 *Fear & Greed Index*\n\n"
            f"{bar}\n\n"
            f"{tip}\n\n"
            f"_Updated daily — reflects market sentiment from volatility, momentum, social media, surveys, and dominance._"
        )
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "news":
        articles = await fetch_crypto_news()
        if not articles:
            await query.message.reply_text("⚠️ News unavailable right now.")
            return
        limit = len(articles) if premium else 3
        msg = "📰 *Latest Crypto News*\n\n"
        for art in articles[:limit]:
            msg += f"• [{art['title']}]({art['url']}) — _{art['source']}_\n\n"
        if not premium:
            msg += "⭐ *Premium:* Full news feed + real-time breaking alerts!"
        await query.message.reply_text(msg, parse_mode="Markdown",
                                       disable_web_page_preview=True,
                                       reply_markup=None if premium else premium_keyboard())

    elif data == "my_alerts":
        threshold = PRICE_CHANGE_THRESHOLD_PREMIUM if premium else PRICE_CHANGE_THRESHOLD_FREE
        msg = (
            f"🔔 *Your Alert Settings*\n\n"
            f"Trigger: price moves ≥ {threshold}% in 1 hour\n"
            f"Coins watched: {len(get_watchlist(user_id))}\n\n"
            + ("✅ Alerts are active and running 24/7." if premium
               else "⭐ Upgrade to Premium for 2% alerts on all coins!")
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
                                       reply_markup=None if premium else premium_keyboard())

    elif data == "watchlist":
        coins = get_watchlist(user_id)
        msg = (
            f"📋 *Your Watchlist*\n\n"
            + "\n".join(f"• {c}" for c in coins)
            + ("\n\n⭐ *Premium:* Customise with any coin!" if not premium else
               "\n\n_Reply /watch <coin_id> to add a coin (e.g. /watch pepe)_")
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
                                       reply_markup=None if premium else premium_keyboard())

    elif data == "about_premium":
        await query.message.reply_text(
            "⭐ *Premium Features*\n\n"
            "✅ 2% price move alerts (vs 5% free)\n"
            "✅ All coins tracked (vs top 5)\n"
            "✅ Whale transaction alerts ($1M+)\n"
            "✅ Real-time signals every hour\n"
            "✅ Full crypto news feed\n"
            "✅ Custom watchlist\n"
            "✅ Fear & Greed history\n\n"
            "*Only 500 Telegram Stars/month* (~$6.25)",
            parse_mode="Markdown",
            reply_markup=premium_keyboard()
        )

    elif data == "buy_premium":
        await ctx.bot.send_invoice(
            chat_id=user_id,
            title="⭐ Crypto Signals Premium — 30 Days",
            description="2% move alerts · whale tracking · all coins · real-time signals",
            payload="crypto_premium_30d",
            currency="XTR",
            prices=[LabeledPrice("Premium 30 days", PREMIUM_PRICE_STARS)],
            provider_token="",
        )

    elif data == "status":
        await cmd_status(update, ctx)

# ── Watchlist Command ─────────────────────────────────
async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text(
            "⭐ Custom watchlists are a Premium feature.",
            reply_markup=premium_keyboard()
        )
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /watch <coin_id>\nExample: /watch pepe")
        return
    coin = ctx.args[0].lower()
    # Validate coin exists
    prices = await fetch_prices([coin])
    if not prices:
        await update.message.reply_text(f"❌ Coin '{coin}' not found on CoinGecko. Check the ID.")
        return
    watchlist = get_watchlist(user_id)
    if coin not in watchlist:
        watchlist.append(coin)
        set_watchlist(user_id, watchlist)
    await update.message.reply_text(f"✅ Added *{coin}* to your watchlist!", parse_mode="Markdown")

# ── Payment Handlers ──────────────────────────────────
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    log_payment(user_id, payment.total_amount, payment.currency)
    set_premium(user_id, PREMIUM_DAYS)
    await update.message.reply_text(
        "🎉 *Premium Activated!*\n\n"
        "✅ 2% move alerts — live\n"
        "✅ Whale alerts — live\n"
        "✅ All coins tracked\n"
        "✅ Real-time signals\n\n"
        "You'll start receiving alerts immediately. 🚀",
        parse_mode="Markdown"
    )

# ── Scheduled Jobs ────────────────────────────────────
async def check_price_alerts(app: Application):
    """Runs every 30 min. Sends alerts when coins move significantly."""
    log.info("📡 Checking price alerts...")
    prices = await fetch_prices(TOP_COINS)
    if not prices:
        return

    users = get_all_users()
    for (user_id, user_premium, threshold) in users:
        watchlist = get_watchlist(user_id) if user_premium else TOP_COINS[:5]
        for coin_id in watchlist:
            data = prices.get(coin_id)
            if not data:
                continue
            change = data["change_24h"]
            direction = "up" if change > 0 else "down"

            if abs(change) >= (threshold or 5.0):
                if alert_already_sent(user_id, coin_id, direction):
                    continue
                e = "🚀" if change > 0 else "💀"
                msg = (
                    f"{e} *Price Alert!*\n\n"
                    f"*{data['name']} ({data['symbol']})* moved *{change:+.2f}%* in 24h\n"
                    f"Current price: {format_price(data['price'])}\n"
                    f"Volume: {format_large(data['volume'])}"
                )
                try:
                    await app.bot.send_message(user_id, msg, parse_mode="Markdown")
                    log_alert(user_id, coin_id, direction)
                except Exception as e:
                    log.warning(f"Alert failed for {user_id}: {e}")
                await asyncio.sleep(0.3)

async def broadcast_daily_digest(app: Application):
    """Runs every morning at 7 AM UTC. Sends market digest."""
    log.info("📨 Broadcasting daily crypto digest...")
    prices  = await fetch_prices(TOP_COINS)
    fg      = await fetch_fear_greed()
    trending= await fetch_trending()
    news    = await fetch_crypto_news()

    if not prices:
        return

    users = get_all_users()
    for (user_id, user_premium, _) in users:
        try:
            # Header
            bar = build_fear_greed_bar(fg["value"])
            header = (
                f"☀️ *Good morning! Crypto Daily Digest*\n"
                f"_{datetime.utcnow().strftime('%B %d, %Y')} — UTC_\n\n"
                f"*Market Mood:* {bar}\n\n"
            )
            # Prices
            watchlist = get_watchlist(user_id) if user_premium else TOP_COINS[:5]
            price_section = "*📊 Prices:*\n"
            for coin_id in watchlist:
                if coin_id in prices:
                    price_section += build_price_card(coin_id, prices[coin_id])

            # Trending
            trend_section = ""
            if user_premium and trending:
                trend_section = "\n*🔥 Trending:*\n" + "\n".join(
                    f"• {c['name']} ({c['symbol']})" for c in trending[:3]
                )

            # News headlines
            news_limit = 5 if user_premium else 2
            news_section = "\n*📰 News:*\n" + "\n".join(
                f"• [{a['title'][:60]}...]({a['url']})" for a in news[:news_limit]
            )

            full_msg = header + price_section + trend_section + news_section

            await app.bot.send_message(
                user_id, full_msg,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            if not user_premium:
                await app.bot.send_message(
                    user_id,
                    "⭐ *Upgrade to Premium* for whale alerts, all coins & 2% move triggers!",
                    parse_mode="Markdown",
                    reply_markup=premium_keyboard()
                )
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning(f"Digest failed for {user_id}: {e}")

async def broadcast_whale_alerts(app: Application):
    """Runs every hour. Fetches whale transactions for premium users."""
    if not WHALE_ALERT_API_KEY:
        return
    try:
        since = int((datetime.now() - timedelta(hours=1)).timestamp())
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                WHALE_ALERT_API,
                params={
                    "api_key": WHALE_ALERT_API_KEY,
                    "min_value": WHALE_THRESHOLD_USD,
                    "start": since,
                }
            )
            data = resp.json()
            transactions = data.get("transactions", [])
    except Exception as e:
        log.error(f"Whale Alert error: {e}")
        return

    if not transactions:
        return

    users = get_all_users()
    for (user_id, user_premium, _) in users:
        if not user_premium:
            continue
        for tx in transactions[:5]:
            amount_usd = tx.get("amount_usd", 0)
            symbol = tx.get("symbol", "?").upper()
            from_owner = tx.get("from", {}).get("owner_type", "unknown")
            to_owner = tx.get("to", {}).get("owner_type", "unknown")
            msg = (
                f"🐋 *Whale Alert!*\n\n"
                f"*{format_large(amount_usd)}* of *{symbol}* moved\n"
                f"From: {from_owner} → To: {to_owner}\n"
                f"_This may indicate large institutional movement._"
            )
            try:
                await app.bot.send_message(user_id, msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Whale alert failed for {user_id}: {e}")
            await asyncio.sleep(0.3)

# ── Main ──────────────────────────────────────────────
def main():
    init_db()
    log.info("🚀 Crypto Signals Bot starting...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("price",  cmd_price))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watch",  cmd_watch))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: asyncio.create_task(broadcast_daily_digest(app)),
        "cron", hour=7, minute=0, id="daily_digest"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(check_price_alerts(app)),
        "interval", minutes=30, id="price_alerts"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(broadcast_whale_alerts(app)),
        "interval", hours=1, id="whale_alerts"
    )
    scheduler.start()

    log.info("✅ Crypto bot running. Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
