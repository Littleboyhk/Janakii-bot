"""
Unit Tests for AI Metadata Parser & Fallback Mechanisms
======================================================
Tests:
1. Fallback behavior when API key is missing or AI is disabled
2. In-memory caching
3. Clean regex extraction on TV series and movies
4. Batch parsing structure & ID preservation
5. Simulated Gemini API structured JSON response handling
"""

import json
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

import config
import ai_parser


class TestAIMetadataParser(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Clear cache before each test
        ai_parser._ai_parse_cache.clear()

    async def test_01_fallback_when_no_api_key(self):
        """When GEMINI_API_KEY is empty, fallback parser must execute gracefully without crashing."""
        with patch.object(config, "GEMINI_API_KEY", ""), patch.object(config, "ENABLE_AI_INGESTION", True):
            raw = "Spider-Man.Across.the.Spider-Verse.2023.1080p.BluRay.x265.mkv"
            result = await ai_parser.parse_media_metadata_ai(raw)

            self.assertIsNotNone(result)
            self.assertEqual(result["is_ai"], False)
            self.assertIn("Spider", result["title"])
            self.assertIn("1080p", result["quality"])

    async def test_02_tv_series_season_episode_detection(self):
        """Verify season and episode codes (e.g. S03E05) are captured correctly."""
        with patch.object(config, "GEMINI_API_KEY", ""):
            raw = "Mirzapur S03E05 720p 10bit AMZN WEBRip.mkv"
            result = await ai_parser.parse_media_metadata_ai(raw)

            self.assertEqual(result["title"], "Mirzapur")
            self.assertEqual(result["season_episode"], "S03E05")
            self.assertIn("720p", result["quality"])

    async def test_03_in_memory_cache(self):
        """Verify that repeated queries hit the in-memory LRU cache."""
        raw = "The.Batman.2022.1080p.WEBRip.x264.mkv"
        res1 = await ai_parser.parse_media_metadata_ai(raw)
        cache_key = raw.strip().lower()

        self.assertIn(cache_key, ai_parser._ai_parse_cache)
        res2 = await ai_parser.parse_media_metadata_ai(raw)
        self.assertEqual(res1, res2)

    async def test_04_batch_fallback_preserves_order_and_ids(self):
        """Batch parsing in fallback mode should preserve item IDs and correctly parse all entries."""
        items = [
            {"id": 101, "raw_text": "Inception.2010.1080p.BluRay.mkv", "caption": ""},
            {"id": 102, "raw_text": "Breaking.Bad.S05E14.720p.mkv", "caption": ""},
            {"id": 103, "raw_text": "Interstellar.2014.2160p.UHD.mkv", "caption": ""}
        ]
        with patch.object(config, "GEMINI_API_KEY", ""):
            results = await ai_parser.parse_media_metadata_ai_batch(items)

            self.assertEqual(len(results), 3)
            self.assertEqual(results[0]["item_id"], 101)
            self.assertIn("Inception", results[0]["title"])
            self.assertEqual(results[1]["item_id"], 102)
            self.assertEqual(results[1]["season_episode"], "S05E14")
            self.assertEqual(results[2]["item_id"], 103)
            self.assertIn("Interstellar", results[2]["title"])

    async def test_05_mocked_gemini_api_success(self):
        """Simulate a successful Gemini API response returning rich metadata."""
        mock_response_json = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "title": "Oppenheimer",
                                    "year": 2023,
                                    "season_episode": None,
                                    "quality": "1080p IMAX WEB-DL",
                                    "language": "Dual Audio"
                                })
                            }
                        ]
                    }
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_response_json

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        with patch.object(config, "GEMINI_API_KEY", "dummy_gemini_key"), \
             patch.object(config, "ENABLE_AI_INGESTION", True), \
             patch.object(ai_parser, "get_http_client", return_value=mock_client):

            raw = "[TG_Uploads] Oppen.2023.IMAX.1080p.Dual.x265-ESub.mkv"
            result = await ai_parser.parse_media_metadata_ai(raw)

            self.assertTrue(result["is_ai"])
            self.assertEqual(result["title"], "Oppenheimer")
            self.assertEqual(result["year"], 2023)
            self.assertIsNone(result["season_episode"])
            self.assertEqual(result["quality"], "1080p IMAX WEB-DL")
            self.assertEqual(result["language"], "Dual Audio")


if __name__ == "__main__":
    unittest.main()
