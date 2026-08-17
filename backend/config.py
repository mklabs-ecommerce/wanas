"""Configuration, read from the environment only.

Nothing here has a hardcoded credential. Anything missing degrades to a
documented "not configured" behaviour rather than raising at import time,
because Phase 1 is deliberately built to make progress without Meta
credentials, an LLM key, shipping fees, or a staff login (see AGENTS.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _first_env(*names: str, default: str = "") -> str:
    """First of several environment names that is actually set."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str

    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_debug_payload: bool

    whatsapp_phone_number_id: str
    whatsapp_access_token: str
    whatsapp_app_secret: str
    whatsapp_verify_token: str
    whatsapp_api_version: str

    history_cap: int
    session_expiry_hours: int
    tool_loop_cap: int
    max_quantity_per_line: int

    chatbot_debug: bool
    harness_enabled: bool

    shopify_store_domain: str
    shopify_admin_token: str
    shopify_api_version: str

    @property
    def shopify_configured(self) -> bool:
        """Without credentials the catalog serves wanas.db's own prices and
        stock, exactly as it did before the move -- degraded, but not broken,
        and logged when it happens."""
        return bool(self.shopify_store_domain and self.shopify_admin_token)

    @property
    def whatsapp_configured(self) -> bool:
        """The adapter is inert until Meta credentials exist.

        Outbound messages are logged and the inbound webhook refuses, rather
        than the app failing to start -- the whole system apart from delivery
        is testable through the local harness without Meta.
        """
        return bool(self.whatsapp_phone_number_id and self.whatsapp_access_token)


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./wanas.db"),
        llm_provider=os.getenv("LLM_PROVIDER", "fake").strip().lower(),
        # `GEMINI_*` accepted as aliases: that is what a Gemini-only .env
        # already calls them, and a key that is present under the "wrong" name
        # presents as an auth failure, which is a slow thing to diagnose.
        # No format check on the key -- newer Google keys are `AQ.Ab...` rather
        # than `AIzaSy...`, and anything that pattern-matches a key prefix
        # rejects valid credentials.
        llm_model=_first_env("LLM_MODEL", "GEMINI_MODEL", default=""),
        llm_api_key=_first_env("LLM_API_KEY", "GEMINI_API_KEY", default=""),
        llm_debug_payload=_bool("LLM_DEBUG_PAYLOAD", False),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_app_secret=os.getenv("WHATSAPP_APP_SECRET", ""),
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v21.0"),
        history_cap=_int("HISTORY_CAP", 40),
        session_expiry_hours=_int("SESSION_EXPIRY_HOURS", 6),
        tool_loop_cap=_int("TOOL_LOOP_CAP", 8),
        max_quantity_per_line=_int("MAX_QUANTITY_PER_LINE", 10),
        chatbot_debug=_bool("CHATBOT_DEBUG", False),
        harness_enabled=_bool("HARNESS_ENABLED", True),
        shopify_store_domain=os.getenv("SHOPIFY_STORE_DOMAIN", "").strip(),
        shopify_admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        shopify_api_version=os.getenv("SHOPIFY_API_VERSION", "2025-01").strip(),
    )


settings = load_settings()
