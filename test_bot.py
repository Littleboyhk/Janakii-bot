"""
Automated Test Suite for Movie & Series Librarian Bot
=====================================================
Validates:
1. Schema & Data Model
2. Case-insensitive Regex Fuzzy Search
3. Pagination Math & Boundary Conditions
4. Button Text Format: [File Size] Title Quality
5. Rate Limiting Mechanism
6. Inline Keyboard Generation
"""

import asyncio
import unittest
from mongomock_motor import AsyncMongoMockClient

import database
import bot
import config


class TestMovieLibrarianBot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Use an in-memory mock MongoDB client
        self.mock_client = AsyncMongoMockClient()
        self.mock_db = self.mock_client["test_movie_db"]
        database.client = self.mock_client
        database.db = self.mock_db
        database.movies_collection = self.mock_db["movies"]

    async def asyncTearDown(self):
        await database.movies_collection.drop()

    async def test_01_insert_and_retrieve_movie(self):
        """Test inserting a movie/series document adhering to the required schema."""
        movie_id = await database.insert_movie(
            title="Ultimate Spider-Man",
            file_id="BQACAgUAAxkBAAI_TestFileId",
            quality="1080p JHS DUA",
            file_size="890.19 MB",
            season_episode="S01E21"
        )
        self.assertIsNotNone(movie_id)

        retrieved = await database.get_movie_by_id(movie_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["title"], "Ultimate Spider-Man")
        self.assertEqual(retrieved["season_episode"], "S01E21")
        self.assertEqual(retrieved["quality"], "1080p JHS DUA")
        self.assertEqual(retrieved["file_size"], "890.19 MB")
        self.assertEqual(retrieved["file_id"], "BQACAgUAAxkBAAI_TestFileId")

    async def test_02_button_text_formatting(self):
        """
        Verify button text format strictly matches:
        [File Size] Title Quality (e.g. [890.19 MB] Ultimate Spider-Man S01E21 1080p)
        """
        # Case A: TV Series with season_episode
        tv_series = {
            "title": "Ultimate Spider-Man",
            "season_episode": "S01E21",
            "quality": "1080p JHS DUA",
            "file_size": "890.19 MB",
            "file_id": "file_1"
        }
        btn_series = database.format_movie_button_text(tv_series)
        self.assertEqual(btn_series, "[890.19 MB] Ultimate Spider-Man S01E21 1080p JHS DUA")

        # Case B: Movie without season_episode
        movie = {
            "title": "Inception",
            "season_episode": None,
            "quality": "1080p BluRay",
            "file_size": "2.10 GB",
            "file_id": "file_2"
        }
        btn_movie = database.format_movie_button_text(movie)
        self.assertEqual(btn_movie, "[2.10 GB] Inception 1080p BluRay")

    async def test_03_case_insensitive_fuzzy_search(self):
        """Test that searching is case-insensitive and supports fuzzy/partial matches."""
        await database.insert_movie(
            title="Spider-Man: Across the Spider-Verse",
            file_id="id_1",
            quality="1080p",
            file_size="2.4 GB"
        )
        await database.insert_movie(
            title="The Amazing Spider-Man",
            file_id="id_2",
            quality="720p",
            file_size="1.1 GB"
        )
        await database.insert_movie(
            title="Batman Begins",
            file_id="id_3",
            quality="1080p",
            file_size="1.8 GB"
        )

        # Lowercase search
        results, count, pages = await database.search_movies("spider-man")
        self.assertEqual(count, 2)
        self.assertEqual(len(results), 2)

        # Uppercase search
        results, count, pages = await database.search_movies("SPIDER")
        self.assertEqual(count, 2)

        # Partial search
        results, count, pages = await database.search_movies("batman")
        self.assertEqual(count, 1)
        self.assertEqual(results[0]["title"], "Batman Begins")

        # Search with special characters (safe regex escape)
        results, count, pages = await database.search_movies("Spider-Man (2023)")
        self.assertEqual(count, 0)  # Should not throw invalid regex syntax error

    async def test_04_pagination_logic(self):
        """Verify 10 results per page and pagination navigation."""
        # Insert 25 episodes
        for i in range(1, 26):
            await database.insert_movie(
                title="Bleach: Thousand-Year Blood War",
                file_id=f"bleach_file_{i}",
                quality="1080p",
                file_size="650 MB",
                season_episode=f"E{i:02d}"
            )

        # Page 1: 10 items
        p1_res, total_count, total_pages = await database.search_movies("Bleach", page=1, page_size=10)
        self.assertEqual(total_count, 25)
        self.assertEqual(total_pages, 3)
        self.assertEqual(len(p1_res), 10)
        self.assertEqual(p1_res[0]["season_episode"], "E01")
        self.assertEqual(p1_res[9]["season_episode"], "E10")

        # Page 2: 10 items
        p2_res, _, _ = await database.search_movies("Bleach", page=2, page_size=10)
        self.assertEqual(len(p2_res), 10)
        self.assertEqual(p2_res[0]["season_episode"], "E11")

        # Page 3: 5 items
        p3_res, _, _ = await database.search_movies("Bleach", page=3, page_size=10)
        self.assertEqual(len(p3_res), 5)
        self.assertEqual(p3_res[4]["season_episode"], "E25")

    async def test_05_keyboard_builder(self):
        """Test inline keyboard structure and callback data length under 64 bytes."""
        movies = []
        for i in range(1, 4):
            m_id = await database.insert_movie(
                title=f"Sample Movie Title Number {i}",
                file_id=f"fid_{i}",
                quality="1080p",
                file_size="1.2 GB"
            )
            doc = await database.get_movie_by_id(m_id)
            movies.append(doc)

        markup = bot.build_search_keyboard(results=movies, current_page=1, total_pages=2)
        # Should have 3 movie rows + 1 navigation row = 4 rows
        self.assertEqual(len(markup.inline_keyboard), 4)

        # Check file download callback_data length
        movie_btn = markup.inline_keyboard[0][0]
        self.assertTrue(movie_btn.callback_data.startswith("dl:"))
        self.assertLess(len(movie_btn.callback_data), 64)

        # Check navigation row
        nav_row = markup.inline_keyboard[3]
        self.assertEqual(len(nav_row), 2)  # Current page indicator + Next >> (no Prev on page 1)
        self.assertEqual(nav_row[0].text, "1/2")
        self.assertEqual(nav_row[1].text, "Next >>")
        self.assertEqual(nav_row[1].callback_data, "page_2")

    def test_06_rate_limiter(self):
        """Verify that searches under cooldown are blocked."""
        user_id = 999888
        config.SEARCH_COOLDOWN_SECONDS = 3.0
        bot.user_last_search.clear()

        # First search should be allowed
        allowed1, _ = bot.check_rate_limit(user_id)
        self.assertTrue(allowed1)

        # Immediate second search should be blocked
        allowed2, wait = bot.check_rate_limit(user_id)
        self.assertFalse(allowed2)
        self.assertGreater(wait, 0.0)

    async def test_07_netflix_quality_filter_and_meta(self):
        """Verify Netflix-style quality filtering and keyboard buttons."""
        await database.insert_movie(
            title="Interstellar",
            file_id="id_4k",
            quality="4K UHD HDR",
            file_size="14.2 GB"
        )
        await database.insert_movie(
            title="Interstellar",
            file_id="id_1080p",
            quality="1080p BluRay",
            file_size="3.5 GB"
        )
        await database.insert_movie(
            title="Interstellar",
            file_id="id_720p",
            quality="720p WEB-DL",
            file_size="1.2 GB"
        )

        # 1. Search with return_qualities=True
        results, count, pages, qualities = await database.search_movies(
            query="Interstellar",
            page=1,
            page_size=10,
            return_qualities=True
        )
        self.assertEqual(count, 3)
        self.assertIn("4K", qualities)
        self.assertIn("1080p", qualities)
        self.assertIn("720p", qualities)

        # 2. Filter by 1080p
        f_results, f_count, f_pages, _ = await database.search_movies(
            query="Interstellar",
            page=1,
            page_size=10,
            quality_filter="1080p",
            return_qualities=True
        )
        self.assertEqual(f_count, 1)
        self.assertEqual(f_results[0]["file_id"], "id_1080p")

        # 3. Test keyboard contains filter row
        markup = bot.build_search_keyboard(
            results=f_results,
            current_page=1,
            total_pages=1,
            available_qualities=qualities,
            active_filter="1080p"
        )
        top_row = markup.inline_keyboard[0]
        top_row_texts = [b.text for b in top_row]
        self.assertTrue(any("All" in t for t in top_row_texts))
        self.assertTrue(any("1080p" in t for t in top_row_texts))
        self.assertTrue(any(b.callback_data == "qfilter:1080p" for b in top_row))

    async def test_08_group_and_deep_linking(self):
        """Verify group URL deep-linking vs PM callback button generation."""
        movie_doc = {
            "_id": "650000000000000000000001",
            "title": "Jailer",
            "quality": "1080p",
            "file_size": "1.99 GB",
            "season_episode": None
        }

        # 1. Group Mode: Must generate URL button pointing to bot PM
        group_markup = bot.build_search_keyboard(
            results=[movie_doc],
            current_page=1,
            total_pages=1,
            is_group=True,
            bot_username="Janakii_bot"
        )
        group_btn = group_markup.inline_keyboard[0][0]
        self.assertIsNotNone(group_btn.url)
        self.assertEqual(group_btn.url, "https://t.me/Janakii_bot?start=dl_650000000000000000000001")
        self.assertIsNone(group_btn.callback_data)

        # 2. PM Mode: Must generate direct callback_data button
        pm_markup = bot.build_search_keyboard(
            results=[movie_doc],
            current_page=1,
            total_pages=1,
            is_group=False
        )
        pm_btn = pm_markup.inline_keyboard[0][0]
        self.assertIsNone(pm_btn.url)
        self.assertEqual(pm_btn.callback_data, "dl:650000000000000000000001")

    async def test_09_season_pack_extraction_and_filtering(self):
        """Verify season extraction, season filtering, and season count in database."""
        await database.insert_movie(
            title="Loki",
            season_episode="S01E01",
            quality="1080p",
            file_size="1.2 GB"
        )
        await database.insert_movie(
            title="Loki",
            season_episode="S01E02",
            quality="1080p",
            file_size="1.1 GB"
        )
        await database.insert_movie(
            title="Loki",
            season_episode="S02E01",
            quality="1080p",
            file_size="1.3 GB"
        )

        # 1. Search with return_seasons=True
        results, count, pages, qualities, seasons = await database.search_movies(
            query="Loki",
            page=1,
            page_size=10,
            return_seasons=True
        )
        self.assertEqual(count, 3)
        self.assertIn("S01", seasons)
        self.assertIn("S02", seasons)

        # 2. Filter by Season 1
        s1_results, s1_count, s1_pages, _, _ = await database.search_movies(
            query="Loki",
            page=1,
            page_size=10,
            season_filter="S01",
            return_seasons=True
        )
        self.assertEqual(s1_count, 2)
        for r in s1_results:
            self.assertTrue(r["season_episode"].startswith("S01"))

    async def test_10_season_episode_ordering_and_batch(self):
        """Verify get_season_episodes returns all episodes in natural ascending episode order."""
        await database.insert_movie(
            title="Mirzapur",
            season_episode="S02E05",
            quality="1080p",
            file_size="900 MB"
        )
        await database.insert_movie(
            title="Mirzapur",
            season_episode="S02E01",
            quality="1080p",
            file_size="850 MB"
        )
        await database.insert_movie(
            title="Mirzapur",
            season_episode="S02E02",
            quality="1080p",
            file_size="890 MB"
        )

        episodes = await database.get_season_episodes(query="Mirzapur", season="S02")
        self.assertEqual(len(episodes), 3)
        ep_codes = [e["season_episode"] for e in episodes]
        self.assertEqual(ep_codes, ["S02E01", "S02E02", "S02E05"])

    async def test_11_trailer_and_imdb_keyboard(self):
        """Verify YouTube trailer link and IMDb ratings button in keyboard."""
        meta = {
            "title": "Inception",
            "year": "2010",
            "rating": "8.8",
            "trailer_url": "https://www.youtube.com/watch?v=cdx31ak4KbQ",
            "imdb_url": "https://www.imdb.com/title/tt1375666/",
            "cast": ["Leonardo DiCaprio", "Joseph Gordon-Levitt"]
        }
        movies = [{
            "_id": "650000000000000000000002",
            "title": "Inception",
            "quality": "1080p",
            "file_size": "2.1 GB",
            "season_episode": None
        }]

        markup = bot.build_search_keyboard(
            results=movies,
            current_page=1,
            total_pages=1,
            meta=meta,
            available_seasons=["S01", "S02"],
            active_season="S01",
            total_count=1
        )

        # First row should be Trailer and IMDb buttons
        first_row = markup.inline_keyboard[0]
        button_texts = [b.text for b in first_row]
        button_urls = [b.url for b in first_row]
        self.assertTrue(any("Trailer" in t for t in button_texts))
        self.assertIn("https://www.youtube.com/watch?v=cdx31ak4KbQ", button_urls)
        self.assertTrue(any("IMDb" in t for t in button_texts))
        self.assertIn("https://www.imdb.com/title/tt1375666/", button_urls)

        # Second row should be Season folders
        second_row = markup.inline_keyboard[1]
        s_texts = [b.text for b in second_row]
        self.assertTrue(any("All Seasons" in t for t in s_texts))
        self.assertTrue(any("S1" in t for t in s_texts))
        self.assertTrue(any("S2" in t for t in s_texts))

        # Third row should be Batch download button for active season
        third_row = markup.inline_keyboard[2]
        self.assertTrue(any("Batch" in b.text or "Entire" in b.text for b in third_row))


if __name__ == "__main__":
    unittest.main()


