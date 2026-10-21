import re
import time
import logging
import urllib.parse
from typing import Optional, List, Dict, Any, Tuple, Union
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
import httpx
import config

logger = logging.getLogger(__name__)

# Global cache for Indic language translations: {clean_indic_text: english_translation}
_indic_trans_cache: Dict[str, str] = {}
# Shared persistent HTTP client for fast async translation lookups
_trans_http_client: Optional[httpx.AsyncClient] = None

def get_trans_http_client() -> httpx.AsyncClient:
    global _trans_http_client
    if _trans_http_client is None or _trans_http_client.is_closed:
        _trans_http_client = httpx.AsyncClient(
            timeout=2.5,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=60.0)
        )
    return _trans_http_client

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
    """Scan matches to discover available quality tiers (e.g. ['4K', '2160p', '1080p', '720p', '540p', '480p', '360p', 'HEVC'])."""
    qualities_order = ["4K", "2160p", "1080p", "720p", "540p", "480p", "360p", "HEVC", "HD"]
    found = set()
    for m in matches:
        q_str = (m.get("quality") or "").upper()
        title_str = (m.get("title") or "").upper()
        full_text = f"{q_str} {title_str}"
        for q in qualities_order:
            if q.upper() in full_text:
                found.add(q)
    return [q for q in qualities_order if q in found]


def extract_season_from_doc(doc: Dict[str, Any]) -> Optional[str]:
    """Extract normalized season tag (e.g. 'S01', 'S02') from season_episode or title."""
    se = doc.get("season_episode") or ""
    m = re.search(r"(?i)\b(?:S|Season\s*)(\d{1,2})(?:[Ex\s]|$)", se)
    if m:
        return f"S{int(m.group(1)):02d}"
    title = doc.get("title") or ""
    m = re.search(r"(?i)\b(?:S|Season\s*)(\d{1,2})(?:[Ex\s]|$)", title)
    if m:
        return f"S{int(m.group(1)):02d}"
    return None


def extract_available_seasons(matches: List[Dict[str, Any]]) -> List[str]:
    """Scan matches to discover all available season tags (e.g. ['S01', 'S02'])."""
    found = set()
    for m in matches:
        s = extract_season_from_doc(m)
        if s:
            found.add(s)
    return sorted(list(found))


def parse_episode_num(doc: Dict[str, Any]) -> int:
    """Extract numeric episode number for natural ascending ordering (e.g. S01E05 -> 5)."""
    se = (doc.get("season_episode") or "") + " " + (doc.get("title") or "")
    m = re.search(r"(?i)(?:E|Episode\s*|x)(\d{1,3})\b", se)
    if m:
        return int(m.group(1))
    return 999


def format_ep_range(ep_nums: List[int]) -> List[str]:
    """Compress episode numbers into clean ranges (e.g. [1,2,3,5,7,8] -> ['E01-E03', 'E05', 'E07-E08'])."""
    if not ep_nums:
        return []
    sorted_nums = sorted(list(set(ep_nums)))
    ranges = []
    start = sorted_nums[0]
    prev = sorted_nums[0]

    for n in sorted_nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            if start == prev:
                ranges.append(f"E{start:02d}")
            else:
                ranges.append(f"E{start:02d}-E{prev:02d}")
            start = n
            prev = n
    if start == prev:
        ranges.append(f"E{start:02d}")
    else:
        ranges.append(f"E{start:02d}-E{prev:02d}")
    return ranges


def analyze_series_availability(
    matches: List[Dict[str, Any]],
    active_season: str = "ALL",
    active_quality: str = "ALL"
) -> Dict[str, Any]:
    """
    Examines all indexed media records for a series or movie to detect:
    1. Sequence gaps (missing episodes in season)
    2. Quality breakdown across available episodes (e.g. 720p: 6 ep, 1080p: 2 ep)
    3. Episodes available in alternative qualities when active quality is filtered
    """
    ep_docs = [m for m in matches if parse_episode_num(m) < 999]
    if not ep_docs:
        qualities = extract_available_qualities(matches)
        return {
            "is_series": False,
            "available_qualities": qualities,
            "missing_episodes": [],
            "missing_in_active_q": [],
            "alt_qualities": [],
            "quality_map": {}
        }

    # Filter to active season if specified
    if active_season.upper() != "ALL":
        target_s = active_season.upper()
        target_docs = [m for m in ep_docs if extract_season_from_doc(m) == target_s]
        if not target_docs:
            target_docs = ep_docs
    else:
        target_docs = ep_docs

    all_ep_nums = sorted(list({parse_episode_num(m) for m in target_docs if parse_episode_num(m) < 999}))

    # Check for missing episodes in sequence
    missing_nums = []
    if all_ep_nums:
        min_ep = all_ep_nums[0]
        max_ep = all_ep_nums[-1]

        # If season starts above E01 (e.g. E21..E23), prefix episodes E01-E20 are missing
        if min_ep > 1 and min_ep <= 30:
            missing_nums.extend(range(1, min_ep))

        existing_set = set(all_ep_nums)
        for i in range(min_ep, max_ep + 1):
            if i not in existing_set:
                missing_nums.append(i)

    missing_eps_formatted = format_ep_range(missing_nums)

    # Compute quality coverage
    quality_map = {}
    for m in target_docs:
        q_text = (m.get("quality") or "").upper() + " " + (m.get("title") or "").upper()
        ep = parse_episode_num(m)
        for q_tier in ["4K", "2160P", "1080P", "720P", "540P", "480P", "360P", "HEVC", "HD"]:
            if q_tier in q_text:
                norm_q = q_tier.lower()
                if norm_q not in quality_map:
                    quality_map[norm_q] = set()
                quality_map[norm_q].add(ep)

    missing_in_active_q = []
    alt_qualities = []
    if active_quality.upper() != "ALL":
        act_q = active_quality.lower()
        act_eps = quality_map.get(act_q, set())
        other_eps = set(all_ep_nums) - act_eps
        if other_eps:
            missing_in_active_q = format_ep_range(list(other_eps))
            for q_name, q_eps in quality_map.items():
                if q_name != act_q and (q_eps & other_eps):
                    alt_qualities.append(q_name.upper())

    return {
        "is_series": True,
        "available_episodes": all_ep_nums,
        "missing_episodes": missing_eps_formatted,
        "quality_map": {k.upper(): len(v) for k, v in quality_map.items()},
        "missing_in_active_q": missing_in_active_q,
        "alt_qualities": alt_qualities
    }# ==============================================================================
# Indic Script Helpers, Offline Transliteration, Translation & Query Resolution
# ==============================================================================

SCRIPT_BASES = [
    (0x0900, 0x097F, "hi"),  # Devanagari (Hindi, Marathi)
    (0x0980, 0x09FF, "bn"),  # Bengali
    (0x0A80, 0x0AFF, "gu"),  # Gujarati
    (0x0A00, 0x0A7F, "pa"),  # Gurmukhi
    (0x0B80, 0x0BFF, "ta"),  # Tamil
    (0x0C00, 0x0C7F, "te"),  # Telugu
    (0x0C80, 0x0CFF, "kn"),  # Kannada
    (0x0D00, 0x0D7F, "ml"),  # Malayalam
]

VOWELS_MAP = {
    0x05: "a", 0x06: "aa", 0x07: "i", 0x08: "ee", 0x09: "u", 0x0A: "oo",
    0x0E: "e", 0x0F: "e", 0x10: "ai", 0x12: "o", 0x13: "o", 0x14: "au"
}
VOWEL_SIGNS_MAP = {
    0x3E: "a", 0x3F: "i", 0x40: "ee", 0x41: "u", 0x42: "oo",
    0x46: "e", 0x47: "e", 0x48: "ai", 0x4A: "o", 0x4B: "o", 0x4C: "au"
}
CONSONANTS_MAP = {
    0x15: "k", 0x16: "kh", 0x17: "g", 0x18: "gh", 0x19: "ng",
    0x1A: "ch", 0x1B: "chh", 0x1C: "j", 0x1D: "jh", 0x1E: "ny",
    0x1F: "t", 0x20: "th", 0x21: "d", 0x22: "dh", 0x23: "n",
    0x24: "t", 0x25: "th", 0x26: "d", 0x27: "dh", 0x28: "n",
    0x2A: "p", 0x2B: "f", 0x2C: "b", 0x2D: "bh", 0x2E: "m",
    0x2F: "y", 0x30: "r", 0x31: "rr", 0x32: "l", 0x33: "ll", 0x34: "zh",
    0x35: "v", 0x36: "sh", 0x37: "sh", 0x38: "s", 0x39: "h"
}
CHILLU_MAP = {
    0x7A: "nn", 0x7B: "n", 0x7C: "r", 0x7D: "l", 0x7E: "ll", 0x7F: "k"
}

def has_indic_script(text: str) -> bool:
    """Return True if text contains any Indic Unicode script characters."""
    for ch in text:
        code = ord(ch)
        for b_start, b_end, _ in SCRIPT_BASES:
            if b_start <= code <= b_end:
                return True
    return False

def detect_indic_lang(text: str) -> str:
    """Detect dominant Indic language code ('ml', 'ta', 'te', 'kn', 'hi', etc.)."""
    for ch in text:
        code = ord(ch)
        for b_start, b_end, lang in SCRIPT_BASES:
            if b_start <= code <= b_end:
                return lang
    return "ml"

def has_latin_script(text: str) -> bool:
    """Return True if text contains any Latin A-Z letters."""
    return bool(re.search(r"[a-zA-Z]", text))

def transliterate_indic(word: str) -> str:
    """Fast, 100% offline rule-based phonetic transliteration from Indic script to Latin."""
    res = []
    i = 0
    n = len(word)
    while i < n:
        ch = word[i]
        code = ord(ch)
        base = None
        for b_start, b_end, _ in SCRIPT_BASES:
            if b_start <= code <= b_end:
                base = b_start
                break
        if base is None:
            res.append(ch)
            i += 1
            continue
        
        offset = code - base
        
        # Check Malayalam chillu (base == 0x0D00)
        if base == 0x0D00 and offset in CHILLU_MAP:
            res.append(CHILLU_MAP[offset])
            i += 1
            continue
            
        # Independent vowel
        if offset in VOWELS_MAP:
            res.append(VOWELS_MAP[offset])
            i += 1
            continue
            
        # Consonant
        if offset in CONSONANTS_MAP:
            cons = CONSONANTS_MAP[offset]
            if i + 1 < n:
                next_code = ord(word[i + 1])
                next_offset = next_code - base
                if next_offset == 0x4D:  # Virama (halant / suppress inherent vowel)
                    res.append(cons)
                    i += 2
                    continue
                elif next_offset in VOWEL_SIGNS_MAP:
                    res.append(cons + VOWEL_SIGNS_MAP[next_offset])
                    i += 2
                    continue
                elif base <= next_code <= base + 0x7F:
                    res.append(cons + "a")
                    i += 1
                    continue
            res.append(cons)
            i += 1
            continue
            
        if offset in VOWEL_SIGNS_MAP:
            res.append(VOWEL_SIGNS_MAP[offset])
            i += 1
            continue
            
        if offset == 0x4D:  # Standalone virama
            i += 1
            continue
            
        if offset == 0x02:  # Anusvara (nasal)
            res.append("m")
            i += 1
            continue
            
        i += 1
        
    return "".join(res).strip()

async def translate_indic_to_en(text: str) -> Optional[str]:
    """Translate Indic script query (e.g. Malayalam 'ലൂസിഫർ') to English using MyMemory with cache."""
    clean = text.strip()
    if not clean or not has_indic_script(clean):
        return None
    cache_key = clean.lower()
    if cache_key in _indic_trans_cache:
        return _indic_trans_cache[cache_key]
    
    lang = detect_indic_lang(clean)
    try:
        client = get_trans_http_client()
        url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(clean)}&langpair={lang}|en"
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if r.status_code == 200:
            data = r.json()
            translated = data.get("responseData", {}).get("translatedText")
            if translated and not translated.startswith("MYMEMORY WARNING"):
                translated = translated.strip()
                if len(translated) >= 2 and not translated.isdigit():
                    _indic_trans_cache[cache_key] = translated
                    return translated
    except Exception as e:
        logger.debug("Indic translation failed for '%s': %s", clean, e)
    return None

def generate_query_candidates(raw_query: str) -> List[str]:
    """
    Generate prioritized search query candidates:
    1. Exact query as entered (to find matching local language titles directly in DB)
    2. Outside brackets: 'Lucifer (ലൂസിഫർ)' -> 'Lucifer'
    3. Inside brackets: 'ലൂസിഫർ'
    4. Latin-only part: 'Lucifer ലൂസിഫർ' -> 'Lucifer'
    5. Title without year: 'Lucifer 2019' -> 'Lucifer'
    """
    candidates = []
    text = raw_query.strip()
    if not text:
        return candidates
    
    candidates.append(text)
    
    bracket_matches = re.findall(r"[\(\[\{]([^\)\]\}]+)[\)\]\}]", text)
    is_pure_year_bracket = all(b.strip().isdigit() and len(b.strip()) == 4 for b in bracket_matches) if bracket_matches else False
    
    if not is_pure_year_bracket:
        outside = re.sub(r"[\(\[\{][^\)\]\}]+[\)\]\}]", " ", text).strip()
        outside = re.sub(r"^[\s\-:_.,]+|[\s\-:_.,]+$", "", outside).strip()
        outside = re.sub(r"\s+", " ", outside).strip()
        if outside and outside.lower() != text.lower() and len(outside) >= 2:
            candidates.append(outside)
            
        for b in bracket_matches:
            b_clean = re.sub(r"^[\s\-:_.,]+|[\s\-:_.,]+$", "", b).strip()
            b_clean = re.sub(r"\s+", " ", b_clean).strip()
            if b_clean and b_clean.lower() != text.lower() and len(b_clean) >= 2:
                candidates.append(b_clean)
            
    if has_latin_script(text) and has_indic_script(text):
        latin_only = re.sub(r"[^\x20-\x7E]+", " ", text)
        latin_only = re.sub(r"[\(\)\[\]\{\}]", " ", latin_only)
        latin_only = re.sub(r"^[\s\-:_.,]+|[\s\-:_.,]+$", "", latin_only).strip()
        latin_only = re.sub(r"\s+", " ", latin_only).strip()
        if latin_only and len(latin_only) >= 2 and latin_only.lower() != text.lower():
            candidates.append(latin_only)
            
        indic_only = "".join(ch for ch in text if ord(ch) > 127).strip()
        indic_only = re.sub(r"^[\s\-:_.,]+|[\s\-:_.,]+$", "", indic_only).strip()
        if indic_only and len(indic_only) >= 2 and indic_only.lower() != text.lower():
            candidates.append(indic_only)
            
    if not is_pure_year_bracket:
        no_year = re.sub(r"\b(19\d\d|20\d\d)\b", " ", text)
        no_year = re.sub(r"[\(\)\[\]\{\}]", " ", no_year)
        no_year = re.sub(r"^[\s\-:_.,]+|[\s\-:_.,]+$", "", no_year).strip()
        no_year = re.sub(r"\s+", " ", no_year).strip()
        if no_year and no_year.lower() != text.lower() and len(no_year) >= 2:
            candidates.append(no_year)
        
    seen = set()
    deduped = []
    for c in candidates:
        norm = c.lower()
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(c)
    return deduped

async def resolve_query_to_matches(
    query: str,
    max_pool: int = 150
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Intelligently resolve search query against MongoDB:
    1. First tries exact query as-is. If the exact query (including local language text)
       exists in the database, returns matching records immediately!
    2. If no matches, evaluates candidate variations (bracket stripped, Latin-only, year-stripped).
    3. If any candidate contains Indic script, attempts translation & phonetic transliteration to English.
    4. If still no matches, attempts multi-word token AND query.
    Returns (resolved_query_string, all_matches_list).
    """
    col = get_movies_collection()
    cleaned = query.strip()
    if not cleaned:
        return ("", [])
        
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
    
    # 1. Try exact query first (matches if exact local language or title is in DB)
    escaped_raw = re.escape(cleaned)
    raw_cursor = col.find({"title": {"$regex": escaped_raw, "$options": "i"}}, projection).limit(max_pool)
    raw_matches = await raw_cursor.to_list(length=max_pool)
    if raw_matches:
        raw_matches.sort(key=lambda d: parse_size_to_bytes(d.get("file_size")), reverse=True)
        return (cleaned, raw_matches)
        
    # 2. Generate structured candidates
    candidates = generate_query_candidates(cleaned)
    
    # 3. For any candidate containing Indic script, resolve English translation and transliteration
    indic_candidates = [c for c in candidates if has_indic_script(c)]
    for ic in indic_candidates:
        trans = await translate_indic_to_en(ic)
        if trans and trans.lower() not in [x.lower() for x in candidates]:
            candidates.append(trans)
        tl = transliterate_indic(ic)
        if tl and len(tl) >= 3 and tl.lower() not in [x.lower() for x in candidates]:
            candidates.append(tl)
            
    # 4. Search candidates in order (excluding candidate[0] which was checked in step 1)
    for cand in candidates[1:]:
        cand_clean = cand.strip()
        if not cand_clean or len(cand_clean) < 2:
            continue
        cand_escaped = re.escape(cand_clean)
        cand_cursor = col.find({"title": {"$regex": cand_escaped, "$options": "i"}}, projection).limit(max_pool)
        matches = await cand_cursor.to_list(length=max_pool)
        if matches:
            matches.sort(key=lambda d: parse_size_to_bytes(d.get("file_size")), reverse=True)
            return (cand_clean, matches)
            
    # 5. Multi-word AND matching (e.g. "Lucifer Malayalam" -> matches files containing both Lucifer and Malayalam)
    words = [w for w in re.split(r"[\s._\-:;,\(\)\[\]\{\}]+", cleaned) if len(w) >= 2]
    if len(words) > 1:
        and_filter = {"$and": [{"title": {"$regex": re.escape(w), "$options": "i"}} for w in words]}
        token_cursor = col.find(and_filter, projection).limit(max_pool)
        token_matches = await token_cursor.to_list(length=max_pool)
        if token_matches:
            token_matches.sort(key=lambda d: parse_size_to_bytes(d.get("file_size")), reverse=True)
            return (" ".join(words), token_matches)
            
    return (cleaned, [])

async def resolve_search_query(query: str) -> str:
    """Resolve a raw query to its matching database title (or cleaned query if not found)."""
    resolved_title, _ = await resolve_query_to_matches(query, max_pool=1)
    return resolved_title


async def get_series_availability(
    query: str,
    season: str = "ALL",
    quality: str = "ALL"
) -> Dict[str, Any]:
    """Fetch search results and run full series & quality availability analysis."""
    col = get_movies_collection()
    cleaned = query.strip()
    escaped = re.escape(cleaned)
    projection = {
        "_id": 1,
        "title": 1,
        "quality": 1,
        "season_episode": 1
    }
    cursor = col.find({"title": {"$regex": escaped, "$options": "i"}}, projection).limit(300)
    matches = await cursor.to_list(length=300)
    if not matches:
        _, matches = await resolve_query_to_matches(cleaned, max_pool=300)
    return analyze_series_availability(matches, season, quality)


async def record_user_request(
    user_id: int,
    username: str,
    title: str,
    details: Optional[str] = None
) -> Dict[str, Any]:
    """Record a user request for missing movie, series, season, or quality in MongoDB."""
    global db
    if db is None:
        raise RuntimeError("Database is not initialized.")
    req_col = db["requests"]
    doc = {
        "user_id": user_id,
        "username": username or f"User_{user_id}",
        "title": title.strip(),
        "details": details.strip() if details else "",
        "created_at": time.time(),
        "status": "pending"
    }
    result = await req_col.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_season_episodes(
    query: str,
    season: str,
    quality_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve all episodes belonging to a specific season, sorted in natural episode order."""
    col = get_movies_collection()
    cleaned = query.strip()
    escaped = re.escape(cleaned)
    filter_query = {
        "title": {
            "$regex": escaped,
            "$options": "i"
        }
    }
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
    cursor = col.find(filter_query, projection).limit(250)
    matches = await cursor.to_list(length=250)
    if not matches:
        _, matches = await resolve_query_to_matches(cleaned, max_pool=250)

    target_s = season.upper()
    season_matches = [m for m in matches if extract_season_from_doc(m) == target_s]

    if quality_filter and quality_filter.upper() != "ALL":
        q_target = quality_filter.upper()
        season_matches = [
            m for m in season_matches
            if q_target in (m.get("quality") or "").upper() or q_target in (m.get("title") or "").upper()
        ]

    season_matches.sort(key=parse_episode_num)
    return season_matches


async def search_movies(
    query: str,
    page: int = 1,
    page_size: int = 10,
    quality_filter: Optional[str] = None,
    season_filter: Optional[str] = None,
    return_qualities: bool = False,
    return_seasons: bool = False,
    return_resolved_query: bool = False
) -> Any:
    """
    Search for movies matching title using case-insensitive regex.
    Supports candidate resolution for bracketed text, Indic script transliteration/translation,
    and token matching. Results are sorted in descending order of file size (highest GB first).
    Supports optional filtering by quality tag and season tag.
    """
    col = get_movies_collection()
    cleaned_query = query.strip()
    if not cleaned_query:
        if return_resolved_query:
            if return_seasons:
                return ([], 0, 0, [], [], "")
            return ([], 0, 0, [], "") if return_qualities else ([], 0, 0, "")
        if return_seasons:
            return ([], 0, 0, [], [])
        return ([], 0, 0, []) if return_qualities else ([], 0, 0)

    cache_key = cleaned_query.lower()
    now = time.time()
    all_matches = None
    resolved_query = cleaned_query

    # Check hot cache first (0.0001s lookup)
    if cache_key in _db_search_cache:
        cached_ts, cached_matches = _db_search_cache[cache_key]
        if (now - cached_ts) < SEARCH_CACHE_TTL:
            all_matches = list(cached_matches)

    if all_matches is None:
        MAX_SEARCH_POOL = 150
        resolved_query, all_matches = await resolve_query_to_matches(cleaned_query, max_pool=MAX_SEARCH_POOL)

        if not all_matches:
            if return_resolved_query:
                if return_seasons:
                    return ([], 0, 0, [], [], cleaned_query)
                return ([], 0, 0, [], cleaned_query) if return_qualities else ([], 0, 0, cleaned_query)
            if return_seasons:
                return ([], 0, 0, [], [])
            return ([], 0, 0, []) if return_qualities else ([], 0, 0)

        # Update cache for both the input query and the resolved query
        if len(_db_search_cache) >= SEARCH_CACHE_MAX:
            _db_search_cache.pop(next(iter(_db_search_cache)))
        _db_search_cache[cache_key] = (now, list(all_matches))
        if resolved_query.lower() != cache_key:
            _db_search_cache[resolved_query.lower()] = (now, list(all_matches))

    available_qualities = extract_available_qualities(all_matches)
    available_seasons = extract_available_seasons(all_matches)

    # Apply season filter if requested and not "ALL"
    filtered_matches = all_matches
    if season_filter and season_filter.upper() != "ALL":
        s_target = season_filter.upper()
        filtered_matches = [
            m for m in filtered_matches
            if extract_season_from_doc(m) == s_target
        ]

    # Apply quality filter if requested and not "ALL"
    if quality_filter and quality_filter.upper() != "ALL":
        q_target = quality_filter.upper()
        filtered_matches = [
            m for m in filtered_matches
            if q_target in (m.get("quality") or "").upper() or q_target in (m.get("title") or "").upper()
        ]

    total_count = len(filtered_matches)
    if total_count == 0:
        if return_resolved_query:
            if return_seasons:
                return ([], 0, 0, available_qualities, available_seasons, resolved_query)
            return ([], 0, 0, available_qualities, resolved_query) if return_qualities else ([], 0, 0, resolved_query)
        if return_seasons:
            return ([], 0, 0, available_qualities, available_seasons)
        return ([], 0, 0, available_qualities) if return_qualities else ([], 0, 0)

    total_pages = (total_count + page_size - 1) // page_size
    page = max(1, min(page, total_pages))

    skip = (page - 1) * page_size
    results = filtered_matches[skip : skip + page_size]

    if return_resolved_query:
        if return_seasons:
            return results, total_count, total_pages, available_qualities, available_seasons, resolved_query
        if return_qualities:
            return results, total_count, total_pages, available_qualities, resolved_query
        return results, total_count, total_pages, resolved_query

    if return_seasons:
        return results, total_count, total_pages, available_qualities, available_seasons
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
    message_id: Optional[int] = None,
    year: Optional[int] = None,
    language: Optional[str] = None
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
      year: Optional int
      language: Optional str
    """
    col = get_movies_collection()
    doc = {
        "title": title.strip(),
        "season_episode": season_episode.strip() if season_episode else None,
        "quality": quality.strip(),
        "file_size": file_size.strip(),
        "file_id": file_id.strip() if file_id else "",
        "channel_id": channel_id,
        "message_id": message_id,
        "year": year,
        "language": language.strip() if language else None
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
