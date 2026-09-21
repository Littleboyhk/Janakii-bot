import os
import sys
import logging
from typing import List, Union, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

def _parse_channel_id(value: Optional[str]) -> Optional[Union[int, str]]:
    """Parse channel identifier: converts to integer if numeric (e.g. -100...), otherwise leaves as string."""
    if not value or not value.strip():
        return None
    val = value.strip()
    try:
        return int(val)
    except ValueError:
        return val

def _parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default

def _parse_float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value else default
    except ValueError:
        return default

def _parse_admin_ids(value: Optional[str]) -> List[int]:
    if not value:
        return []
    ids = []
    for item in value.split(","):
        cleaned = item.strip()
        if cleaned.isdigit():
            ids.append(int(cleaned))
    return ids


# Configuration values
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017").strip()
DATABASE_NAME: str = os.getenv("DATABASE_NAME", "movie_librarian").strip()

FORCE_SUB_CHANNEL_ID: Optional[Union[int, str]] = _parse_channel_id(os.getenv("FORCE_SUB_CHANNEL_ID"))
FORCE_SUB_CHANNEL_LINK: Optional[str] = os.getenv("FORCE_SUB_CHANNEL_LINK", "").strip() or None

AUTO_DELETE_SECONDS: int = _parse_int(os.getenv("AUTO_DELETE_SECONDS"), 300)
SEARCH_COOLDOWN_SECONDS: float = _parse_float(os.getenv("SEARCH_COOLDOWN_SECONDS"), 3.0)
RESULTS_PER_PAGE: int = _parse_int(os.getenv("RESULTS_PER_PAGE"), 10)

ADMIN_USER_IDS: List[int] = _parse_admin_ids(os.getenv("ADMIN_USER_IDS"))

TELEGRAM_API_ID: int = _parse_int(os.getenv("TELEGRAM_API_ID"), 0)
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "").strip()
TARGET_CHANNEL: str = os.getenv("TARGET_CHANNEL", "").strip()

# AI Ingestion Settings (Google Gemini)
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
ENABLE_AI_INGESTION: bool = os.getenv("ENABLE_AI_INGESTION", "true").lower() in ("true", "1", "yes")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()


def validate_config() -> None:
    """Validate that mandatory configuration settings are present."""
    missing = []
    if not BOT_TOKEN or BOT_TOKEN == "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ":
        missing.append("BOT_TOKEN (must be a valid token from @BotFather)")
    if not MONGO_URI:
        missing.append("MONGO_URI (must be a valid MongoDB connection string)")

    if missing:
        logger.warning(
            "Config notice: The following settings are not configured yet:\n - %s\n"
            "Please configure your .env file before starting the live bot.",
            "\n - ".join(missing)
        )
