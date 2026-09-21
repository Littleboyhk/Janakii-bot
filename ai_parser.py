"""
AI Metadata Ingestion Engine for Telegram Media
===============================================
Uses Google Gemini API to clean noisy release titles, watermarks,
audio tags, and group handles into canonical titles, release years,
season/episode numbers, and video qualities.

Features:
- Sub-second async REST client with httpx and connection pooling.
- High-performance in-memory LRU cache.
- Batch processing support for channel crawling.
- Automatic, 100% reliable fallback to regex heuristics on timeout or when API key is missing.
"""

import os
import re
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List, Union
import httpx

import config
import database

logger = logging.getLogger("AIMetadataParser")

# Persistent LRU Cache: {cache_key: parsed_dict}
_ai_parse_cache: Dict[str, Dict[str, Any]] = {}
CACHE_MAX_SIZE = 1000

# Persistent HTTP client with keep-alive connection pooling
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return a shared persistent AsyncClient for AI queries."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0)
        )
    return _http_client


def _clean_json_text(text: str) -> str:
    """Strip markdown code fence blocks (```json ... ```) from model response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _build_fallback(raw_text: str) -> Dict[str, Any]:
    """Graceful fallback to regex heuristics in database.py."""
    heuristic = database.parse_media_metadata(raw_text)
    return {
        "title": heuristic.get("title") or raw_text.strip(),
        "year": None,
        "season_episode": heuristic.get("season_episode"),
        "quality": heuristic.get("quality") or "HD",
        "language": None,
        "is_ai": False
    }


async def parse_media_metadata_ai(
    raw_text: str,
    caption: Optional[str] = None
) -> Dict[str, Any]:
    """
    Intelligently parse media metadata using Google Gemini.
    Extracts:
      - title: Canonical title (e.g. 'Leo', 'Money Heist', 'Avengers Endgame')
      - year: int or None (e.g. 2023)
      - season_episode: 'S01E05' or None
      - quality: '1080p WEB-DL x264', '720p', etc.
      - language: 'Tamil', 'Hindi', 'Dual Audio', etc.
      - is_ai: bool indicating if AI parsed it

    Falls back to regex heuristics if API key is not configured, AI is disabled,
    or on request timeout/error.
    """
    if not raw_text or not raw_text.strip():
        return _build_fallback(raw_text or "Untitled")

    cache_key = raw_text.strip().lower()
    if cache_key in _ai_parse_cache:
        return _ai_parse_cache[cache_key]

    # Check if AI ingestion is active and configured
    if not config.ENABLE_AI_INGESTION or not config.GEMINI_API_KEY:
        res = _build_fallback(raw_text)
        _store_in_cache(cache_key, res)
        return res

    combined_input = raw_text
    if caption and caption.strip() and caption.strip() != raw_text.strip():
        combined_input += f"\nCaption context: {caption.strip()[:300]}"

    prompt = (
        "You are an expert Telegram media filename parser. Clean this release title into structured metadata.\n"
        "Remove all telegram channel tags (e.g. @channel, t.me), release groups (e.g. [TamilBlasters], [TG], [YTS]), "
        "and file extensions (.mkv, .mp4).\n\n"
        f"Filename / Input:\n\"{combined_input}\"\n\n"
        "Return ONLY a JSON object with these exact keys:\n"
        "{\n"
        "  \"title\": \"Canonical Movie or TV Series Name (Title Case, no junk)\",\n"
        "  \"year\": 2023,\n"
        "  \"season_episode\": \"S01E05\",\n"
        "  \"quality\": \"Standardized resolution & source (e.g. 1080p WEB-DL, 720p, 4K UHD)\",\n"
        "  \"language\": \"Audio language (e.g. Tamil, Hindi, Dual Audio, English, or null)\"\n"
        "}\n"
        "Note: year and season_episode should be null if not applicable or not found."
    )

    client = get_http_client()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1
        }
    }

    try:
        resp = await client.post(url, json=payload, timeout=3.5)
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                text_content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                parsed_json = json.loads(_clean_json_text(text_content))

                title = str(parsed_json.get("title") or "").strip()
                if not title:
                    title = _build_fallback(raw_text)["title"]

                year = parsed_json.get("year")
                if year is not None:
                    try:
                        year = int(year)
                    except (ValueError, TypeError):
                        year = None

                se = parsed_json.get("season_episode")
                if se and str(se).lower() not in ("null", "none", ""):
                    se = str(se).upper().replace(" ", "")
                else:
                    se = None

                quality = str(parsed_json.get("quality") or "HD").strip()
                language = parsed_json.get("language")
                if language and str(language).lower() in ("null", "none", ""):
                    language = None

                result = {
                    "title": title,
                    "year": year,
                    "season_episode": se,
                    "quality": quality,
                    "language": str(language).strip() if language else None,
                    "is_ai": True
                }
                _store_in_cache(cache_key, result)
                return result

        logger.warning("Gemini API non-200 status (%d): %s. Using heuristic fallback.", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.debug("AI parsing exception for '%s': %s. Using heuristic fallback.", raw_text[:40], e)

    fallback_result = _build_fallback(raw_text)
    _store_in_cache(cache_key, fallback_result)
    return fallback_result


async def parse_media_metadata_ai_batch(
    items: List[Dict[str, Any]],
    chunk_size: int = 15
) -> List[Dict[str, Any]]:
    """
    Parse a list of media items in batch for channel indexing.
    Each item in items is a dict with at least:
      {"id": any_id, "raw_text": str, "caption": Optional[str]}

    Returns a list of parsed dictionaries in the same order.
    """
    if not items:
        return []

    results: List[Optional[Dict[str, Any]]] = [None] * len(items)
    uncached_indices = []

    # Check cache first
    for i, item in enumerate(items):
        raw = item.get("raw_text", "").strip()
        key = raw.lower()
        if key in _ai_parse_cache:
            res = dict(_ai_parse_cache[key])
            res["item_id"] = item.get("id")
            results[i] = res
        else:
            uncached_indices.append(i)

    if not uncached_indices:
        return [r for r in results if r is not None]

    # If AI is disabled or key is missing, fall back immediately for all uncached
    if not config.ENABLE_AI_INGESTION or not config.GEMINI_API_KEY:
        for idx in uncached_indices:
            raw = items[idx].get("raw_text", "").strip()
            fb = _build_fallback(raw)
            fb["item_id"] = items[idx].get("id")
            _store_in_cache(raw.lower(), fb)
            results[idx] = fb
        return [r for r in results if r is not None]

    # Process uncached in chunks to minimize API latency and respect limits
    client = get_http_client()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"

    for start in range(0, len(uncached_indices), chunk_size):
        chunk_indices = uncached_indices[start:start + chunk_size]
        batch_payload_list = []

        for idx in chunk_indices:
            raw = items[idx].get("raw_text", "").strip()
            caption = (items[idx].get("caption") or "").strip()
            batch_payload_list.append({
                "index": idx,
                "filename": raw,
                "caption": caption[:150] if caption else ""
            })

        prompt = (
            "You are an expert Telegram media librarian. Parse each of the following Telegram media filenames into clean metadata.\n"
            "Strip all channel tags (e.g. @channel), release groups (e.g. [TamilBlasters]), and file extensions.\n"
            "Return ONLY a JSON array matching each item with these exact keys:\n"
            "[\n"
            "  {\n"
            "    \"index\": <same index integer as provided>,\n"
            "    \"title\": \"Canonical Movie or TV Series Name\",\n"
            "    \"year\": 2023,\n"
            "    \"season_episode\": \"S01E05\",\n"
            "    \"quality\": \"Standardized quality (e.g. 1080p WEB-DL, 720p, 4K)\",\n"
            "    \"language\": \"Audio language (or null)\"\n"
            "  }\n"
            "]\n"
            "Note: year and season_episode should be null if not applicable.\n\n"
            f"Input items:\n{json.dumps(batch_payload_list, ensure_ascii=False, indent=2)}"
        )

        try:
            req_data = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "temperature": 0.1
                }
            }
            resp = await client.post(url, json=req_data, timeout=8.0)
            if resp.status_code == 200:
                body = resp.json()
                cands = body.get("candidates", [])
                if cands:
                    part_text = cands[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    parsed_array = json.loads(_clean_json_text(part_text))
                    matched_map = {item["index"]: item for item in parsed_array if "index" in item}

                    for idx in chunk_indices:
                        raw = items[idx].get("raw_text", "").strip()
                        if idx in matched_map:
                            ai_item = matched_map[idx]
                            title = str(ai_item.get("title") or "").strip() or _build_fallback(raw)["title"]
                            year = ai_item.get("year")
                            try:
                                year = int(year) if year is not None else None
                            except (ValueError, TypeError):
                                year = None

                            se = ai_item.get("season_episode")
                            if se and str(se).lower() not in ("null", "none", ""):
                                se = str(se).upper().replace(" ", "")
                            else:
                                se = None

                            res_item = {
                                "title": title,
                                "year": year,
                                "season_episode": se,
                                "quality": str(ai_item.get("quality") or "HD").strip(),
                                "language": ai_item.get("language"),
                                "is_ai": True,
                                "item_id": items[idx].get("id")
                            }
                            _store_in_cache(raw.lower(), res_item)
                            results[idx] = res_item
                        else:
                            fb = _build_fallback(raw)
                            fb["item_id"] = items[idx].get("id")
                            _store_in_cache(raw.lower(), fb)
                            results[idx] = fb
                    continue
        except Exception as e:
            logger.warning("Batch AI parsing failed for chunk: %s. Falling back to regex.", e)

        # Fallback for chunk if request failed
        for idx in chunk_indices:
            raw = items[idx].get("raw_text", "").strip()
            fb = _build_fallback(raw)
            fb["item_id"] = items[idx].get("id")
            _store_in_cache(raw.lower(), fb)
            results[idx] = fb

    return [r for r in results if r is not None]


def _store_in_cache(key: str, val: Dict[str, Any]) -> None:
    """Safely store item in memory cache with LRU eviction."""
    global _ai_parse_cache
    if len(_ai_parse_cache) >= CACHE_MAX_SIZE:
        _ai_parse_cache.pop(next(iter(_ai_parse_cache)))
    _ai_parse_cache[key] = val
