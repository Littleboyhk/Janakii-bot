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
            timeout=3.5,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0)
        )
    return _http_client


def clean_query_for_meta(raw_query: str) -> str:
    """Strip release tags, years, brackets, and file markers for accurate movie lookup."""
    text = raw_query.strip()
    # Remove common file extensions
    for ext in [".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v"]:
        if text.lower().endswith(ext):
            text = text[:-len(ext)]
            break

    # If query has brackets e.g. "Lucifer (ലൂസിഫർ)", extract the primary English part if present
    bracket_match = re.search(r"^([^\(\[\{]+)[\(\[\{]", text)
    if bracket_match:
        outside = bracket_match.group(1).strip()
        if re.search(r"[a-zA-Z0-9]", outside) and len(outside) >= 2:
            text = outside

    # Remove channel handles e.g. @CC New Lucifer -> Lucifer
    text = re.sub(r"@\w+", " ", text)

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
    Query Cinemeta for metadata (posters, ratings, trailers, synopsis).
    Queries catalog with a fast 5.0s timeout.
    """
    clean_title = clean_query_for_meta(raw_title)
    cache_key = clean_title.lower()

    if cache_key in _meta_cache and _meta_cache[cache_key] is not None:
        return _meta_cache[cache_key]

    meta = None
    client = get_http_client()
    try:
        meta = await asyncio.wait_for(_lookup_meta(client, clean_title), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("Metadata lookup timed out or failed for '%s': %s", clean_title, e)
        meta = None

    if meta is not None:
        if len(_meta_cache) >= CACHE_MAX_SIZE:
            _meta_cache.pop(next(iter(_meta_cache)))
        _meta_cache[cache_key] = meta

    return meta


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


async def _lookup_meta(client: httpx.AsyncClient, clean_title: str) -> Optional[Dict[str, Any]]:
    """Execute movie & series catalog search concurrently, choosing the best match."""
    movie_task = asyncio.create_task(_search_catalog(client, "movie", clean_title))
    series_task = asyncio.create_task(_search_catalog(client, "series", clean_title))
    movie_meta, series_meta = await asyncio.gather(movie_task, series_task)

    if series_meta and movie_meta:
        norm_query = _normalize_for_match(clean_title)
        s_norm = _normalize_for_match(series_meta.get("title") or "")
        m_norm = _normalize_for_match(movie_meta.get("title") or "")

        # If series title is an exact match and movie is not, pick series
        if s_norm == norm_query and m_norm != norm_query:
            return series_meta
        # If movie title is an exact match and series is not, pick movie
        if m_norm == norm_query and s_norm != norm_query:
            return movie_meta
        # If series has rating/trailer and movie doesn't, pick series
        if (series_meta.get("rating") or series_meta.get("trailer_url")) and not (movie_meta.get("rating") or movie_meta.get("trailer_url")):
            return series_meta
        return movie_meta

    return movie_meta or series_meta


async def _search_catalog(client: httpx.AsyncClient, cat_type: str, title: str) -> Optional[Dict[str, Any]]:
    """Query catalog endpoint and fetch details if available within tight deadline."""
    import urllib.parse
    search_url = f"https://v3-cinemeta.strem.io/catalog/{cat_type}/top/search={urllib.parse.quote(title)}.json"
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
        trailer_url = None
        imdb_url = None
        cast = []
        director = None

        if imdb_id and str(imdb_id).startswith("tt"):
            imdb_url = f"https://www.imdb.com/title/{imdb_id}/"

        # Try rapid detail view for rating, genres, plot, trailers, cast
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

                    # Extract YouTube Trailer stream / source
                    for ts in d_meta.get("trailerStreams", []):
                        if isinstance(ts, dict) and ts.get("ytId"):
                            trailer_url = f"https://www.youtube.com/watch?v={ts['ytId']}"
                            break
                    if not trailer_url:
                        for t in d_meta.get("trailers", []):
                            if isinstance(t, dict) and t.get("source") and t.get("type", "").lower() == "trailer":
                                trailer_url = f"https://www.youtube.com/watch?v={t['source']}"
                                break

                    # Extract cast & director
                    d_cast = d_meta.get("cast", [])
                    if isinstance(d_cast, list) and d_cast:
                        cast = d_cast[:3]
                    d_dir = d_meta.get("director", [])
                    if isinstance(d_dir, list) and d_dir:
                        director = d_dir[0]
            except Exception:
                pass

        # Fallback YouTube trailer search URL if direct video ID not found
        if not trailer_url and (name or title):
            import urllib.parse
            q_str = f"{name or title} {year or ''} official trailer".strip()
            trailer_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q_str)}"

        return {
            "title": name,
            "year": year,
            "rating": str(rating) if rating else None,
            "genres": genres,
            "runtime": runtime,
            "poster": poster,
            "plot": plot,
            "trailer_url": trailer_url,
            "imdb_url": imdb_url,
            "imdb_id": imdb_id,
            "cast": cast,
            "director": director,
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
    active_filter: str = "ALL",
    active_season: str = "ALL",
    availability: Optional[Dict[str, Any]] = None
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

        cast = meta.get("cast")
        if cast:
            lines.append(f"👥 *Cast:* {', '.join(cast)}")

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
    filter_tags = []
    if active_season != "ALL":
        filter_tags.append(active_season)
    if active_filter != "ALL":
        filter_tags.append(active_filter)
    filter_label = f" [{' • '.join(filter_tags)}]" if filter_tags else ""

    item_type = "episode(s)" if (meta and meta.get("is_series")) or (active_season != "ALL") else "file(s)"
    lines.append(f"📊 **{total_count}** {item_type} available{filter_label} • Page {current_page}/{total_pages}")

    # Availability & Missing Status Details
    if availability and availability.get("is_series"):
        missing_eps = availability.get("missing_episodes", [])
        missing_in_q = availability.get("missing_in_active_q", [])
        alt_q = availability.get("alt_qualities", [])
        q_map = availability.get("quality_map", {})

        # Show quality distribution if viewing all qualities
        if q_map and len(q_map) > 1 and active_filter == "ALL":
            q_parts = [f"`{k}` ({v})" for k, v in sorted(q_map.items())]
            lines.append(f"📁 **Qualities:** {' • '.join(q_parts)}")

        # If filtered by quality, inform user if missing episodes are present in other qualities
        if missing_in_q and alt_q:
            alt_str = ", ".join(alt_q)
            lines.append(f"💡 *{', '.join(missing_in_q)} missing in {active_filter}, but available in {alt_str}!*")

        # Missing episodes from the season sequence
        if missing_eps:
            lines.append(f"⚠️ **Missing Episode(s):** {', '.join(missing_eps)} *(Not in database)*")

    lines.append("👇 *Tap a file below to download:*")

    return "\n".join(lines)
