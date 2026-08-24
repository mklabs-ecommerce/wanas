"""Outbound Instagram delivery, direct against the Instagram Graph API.

Direct rather than through a BSP because that is what made the Phase 1
WhatsApp plan work, and the same reasoning holds here: Meta's Instagram Login
flavour (graph.instagram.com, an Instagram User Access Token, the *Instagram*
app secret for webhook signatures) runs the whole flow end to end with
nothing but credentials the shop itself owns. Swapping flavours later -- or
routing through a BSP -- touches the host, the auth header and the signature
check, and nothing else, provided the adapter stays behind this interface.

This lives in /backend/ and not /assistant/ on purpose, exactly as its WhatsApp
sibling does. The Notification service has to send confirmations and status
pushes, and /backend/ must never import /assistant/ -- the dependency direction
is one-way. Inbound message handling is the adapter's job and lives in
assistant/channels/instagram.py.

Two things this channel does not have, both deliberate:

* **No templates.** Instagram has no template concept at all -- there is no
  pre-approved-message mechanism, so proactive outreach outside a live
  conversation simply cannot be automated here. `send_template` therefore
  refuses with `error="instagram_has_no_templates"` rather than pretending;
  `notifications.send_proactive` then falls through to its staff-alert path,
  which is the correct outcome. Do not "fix" this by inventing a template
  name.
* **No media upload.** There is no upload endpoint; an outbound image is a
  **public HTTPS URL** Meta fetches itself. Local files go through
  `backend/public_media.py::public_url_for` (STEP 5), which is why that route
  exists at all.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from pathlib import Path

import httpx

from config.settings import PROJECT_ROOT, settings
from domain.services.notifications import OutboundMessage

log = logging.getLogger("wanas.instagram")

GRAPH = "https://graph.instagram.com"

#: Instagram rejects a `text` over ~1000 *bytes*. Arabic is two bytes per
#: character in UTF-8, so the real limit is roughly 500 characters -- inside
#: most replies, well outside a full sizing answer. The agent is never told
#: about this cap; the client splits. Chunks are kept comfortably under the
#: documented figure so the envelope JSON overhead cannot tip one over.
MAX_CHUNK_BYTES = 950

#: Quick replies: up to 13, titles capped at 20 characters. There is no
#: sectioned-list equivalent on this channel, so anything bigger than the cap
#: degrades to a numbered plain-text list (`_numbered_list_text`).
MAX_QUICK_REPLIES = 13
MAX_QUICK_TITLE = 20

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?؟…])\s+")


def _is_url(image_path: str) -> bool:
    """Whether this is already hosted somewhere, rather than a path on this
    process's own disk. Same signal as the WhatsApp client's helper: Shopify
    CDN links are the only URLs this value ever carries, local catalog files
    are always relative paths."""
    return image_path.startswith(("http://", "https://"))


def _split_paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n+", text) if p.strip()]


def _split_sentences(text: str) -> list[str]:
    return [p for p in _SENTENCE_BOUNDARY.split(text) if p.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    """Character-level last resort. Walks whole characters, never code-unit
    halves, so a chunk is always valid UTF-8 on its own."""
    pieces: list[str] = []
    current = ""
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if current and size + char_size > limit:
            pieces.append(current)
            current, size = "", 0
        current += char
        size += char_size
    if current.strip():
        pieces.append(current)
    return pieces


def _chunks(text: str, *, limit: int = MAX_CHUNK_BYTES) -> list[str]:
    """Split for the byte cap, at the most natural boundary left.

    Paragraph boundaries first, then sentence boundaries, then a hard
    character-level cut -- in that order, so every break lands where a person
    would have broken the text anyway. Each returned chunk is within `limit`
    bytes of UTF-8, and joining them back (whitespace aside) reproduces the
    original in order.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]

    for splitter in (_split_paragraphs, _split_sentences):
        parts = splitter(text)
        if len(parts) < 2:
            continue
        chunks: list[str] = []
        buffer = ""
        for part in parts:
            candidate = f"{buffer} {part}" if buffer else part
            if len(candidate.encode("utf-8")) <= limit:
                buffer = candidate
                continue
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_chunks(part, limit=limit))
        if buffer:
            chunks.append(buffer)
        return [c for c in chunks if c.strip()]

    return _hard_split(text, limit)


def _clip_title(title: str) -> str:
    """Titles are truncated by the client, not the caller -- a row one
    character over the cap costs an ellipsis, not the whole picker."""
    title = " ".join((title or "").split())
    return title if len(title) <= MAX_QUICK_TITLE else title[: MAX_QUICK_TITLE - 1].rstrip() + "…"


def _translate_quick_replies(payload: dict) -> tuple[list[dict] | None, str | None]:
    """The neutral shape -> Instagram quick replies.

    Returns `(quick_replies, None)` when the payload fits this channel,
    `(None, numbered_text)` when it must degrade to plain text, or
    `(None, None)` for a kind this channel does not know.
    """
    kind = payload.get("kind")

    if kind == "buttons":
        buttons = payload.get("buttons") or []
        return (
            [
                {
                    "content_type": "text",
                    "title": _clip_title(button.get("title") or button.get("id", "")),
                    "payload": str(button.get("id", "")),
                }
                for button in buttons
            ],
            None,
        )

    if kind == "list":
        rows = [
            row
            for section in payload.get("sections") or []
            for row in section.get("rows") or []
        ]
        if len(rows) > MAX_QUICK_REPLIES:
            return None, _numbered_list_text(payload, rows)
        return (
            [
                {
                    "content_type": "text",
                    "title": _clip_title(row.get("title") or row.get("id", "")),
                    "payload": str(row.get("id", "")),
                }
                for row in rows
            ],
            None,
        )

    return None, None


def _numbered_list_text(payload: dict, rows: list[dict]) -> str:
    """The >13-row degradation: the same choices as a numbered list, with an
    instruction to answer by number or name."""
    lines = [f"{index}. {row.get('title') or row.get('id')}" for index, row in enumerate(rows, start=1)]
    return "\n".join(
        [
            payload.get("body") or "",
            *lines,
            "ابعتلي برقم الاختيار، أو اكتب الاسم لو تحب.",
        ]
    ).strip()


class InstagramClient:
    """Implements the Notification service's OutboundSender port for Instagram."""

    def __init__(
        self,
        account_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        timeout: float = 20.0,
    ):
        self.account_id = account_id or settings.instagram_account_id
        if access_token is not None:
            self.access_token = access_token
        else:
            # A refreshed token is stored in the database
            # (`backend/services/instagram_token.py`); once a row exists it is
            # authoritative -- reading only the env var here would make every
            # refresh a write to nowhere.
            from integrations.instagram.token import stored_token

            self.access_token = stored_token() or settings.instagram_access_token
        self.api_version = api_version or settings.instagram_api_version
        self.timeout = timeout

    # -- helpers ----------------------------------------------------------

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _messages_url(self) -> str:
        return f"{GRAPH}/{self.api_version}/{self.account_id}/messages"

    @property
    def _configured(self) -> bool:
        return bool(self.account_id and self.access_token)

    def _post(self, payload: dict) -> tuple[bool, str | None]:
        ok, error, _body = self._post_json(self._messages_url(), payload)
        return ok, error

    def _post_json(self, url: str, payload: dict) -> tuple[bool, str | None, dict]:
        """POST and hand back the parsed response body (empty on failure)."""
        if not self._configured:
            # Inert until credentials exist, same contract as everything else
            # built before launch: observable in the log, never a crash.
            log.info(
                "instagram not configured: outbound logged, not sent: %s",
                str(payload.get("message"))[:120],
            )
            return False, "instagram_not_configured", {}
        try:
            response = httpx.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            log.error("instagram send failed: %s", exc)
            return False, str(exc), {}
        if response.status_code >= 400:
            log.error(
                "instagram send rejected %s: %s", response.status_code, response.text[:400]
            )
            return False, f"{response.status_code}: {response.text[:200]}", {}
        try:
            return True, None, response.json()
        except ValueError:
            return True, None, {}

    # -- the OutboundSender port -----------------------------------------

    def send_text(self, to: str, text: str, *, template: str | None = None) -> OutboundMessage:
        # `template` is accepted for protocol compatibility. Instagram has no
        # template mechanism (see the module docstring); the proactive path
        # learns that from `send_template`'s refusal, so here it is only noted.
        if template:
            log.info(
                "proactive message (%s) sent free-form; Instagram has no approved-template "
                "mechanism, so outside a live conversation this cannot deliver",
                template,
            )

        outcomes: list[tuple[bool, str | None]] = []
        for index, chunk in enumerate(_chunks(text)):
            ok, error = self._post(
                {
                    "recipient": {"id": to},
                    "message": {"text": chunk},
                }
            )
            outcomes.append((ok, error))
            if not ok:
                log.error("chunk %d of a reply to %s failed: %s", index + 1, to, error)

        delivered = all(ok for ok, _ in outcomes) if outcomes else True
        error = next((e for _, e in outcomes if e), None)
        return OutboundMessage(to=to, text=text, template=template, delivered=delivered, error=error)

    def send_image(self, to: str, image_path: str, *, caption: str = "") -> OutboundMessage:
        """An outbound picture -- a public URL, never an upload.

        Instagram has no media-upload endpoint: Meta fetches the URL itself,
        so a local file has to be published through
        `backend/public_media.py::public_url_for` first. A Shopify CDN link
        (`http(s)://...`) is already hosted and is sent as-is.

        Instagram attachments carry **no caption field** -- a caption is sent
        as a separate text message *before* the image, so a size chart still
        arrives with its explanation, in the order a person would send them.
        """
        from api.public_media import public_url_for

        if _is_url(image_path):
            url = image_path
        else:
            url = public_url_for(image_path)
            if url is None:
                # No PUBLIC_BASE_URL (or no signing secret, or a path that is
                # not publicly servable): posting nothing beats posting a URL
                # Meta cannot fetch, which reads to the customer as "sent"
                # followed by silence.
                return OutboundMessage(
                    to=to,
                    text=caption,
                    kind="image",
                    image_path=image_path,
                    delivered=False,
                    error="no_public_base_url",
                )

        if caption.strip():
            self.send_text(to, caption)

        ok, error = self._post(
            {
                "recipient": {"id": to},
                "message": {
                    "attachment": {"type": "image", "payload": {"url": url}},
                },
            }
        )
        return OutboundMessage(
            to=to, text=caption, kind="image", image_path=image_path, delivered=ok, error=error
        )

    def send_template(self, to: str, template: str, *, language: str = "ar") -> OutboundMessage:
        """A refusal, on purpose -- Instagram has no templates.

        WhatsApp needs a pre-approved template to open a conversation that has
        gone quiet; Instagram has no such concept and no such escape hatch.
        Returning `delivered=False` with a named error lets
        `notifications.send_proactive` fall through to its staff alert, which
        is the honest outcome: reaching this customer again needs a person,
        not a different payload. See the module docstring before changing
        this.
        """
        log.warning(
            "template %r requested for %s: Instagram has no template concept; "
            "falling back to the caller's staff-alert path",
            template,
            to,
        )
        return OutboundMessage(
            to=to,
            text=f"[template:{template}]",
            template=template,
            delivered=False,
            error="instagram_has_no_templates",
        )

    def send_interactive(self, to: str, payload: dict, *, fallback: str = "") -> OutboundMessage:
        """A tappable picker, from the neutral shape.

        Translation (`assistant/interactive.py`'s shapes -> Instagram):

        * `buttons` (never more than 3) -> one quick reply per button,
          `payload` = the button id.
        * `list` with up to 13 rows total -> one quick reply per row,
          `payload` = the row id, row descriptions dropped.
        * `list` with more than 13 rows -- in practice the 27-governorate
          picker, which Meta would reject outright -> a **numbered
          plain-text** list instead. The inbound side needs no special
          parsing for this: `backend/services/shipping.py::resolve` already
          handles free text and aliases, which is exactly the path this
          falls back to. Logged at INFO so a degraded picker is visible
          rather than mysterious.
        * Anything else -> plain text, like a rejected interactive message on
          WhatsApp. A picker that fails must never mean the customer is left
          with no reply at all.
        """
        body = payload.get("body") or fallback

        quick_replies, degraded_text = _translate_quick_replies(payload)
        if quick_replies:
            ok, error = self._post(
                {
                    "recipient": {"id": to},
                    "message": {
                        "text": body,
                        "quick_replies": quick_replies,
                    },
                }
            )
            if not ok:
                log.warning("quick replies rejected (%s); falling back to text", error)
                return self.send_text(to, fallback or body)
            return OutboundMessage(to=to, text=body, kind="interactive", delivered=True)

        if degraded_text is not None:
            # Too many rows for this channel; the numbered text carries the
            # same choices in the order they were given.
            return self.send_text(to, degraded_text)

        log.warning("unknown interactive kind %r; sending text instead", payload.get("kind"))
        return self.send_text(to, fallback or body)

    # -- sender actions ---------------------------------------------------

    def mark_seen(self, igsid: str) -> bool:
        """Best effort, like every sender action: never blocks the reply,
        never raises."""
        ok, error = self._post({"recipient": {"id": igsid}, "sender_action": "mark_seen"})
        if not ok:
            log.info("could not mark the conversation as seen: %s", error)
        return ok

    def typing_on(self, igsid: str) -> bool:
        ok, error = self._post({"recipient": {"id": igsid}, "sender_action": "typing_on"})
        if not ok:
            log.info("could not show the typing indicator: %s", error)
        return ok

    def mark_as_read(self, message_id: str) -> bool:
        """Protocol compatibility only.

        Instagram marks a whole conversation seen, not an individual message,
        so a message id carries nothing actionable here -- the adapter calls
        `mark_seen(igsid)` directly. Kept so the `OutboundSender` protocol
        holds; deliberately a no-op rather than a guessed network call.
        """
        return True

    # -- comments ---------------------------------------------------------

    def reply_to_comment(self, comment_id: str, text: str) -> OutboundMessage:
        """A visible public reply under one comment."""
        ok, error, _body = self._post_json(
            f"{GRAPH}/{self.api_version}/{comment_id}/replies",
            {"message": text},
        )
        return OutboundMessage(to=f"comment:{comment_id}", text=text, delivered=ok, error=error)

    def hide_comment(self, comment_id: str) -> bool:
        """Hide a comment from the post's public view.

        Deliberately NOT wired to the agent -- this exists for a future staff
        action and for the abuse path. Shipped unused rather than reached for
        later under pressure.
        """
        ok, error, _body = self._post_json(
            f"{GRAPH}/{self.api_version}/{comment_id}",
            {"hide": True},
        )
        if not ok:
            log.warning("could not hide comment %s: %s", comment_id, error)
        return ok

    def send_private_reply(self, comment_id: str, text: str) -> OutboundMessage:
        """The private reply to a comment -- the DM that starts the conversation.

        Meta allows **one** private reply per comment, ever, inside a 7-day
        window from when the comment was posted. Two layers make that a fact
        rather than a hope:

        * `InstagramCommentReply` (`backend/models.py`) records every handled
          comment; the comment handler writes its row *before* calling this,
          so a crash cannot open a second attempt.
        * This method itself refuses (`error="already_replied"`) when a
          completed private reply is already recorded.

        A successful response carries the resulting conversation's
        `recipient_id` -- that is the commenter's IGSID and how the DM thread
        that follows is keyed. It is returned as the message's `to`.
        """
        from domain.db import session_scope
        from domain.models import InstagramCommentReply

        with session_scope() as session:
            row = session.get(InstagramCommentReply, comment_id)
            if row is not None and row.private_replied:
                log.warning("blocked a second private reply to comment %s", comment_id)
                return OutboundMessage(
                    to="",
                    text=text,
                    delivered=False,
                    error="already_replied",
                )

        ok, error, body = self._post_json(
            self._messages_url(),
            {
                "recipient": {"comment_id": comment_id},
                "message": {"text": text},
            },
        )
        # The response carries the resulting conversation's recipient -- the
        # commenter's IGSID, which is what keys the DM thread that follows.
        recipient_id = str(body.get("recipient_id") or "")
        return OutboundMessage(to=recipient_id, text=text, delivered=ok, error=error)

    # -- media ------------------------------------------------------------

    def download_attachment(
        self,
        url: str,
        destination_dir: Path,
        *,
        default_extension: str = ".jpg",
    ) -> str | None:
        """Fetch an inbound attachment so it can be read, and so staff can see
        it.

        These URLs (lookaside CDN links, story images) are **pre-signed and
        short-lived**, and carry no authorisation of their own -- deliberately
        no Authorization header here: sending our API token to Meta's CDN
        would leak it into access logs far outside its intended audience.
        `default_extension` matters more than it looks, as in the WhatsApp
        client: an Instagram voice note arrives as `audio/mp4`, which some
        platforms' mime databases guess as `.m4a` -- the transcriber wants a
        name it recognises.
        """
        try:
            blob = httpx.get(url, timeout=60.0, follow_redirects=True)
            if blob.status_code >= 400:
                log.error("attachment download failed %s", blob.status_code)
                return None
        except httpx.HTTPError as exc:
            log.error("attachment download failed: %s", exc)
            return None

        destination_dir.mkdir(parents=True, exist_ok=True)
        content_type = (blob.headers.get("content-type") or "").split(";")[0].strip().lower()
        extension = mimetypes.guess_extension(content_type) if content_type else None
        if content_type == "audio/mp4":
            # Platform-dependent (.mp4 vs .m4a vs nothing); pin it.
            extension = ".mp4"
        extension = extension or default_extension
        # Attachments carry no media id, so the URL hash is the stable name:
        # the same link fetched twice writes the same file rather than piling up.
        stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        destination = destination_dir / f"{stem}{extension}"
        destination.write_bytes(blob.content)
        try:
            return str(destination.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:  # pragma: no cover - destination outside the project
            return str(destination)
