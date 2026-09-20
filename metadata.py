"""
Metadata Engine for Movie & TV Series
======================================
Fetches official posters, IMDb ratings, genres, release year, runtime, and
plot synopses using Cinemeta's free open metadata API (no API key required).
Includes an in-memory LRU cache to optimize latency.
"""

import asyncio
import re
import logging
from typing import Dict, Any, Optional, List
import httpx

logger = logging.getLogger("MetadataEngine")

# In-memory cache: {cleaned_query: meta_dict}
_meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
CACHE_MAX_SIZE = 500

# Persistent HTTP client with keep-alive connection pooling
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared persistent AsyncClient with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=0.8,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0)
        )
    return _http_client


def clean_query_for_meta(raw_query: str) -> str:
    """Strip release tags, years, and file markers for accurate movie lookup."""
    text = raw_query.strip()
    # Remove common file extensions
    for ext in [".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"]:
        if text.lower().endswith(ext):
            text = text[:-len(ext)]
            break

    # Remove season/episode tags (e.g. S01E01, Season 1)
    text = re.sub(r"(?i)\b(S\d{1,2}\s?[E|x]\d{1,2}|Season\s?\d{1,2}\s?Episode\s?\d{1,2}|E\d{1,3})\b", "", text)
    # Remove quality tags
    text = re.sub(
        r"(?i)\b(4K|2160p|1080p|720p|480p|360p|BluRay|BRRip|WEB-DL|WEBRip|HDR|REMUX|HDTC|DVDRip|10bit|HEVC|x265|x264|DDP5\.1|AAC|ESub)\b",
        "",
        text
    )
    # Remove release years in brackets/parentheses
    text = re.sub(r"[\(\[]\d{4}[\)\]]", "", text)
    # Clean non-alphanumeric punctuation
    text = re.sub(r"[@_.\-+]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or raw_query.strip()


async def fetch_movie_meta(raw_title: str) -> Optional[Dict[str, Any]]:
    """
    Query Cinemeta for metadata with sub-second execution guarantee.
    Enforces a strict 350ms deadline so user searches are never delayed.
    """
    clean_title = clean_query_for_meta(raw_title)
    cache_key = clean_title.lower()

    if cache_key in _meta_cache:
        return _meta_cache[cache_key]

    meta = None
    client = get_http_client()
    try:
        # Enforce 350ms maximum ceiling for metadata lookup
        meta = await asyncio.wait_for(_lookup_meta(client, clean_title), timeout=0.35)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("Metadata lookup timed out or failed for '%s': %s", clean_title, e)
        meta = None

    # Manage cache size
    if len(_meta_cache) >= CACHE_MAX_SIZE:
        _meta_cache.pop(next(iter(_meta_cache)))

    _meta_cache[cache_key] = meta
    return meta


async def _lookup_meta(client: httpx.AsyncClient, clean_title: str) -> Optional[Dict[str, Any]]:
    """Execute movie catalog search, falling back to series catalog if no match."""
    meta = await _search_catalog(client, "movie", clean_title)
    if not meta:
        meta = await _search_catalog(client, "series", clean_title)
    return meta


async def _search_catalog(client: httpx.AsyncClient, cat_type: str, title: str) -> Optional[Dict[str, Any]]:
    """Query catalog endpoint and fetch details if available within tight deadline."""
    search_url = f"https://v3-cinemeta.strem.io/catalog/{cat_type}/top/search={title}.json"
    try:
        resp = await client.get(search_url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        metas = data.get("metas", [])
        if not metas:
            return None

        # Take primary match
        match = metas[0]
        imdb_id = match.get("id")

        # Extract primary data immediately from fast catalog endpoint (~45ms)
        name = match.get("name") or title
        year = match.get("releaseInfo") or match.get("year")
        poster = match.get("poster")
        if poster and not poster.startswith("http"):
            poster = None

        rating = None
        genres = []
        runtime = None
        plot = None

        # Try rapid detail view for rating, genres, plot
        if imdb_id:
            try:
                detail_url = f"https://v3-cinemeta.strem.io/meta/{cat_type}/{imdb_id}.json"
                d_resp = await client.get(detail_url)
                if d_resp.status_code == 200:
                    d_meta = d_resp.json().get("meta", {})
                    rating = d_meta.get("imdbRating")
                    d_genres = d_meta.get("genres", [])
                    if isinstance(d_genres, list):
                        genres = d_genres[:3]
                    runtime = d_meta.get("runtime")
                    plot = d_meta.get("description")
                    if not poster:
                        poster = d_meta.get("poster")
            except Exception:
                pass

        return {
            "title": name,
            "year": year,
            "rating": str(rating) if rating else None,
            "genres": genres,
            "runtime": runtime,
            "poster": poster,
            "plot": plot,
            "is_series": (cat_type == "series")
        }
    except Exception as e:
        logger.debug("Cinemeta lookup failed for '%s' (%s): %s", title, cat_type, e)
        return None


def format_netflix_card(
    meta: Optional[Dict[str, Any]],
    query: str,
    total_count: int,
    current_page: int,
    total_pages: int,
    active_filter: str = "ALL"
) -> str:
    """Format a rich Netflix-style movie info card in Markdown."""
    lines = []

    if meta:
        title_str = meta.get("title", query.title())
        year_str = f" ({meta.get('year')})" if meta.get("year") else ""
        type_badge = "📺 Series" if meta.get("is_series") else "🎬 Movie"
        lines.append(f"**{title_str}{year_str}** • {type_badge}")

        info_parts = []
        if meta.get("rating"):
            info_parts.append(f"⭐ **{meta.get('rating')}/10**")
        if meta.get("runtime"):
            info_parts.append(f"⏱ `{meta.get('runtime')}`")
        if info_parts:
            lines.append(" • ".join(info_parts))

        if meta.get("genres"):
            genre_text = ", ".join(meta.get("genres"))
            lines.append(f"🎭 *{genre_text}*")

        plot = meta.get("plot")
        if plot:
            # Truncate plot for clean card layout
            clean_plot = plot.strip().replace("\n", " ")
            if len(clean_plot) > 160:
                clean_plot = clean_plot[:157] + "..."
            lines.append(f"\n📖 _{clean_plot}_")
    else:
        lines.append(f"🎬 **{query.title()}**")

    # Divider & count summary
    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    filter_label = f" [{active_filter}]" if active_filter != "ALL" else ""
    lines.append(f"📊 **{total_count}** file(s) available{filter_label} • Page {current_page}/{total_pages}")
    lines.append("👇 *Tap a file below to download:*")

    return "\n".join(lines)
