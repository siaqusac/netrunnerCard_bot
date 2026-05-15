#!/usr/bin/env python3
"""
Netrunner Card Bot for Telegram
Detects [[Card Name]] patterns in messages and replies with the card image and info.
Uses the NetrunnerDB v3 API: https://api.netrunnerdb.com
"""

import re
import logging
import urllib.parse
from typing import Optional

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ── Configuration ─────────────────────────────────────────────────────────────
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

NRDB_API_BASE = "https://api.netrunnerdb.com/api/v3/public"
CARD_IMAGE_BASE = "https://card-images.netrunnerdb.com/v2/large"
NRDB_CARD_URL = "https://netrunnerdb.com/en/card"

CARD_PATTERN = re.compile(r"\[\[(.+?)\]\]")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def clean_query(s: str) -> str:
    """Normalize unicode and remove characters that break the NRDB API."""
    # Normalize unicode (curly quotes → ascii equivalents)
    s = unicodedata.normalize("NFKD", s)
    # Remove apostrophes and similar characters
    s = re.sub(r"[''`´'\"\\]", " ", s)
    return s.strip()

# ── NetrunnerDB API ────────────────────────────────────────────────────────────

def search_card(card_name: str) -> Optional[dict]:
    
    query = clean_query(card_name)
    params = {
        "filter[search]": query,
        "filter[distinct_cards]": "true",
        "page[size]": 10,
    }
    headers = {"Accept": "application/json", "User-Agent": "NetrunnerTelegramBot/1.0"}

    try:
        resp = requests.get(
            f"{NRDB_API_BASE}/printings",
            params=params,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("API error searching for %r: %s", card_name, e)
        return None

    results = data.get("data", [])
    if not results:
        return None

    # Prefer exact title match (case-insensitive)
    needle = card_name.strip().lower()
    best = next(
        (r for r in results if r["attributes"].get("stripped_title", "").lower() == needle),
        results[0],  # fall back to first result
    )

    attrs = best["attributes"]
    attrs["_printing_id"] = best["id"]
    attrs["_card_id"] = attrs.get("card_id", best["id"])
    return attrs


def get_image_url(attrs: dict) -> str:
    """
    Extract the large image URL from the printing attributes.
    Falls back to building the URL from the printing ID.
    """
    images = attrs.get("images", {})
    # Try nrdb_classic style first (new API)
    for style in ("nrdb_classic", "fanwork"):
        if style in images and "large" in images[style]:
            return images[style]["large"]

    # Fallback: construct from printing ID (works for most cards)
    printing_id = attrs.get("_printing_id", "")
    if printing_id:
        return f"{CARD_IMAGE_BASE}/{printing_id}.jpg"

    return ""


# ── Caption builder ────────────────────────────────────────────────────────────

def build_caption(attrs: dict) -> str:
    """Build a nicely formatted HTML caption for the card."""
    title = attrs.get("title", "Unknown Card")
    card_type = attrs.get("card_type_id", "").replace("_", " ").title()
    faction = attrs.get("faction_id", "").replace("_", " ").title()
    side = attrs.get("side_id", "").title()
    subtypes = attrs.get("card_subtype_names") or []
    subtype_str = f": {' · '.join(subtypes)}" if subtypes else ""

    lines = []

    # Title
    lines.append(f"<b>{title}</b>")

    # Type · Faction (Side)
    lines.append(f"{card_type}{subtype_str} · {faction} ({side})")

    # Stats line
    stats = []
    cost = attrs.get("cost")
    strength = attrs.get("strength")
    agenda_points = attrs.get("agenda_points")
    trash = attrs.get("trash_cost")
    influence = attrs.get("influence_cost")
    mem = attrs.get("memory_cost")

    if cost is not None:
        stats.append(f"Cost: {cost}")
    if mem is not None:
        stats.append(f"MU: {mem}")
    if strength is not None:
        stats.append(f"Str: {strength}")
    if agenda_points is not None:
        stats.append(f"Agenda: {agenda_points} pts")
    if trash is not None:
        stats.append(f"Trash: {trash}")
    if influence:
        stats.append(f"Influence: {influence}")

    if stats:
        lines.append(" | ".join(stats))

    # Card text
    text = attrs.get("stripped_text", "").strip()
    if text:
        lines.append(f"<i>{text}</i>")

    # Flavor
    flavor = (attrs.get("flavor") or "").strip()
    if flavor:
        lines.append(f"<blockquote>{flavor}</blockquote>")

    # Legality
    formats = attrs.get("format_ids") or []
    if formats:
        lines.append("📋 Legal in: " + ", ".join(f.title() for f in formats))

    # NetrunnerDB link
    card_id = attrs.get("_card_id") or attrs.get("card_id", "")
    if card_id:
        lines.clear()
        lines.append(f'🔗 <a href="{NRDB_CARD_URL}/{card_id}">View on NetrunnerDB</a>')

    return "\n".join(lines)


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan each message for [[Card Name]] patterns and reply with card images."""
    message = update.effective_message
    if not message or not message.text:
        return

    card_names = CARD_PATTERN.findall(message.text)
    if not card_names:
        return

    # Deduplicate while preserving order
    seen = set()
    unique_names = []
    for name in card_names:
        key = name.strip().lower()
        if key not in seen:
            seen.add(key)
            unique_names.append(name.strip())

    for card_name in unique_names:
        logger.info("Looking up card: %r", card_name)
        attrs = search_card(card_name)

        if attrs is None:
            await message.reply_text(
                f"❌ Card not found: <b>{card_name}</b>",
                parse_mode=ParseMode.HTML,
            )
            continue

        image_url = get_image_url(attrs)
        caption = build_caption(attrs)

        if image_url:
            try:
                await message.reply_photo(
                    photo=image_url,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("Could not send photo for %r (%s), sending text.", card_name, e)
                await message.reply_text(
                    caption + f'\n\n🖼 <a href="{image_url}">Card image</a>',
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
        else:
            await message.reply_text(caption, parse_mode=ParseMode.HTML)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Netrunner Card Bot</b>\n\n"
        "Type any card name between double brackets and I'll show you the card!\n\n"
        "Example: <code>[[Sure Gamble]]</code>\n\n"
        "You can include multiple cards in one message:\n"
        "<code>[[Hedge Fund]] is great but so is [[Bravado]]</code>",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>How to use:</b>\n"
        "• Wrap any card name in <code>[[double brackets]]</code>\n"
        "• The bot works in groups and DMs\n"
        "• Card names are matched exactly, then fuzzy if no exact match\n\n"
        "<b>Examples:</b>\n"
        "<code>[[Sure Gamble]]</code>\n"
        "<code>[[Hedge Fund]] vs [[Bravado]]</code>\n"
        "<code>[[Asa Group: Security Through Vigilance]]</code>",
        parse_mode=ParseMode.HTML,
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Replace BOT_TOKEN in netrunner_bot.py with your actual token!")
        print("   Get one from @BotFather on Telegram.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
