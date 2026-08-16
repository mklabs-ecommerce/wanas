"""Outbound WhatsApp delivery, direct against Meta's Cloud API.

Direct rather than through a BSP because that is what makes the Phase 1 test
plan work: Meta's free test number with verified test recipients runs the whole
flow end to end before business verification completes. Swapping to a BSP later
touches the send call, the signature check and template submission, and nothing
else, provided the adapter stays behind this interface.

This lives in /backend/ and not /chatbot/ on purpose. The Notification service
has to send confirmations and status pushes, and /backend/ must never import
/chatbot/ -- the dependency direction is one-way. Inbound message handling is
the adapter's job and lives in chatbot/channels/whatsapp.py.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

from backend.config import PROJECT_ROOT, settings
from backend.db import session_scope
from backend.models import WhatsAppMedia
from backend.services.notifications import OutboundMessage

log = logging.getLogger("wanas.whatsapp")

GRAPH = "https://graph.facebook.com"


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
        media_id = self.media_id_for(image_path)
        if media_id is None:
            return OutboundMessage(
                to=to, text=caption, kind="image", image_path=image_path, delivered=False, error="upload_failed"
            )
        ok, error = self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": self.normalise_recipient(to),
                "type": "image",
                # A real image message, not a link: a link in a DM looks like
                # spam and often is not tapped.
                "image": {"id": media_id, "caption": caption[:1024]} if caption else {"id": media_id},
            }
        )
        return OutboundMessage(
            to=to, text=caption, kind="image", image_path=image_path, delivered=ok, error=error
        )

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

    def download_media(self, media_id: str, destination_dir: Path) -> str | None:
        """Fetch an inbound photo so staff can actually see it in the queue."""
        try:
            meta = httpx.get(f"{GRAPH}/{self.api_version}/{media_id}", headers=self._headers, timeout=self.timeout)
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
        extension = mimetypes.guess_extension(blob.headers.get("content-type", "image/jpeg")) or ".jpg"
        destination = destination_dir / f"{media_id}{extension}"
        destination.write_bytes(blob.content)
        try:
            return str(destination.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except ValueError:  # pragma: no cover - destination outside the project
            return str(destination)
