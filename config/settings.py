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


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    """A comma-separated env var as a tuple, blanks and whitespace dropped.

    An explicitly empty variable means an empty tuple ("do not constrain
    this"), which is a different answer from "unset" and has to stay
    distinguishable from it -- hence the default is applied to the *unset*
    case only.
    """
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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
    #: Optional separate model for the Instagram comment classifier. Blank
    #: reuses the conversation model, which is today's behaviour exactly.
    comment_classifier_model: str
    llm_debug_payload: bool
    #: OpenRouter routes the conversation model by default. Its own variable,
    #: deliberately not an alias of `llm_api_key` above: sharing one would
    #: hand a routed-inference key to Google (or a Google key to OpenRouter),
    #: which only surfaces later as an auth failure far from its cause.
    openrouter_api_key: str

    #: Which upstream providers OpenRouter may serve the conversation model
    #: from, most preferred first (provider slugs, comma-separated).
    #:
    #: This exists because OpenRouter's *default* is to load-balance one model
    #: id across every upstream that hosts it -- 23 of them for
    #: `z-ai/glm-5.3-flash`, at three different quantizations, and six
    #: identical requests were measured landing on three different ones. That
    #: is not a performance detail: with `require_parameters` off, a provider
    #: that does not support `temperature` is sent the request anyway and
    #: **silently ignores it**, so a reply this shop meant to generate at 0.3
    #: was generated at whatever that stack defaults to. Egyptian Arabic is
    #: the first thing to break under that, and it breaks intermittently --
    #: which is exactly how it presented: most replies fine, some garbled.
    #:
    #: Blank keeps OpenRouter's own preference order within the *filtered*
    #: candidate set; it never restores the unfiltered default.
    openrouter_providers: tuple[str, ...]
    #: Quantizations the conversation model may be served at. "unknown" is
    #: excluded on purpose -- an endpoint that will not say what precision it
    #: runs at is not one to put a customer's Arabic through.
    openrouter_quantizations: tuple[str, ...]

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
    #: The *app-scoped* id of the same account (`GET /me?fields=id` on
    #: graph.instagram.com), which is a different number from
    #: `instagram_account_id` (`?fields=user_id`). Optional, and only ever
    #: used to recognise ourselves -- see `instagram_self_ids`.
    instagram_app_scoped_id: str   # INSTAGRAM_APP_SCOPED_ID

    #: Comments are a public surface. Off by default so the DM half can ship
    #: and be watched for a week before anything the bot writes is visible to
    #: everyone who scrolls past the post.
    instagram_comments_enabled: bool       # INSTAGRAM_COMMENTS_ENABLED, default False
    #: Whether the bot writes a visible reply under the comment at all, or
    #: only slides into the DM.
    instagram_public_reply_enabled: bool   # INSTAGRAM_PUBLIC_REPLY_ENABLED, default True
    #: The mirror of the flag above, for the private half. Off means every
    #: category answers in public only and nobody is cold-DMed -- the setting
    #: to reach for if private replies ever draw a warning from Meta, without
    #: taking the whole comment surface down with them.
    instagram_comments_dm_enabled: bool    # INSTAGRAM_COMMENTS_DM_ENABLED, default True
    #: A comment older than this is ignored -- Meta's private-reply window is
    #: 7 days and a reply to a month-old post is noise, not service.
    instagram_comment_max_age_hours: float  # INSTAGRAM_COMMENT_MAX_AGE_HOURS, default 48.0
    #: Per-commenter cap inside a rolling hour, so one person spamming a post
    #: cannot cost 40 model calls.
    instagram_comment_rate_limit: int      # INSTAGRAM_COMMENT_RATE_LIMIT, default 3
    #: The same cap for the fixed FAQ answers, counted separately. An FAQ
    #: reply sends no DM and costs no model call, so it must not spend the
    #: budget that exists to stop a flood of DMs -- but it is still visible
    #: under a post, so it is not unlimited either.
    instagram_faq_rate_limit: int          # INSTAGRAM_FAQ_RATE_LIMIT, default 5
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

    #: Report, at boot, which wanas.db products Shopify no longer has -- a log
    #: line, never a write. The deleting half stays
    #: `scripts/shopify_reconcile_products.py`, run by hand: this runs
    #: unattended on every deploy, and a reconcile that deletes unattended is
    #: one bad Shopify read away from an empty catalog.
    reconcile_report_on_boot: bool

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
    #: ...and the same for the messages that follow an order rather than a
    #: cart. These are the ones that most often fall outside the window: a
    #: customer orders, stops writing, and the fulfilment happens a day or
    #: two later. `order_update` covers every status push (packed, shipped,
    #: delivered, cancelled), `feedback_request` the rating ask after
    #: delivery, and `order_confirmation` exists for completeness -- the
    #: confirmation is always sent seconds after the customer's own message,
    #: so it is inside the window by construction.
    whatsapp_template_order_update: str
    whatsapp_template_feedback_request: str
    whatsapp_template_order_confirmation: str
    whatsapp_template_language: str

    #: Email alerts to the owner. The staff queue is the record; this is the
    #: tap on the shoulder that makes someone open it. Only the queue items a
    #: person has to act on *now* are mailed -- a complaint, a handoff, a
    #: crash -- never the routine ones (an order confirmed, a low-stock note),
    #: which arrive by the dozen and would train the owner to filter the
    #: address. See `domain/services/alert_email.py`.
    #:
    #: Gmail refuses an account password over SMTP; ALERT_SMTP_PASSWORD is a
    #: 16-character App Password (Google Account -> Security -> 2-Step
    #: Verification -> App passwords). It is a credential like any other here:
    #: .env and Railway only, never logged.
    alert_email_to: str
    alert_email_from: str
    alert_smtp_host: str
    alert_smtp_port: int
    alert_smtp_username: str
    alert_smtp_password: str
    #: How long the same reason about the same conversation stays quiet after
    #: one mail. A crash loop or a comment flood raises one queue item per
    #: event by design; it must not raise one email per event.
    #: Gmail over HTTPS, because Railway blocks every outbound SMTP port --
    #: 25, 465, 587 and 2525 all answer "Network is unreachable" from inside
    #: the container while plain HTTP connects instantly. That is a platform
    #: policy against spam, not a setting, so the SMTP fields above cannot
    #: deliver from production however correct they are. They still work from
    #: a developer's machine and on a host that permits SMTP, which is why
    #: they stay.
    #:
    #: The Gmail API costs nothing and needs no new vendor: it sends as the
    #: same mailbox, over 443. What it needs is OAuth -- a client id and
    #: secret from a Google Cloud project, and a refresh token minted once by
    #: `scripts/gmail_authorise.py`.
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str

    alert_email_cooldown_seconds: float
    #: A hard ceiling per hour across everything, so no bug can turn the
    #: owner's inbox into the log file.
    alert_email_max_per_hour: int

    @property
    def gmail_api_configured(self) -> bool:
        """All three OAuth values, or none of them is usable."""
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)

    @property
    def alert_smtp_configured(self) -> bool:
        return bool(self.alert_smtp_host and self.alert_smtp_username and self.alert_smtp_password)

    @property
    def alert_email_configured(self) -> bool:
        """No recipient, or no way to send, means no email -- and that is a
        documented off state, not an error: the staff queue and the dashboard
        carry every one of these alerts either way.

        Either transport counts. The Gmail API is the one that works from
        Railway; SMTP is what a developer's machine and most other hosts have.
        """
        return bool(self.alert_email_to) and (
            self.gmail_api_configured or self.alert_smtp_configured
        )

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

    @property
    def whatsapp_webhooks_configured(self) -> bool:
        """Whether inbound WhatsApp can be *authenticated*, which is a
        stricter question than whether outbound can be sent.

        Deliberately separate from `whatsapp_configured`: the access token is
        what sends a reply, the app secret is what proves a request came from
        Meta. An operator who sets the first pair and forgets `APP_SECRET`
        used to get a webhook that skipped signature verification entirely --
        `if app_secret and not verify(...)` -- so anyone who found the URL
        could inject a customer message and drive a real Shopify order. Same
        contract as `shopify_webhooks_configured`: no secret means refuse,
        never fall back to trusting the caller.
        """
        return bool(self.whatsapp_configured and self.whatsapp_app_secret)

    @property
    def instagram_webhooks_configured(self) -> bool:
        """The Instagram twin of `whatsapp_webhooks_configured`, keyed on the
        *Instagram* app secret -- a different string from the WhatsApp one
        even inside the same Meta app."""
        return bool(self.instagram_configured and self.instagram_app_secret)

    @property
    def instagram_self_ids(self) -> frozenset[str]:
        """Every id that means "this is us" in an Instagram webhook.

        Instagram Login hands the same account out under two different
        numbers: `GET /me?fields=user_id` is the professional-account id that
        goes in `INSTAGRAM_ACCOUNT_ID` and addresses the Graph endpoints,
        while `GET /me?fields=id` is an app-scoped id -- and which of the two
        turns up as `sender.id` on an echo, or `from.id` on the shop's own
        comment, is Meta's choice, not ours. Matching only one of them is how
        the bot ends up answering itself in public.

        So the check is a *set*, and both ids belong in it. Widening it cannot
        silence a customer -- no customer holds either number -- while
        narrowing it re-opens the one failure this channel cannot survive.
        """
        return frozenset(
            value for value in (self.instagram_account_id, self.instagram_app_scoped_id) if value
        )


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
        comment_classifier_model=_first_env("COMMENT_CLASSIFIER_MODEL", default=""),
        llm_debug_payload=_bool("LLM_DEBUG_PAYLOAD", False),
        openrouter_api_key=_first_env("OPENROUTER_API_KEY", default=""),
        openrouter_providers=_csv(
            "OPENROUTER_PROVIDERS", default="z-ai,deepinfra,novita"
        ),
        openrouter_quantizations=_csv(
            "OPENROUTER_QUANTIZATIONS", default="fp8,bf16,fp16"
        ),
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
        instagram_app_scoped_id=os.getenv("INSTAGRAM_APP_SCOPED_ID", ""),
        instagram_comments_enabled=_bool("INSTAGRAM_COMMENTS_ENABLED", False),
        instagram_public_reply_enabled=_bool("INSTAGRAM_PUBLIC_REPLY_ENABLED", True),
        instagram_comments_dm_enabled=_bool("INSTAGRAM_COMMENTS_DM_ENABLED", True),
        instagram_comment_max_age_hours=_float("INSTAGRAM_COMMENT_MAX_AGE_HOURS", 48.0),
        instagram_comment_rate_limit=_int("INSTAGRAM_COMMENT_RATE_LIMIT", 3),
        instagram_faq_rate_limit=_int("INSTAGRAM_FAQ_RATE_LIMIT", 5),
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
        reconcile_report_on_boot=_bool("RECONCILE_REPORT_ON_BOOT", True),
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
        whatsapp_template_order_update=os.getenv("WHATSAPP_TEMPLATE_ORDER_UPDATE", "").strip(),
        whatsapp_template_feedback_request=os.getenv("WHATSAPP_TEMPLATE_FEEDBACK_REQUEST", "").strip(),
        whatsapp_template_order_confirmation=os.getenv(
            "WHATSAPP_TEMPLATE_ORDER_CONFIRMATION", ""
        ).strip(),
        whatsapp_template_language=os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "ar").strip() or "ar",
        # Each of these accepts a plain SMTP_* / STORE_OWNER_EMAIL alias
        # alongside the ALERT_-prefixed name. The prefixed name is the
        # documented one -- it says which feature the variable belongs to,
        # which matters in a Railway panel holding forty of them -- but the
        # generic set is what people paste out of a mail provider's own
        # instructions, and silently ignoring it would look exactly like the
        # feature not working.
        alert_email_to=_first_env("ALERT_EMAIL_TO", "STORE_OWNER_EMAIL"),
        # Gmail will not let you forge the From address, so the sensible
        # default is the mailbox doing the sending.
        alert_email_from=_first_env(
            "ALERT_EMAIL_FROM", "SMTP_FROM", "ALERT_SMTP_USERNAME", "SMTP_USER"
        ),
        alert_smtp_host=_first_env("ALERT_SMTP_HOST", "SMTP_HOST", default="smtp.gmail.com"),
        alert_smtp_port=_int("ALERT_SMTP_PORT", _int("SMTP_PORT", 587)),
        alert_smtp_username=_first_env("ALERT_SMTP_USERNAME", "SMTP_USER"),
        alert_smtp_password=_first_env("ALERT_SMTP_PASSWORD", "SMTP_PASS"),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        gmail_refresh_token=os.getenv("GMAIL_REFRESH_TOKEN", "").strip(),
        alert_email_cooldown_seconds=_float("ALERT_EMAIL_COOLDOWN_SECONDS", 900.0),
        alert_email_max_per_hour=_int("ALERT_EMAIL_MAX_PER_HOUR", 20),
    )


settings = load_settings()
