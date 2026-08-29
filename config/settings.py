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


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _public_base_url() -> str:
    """Where this app is reachable from the internet, for anything (Shopify
    webhook registration, so far) that needs to hand Shopify a callback URL.

    `PUBLIC_BASE_URL` if set explicitly; otherwise Railway already injects
    `RAILWAY_PUBLIC_DOMAIN` into every deploy, so that is used without asking
    anyone to duplicate it into a second variable. Blank off Railway and with
    nothing set -- callers treat that as "nothing to register against", the
    same off-by-default shape as every other optional integration here.
    """
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


@dataclass(frozen=True)
class Settings:
    database_url: str

    llm_provider: str
    llm_model: str
    #: Optional second model for voice notes and photos. Blank reuses the
    #: conversation model.
    llm_media_model: str
    llm_api_key: str
    llm_debug_payload: bool
    #: OpenRouter routes the conversation model by default. Its own variable,
    #: deliberately not an alias of `llm_api_key` above: sharing one would
    #: hand a routed-inference key to Google (or a Google key to OpenRouter),
    #: which only surfaces later as an auth failure far from its cause.
    openrouter_api_key: str

    whatsapp_phone_number_id: str
    whatsapp_access_token: str
    whatsapp_app_secret: str
    whatsapp_verify_token: str
    whatsapp_api_version: str

    instagram_account_id: str      # INSTAGRAM_ACCOUNT_ID -- the numeric IG_ID, not the @handle
    instagram_access_token: str    # INSTAGRAM_ACCESS_TOKEN
    # The *Instagram* app secret, not WHATSAPP_APP_SECRET.
    instagram_app_secret: str      # INSTAGRAM_APP_SECRET
    instagram_verify_token: str    # INSTAGRAM_VERIFY_TOKEN
    instagram_api_version: str     # INSTAGRAM_API_VERSION, default "v23.0"
    instagram_username: str        # INSTAGRAM_USERNAME -- only ever used in customer-facing copy

    #: Comments are a public surface. Off by default so the DM half can ship
    #: and be watched for a week before anything the bot writes is visible to
    #: everyone who scrolls past the post.
    instagram_comments_enabled: bool       # INSTAGRAM_COMMENTS_ENABLED, default False
    #: Whether the bot writes a visible reply under the comment at all, or
    #: only slides into the DM.
    instagram_public_reply_enabled: bool   # INSTAGRAM_PUBLIC_REPLY_ENABLED, default True
    #: A comment older than this is ignored -- Meta's private-reply window is
    #: 7 days and a reply to a month-old post is noise, not service.
    instagram_comment_max_age_hours: float  # INSTAGRAM_COMMENT_MAX_AGE_HOURS, default 48.0
    #: Per-commenter cap inside a rolling hour, so one person spamming a post
    #: cannot cost 40 model calls.
    instagram_comment_rate_limit: int      # INSTAGRAM_COMMENT_RATE_LIMIT, default 3
    #: Signs the public media URLs Meta fetches attachments from (STEP 5).
    #: Falls back to DASHBOARD_SESSION_SECRET so one less secret has to be set.
    media_url_secret: str                  # MEDIA_URL_SECRET

    #: How many messages stay in the *live* slice of a conversation -- the
    #: part `session.load()` returns and a new turn continues from. It is not
    #: how much the model is sent: see `model_context_messages` below and
    #: `assistant/context.py`. Counted in messages, and one exchange costs
    #: four to six of them once tool calls and their results are included.
    history_cap: int
    #: The verbatim window handed to the provider: the last N messages exactly
    #: as stored, tool calls and results included.
    model_context_messages: int
    #: How much of the conversation *before* that window is still recalled,
    #: compacted to what was actually said (`assistant/context.py`). 0 turns
    #: recall off and makes the model's memory `model_context_messages` again.
    model_context_recall: int
    #: How many messages one conversation's stored transcript keeps. The live
    #: context is `history_cap`; this bounds the archive behind it.
    session_archive_cap: int
    session_expiry_hours: int
    tool_loop_cap: int
    max_quantity_per_line: int

    #: How long an inbound message waits for the customer's next one before the
    #: agent runs. WhatsApp is typed in fragments -- "عايز هودي" / "أسود" /
    #: "لارج" inside five seconds is one request, and answering it three times
    #: costs three model calls and reads like three different people replying.
    #: 0 processes each message on arrival, in the caller's thread, which is
    #: what the tests want.
    message_debounce_seconds: float
    #: Threads that run agent turns. One conversation is always serial; this
    #: caps how many *different* conversations run at once.
    message_workers: int

    #: How sure the vision pass has to be before a photo is treated as "this
    #: product". Below it the reading is only used to ask a better question.
    image_match_confidence: float
    voice_notes_enabled: bool
    image_understanding_enabled: bool
    interactive_messages_enabled: bool

    chatbot_debug: bool
    harness_enabled: bool

    #: Add columns the models declare and an existing table lacks, at startup.
    #: On by default: `create_all` never adds a column to a table it did not
    #: create, so the alternative default is a database that silently keeps an
    #: old shape until the first write that needs the new column fails.
    auto_migrate_schema: bool

    #: The staff dashboard: view conversations, reply to a paused one, resolve
    #: the handoff. On by default -- unlike the harness it requires a staff
    #: login -- but with no secret set it refuses every login rather than
    #: signing session cookies nobody could invalidate; see `dashboard_configured`.
    dashboard_enabled: bool
    dashboard_session_secret: str
    dashboard_session_hours: int

    shopify_store_domain: str
    shopify_admin_token: str
    shopify_api_version: str
    #: The brand name on every product this app creates. Shopify defaults a
    #: new product's vendor to the *store's* name ("My Store"), which is not
    #: what the 18 products already on the shelf say, and vendor is shown on
    #: the storefront and filtered on in Admin.
    shopify_vendor: str
    #: The Shopify app's webhook signing secret. Without it the status webhook
    #: refuses every delivery rather than trusting an unsigned one.
    shopify_webhook_secret: str
    #: See `_public_base_url`. Used only to register Shopify's webhook
    #: subscriptions against; never required for anything to keep working.
    public_base_url: str

    #: How often the background job checks for back-in-stock waitlists and
    #: idle carts (`domain/services/scheduler.py`). <= 0 disables the loop
    #: entirely -- for tests, which call the checks directly instead.
    reengagement_interval_seconds: float
    #: How long a cart sits untouched before `check_abandoned_carts` sends the
    #: "still interested?" nudge.
    abandoned_cart_hours: float
    #: Past this age a cart is treated as dead rather than abandoned -- an
    #: ancient test cart must not get nudged on every restart forever.
    abandoned_cart_max_age_hours: float
    #: Meta template names for the two proactive message types Feature 3/4
    #: need outside the 24-hour customer service window (`notifications.
    #: send_proactive`). Blank until a real one is submitted and approved --
    #: see docs/OPERATIONS.md, which is also where `order_confirmation` /
    #: `status_*` / `feedback_request` are tracked as not yet approved either.
    whatsapp_template_back_in_stock: str
    whatsapp_template_abandoned_cart: str
    whatsapp_template_language: str

    @property
    def shopify_configured(self) -> bool:
        """Without credentials the catalog serves wanas.db's own prices and
        stock, exactly as it did before the move -- degraded, but not broken,
        and logged when it happens."""
        return bool(self.shopify_store_domain and self.shopify_admin_token)

    @property
    def shopify_webhooks_configured(self) -> bool:
        """Order-status pushes only work when Shopify can tell us it shipped."""
        return bool(self.shopify_webhook_secret)

    @property
    def dashboard_configured(self) -> bool:
        """Without a secret, a signed session cookie could never be told apart
        from a forged one -- refusing login is the same call the Shopify
        webhook makes with no signing secret set."""
        return bool(self.dashboard_session_secret)

    @property
    def whatsapp_configured(self) -> bool:
        """The adapter is inert until Meta credentials exist.

        Outbound messages are logged and the inbound webhook refuses, rather
        than the app failing to start -- the whole system apart from delivery
        is testable through the local harness without Meta.
        """
        return bool(self.whatsapp_phone_number_id and self.whatsapp_access_token)

    @property
    def instagram_configured(self) -> bool:
        """The adapter is inert until Instagram credentials exist.

        Same contract as `whatsapp_configured`: the webhook refuses with 503
        and outbound is logged, rather than the app failing to start.
        """
        return bool(self.instagram_account_id and self.instagram_access_token)


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./wanas.db"),
        # Default is openrouter; the test suite pins LLM_PROVIDER=fake in
        # tests/conftest.py before this module is imported anywhere, so the
        # suite never depends on the production default.
        llm_provider=os.getenv("LLM_PROVIDER", "openrouter").strip().lower(),
        # `GEMINI_*` accepted as aliases: that is what a Gemini-only .env
        # already calls them, and a key that is present under the "wrong" name
        # presents as an auth failure, which is a slow thing to diagnose.
        # No format check on the key -- newer Google keys are `AQ.Ab...` rather
        # than `AIzaSy...`, and anything that pattern-matches a key prefix
        # rejects valid credentials.
        llm_model=_first_env("LLM_MODEL", "GEMINI_MODEL", default=""),
        llm_media_model=_first_env("LLM_MEDIA_MODEL", "GEMINI_MEDIA_MODEL", default=""),
        llm_api_key=_first_env("LLM_API_KEY", "GEMINI_API_KEY", default=""),
        llm_debug_payload=_bool("LLM_DEBUG_PAYLOAD", False),
        openrouter_api_key=_first_env("OPENROUTER_API_KEY", default=""),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_app_secret=os.getenv("WHATSAPP_APP_SECRET", ""),
        whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v21.0"),
        instagram_account_id=os.getenv("INSTAGRAM_ACCOUNT_ID", ""),
        instagram_access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN", ""),
        instagram_app_secret=os.getenv("INSTAGRAM_APP_SECRET", ""),
        instagram_verify_token=os.getenv("INSTAGRAM_VERIFY_TOKEN", ""),
        instagram_api_version=os.getenv("INSTAGRAM_API_VERSION", "v23.0"),
        instagram_username=os.getenv("INSTAGRAM_USERNAME", ""),
        instagram_comments_enabled=_bool("INSTAGRAM_COMMENTS_ENABLED", False),
        instagram_public_reply_enabled=_bool("INSTAGRAM_PUBLIC_REPLY_ENABLED", True),
        instagram_comment_max_age_hours=_float("INSTAGRAM_COMMENT_MAX_AGE_HOURS", 48.0),
        instagram_comment_rate_limit=_int("INSTAGRAM_COMMENT_RATE_LIMIT", 3),
        media_url_secret=_first_env("MEDIA_URL_SECRET", "DASHBOARD_SESSION_SECRET", default=""),
        history_cap=_int("HISTORY_CAP", 150),
        model_context_messages=_int("MODEL_CONTEXT_MESSAGES", 24),
        model_context_recall=_int("MODEL_CONTEXT_RECALL", 60),
        session_archive_cap=_int("SESSION_ARCHIVE_CAP", 2000),
        session_expiry_hours=_int("SESSION_EXPIRY_HOURS", 6),
        tool_loop_cap=_int("TOOL_LOOP_CAP", 8),
        max_quantity_per_line=_int("MAX_QUANTITY_PER_LINE", 10),
        message_debounce_seconds=_float("MESSAGE_DEBOUNCE_SECONDS", 6.0),
        message_workers=_int("MESSAGE_WORKERS", 8),
        image_match_confidence=_float("IMAGE_MATCH_CONFIDENCE", 0.6),
        voice_notes_enabled=_bool("VOICE_NOTES_ENABLED", True),
        image_understanding_enabled=_bool("IMAGE_UNDERSTANDING_ENABLED", True),
        interactive_messages_enabled=_bool("INTERACTIVE_MESSAGES_ENABLED", True),
        chatbot_debug=_bool("CHATBOT_DEBUG", False),
        # On by default, and additive-only: see `_ensure_schema_columns` in
        # app.py. Set it to 0 to have startup report the drift and change
        # nothing, leaving `scripts/migrate_schema.py` to do it by hand.
        auto_migrate_schema=_bool("AUTO_MIGRATE_SCHEMA", True),
        # Off unless asked for. It is an unauthenticated surface that can
        # converse as any customer identity, so the default that costs
        # something when you forget it has to be the closed one.
        harness_enabled=_bool("HARNESS_ENABLED", False),
        # On by default: unlike the harness this requires a staff login, so
        # forgetting the flag does not expose anything -- only forgetting the
        # secret does, and that is refused explicitly (dashboard_configured).
        dashboard_enabled=_bool("DASHBOARD_ENABLED", True),
        dashboard_session_secret=os.getenv("DASHBOARD_SESSION_SECRET", "").strip(),
        dashboard_session_hours=_int("DASHBOARD_SESSION_HOURS", 12),
        shopify_store_domain=os.getenv("SHOPIFY_STORE_DOMAIN", "").strip(),
        shopify_admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        shopify_api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07").strip(),
        shopify_vendor=os.getenv("SHOPIFY_VENDOR", "Wanas Gallery").strip(),
        shopify_webhook_secret=_first_env(
            "SHOPIFY_WEBHOOK_SECRET", "SHOPIFY_API_SECRET", default=""
        ),
        public_base_url=_public_base_url(),
        reengagement_interval_seconds=_float("REENGAGEMENT_INTERVAL_SECONDS", 1800.0),
        abandoned_cart_hours=_float("ABANDONED_CART_HOURS", 2.0),
        abandoned_cart_max_age_hours=_float("ABANDONED_CART_MAX_AGE_HOURS", 48.0),
        whatsapp_template_back_in_stock=os.getenv("WHATSAPP_TEMPLATE_BACK_IN_STOCK", "").strip(),
        whatsapp_template_abandoned_cart=os.getenv("WHATSAPP_TEMPLATE_ABANDONED_CART", "").strip(),
        whatsapp_template_language=os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "ar").strip() or "ar",
    )


settings = load_settings()
