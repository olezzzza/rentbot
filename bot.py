import os
import json
import asyncio
import re
import urllib.request
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
DUBLIN_TZ       = ZoneInfo("Europe/Dublin")
SEEN_FILE       = "seen_listings.json"

# ── Параметры поиска ──────────────────────────────────────────────
SEARCH_URL = (
    "https://www.daft.ie/property-for-rent/gorey-co-wexford"
    "?numBeds_from=3&rentalPrice_to=1200&sort=publishDateDesc"
)
API_URL = "https://gateway.daft.ie/old/v1/listings/residential-for-rent/"
CHECK_INTERVAL_MINUTES = 30


# ═══════════════════════════════════════════════════════════════════
# ХРАНИЛИЩЕ ВИДЕННЫХ ОБЪЯВЛЕНИЙ
# ═══════════════════════════════════════════════════════════════════

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


# ═══════════════════════════════════════════════════════════════════
# ПАРСИНГ DAFT.IE
# ═══════════════════════════════════════════════════════════════════

def fetch_listings() -> list:
    """Получает объявления через внутренний API daft.ie."""
    body = json.dumps({
        "section":        "residential-for-rent",
        "filters": [
            {"name": "adState",          "values": ["published"]},
            {"name": "rentalPrice_to",   "values": ["1200"]},
            {"name": "numBedrooms_from", "values": ["3"]},
        ],
        "andFilters": [],
        "ranges": [],
        "geoFilter": {
            "storedShapeIds": ["1085"],   # Gorey, Co. Wexford
            "geoSearchType":  "STORED_SHAPE"
        },
        "sort":     "publishDateDesc",
        "from":     0,
        "pageSize": 20,
    }).encode("utf-8")

    headers = {
        "Content-Type":  "application/json",
        "brand":         "daft",
        "platform":      "web",
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin":        "https://www.daft.ie",
        "Referer":       "https://www.daft.ie/",
    }

    req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    listings = []
    for item in data.get("listings", []):
        listing = item.get("listing", item)
        parsed  = _parse_listing(listing)
        if parsed:
            listings.append(parsed)

    return listings


def _parse_listing(item: dict) -> dict | None:
    """Парсит один листинг из JSON API daft.ie."""
    try:
        listing_id = str(item.get("id", ""))
        if not listing_id:
            return None

        price = item.get("price", "") or item.get("rent", "")
        if isinstance(price, dict):
            price = price.get("value", "") or price.get("display", "")

        title   = item.get("title",          "") or item.get("header",          "")
        address = item.get("address",         "") or item.get("displayAddress",  "")
        beds    = item.get("numBedrooms",     "") or item.get("bedrooms",        "")
        baths   = item.get("numBathrooms",    "") or item.get("bathrooms",       "")
        url     = item.get("daftShortcode",   "") or item.get("url",             "")
        if url and not url.startswith("http"):
            url = f"https://www.daft.ie{url}"

        return {
            "id":      listing_id,
            "title":   title or address,
            "address": address,
            "price":   str(price),
            "beds":    str(beds),
            "baths":   str(baths),
            "url":     url,
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# ПРОВЕРКА НОВЫХ ОБЪЯВЛЕНИЙ
# ═══════════════════════════════════════════════════════════════════

def check_new_listings() -> list:
    """Возвращает только новые (ещё не виденные) объявления."""
    seen     = load_seen()
    all_l    = fetch_listings()
    new_ones = []

    for l in all_l:
        if l["id"] not in seen:
            new_ones.append(l)
            seen.add(l["id"])

    if new_ones:
        save_seen(seen)

    return new_ones


def format_listing(l: dict) -> str:
    lines = []
    if l.get("title"):
        lines.append(f"🏠 *{l['title']}*")
    if l.get("address") and l["address"] != l.get("title"):
        lines.append(f"📍 {l['address']}")
    if l.get("price"):
        lines.append(f"💶 {l['price']}")
    if l.get("beds"):
        beds_str = f"🛏 {l['beds']} спален"
        if l.get("baths"):
            beds_str += f"  🚿 {l['baths']} ванных"
        lines.append(beds_str)
    if l.get("url"):
        lines.append(f"🔗 {l['url']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 Бот мониторинга аренды на daft.ie\n\n"
        "Параметры поиска:\n"
        "📍 Город: Gorey, Co. Wexford\n"
        "🛏 Спален: от 3\n"
        "💶 Цена: до €1200/мес\n\n"
        "Проверяю каждые 30 минут.\n"
        "Новые объявления пришлю сразу!\n\n"
        "/check — проверить прямо сейчас\n"
        "/reset — сбросить историю (пришлёт все текущие)\n"
        "/params — текущие параметры поиска"
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("🔍 Проверяю daft.ie...")
    try:
        new_listings = check_new_listings()
        if new_listings:
            await update.message.reply_text(
                f"🎉 Найдено новых объявлений: {len(new_listings)}"
            )
            for l in new_listings:
                await update.message.reply_text(format_listing(l), parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "😕 Новых объявлений нет.\n"
                f"Слежу за: Gorey, 3+ спальни, до €1200"
            )
    except Exception as e:
        await update.message.reply_text(f"Ошибка при проверке: {e}")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
    await update.message.reply_text(
        "♻️ История сброшена. Следующая проверка пришлёт все текущие объявления."
    )


async def params_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ Параметры поиска:\n\n"
        f"📍 Город: Gorey, Co. Wexford\n"
        f"🛏 Спален: от 3\n"
        f"💶 Цена: до €1200/мес\n"
        f"🔄 Проверка: каждые {CHECK_INTERVAL_MINUTES} минут\n\n"
        f"🔗 {SEARCH_URL}"
    )


# ═══════════════════════════════════════════════════════════════════
# ФОНОВАЯ ЗАДАЧА — МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════════════

async def monitor_task(app: Application):
    """Каждые 30 минут проверяет новые объявления."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
        if not ALLOWED_USER_ID:
            continue
        try:
            new_listings = check_new_listings()
            if new_listings:
                await app.bot.send_message(
                    chat_id=ALLOWED_USER_ID,
                    text=f"🏠 Новые объявления на daft.ie: {len(new_listings)}"
                )
                for l in new_listings:
                    await app.bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text=format_listing(l),
                        parse_mode="Markdown"
                    )
        except Exception as e:
            print(f"Ошибка мониторинга: {e}")


# ═══════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("check",  check_cmd))
    app.add_handler(CommandHandler("reset",  reset_cmd))
    app.add_handler(CommandHandler("params", params_cmd))

    async def post_init(app: Application):
        asyncio.create_task(monitor_task(app))

    app.post_init = post_init

    print("Бот аренды запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
