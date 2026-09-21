# 🎬 Janakii — Telegram Movie & TV Series Librarian Bot

A high-performance, production-ready Telegram bot built with **Python 3.10+**, **python-telegram-bot (v20+)**, and **MongoDB (Motor async driver)**. 

The bot serves as an automated media librarian: it does not store media files on the server. Instead, it queries a MongoDB database for media metadata and delivers files stored in Telegram's cloud directly to users via their unique **Telegram File IDs**.

---

## 🌟 Key Features

- ⚡ **Pure Asynchronous Architecture**: Non-blocking I/O using `python-telegram-bot` v20+ and `motor`.
- 🔍 **Case-Insensitive Fuzzy Search**: Fast MongoDB regex search on the `title` field with special-character escaping.
- 📑 **10-Item Inline Pagination**: Dynamic inline keyboard results with `<< Prev` and `Next >>` controls that adhere strictly to Telegram's 64-byte `callback_data` limit.
- 🏷 **Standardized Button UI**: Every media button follows the exact format:
  `[File Size] Title Quality` (e.g., `[890.19 MB] Ultimate Spider-Man S01E21 1080p`).
- 💣 **Auto-Delete (Ban Prevention)**: Uses `JobQueue` (APScheduler) to automatically delete both the sent media file message **and** the user's original search message after **300 seconds (5 minutes)** with a self-destruct warning caption.
- 📢 **Force-Subscribe Gatekeeper**: Checks if users are members of your updates channel before allowing searches or file downloads.
- ⏳ **Anti-Spam Rate Limiting**: Enforces a 3-second cooldown between user queries to protect the Telegram API.
- 🔒 **PM-Only Restriction**: Automatically rejects or ignores group chat attempts.
- 🤖 **Intelligent Channel Ingestion & Filename Parsing**: Powered by Google Gemini AI to strip watermarks, group handles, and noisy release tags, extracting canonical titles, release years, quality, and audio language. Includes zero-downtime heuristic fallback.
- 🛠 **Media Ingestion Tools**: Includes interactive CLI, sample data seeder, automated channel crawler (`index_channel.py`), and live Telegram File ID forward listener (`add_movie.py`).

---

## 🗄 MongoDB Schema (`movies`)

The bot operates on a MongoDB collection named `movies` with the following document structure:

| Field | Type | Description | Example |
| :--- | :--- | :--- | :--- |
| `_id` | `ObjectId` | MongoDB primary key | `ObjectId("65f1234...")` |
| `title` | `String` | Movie or TV Series title | `"Ultimate Spider-Man"` |
| `season_episode` | `String` (or `null`) | Season & Episode code (null for movies) | `"S01E21"` or `null` |
| `quality` | `String` | Resolution & video encoding info | `"1080p JHS DUA"` |
| `file_size` | `String` | Formatted file size | `"890.19 MB"` |
| `file_id` | `String` | Telegram File ID of the stored media | `"BQACAgUAAxkBAAI..."` |
| `year` | `Integer` (or `null`) | Release year (optional AI-enriched) | `2023` |
| `language` | `String` (or `null`) | Audio language (optional AI-enriched) | `"Dual Audio"` |

---

## 📂 Project Structure

```
├── .env.example       # Template for environment variables
├── requirements.txt   # Python package dependencies
├── config.py          # Centralized configuration & env validation
├── database.py        # Motor async MongoDB client, indexes & search queries
├── ai_parser.py       # Google Gemini AI intelligent filename parsing engine
├── bot.py             # Main bot application, handlers, and JobQueue callbacks
├── index_channel.py   # Bulk channel crawling & batch indexing with AI support
├── add_movie.py       # Helper utility: CLI, sample data seeder, and File ID listener
├── test_bot.py        # Core automated test suite
├── test_ai_parser.py  # AI parser unit & fallback test suite
└── README.md          # Complete documentation & operational guide
```

---

## 🚀 Quick Setup & Installation

### 1. Clone & Navigate
```bash
cd Janakii
```

### 2. Create and Activate Virtual Environment (Recommended)
```bash
# On Linux/macOS:
python -m venv venv
source venv/bin/activate

# On Windows (PowerShell):
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` with your actual credentials:
```env
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
MONGO_URI=mongodb://localhost:27017
DATABASE_NAME=movie_librarian
FORCE_SUB_CHANNEL_ID=-1001234567890
FORCE_SUB_CHANNEL_LINK=https://t.me/MyChannelUsername
AUTO_DELETE_SECONDS=300
SEARCH_COOLDOWN_SECONDS=3
RESULTS_PER_PAGE=10
ADMIN_USER_IDS=123456789
```

---

## 🔑 How to Obtain Necessary Credentials

### 1. Telegram Bot Token (`BOT_TOKEN`)
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Choose a name and a username ending in `bot` (e.g. `MyCinemaLibrarianBot`).
4. Copy the API HTTP Token provided and paste it as `BOT_TOKEN` in `.env`.

### 2. Force Subscribe Channel ID (`FORCE_SUB_CHANNEL_ID`)
1. Create a Public or Private Channel on Telegram (e.g. `@MyMovieUpdatesChannel`).
2. Add your bot to the channel as an **Administrator** with at least "Post Messages" or "Invite Users via Link" permissions.
3. To find the Channel ID:
   - For a public channel: You can directly use `@MyMovieUpdatesChannel`.
   - For a numerical ID: Forward any post from the channel to [@userinfobot](https://t.me/userinfobot) or [@JsonDumpBot](https://t.me/JsonDumpBot). The ID will look like `-1001234567890`.

---

## 📡 How to Get `file_id` from a Private Channel & Insert into MongoDB

Files on Telegram can be stored securely in your own **Private Telegram Storage Channel**. Since Telegram hosts the files indefinitely, your bot only needs the **File ID** to instantly send the file to users.

Here are the **three easiest ways** to extract the `file_id` and insert it into MongoDB:

### Method 1: Use the Built-in Telegram Listener (Fastest & Easiest) 🌟

We built a dedicated media listener inside `add_movie.py`:

1. Run the listener in your terminal:
   ```bash
   python add_movie.py --listen
   ```
2. Open Telegram and open a chat with your bot.
3. **Forward any video or document** from your private storage channel to the bot.
4. The bot will instantly reply with:
   ```
   ✅ Media Received!
   📁 File Name: Ultimate.Spider-Man.S01E21.1080p.mkv
   💾 File Size: 890.19 MB
   🔑 File ID: BQACAgUAAxkBAAI_TestFileId...
   ```
5. You now have the exact `file_id` and calculated file size ready to be stored!

---

### Method 2: Interactive CLI Ingestion

If you already have the `file_id`:

1. Run:
   ```bash
   python add_movie.py
   ```
2. Follow the interactive prompts:
   ```
   🎬 Add New Movie / Series to Database
   ==================================================
   Enter Title (e.g., Ultimate Spider-Man): Ultimate Spider-Man
   Enter Season/Episode (e.g., S01E21 or press Enter for Movies): S01E21
   Enter Quality (e.g., 1080p JHS DUA): 1080p JHS DUA
   Enter File Size (e.g., 890.19 MB): 890.19 MB
   Enter Telegram File ID: BQACAgUAAxkBAAI_TestFileId...

   ✅ Successfully added movie to database!
   Button Preview: [890.19 MB] Ultimate Spider-Man S01E21 1080p JHS DUA
   ```

---

### Method 3: Seed Sample Data for Instant Testing

To quickly test search, pagination, and buttons with 12 pre-configured movies and series (including the required Spider-Man sample):
```bash
python add_movie.py --seed
```

---

## ▶️ Running the Bot

Once your `.env` is configured and your database has entries:

```bash
python bot.py
```

You will see:
```
[INFO] - Connecting to MongoDB...
[INFO] - MongoDB index on 'title' created/verified successfully.
[INFO] - Bot commands menu registered successfully.
[INFO] - Ready! Total movies in database: 12
[INFO] - Bot is polling for updates...
```

Now open Telegram, click `/start`, and search for any movie!

---

## 🧪 Running Automated Tests

Run the included test suite to verify search logic, pagination, button labels, and rate limiting:

```bash
python -m unittest test_bot.py
```

Expected output:
```
......
----------------------------------------------------------------------
Ran 6 tests in 0.115s

OK
```

---

## 🛡 Ban Prevention & Security Details

1. **JobQueue Auto-Deletion**:
   Telegram bots can face copyright notices if media files remain in user chat histories indefinitely. This bot uses `context.job_queue.run_once` scheduled for 300 seconds (5 minutes). When the timer expires, both the media document message and the user's initial search query are cleanly deleted via `bot.delete_message`.

2. **Rate Limiting**:
   Each user has a 3-second cooldown buffer (`SEARCH_COOLDOWN_SECONDS`) to prevent rapid automated searches from triggering Telegram API flood limits.

3. **PM Enforcement**:
   Group interactions are blocked by filtering handlers to `ChatType.PRIVATE`. If added to a group chat, the bot declines requests and informs members to search via Private Message.
>>>>>>> dc21ef7 (Initial commit: Movie & Series Librarian Bot with sub-second search and 24/7 cloud support)
