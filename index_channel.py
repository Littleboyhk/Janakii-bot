"""
Automated Telegram Channel Indexer for Movie & Series Librarian
==============================================================
Crawls your Telegram storage channel, parses media metadata (title, quality,
file size, season/episode), and bulk-inserts them into MongoDB Atlas.

Features:
- Telethon MTProto client for maximum speed and access to channel history.
- Smart filename parser (detects title, SxxExx, 1080p/720p/4K, file size).
- High-performance batch insertion into MongoDB (batches of 100).
- Supports both Bot Token login (--bot) and User Account login.

Usage:
  1. Test with first 100 files:
     python index_channel.py --limit 100

  2. Index all files from the channel:
     python index_channel.py

  3. Use Bot Token login:
     python index_channel.py --bot
"""

import os
import sys
import re
import asyncio
import argparse
import logging
from typing import Optional, Dict, Any, List

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

import config
import database
import ai_parser

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ChannelIndexer")


def format_bytes(size_in_bytes: int) -> str:
    """Format raw byte count into human-readable string (e.g. 890.19 MB, 1.45 GB)."""
    if not size_in_bytes:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"


def parse_media_metadata(raw_text: str) -> Dict[str, Optional[str]]:
    """
    Parse title, season_episode, and quality from filename or message caption.
    Examples:
      - 'Mirzapur S03E03 720p 10bit DS4K AMZN WEBRip' -> Title: 'Mirzapur', S/E: 'S03E03', Quality: '720p DS4K WEBRip'
      - 'Spider-Man.Across.the.Spider-Verse.2023.1080p.BluRay.x265.mkv' -> Title: 'Spider-Man Across the Spider-Verse', Quality: '1080p BluRay'
    """
    cleaned = raw_text.strip()
    # Remove common file extensions
    for ext in [".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"]:
        if cleaned.lower().endswith(ext):
            cleaned = cleaned[:-len(ext)]
            break

    # Extract Season / Episode (e.g., S01E21, S1E05, 1x05, E21)
    se_pattern = r"(?i)\b(S\d{1,2}\s?[E|x]\d{1,2}|Season\s?\d{1,2}\s?Episode\s?\d{1,2}|E\d{1,3})\b"
    se_match = re.search(se_pattern, cleaned)
    season_episode = se_match.group(0).upper().replace(" ", "") if se_match else None

    # Extract Quality tags
    quality_tags = []
    qual_pattern = r"(?i)\b(4K|2160p|1080p|720p|480p|360p|BluRay|BRRip|WEB-DL|WEBRip|HDR|REMUX|HDTC|DVDRip|10bit|HEVC|x265|x264|DDP5\.1)\b"
    for m in re.finditer(qual_pattern, cleaned):
        tag = m.group(0)
        if tag not in quality_tags:
            quality_tags.append(tag)
    quality = " ".join(quality_tags) if quality_tags else "HD"

    # Clean title
    title_part = cleaned
    # Cut at season/episode if present
    if se_match:
        title_part = cleaned[:se_match.start()]
    else:
        # Otherwise cut at first quality tag
        first_q = re.search(qual_pattern, cleaned)
        if first_q:
            title_part = cleaned[:first_q.start()]

    # Replace dots, underscores, and dashes with spaces
    clean_title = re.sub(r"[._\-+]+", " ", title_part).strip()
    # Remove release year in brackets/parentheses e.g. (2023) or [2022] if at end
    clean_title = re.sub(r"[\(\[]\d{4}[\)\]]", "", clean_title).strip()
    # Remove trailing garbage
    clean_title = re.sub(r"\s+", " ", clean_title).strip()

    if not clean_title:
        clean_title = re.sub(r"[._\-+]+", " ", cleaned).strip()

    return {
        "title": clean_title,
        "season_episode": season_episode,
        "quality": quality
    }


async def index_channel(limit: Optional[int] = None, use_bot_token: bool = False, sync_mode: bool = False, use_ai: bool = True):
    """Crawl the Telegram channel and index documents into MongoDB."""
    api_id = config.TELEGRAM_API_ID
    api_hash = config.TELEGRAM_API_HASH
    target = config.TARGET_CHANNEL or config.FORCE_SUB_CHANNEL_LINK

    if not api_id or not api_hash:
        print("❌ TELEGRAM_API_ID and TELEGRAM_API_HASH are required in .env!")
        return

    if not target:
        print("❌ TARGET_CHANNEL is not specified in .env! (e.g., https://t.me/+FhBcSz2mNEIwOGE1)")
        return

    ai_status = "Enabled (Google Gemini)" if (use_ai and config.ENABLE_AI_INGESTION and config.GEMINI_API_KEY) else "Disabled (Regex Heuristics)"
    print("=" * 65)
    print("🚀 Starting Automated Telegram Channel Indexer")
    print(f"• Target Channel: {target}")
    print(f"• Limit:          {limit if limit else 'ALL Files'}")
    print(f"• Mode:           {'Bot Token' if use_bot_token else 'User Session'}")
    print(f"• AI Parsing:     {ai_status}")
    print("=" * 65)

    # Initialize MongoDB
    await database.init_db()

    # Create Telethon client
    session_name = "bot_indexer" if use_bot_token else "user_indexer"
    client = TelegramClient(session_name, api_id, api_hash)

    if use_bot_token:
        await client.start(bot_token=config.BOT_TOKEN)
    else:
        await client.start()

    print("✅ Connected to Telegram MTProto successfully!")

    # Resolve channel entity
    channel = None
    channel_id = None
    try:
        channel = await client.get_entity(target)
    except Exception as e:
        print(f"Notice: Direct entity resolve returned: {e}")
        print("Searching your Telegram chats for a channel matching 'DataBase' or target link...")
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                d_title = getattr(dialog, "title", "").lower()
                if "database" in d_title or "data" in d_title:
                    channel = dialog.entity
                    print(f"🎯 Automatically found matching channel: '{dialog.title}' (ID: {channel.id})")
                    break

    if not channel:
        print(f"❌ Could not locate channel. Please ensure you are a member of 'DataBase'.")
        await client.disconnect()
        await database.close_db()
        return

    channel_id = channel.id
    if not str(channel_id).startswith("-100"):
        channel_id = int(f"-100{channel_id}")
    channel_title = getattr(channel, "title", str(target))
    print(f"📢 Target Channel Selected: {channel_title} (ID: {channel_id})")

    count = 0
    raw_items_buffer: List[Dict[str, Any]] = []
    BATCH_SIZE = 15 if use_ai else 100

    min_id = None
    if sync_mode:
        latest_id = await database.get_latest_message_id(channel_id)
        if latest_id > 0:
            min_id = latest_id
            print(f"⚡ Sync Mode Active: Only fetching new daily uploads (after Message ID {latest_id})...")
        else:
            print("ℹ️ No previous messages recorded. Running standard crawl...")

    async def flush_buffer():
        nonlocal raw_items_buffer, count
        if not raw_items_buffer:
            return

        db_batch = []
        if use_ai and config.ENABLE_AI_INGESTION:
            parsed_list = await ai_parser.parse_media_metadata_ai_batch(
                [{"id": it["msg_id"], "raw_text": it["raw_title"], "caption": it["caption"]} for it in raw_items_buffer],
                chunk_size=15
            )
            parsed_map = {p.get("item_id"): p for p in parsed_list if p}
            for it in raw_items_buffer:
                p = parsed_map.get(it["msg_id"]) or parse_media_metadata(it["raw_title"])
                db_batch.append({
                    "title": p["title"],
                    "season_episode": p.get("season_episode"),
                    "quality": p.get("quality") or "HD",
                    "file_size": it["formatted_size"],
                    "file_id": "",
                    "channel_id": channel_id,
                    "message_id": it["msg_id"],
                    "year": p.get("year"),
                    "language": p.get("language")
                })
        else:
            for it in raw_items_buffer:
                p = parse_media_metadata(it["raw_title"])
                db_batch.append({
                    "title": p["title"],
                    "season_episode": p.get("season_episode"),
                    "quality": p.get("quality") or "HD",
                    "file_size": it["formatted_size"],
                    "file_id": "",
                    "channel_id": channel_id,
                    "message_id": it["msg_id"]
                })

        await database.insert_movies_bulk(db_batch)
        count += len(db_batch)
        sample = db_batch[-1]
        tag = "🤖 AI" if (use_ai and config.ENABLE_AI_INGESTION and config.GEMINI_API_KEY) else "⚡ Regex"
        print(f"  [{tag}] Indexed {count:,} files... (Latest: {sample['title']})")
        raw_items_buffer = []

    print("\n🔍 Crawling media messages and indexing to MongoDB Atlas...")

    async for msg in client.iter_messages(channel, limit=limit, min_id=min_id):
        if not msg.media or not (hasattr(msg.media, "document") or hasattr(msg, "video")):
            continue

        doc = getattr(msg.media, "document", None) or msg.video
        if not doc:
            continue

        file_size_raw = getattr(doc, "size", 0)
        formatted_size = format_bytes(file_size_raw)

        file_name = None
        if hasattr(doc, "attributes"):
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    file_name = attr.file_name
                    break

        raw_title = file_name or msg.message or getattr(channel, "title", "Movie")
        raw_items_buffer.append({
            "msg_id": msg.id,
            "raw_title": raw_title,
            "caption": msg.message or "",
            "formatted_size": formatted_size
        })

        if len(raw_items_buffer) >= BATCH_SIZE:
            await flush_buffer()

    # Flush any remaining items in buffer
    await flush_buffer()

    print("\n" + "=" * 65)
    print(f"🎉 Channel Indexing Complete!")
    print(f"• Total media indexed in this run: {count:,}")
    total_in_db = await database.count_movies()
    print(f"• Total titles now searchable in MongoDB: {total_in_db:,}")
    print("=" * 65 + "\n")

    await client.disconnect()
    await database.close_db()


def main():
    parser = argparse.ArgumentParser(description="Telegram Channel Media Indexer to MongoDB")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of messages to process")
    parser.add_argument("--bot", action="store_true", help="Log in using bot token instead of user phone account")
    parser.add_argument("--sync", action="store_true", help="Incremental sync: only index new files uploaded today since last run")
    parser.add_argument("--no-ai", action="store_true", help="Disable AI metadata parsing and use fast regex heuristics")
    args = parser.parse_args()

    asyncio.run(index_channel(limit=args.limit, use_bot_token=args.bot, sync_mode=args.sync, use_ai=not args.no_ai))


if __name__ == "__main__":
    main()
