"""Voice notes and photos, turned into something the agent can work with.

Phase 1 answered both by handing the conversation to a person. That was the
right call while the model could not see or hear anything -- a guess about a
garment the shop may not make is worse than a handoff. It is the wrong call
now, for two different reasons:

* **Voice notes are not an edge case in Egypt.** A large share of WhatsApp
  traffic is spoken, and "we'll get back to you" is the whole conversation for
  those customers. A transcript costs one call and puts them back in the
  normal flow, where every guardrail already applies.
* **A photo is a question, usually "do you have this?"** Answering it needs
  the model to *look*, but nothing here lets it answer from looking: the
  vision pass may only point at a product from a list built out of the real
  catalog, and what the customer is finally told still comes from
  `get_products` / `get_variants` like every other fact.

Both paths degrade the same way they always did. No key, no support for the
media type, an unreadable file, a low-confidence reading -- the message goes to
a person, exactly as before.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Product
from backend.services import runtime_flags
from chatbot.providers import LLMProvider, ProviderError
from chatbot.providers.base import ImageReading

log = logging.getLogger("wanas.media")

#: Anything larger is not a WhatsApp voice note or a phone photo; it is a
#: mistake, and uploading it would cost a slow call to find that out.
MAX_MEDIA_BYTES = 12 * 1024 * 1024

_AUDIO_MIME = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".wav": "audio/wav",
}

_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".gif": "image/gif",
}


def _read(path: str) -> tuple[bytes, str] | None:
    """File bytes and a mime type, or None if it is not usable.

    A media id we never managed to download is stored as
    `whatsapp-media:<id>` so staff can still chase it -- that is not a path and
    must not be opened.
    """
    if not path or path.startswith("whatsapp-media:"):
        return None
    target = Path(path)
    if not target.is_file():
        log.warning("media file %s is missing", path)
        return None
    size = target.stat().st_size
    if size == 0 or size > MAX_MEDIA_BYTES:
        log.warning("media file %s is %s bytes; skipping", path, size)
        return None

    suffix = target.suffix.lower()
    mime = _AUDIO_MIME.get(suffix) or _IMAGE_MIME.get(suffix)
    if mime is None:
        log.warning("unknown media extension %r for %s", suffix, path)
        return None
    return target.read_bytes(), mime


# --------------------------------------------------------------------------
# voice notes
# --------------------------------------------------------------------------


def transcribe_voice(session: Session, provider: LLMProvider, path: str, *, hint: str = "") -> str:
    """A voice note as text, or "" when it could not be read.

    Empty is not an error to shout about -- it is the documented signal that
    this message belongs to a person, and the caller falls back to the handoff
    it would have done anyway.
    """
    if not runtime_flags.get(session, "voice_notes_enabled", settings.voice_notes_enabled):
        return ""
    if not getattr(provider, "supports_audio", False):
        log.info("provider %s cannot transcribe; voice note goes to a person", provider.name)
        return ""

    payload = _read(path)
    if payload is None:
        return ""

    audio, mime = payload
    if not mime.startswith("audio/"):
        return ""

    try:
        transcript = provider.transcribe(audio, mime, hint=hint)
    except ProviderError as exc:
        log.warning("transcription failed (%s): %s", exc.kind, exc)
        return ""
    except Exception:
        log.exception("unexpected failure transcribing %s", path)
        return ""

    transcript = (transcript or "").strip()
    if not transcript:
        log.info("voice note %s produced no transcript", path)
    return transcript


# --------------------------------------------------------------------------
# photos
# --------------------------------------------------------------------------


def catalog_shortlist(session: Session) -> list[dict]:
    """The products a photo may be matched against.

    Built from the database every time rather than cached: a product added
    this morning has to be matchable this morning, and the list is eighteen
    rows.
    """
    products = session.scalars(select(Product).order_by(Product.name)).all()
    return [
        {
            "product_id": product.product_id,
            "name": product.name,
            "category": product.category,
            "colors": list(product.colors or []),
        }
        for product in products
    ]


def read_photo(session: Session, provider: LLMProvider, path: str) -> ImageReading | None:
    """What the photo shows, or None when nothing could be read.

    None means "hand it to a person" -- unconfigured, unsupported, unreadable,
    or the provider refused. It is not the same as a confident "this is not a
    garment", which is a real reading and handled by the caller.
    """
    if not runtime_flags.get(session, "image_understanding_enabled", settings.image_understanding_enabled):
        return None
    if not getattr(provider, "supports_vision", False):
        log.info("provider %s cannot read images; photo goes to a person", provider.name)
        return None

    payload = _read(path)
    if payload is None:
        return None

    image, mime = payload
    if not mime.startswith("image/"):
        return None

    shortlist = catalog_shortlist(session)
    if not shortlist:
        return None

    try:
        return provider.inspect_image(image, mime, catalog=shortlist)
    except ProviderError as exc:
        log.warning("image reading failed (%s): %s", exc.kind, exc)
        return None
    except Exception:
        log.exception("unexpected failure reading %s", path)
        return None


def matched_product(session: Session, reading: ImageReading) -> Product | None:
    """The product a reading points at, if it is confident enough to act on.

    The threshold is configuration, not a constant, because it is the dial
    between "the bot guesses" and "the bot asks" -- and which side of it a shop
    wants depends on how distinctive its catalog is.
    """
    if reading.product_id is None or reading.confidence < settings.image_match_confidence:
        return None
    return session.get(Product, reading.product_id)


def photo_context(reading: ImageReading, product: Product | None, caption: str = "") -> str:
    """The note the agent reads instead of the photo it cannot see.

    Written as a note *about* the message, in the same language the agent
    replies in, and it deliberately carries a product **name** rather than a
    product_id: a name is something the customer may safely be told, so a model
    that quotes it back has said nothing wrong, while an id echoed into a
    WhatsApp reply is a leak. The instruction to verify with the tools is not
    politeness -- the reading is a hint about which tool to call, and the price,
    the sizes and the stock still have to come from the tool.
    """
    parts = [caption.strip()] if caption.strip() else []
    parts.append("[الزبون بعت صورة]")

    if product is not None:
        parts.append(
            f"قراءة آلية للصورة: أقرب منتج عندنا هو {product.name}. "
            "اتأكد بنفسك بالأدوات قبل ما تقول أي حاجة عن السعر أو المقاسات أو التوفر، "
            "واسأل الزبون لو ده اللي هو قاصده."
        )
    elif not reading.is_garment:
        parts.append("قراءة آلية للصورة: الصورة دي مش قطعة هدوم.")
    else:
        described = reading.description or "قطعة هدوم مش واضح نوعها"
        parts.append(
            f"قراءة آلية للصورة: {described}. "
            "مفيش منتج عندنا مطابق ليها بشكل مؤكد — متقولش إن عندنا زيها، "
            "واسأل الزبون سؤال قصير يوضح هو بيدور على إيه، "
            "أو اعرض عليه أقرب حاجة موجودة فعلاً بعد ما تدور بالأدوات."
        )
    return " ".join(parts)
