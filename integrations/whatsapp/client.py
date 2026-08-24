"""Outbound WhatsApp delivery, direct against Meta's Cloud API.

Direct rather than through a BSP because that is what makes the Phase 1 test
plan work: Meta's free test number with verified test recipients runs the whole
flow end to end before business verification completes. Swapping to a BSP later
touches the send call, the signature check and template submission, and nothing
else, provided the adapter stays behind this interface.

This lives in /backend/ and not /assistant/ on purpose. The Notification service
has to send confirmations and status pushes, and /backend/ must never import
/assistant/ -- the dependency direction is one-way. Inbound message handling is
the adapter's job and lives in assistant/channels/whatsapp.py.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

from config.settings import PROJECT_ROOT, settings
from domain.db import session_scope
from domain.models import WhatsAppMedia
from domain.services.notifications import OutboundMessage

log = logging.getLogger("wanas.whatsapp")

GRAPH = "https://graph.facebook.com"


def _is_url(image_path: str) -> bool:
    """Whether this is already hosted somewhere, rather than a path on this
    process's own disk. `data/images/...` and `data/size-charts/...` are the
    only other shape this value ever takes, so http(s) is an unambiguous
    signal without needing a config flag to say which products came from
    Shopify."""
    return image_path.startswith(("http://", "https://"))


class WhatsAppClient:
    """Implements the Notification service's OutboundSender port."""

    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        timeout: float = 20.0,
    ):
        self.phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        self.access_token = access_token or settings.whatsapp_access_token
        self.api_version = api_version or settings.whatsapp_api_version
        self.timeout = timeout

    # -- helpers ----------------------------------------------------------

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _messages_url(self) -> str:
        return f"{GRAPH}/{self.api_version}/{self.phone_number_id}/messages"

    @staticmethod
    def normalise_recipient(phone: str) -> str:
        """Meta wants an international number with no plus or separators.

        A customer types 01001234567; the API needs 201001234567.
        """
        digits = "".join(ch for ch in (phone or "") if ch.isdigit())
        if digits.startswith("00"):
            digits = digits[2:]
        if digits.startswith("0"):
            digits = "20" + digits[1:]
        return digits

    def _post(self, payload: dict) -> tuple[bool, str | None]:
        try:
            response = httpx.post(
                self._messages_url(), json=payload, headers=self._headers, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            log.error("whatsapp send failed: %s", exc)
            return False, str(exc)
        if response.status_code >= 400:
            log.error("whatsapp send rejected %s: %s", response.status_code, response.text[:400])
            return False, f"{response.status_code}: {response.text[:200]}"
        return True, None

    # -- the OutboundSender port -----------------------------------------

    def send_text(self, to: str, text: str, *, template: str | None = None) -> OutboundMessage:
        # Proactive messages (confirmations, status pushes, feedback requests)
        # need a pre-approved template outside an open customer conversation.
        # Templates are an external dependency with a days-to-weeks lead time,
        # so until they are approved this sends free-form text -- which works
        # for the test recipients and inside the 24-hour window, and is logged
        # loudly when it is a proactive message so the gap stays visible.
        if template:
            log.info("proactive message (%s) sent free-form; approved template required at launch", template)

        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.normalise_recipient(to),
                "type": "text",
                "text": {"preview_url": False, "body": text},
            }
        )
        return OutboundMessage(to=to, text=text, template=template, delivered=ok, error=error)

    def send_image(self, to: str, image_path: str, *, caption: str = "") -> OutboundMessage:
        if _is_url(image_path):
            # Already hosted -- Shopify's CDN today, potentially another host
            # later. Meta fetches the link itself, so there is nothing here to
            # upload or cache; that machinery exists for the files this process
            # actually has on disk (size charts, and any product photo Shopify
            # has no picture for yet).
            image_field = {"link": image_path}
            if caption:
                image_field["caption"] = caption[:1024]
            ok, error = self._post(
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": self.normalise_recipient(to),
                    "type": "image",
                    "image": image_field,
                }
            )
            return OutboundMessage(
                to=to, text=caption, kind="image", image_path=image_path, delivered=ok, error=error
            )

        media_id = self.media_id_for(image_path)
        if media_id is None:
            return OutboundMessage(
                to=to,
                text=caption,
                kind="image",
                image_path=image_path,
                delivered=False,
                error="upload_failed",
            )
        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.normalise_recipient(to),
                "type": "image",
                "image": (
                    {"id": media_id, "caption": caption[:1024]} if caption else {"id": media_id}
                ),
            }
        )
        return OutboundMessage(
            to=to, text=caption, kind="image", image_path=image_path, delivered=ok, error=error
        )

    def send_template(self, to: str, template: str, *, language: str = "ar") -> OutboundMessage:
        """A pre-approved template message -- the only kind Meta allows once
        the customer's last message is more than 24 hours old.

        No components/variables: every template this app sends today is a
        static "here's what's new" nudge with the specifics already spoken
        for in the approved copy, not filled in at send time. A template that
        needs body parameters would extend this, not replace it.
        """
        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.normalise_recipient(to),
                "type": "template",
                "template": {"name": template, "language": {"code": language}},
            }
        )
        return OutboundMessage(
            to=to, text=f"[template:{template}]", template=template, delivered=ok, error=error
        )

    def send_interactive(self, to: str, payload: dict, *, fallback: str = "") -> OutboundMessage:
        """A tappable list or button row, from the neutral shape.

        This is the only place Meta's interactive JSON is written. An unknown
        `kind`, or an interactive message Meta rejects, falls back to sending
        the text -- a picker that fails must never mean the customer is left
        with no reply at all.
        """
        body = payload.get("body") or fallback
        built = self._interactive_payload(payload)
        if built is None:
            log.warning("unknown interactive kind %r; sending text instead", payload.get("kind"))
            return self.send_text(to, fallback or body)

        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.normalise_recipient(to),
                "type": "interactive",
                "interactive": built,
            }
        )
        if not ok:
            log.warning("interactive message rejected (%s); falling back to text", error)
            return self.send_text(to, fallback or body)
        return OutboundMessage(to=to, text=body, kind="interactive", delivered=True)

    @staticmethod
    def _interactive_payload(payload: dict) -> dict | None:
        kind = payload.get("kind")
        body = {"text": payload.get("body") or ""}

        if kind == "list":
            return {
                "type": "list",
                "body": body,
                "action": {
                    "button": payload.get("button") or "اختار",
                    "sections": [
                        {
                            **({"title": section["title"]} if section.get("title") else {}),
                            "rows": [
                                {
                                    "id": row["id"],
                                    "title": row["title"],
                                    **(
                                        {"description": row["description"]}
                                        if row.get("description")
                                        else {}
                                    ),
                                }
                                for row in section.get("rows") or []
                            ],
                        }
                        for section in payload.get("sections") or []
                    ],
                },
            }

        if kind == "buttons":
            return {
                "type": "button",
                "body": body,
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": button["id"], "title": button["title"]}}
                        for button in payload.get("buttons") or []
                    ]
                },
            }
        return None

    # -- media ------------------------------------------------------------

    def media_id_for(self, image_path: str) -> str | None:
        """Upload once, reuse forever.

        There are twelve size charts and they change rarely; re-uploading a
        several-hundred-KB PNG on every sizing question is a slow reply for no
        reason.
        """
        with session_scope() as session:
            cached = session.get(WhatsAppMedia, image_path)
            if cached is not None:
                return cached.media_id

        media_id = self._upload(image_path)
        if media_id is None:
            return None

        with session_scope() as session:
            if session.get(WhatsAppMedia, image_path) is None:
                session.add(WhatsAppMedia(path=image_path, media_id=media_id))
        return media_id

    def _upload(self, image_path: str) -> str | None:
        path = Path(image_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            log.error("cannot upload %s: file not found", path)
            return None

        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        try:
            with open(path, "rb") as fh:
                response = httpx.post(
                    f"{GRAPH}/{self.api_version}/{self.phone_number_id}/media",
                    headers=self._headers,
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (path.name, fh, mime)},
                    timeout=60.0,
                )
        except httpx.HTTPError as exc:
            log.error("media upload failed: %s", exc)
            return None
        if response.status_code >= 400:
            log.error("media upload rejected %s: %s", response.status_code, response.text[:300])
            return None
        return response.json().get("id")

    def mark_as_read(self, message_id: str) -> bool:
        """Turn the customer's ticks blue, and show a typing indicator.

        An agent turn takes seconds -- a model call, sometimes a Shopify read.
        Without this the customer watches an unread message the whole time and
        sends it again, which is a second inbound message and a second reply.
        Best effort by design: a failure here must never cost the actual answer.
        """
        if not message_id:
            return False
        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                # Cleared automatically when the next message is sent, or
                # after 25 seconds, whichever comes first.
                "typing_indicator": {"type": "text"},
            }
        )
        if not ok:
            log.info("could not mark %s as read: %s", message_id, error)
        return ok

    def download_media(
        self, media_id: str, destination_dir: Path, *, default_extension: str = ".jpg"
    ) -> str | None:
        """Fetch inbound media so it can be read, and so staff can see it.

        `default_extension` matters more than it looks: a voice note arrives as
        `audio/ogg; codecs=opus`, which `guess_extension` cannot read with the
        parameter attached, and a `.opus` file saved as `.jpg` is one the
        transcriber refuses.
        """
        try:
            meta = httpx.get(
                f"{GRAPH}/{self.api_version}/{media_id}",
                headers=self._headers,
                timeout=self.timeout,
            )
            if meta.status_code >= 400:
                log.error("media lookup failed %s: %s", meta.status_code, meta.text[:200])
                return None
            url = meta.json().get("url")
            if not url:
                return None
            blob = httpx.get(url, headers=self._headers, timeout=60.0)
            if blob.status_code >= 400:
                log.error("media download failed %s", blob.status_code)
                return None
        except httpx.HTTPError as exc:
            log.error("media download failed: %s", exc)
            return None

        destination_dir.mkdir(parents=True, exist_ok=True)
        content_type = (blob.headers.get("content-type") or "").split(";")[0].strip()
        extension = mimetypes.guess_extension(content_type) if content_type else None
        if content_type in {"audio/ogg", "audio/opus"}:
            # Both map to `.oga` or nothing depending on the platform's mime
            # database; the transcriber wants a name it recognises.
            extension = ".ogg"
        extension = extension or default_extension
        destination = destination_dir / f"{media_id}{extension}"
        destination.write_bytes(blob.content)
        try:
            return str(destination.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:  # pragma: no cover - destination outside the project
            return str(destination)
