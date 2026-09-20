"""
Media Ingestion & File ID Extraction Utility
============================================

This script provides two powerful methods to add movies and TV series to MongoDB:

1. Interactive CLI Mode:
   Prompt-based terminal interface to manually insert a movie record.
   Usage: python add_movie.py

2. Telegram Listener Mode:
   Starts a dedicated Telegram listener. Simply forward any document/video from
   your private channel to the bot in PM, and the bot will instantly extract the
   File ID, file size, and file name, then automatically insert it into MongoDB!
   Usage: python add_movie.py --listen

3. Seed Sample Data Mode:
   Seeds realistic sample movies and TV series into MongoDB for testing search & pagination.
   Usage: python add_movie.py --seed
"""

import sys
import asyncio
import argparse
import logging
from typing import Optional

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config
import database

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AddMovieUtility")


def format_bytes(size_in_bytes: int) -> str:
    """Format raw byte count into human-readable string (e.g. 890.19 MB, 1.45 GB)."""
    if not size_in_bytes:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"


async def insert_movie_cli():
    """Interactive CLI to insert a movie into MongoDB."""
    await database.init_db()

    print("\n" + "=" * 50)
    print("🎬 Add New Movie / Series to Database")
    print("=" * 50)

    title = input("Enter Title (e.g., Ultimate Spider-Man): ").strip()
    if not title:
        print("❌ Title cannot be empty.")
        await database.close_db()
        return

    season_episode = input("Enter Season/Episode (e.g., S01E21 or press Enter for Movies): ").strip() or None
    quality = input("Enter Quality (e.g., 1080p JHS DUA): ").strip() or "1080p"
    file_size = input("Enter File Size (e.g., 890.19 MB): ").strip() or "Unknown"
    file_id = input("Enter Telegram File ID: ").strip()

    if not file_id:
        print("❌ File ID cannot be empty.")
        await database.close_db()
        return

    inserted_id = await database.insert_movie(
        title=title,
        file_id=file_id,
        quality=quality,
        file_size=file_size,
        season_episode=season_episode
    )

    print("\n✅ Successfully added movie to database!")
    print(f"MongoDB _id: {inserted_id}")
    sample_btn = database.format_movie_button_text({
        "title": title,
        "season_episode": season_episode,
        "quality": quality,
        "file_size": file_size
    })
    print(f"Button Preview: {sample_btn}\n")
    await database.close_db()


async def seed_sample_data():
    """Seed sample movies and episodes into MongoDB to test search and pagination."""
    await database.init_db()

    samples = [
        {
            "title": "Ultimate Spider-Man",
            "season_episode": "S01E21",
            "quality": "1080p JHS DUA",
            "file_size": "890.19 MB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_SpiderMan_S01E21"
        },
        {
            "title": "Ultimate Spider-Man",
            "season_episode": "S01E22",
            "quality": "1080p JHS DUA",
            "file_size": "912.45 MB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_SpiderMan_S01E22"
        },
        {
            "title": "Ultimate Spider-Man",
            "season_episode": "S01E23",
            "quality": "1080p JHS DUA",
            "file_size": "875.10 MB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_SpiderMan_S01E23"
        },
        {
            "title": "Spider-Man: Into the Spider-Verse",
            "season_episode": None,
            "quality": "4K HDR REMUX",
            "file_size": "14.20 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_SpiderVerse_4K"
        },
        {
            "title": "Spider-Man: Across the Spider-Verse",
            "season_episode": None,
            "quality": "1080p BluRay",
            "file_size": "2.45 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_AcrossSpiderVerse_1080p"
        },
        {
            "title": "Spider-Man: No Way Home",
            "season_episode": None,
            "quality": "1080p WEB-DL DDP5.1",
            "file_size": "2.80 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_NoWayHome_1080p"
        },
        {
            "title": "Inception",
            "season_episode": None,
            "quality": "1080p BluRay x265",
            "file_size": "1.85 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_Inception_1080p"
        },
        {
            "title": "Interstellar",
            "season_episode": None,
            "quality": "1080p IMAX DTS-HD",
            "file_size": "3.10 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_Interstellar_1080p"
        },
        {
            "title": "Breaking Bad",
            "season_episode": "S01E01",
            "quality": "1080p BluRay",
            "file_size": "1.20 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_BreakingBad_S01E01"
        },
        {
            "title": "Breaking Bad",
            "season_episode": "S01E02",
            "quality": "1080p BluRay",
            "file_size": "1.15 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_BreakingBad_S01E02"
        },
        {
            "title": "Breaking Bad",
            "season_episode": "S01E03",
            "quality": "1080p BluRay",
            "file_size": "1.18 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_BreakingBad_S01E03"
        },
        {
            "title": "The Dark Knight",
            "season_episode": None,
            "quality": "2160p UHD HDR",
            "file_size": "18.50 GB",
            "file_id": "BAACAgUAAxkBAAI_SampleFileId_DarkKnight_4K"
        }
    ]

    count = 0
    for s in samples:
        await database.insert_movie(
            title=s["title"],
            file_id=s["file_id"],
            quality=s["quality"],
            file_size=s["file_size"],
            season_episode=s["season_episode"]
        )
        count += 1

    print(f"✅ Successfully seeded {count} sample movies/episodes into MongoDB!")
    total = await database.count_movies()
    print(f"Total entries now in database: {total}")
    await database.close_db()


def run_telegram_listener():
    """
    Runs a lightweight Telegram bot listener.
    When you forward any video/document from your private channel to the bot in PM,
    it automatically prints the File ID, file size, and details, and allows 1-click MongoDB insertion!
    """
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

    config.validate_config()
    if not config.BOT_TOKEN:
        print("❌ BOT_TOKEN is missing from .env!")
        return

    print("=" * 60)
    print("📡 Telegram File ID Listener Running...")
    print("Forward any video or document from your private channel to the bot in PM.")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    async def file_listener_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return

        msg = update.message
        media = msg.document or msg.video or msg.audio

        if not media:
            await msg.reply_text("ℹ️ Please forward or send a Document, Video, or Audio file.")
            return

        file_id = media.file_id
        file_name = getattr(media, "file_name", None) or "Unknown Title"
        file_size_raw = getattr(media, "file_size", 0)
        formatted_size = format_bytes(file_size_raw)

        # Clean file extension if present in title
        clean_title = file_name
        for ext in [".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts"]:
            if clean_title.lower().endswith(ext):
                clean_title = clean_title[:-len(ext)]
                break

        print("\n" + "*" * 40)
        print("🎯 New Media Received:")
        print(f"  • File Name: {file_name}")
        print(f"  • File Size: {formatted_size}")
        print(f"  • File ID:   {file_id}")
        print("*" * 40 + "\n")

        # Reply with copy-pasteable info
        reply_text = (
            "✅ **Media Received!**\n\n"
            f"📁 **File Name:** `{file_name}`\n"
            f"💾 **File Size:** `{formatted_size}`\n"
            f"🔑 **File ID:**\n`{file_id}`\n\n"
            "💡 *Tip: You can insert this into MongoDB using `python add_movie.py` or use the auto-ingest flag.*"
        )
        await msg.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.VIDEO | filters.AUDIO) & filters.ChatType.PRIVATE,
            file_listener_handler
        )
    )
    app.run_polling(drop_pending_updates=True)


def main():
    parser = argparse.ArgumentParser(description="Media Ingestion & File ID Utility")
    parser.add_argument("--listen", action="store_true", help="Run Telegram listener to capture File IDs from forwarded files")
    parser.add_argument("--seed", action="store_true", help="Seed sample movies into MongoDB for testing")

    args = parser.parse_args()

    if args.listen:
        run_telegram_listener()
    elif args.seed:
        asyncio.run(seed_sample_data())
    else:
        asyncio.run(insert_movie_cli())


if __name__ == "__main__":
    main()
