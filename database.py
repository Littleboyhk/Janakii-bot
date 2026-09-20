import re
import time
import logging
from typing import Optional, List, Dict, Any, Tuple, Union
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
import config

logger = logging.getLogger(__name__)

# Global client and db references
client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None
movies_collection: Optional[AsyncIOMotorCollection] = None

# In-memory search cache for hot queries: {clean_query_lower: (timestamp, matches_list)}
_db_search_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
SEARCH_CACHE_TTL = 300.0  # 5 minutes
SEARCH_CACHE_MAX = 500


async def init_db(mongo_uri: Optional[str] = None, db_name: Optional[str] = None) -> AsyncIOMotorDatabase:
    """Initialize the MongoDB connection and create necessary indexes."""
    global client, db, movies_collection

    uri = mongo_uri or config.MONGO_URI
    database_name = db_name or config.DATABASE_NAME

    logger.info("Connecting to MongoDB at: %s (database: %s)", uri.split("@")[-1] if "@" in uri else uri, database_name)
    client = AsyncIOMotorClient(uri)
    db = client[database_name]
    movies_collection = db["movies"]

    # Ensure index on 'title' for fast search performance
    try:
        await movies_collection.create_index([("title", 1)])
        logger.info("MongoDB index on 'title' created/verified successfully.")
    except Exception as e:
        logger.warning("Could not create index on 'title': %s", e)

    return db


async def close_db() -> None:
    """Close MongoDB connection pool."""
    global client
    if client:
        client.close()
        logger.info("MongoDB connection closed.")


def get_movies_collection() -> AsyncIOMotorCollection:
    """Return the active movies collection instance."""
    if movies_collection is None:
        raise RuntimeError("Database is not initialized. Call init_db() first.")
    return movies_collection


def parse_size_to_bytes(size_str: Optional[str]) -> float:
    """Convert human-readable file size (e.g. '3.18 GB', '890.19 MB') to byte value for sorting."""
    if not size_str:
        return 0.0
    parts = str(size_str).strip().split()
    try:
        num = float(parts[0])
        unit = parts[1].upper() if len(parts) > 1 else ""
        if "TB" in unit:
            return num * (1024 ** 4)
        if "GB" in unit:
            return num * (1024 ** 3)
        if "MB" in unit:
            return num * (1024 ** 2)
        if "KB" in unit:
            return num * 1024
        return num
    except Exception:
        return 0.0


def extract_available_qualities(matches: List[Dict[str, Any]]) -> List[str]:
    """Scan matches to discover available quality tiers (e.g. ['4K', '2160p', '1080p', '720p', '480p', 'HEVC'])."""
    qualities_order = ["4K", "2160p", "1080p", "720p", "480p", "HEVC"]
    found = set()
    for m in matches:
        q_str = (m.get("quality") or "").upper()
        title_str = (m.get("title") or "").upper()
        full_text = f"{q_str} {title_str}"
        for q in qualities_order:
            if q.upper() in full_text:
                found.add(q)
    return [q for q in qualities_order if q in found]


async def search_movies(
    query: str,
    page: int = 1,
    page_size: int = 10,
    quality_filter: Optional[str] = None,
    return_qualities: bool = False
) -> Any:
    """
    Search for movies matching title using case-insensitive regex.
    Results are sorted in descending order of file size (highest GB first).
    Supports optional filtering by quality tag (e.g. '1080p', '720p', '4K').
    """
    col = get_movies_collection()
    cleaned_query = query.strip()
    if not cleaned_query:
        return ([], 0, 0, []) if return_qualities else ([], 0, 0)

    cache_key = cleaned_query.lower()
    now = time.time()
    all_matches = None

    # Check hot cache first (0.0001s lookup)
    if cache_key in _db_search_cache:
        cached_ts, cached_matches = _db_search_cache[cache_key]
        if (now - cached_ts) < SEARCH_CACHE_TTL:
            all_matches = list(cached_matches)

    if all_matches is None:
        # Escaped regex pattern for safe fuzzy search
        escaped = re.escape(cleaned_query)
        filter_query = {
            "title": {
                "$regex": escaped,
                "$options": "i"
            }
        }

        # Fetch matching results up to pool limit with field projection for minimal network payload
        MAX_SEARCH_POOL = 150
        projection = {
            "_id": 1,
            "title": 1,
            "quality": 1,
            "file_size": 1,
            "season_episode": 1,
            "channel_id": 1,
            "message_id": 1,
            "file_id": 1,
        }
        cursor = col.find(filter_query, projection).limit(MAX_SEARCH_POOL)
        all_matches = await cursor.to_list(length=MAX_SEARCH_POOL)

        if not all_matches:
            return ([], 0, 0, []) if return_qualities else ([], 0, 0)

        # Sort so that highest GB files appear first
        all_matches.sort(key=lambda d: parse_size_to_bytes(d.get("file_size")), reverse=True)

        # Update cache
        if len(_db_search_cache) >= SEARCH_CACHE_MAX:
            _db_search_cache.pop(next(iter(_db_search_cache)))
        _db_search_cache[cache_key] = (now, list(all_matches))

    available_qualities = extract_available_qualities(all_matches)

    # Apply quality filter if requested and not "ALL"
    filtered_matches = all_matches
    if quality_filter and quality_filter.upper() != "ALL":
        q_target = quality_filter.upper()
        filtered_matches = [
            m for m in all_matches
            if q_target in (m.get("quality") or "").upper() or q_target in (m.get("title") or "").upper()
        ]

    total_count = len(filtered_matches)
    if total_count == 0:
        return ([], 0, 0, available_qualities) if return_qualities else ([], 0, 0)

    total_pages = (total_count + page_size - 1) // page_size
    page = max(1, min(page, total_pages))

    skip = (page - 1) * page_size
    results = filtered_matches[skip : skip + page_size]

    if return_qualities:
        return results, total_count, total_pages, available_qualities
    return results, total_count, total_pages


async def get_movie_by_id(movie_id: Union[str, ObjectId]) -> Optional[Dict[str, Any]]:
    """Retrieve a movie document by its ObjectId."""
    col = get_movies_collection()
    if isinstance(movie_id, str):
        try:
            movie_id = ObjectId(movie_id)
        except Exception:
            return None

    return await col.find_one({"_id": movie_id})


async def insert_movie(
    title: str,
    file_id: str = "",
    quality: str = "",
    file_size: str = "",
    season_episode: Optional[str] = None,
    channel_id: Optional[Union[int, str]] = None,
    message_id: Optional[int] = None
) -> ObjectId:
    """
    Insert a movie/series document into MongoDB.
    Schema fields:
      _id: ObjectId
      title: String
      season_episode: String (or None)
      quality: String
      file_size: String
      file_id: String
      channel_id: Optional int/str
      message_id: Optional int
    """
    col = get_movies_collection()
    doc = {
        "title": title.strip(),
        "season_episode": season_episode.strip() if season_episode else None,
        "quality": quality.strip(),
        "file_size": file_size.strip(),
        "file_id": file_id.strip() if file_id else "",
        "channel_id": channel_id,
        "message_id": message_id
    }
    result = await col.insert_one(doc)
    _db_search_cache.clear()
    return result.inserted_id


async def insert_movies_bulk(movies_list: List[Dict[str, Any]]) -> int:
    """Bulk insert a batch of movie records into MongoDB for high performance indexing."""
    if not movies_list:
        return 0
    col = get_movies_collection()
    try:
        res = await col.insert_many(movies_list, ordered=False)
        _db_search_cache.clear()
        return len(res.inserted_ids)
    except Exception as e:
        logger.warning("Bulk insert warning: %s", e)
        _db_search_cache.clear()
        # In case of partial duplicates or errors
        return 0


async def count_movies() -> int:
    """Return total number of movies currently stored."""
    col = get_movies_collection()
    return await col.estimated_document_count()


def format_movie_button_text(movie: Dict[str, Any]) -> str:
    """
    Format movie display text for inline keyboard buttons according to specification:
    [File Size] Title Quality (e.g., [890.19 MB] Ultimate Spider-Man S01E21 1080p).
    If season_episode is present, it is included between Title and Quality.
    """
    file_size = movie.get("file_size") or "Unknown Size"
    title = movie.get("title") or "Untitled"
    season_episode = movie.get("season_episode")
    if season_episode in (None, "None", "null", ""):
        season_episode = None

    quality = movie.get("quality") or ""
    if quality in (None, "None", "null"):
        quality = ""

    parts = [f"[{file_size}]", title]
    if season_episode:
        parts.append(str(season_episode))
    if quality:
        parts.append(str(quality))

    button_text = " ".join(parts)
    if len(button_text) > 60:
        prefix = f"[{file_size}] "
        suffix_parts = []
        if season_episode:
            suffix_parts.append(str(season_episode))
        if quality:
            suffix_parts.append(str(quality))
        suffix = " " + " ".join(suffix_parts) if suffix_parts else ""
        avail_len = 57 - len(prefix) - len(suffix)
        if avail_len > 10:
            short_title = title[:avail_len].strip() + "..."
            button_text = f"{prefix}{short_title}{suffix}".strip()

    return button_text


async def get_latest_message_id(channel_id: Union[int, str]) -> int:
    """Find the highest message_id stored in MongoDB for a specific channel."""
    col = get_movies_collection()
    doc = await col.find_one({"channel_id": channel_id}, sort=[("message_id", -1)])
    return doc.get("message_id", 0) if doc else 0


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
    """Parse title, season_episode, and quality from filename or message caption."""
    cleaned = raw_text.strip()
    for ext in [".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"]:
        if cleaned.lower().endswith(ext):
            cleaned = cleaned[:-len(ext)]
            break

    # Extract Season / Episode
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
    if se_match:
        title_part = cleaned[:se_match.start()]
    else:
        first_q = re.search(qual_pattern, cleaned)
        if first_q:
            title_part = cleaned[:first_q.start()]

    clean_title = re.sub(r"[._\-+]+", " ", title_part).strip()
    clean_title = re.sub(r"[\(\[]\d{4}[\)\]]", "", clean_title).strip()
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    if not clean_title:
        clean_title = re.sub(r"[._\-+]+", " ", cleaned).strip()

    return {
        "title": clean_title,
        "season_episode": season_episode,
        "quality": quality
    }
