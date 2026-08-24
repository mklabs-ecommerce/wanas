"""Voice notes and photos.

Both used to end the conversation: a photo or a voice note was queued for a
person and the bot went silent. That was correct while the model could neither
see nor hear. It is not correct now, and these tests pin down both the new
behaviour and the fallback, because the fallback is what runs whenever a key,
a provider or a file is missing.

The invariant that must survive all of it: a vision reading is a hint about
*which tool to call*, never a fact. Nothing here lets a photo produce a price,
a size or an "in stock".
"""

from __future__ import annotations

import dataclasses

import pytest

from assistant import media, runtime
from assistant.providers.base import ImageReading, ModelReply
from assistant.providers.fake import RehearsalProvider, ScriptedProvider
from config.settings import settings
from domain.models import QueueKind
from domain.services import queues

WHO = "201555222333"

#: Smallest thing that is unambiguously a file with the right extension. The
#: provider is scripted, so the bytes are never actually decoded.
BLOB = b"\x00\x01\x02\x03" * 64


@pytest.fixture()
def voice(tmp_path):
    path = tmp_path / "note.ogg"
    path.write_bytes(BLOB)
    return str(path)


@pytest.fixture()
def photo(tmp_path):
    path = tmp_path / "shot.jpg"
    path.write_bytes(BLOB)
    return str(path)


# --- voice notes ----------------------------------------------------------


def test_a_voice_note_becomes_an_ordinary_message(seeded, voice):
    provider = ScriptedProvider()
    provider.push_transcript("عايز الهودي الزيتي مقاس M")
    provider.push(ModelReply(text="تمام، ثانية واحدة 👌"))

    reply = runtime.handle_message(
        "whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=provider
    )

    assert reply.paused is False
    assert reply.transcript == "عايز الهودي الزيتي مقاس M"
    # The transcript is what the agent was actually asked about.
    _system, history, _tools = provider.calls[0]
    assert history[-1]["content"] == "عايز الهودي الزيتي مقاس M"
    # And no one was dragged into it.
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []


def test_the_audio_reaches_the_provider_with_its_mime_type(seeded, voice):
    provider = ScriptedProvider()
    provider.push_transcript("أيوه")
    provider.push(ModelReply(text="تمام"))

    runtime.handle_message("whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=provider)

    audio, mime = provider.audio_calls[0]
    assert audio == BLOB
    assert mime == "audio/ogg"


def test_a_provider_that_cannot_listen_hands_the_voice_note_over(seeded, voice):
    """The old behaviour, kept as the fallback rather than deleted."""
    reply = runtime.handle_message(
        "whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=RehearsalProvider()
    )

    assert reply.paused is True
    assert "صوتية" in (reply.text or "")
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "voice_received"
    assert item.payload["audio"] == [voice]


def test_an_empty_transcript_hands_the_voice_note_over(seeded, voice):
    """Silence, noise, or a language nothing could read."""
    provider = ScriptedProvider()
    provider.push_transcript("   ")

    reply = runtime.handle_message(
        "whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=provider
    )

    assert reply.paused is True
    assert queues.open_items(seeded, QueueKind.HANDOFF.value)[0].reason == "voice_received"


def test_a_missing_audio_file_does_not_raise(seeded):
    provider = ScriptedProvider()
    reply = runtime.handle_message(
        "whatsapp",
        WHO,
        "",
        audio_paths=["whatsapp-media:never-downloaded"],
        db=seeded,
        provider=provider,
    )
    assert reply.paused is True
    assert provider.audio_calls == []


def test_voice_notes_can_be_switched_off(seeded, voice, monkeypatch):
    monkeypatch.setattr(media, "settings", dataclasses.replace(settings, voice_notes_enabled=False))
    provider = ScriptedProvider()
    provider.push_transcript("مش المفروض يتقري")

    reply = runtime.handle_message(
        "whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=provider
    )

    assert reply.paused is True
    assert provider.audio_calls == []


# --- photos ---------------------------------------------------------------


def test_a_recognised_photo_keeps_the_conversation_with_the_bot(seeded, photo):
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.9, description="هودي زيتي"))
    provider.push(ModelReply(text="ده شكله WANAS Hoodie، أجيبلك المقاسات؟"))

    reply = runtime.handle_message(
        "whatsapp", WHO, "عندكم زي ده؟", image_paths=[photo], db=seeded, provider=provider
    )

    assert reply.paused is False
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []

    _system, history, _tools = provider.calls[0]
    asked = history[-1]["content"]
    assert "عندكم زي ده؟" in asked
    # The product **name**, never its id: a name is safe for the customer to
    # read back, an id is a leak.
    assert "WANAS Hoodie" in asked
    assert "wanas-hoodie" not in asked


def test_the_shortlist_comes_from_the_real_catalog(seeded, photo):
    provider = ScriptedProvider()
    provider.push_reading(ImageReading())
    provider.push(ModelReply(text="تحب أساعدك؟"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    _image, _mime, shortlist = provider.image_calls[0]
    names = {item["name"] for item in shortlist}
    assert "WANAS Hoodie" in names and "Worker Jacket" in names
    assert all(set(item) == {"product_id", "name", "category", "colors"} for item in shortlist)


def test_a_product_id_the_shop_does_not_have_is_not_a_match(seeded, photo):
    """The provider may only point at something on the list it was given."""
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="something-we-never-sold", confidence=0.99))
    provider.push(ModelReply(text="مش لاقي زيها بالظبط"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    _system, history, _tools = provider.calls[0]
    assert "something-we-never-sold" not in history[-1]["content"]


def test_a_low_confidence_reading_asks_instead_of_claiming(seeded, photo):
    provider = ScriptedProvider()
    provider.push_reading(
        ImageReading(product_id="wanas-hoodie", confidence=0.2, description="هودي غامق")
    )
    provider.push(ModelReply(text="ممكن تقوللي بتدور على إيه بالظبط؟"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    _system, history, _tools = provider.calls[0]
    asked = history[-1]["content"]
    assert "هودي غامق" in asked
    assert "WANAS Hoodie" not in asked
    # And it is told, in as many words, not to claim we have one.
    assert "متقولش" in asked


def test_a_photo_that_is_not_a_garment_is_flagged_as_such(seeded, photo):
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(is_garment=False, description="إيصال شحن"))
    provider.push(ModelReply(text="شايف الإيصال، ثانية واحدة"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    _system, history, _tools = provider.calls[0]
    assert "مش قطعة هدوم" in history[-1]["content"]


def test_a_provider_that_cannot_see_hands_the_photo_over(seeded, photo):
    reply = runtime.handle_message(
        "whatsapp", WHO, "زي كده", image_paths=[photo], db=seeded, provider=RehearsalProvider()
    )

    assert reply.paused is True
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "image_received"
    assert item.payload["images"] == [photo]


def test_multiple_photos_are_all_read_not_just_the_first(seeded, tmp_path):
    """image_paths[0] used to be the only one ever looked at -- a second
    photo in the same batch was silently never shown to the model at all."""
    photo1 = tmp_path / "a.jpg"
    photo1.write_bytes(BLOB)
    photo2 = tmp_path / "b.jpg"
    photo2.write_bytes(BLOB)
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.9, description="هودي زيتي"))
    provider.push_reading(ImageReading(product_id="worker-jacket", confidence=0.9, description="جاكيت"))
    provider.push(ModelReply(text="تمام"))

    reply = runtime.handle_message(
        "whatsapp",
        WHO,
        "عايز ده وده",
        image_paths=[str(photo1), str(photo2)],
        db=seeded,
        provider=provider,
    )

    assert reply.paused is False
    assert len(provider.image_calls) == 2
    _system, history, _tools = provider.calls[0]
    asked = history[-1]["content"]
    assert "عايز ده وده" in asked
    assert "Photo 1:" in asked
    assert "Photo 2:" in asked
    assert "WANAS Hoodie" in asked
    assert "Worker Jacket" in asked


def test_a_single_photo_gets_no_numbered_label(seeded, photo):
    """The common case must read exactly as it always did -- no "Photo 1:"
    noise when there is only one."""
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.9, description="هودي"))
    provider.push(ModelReply(text="تمام"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    _system, history, _tools = provider.calls[0]
    assert "Photo 1:" not in history[-1]["content"]


def test_an_unreadable_photo_alongside_a_readable_one_is_silently_skipped(seeded, tmp_path):
    photo2 = tmp_path / "b.jpg"
    photo2.write_bytes(BLOB)
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.9, description="هودي"))
    provider.push(ModelReply(text="تمام"))

    reply = runtime.handle_message(
        "whatsapp",
        WHO,
        "",
        image_paths=["whatsapp-media:never-downloaded", str(photo2)],
        db=seeded,
        provider=provider,
    )

    assert reply.paused is False
    assert len(provider.image_calls) == 1


def test_multiple_voice_notes_are_all_transcribed(seeded, tmp_path):
    voice1 = tmp_path / "a.ogg"
    voice1.write_bytes(BLOB)
    voice2 = tmp_path / "b.ogg"
    voice2.write_bytes(BLOB)
    provider = ScriptedProvider()
    provider.push_transcript("عايز الهودي الزيتي")
    provider.push_transcript("مقاس M لو سمحت")
    provider.push(ModelReply(text="تمام"))

    reply = runtime.handle_message(
        "whatsapp", WHO, "", audio_paths=[str(voice1), str(voice2)], db=seeded, provider=provider
    )

    assert reply.paused is False
    assert len(provider.audio_calls) == 2
    assert reply.transcript == "عايز الهودي الزيتي\nمقاس M لو سمحت"


# --- history keeps the real media, not just its text ----------------------


def test_a_successful_voice_note_keeps_the_audio_in_history(seeded, voice):
    from assistant import session as session_store

    provider = ScriptedProvider()
    provider.push_transcript("عايز الهودي الزيتي")
    provider.push(ModelReply(text="تمام"))

    runtime.handle_message("whatsapp", WHO, "", audio_paths=[voice], db=seeded, provider=provider)

    history = session_store.load(seeded, "whatsapp", WHO)
    turn = next(m for m in history if m.get("role") == "user")
    assert turn["audio"] == [voice]
    # The audio is additional, never a replacement for the transcript text
    # the model actually answered.
    assert turn["content"] == "عايز الهودي الزيتي"


def test_a_successful_photo_keeps_the_image_in_history(seeded, photo):
    from assistant import session as session_store

    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.9, description="هودي"))
    provider.push(ModelReply(text="تمام"))

    runtime.handle_message("whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider)

    history = session_store.load(seeded, "whatsapp", WHO)
    turn = next(m for m in history if m.get("role") == "user")
    assert turn["images"] == [photo]


def test_an_unreadable_photo_still_keeps_the_image_on_the_handoff_message(seeded, photo):
    from assistant import session as session_store

    reply = runtime.handle_message(
        "whatsapp", WHO, "زي كده", image_paths=[photo], db=seeded, provider=RehearsalProvider()
    )
    assert reply.paused is True

    history = session_store.load(seeded, "whatsapp", WHO)
    turn = next(m for m in history if m.get("role") == "user")
    assert turn["images"] == [photo]


def test_image_understanding_can_be_switched_off(seeded, photo, monkeypatch):
    monkeypatch.setattr(
        media, "settings", dataclasses.replace(settings, image_understanding_enabled=False)
    )
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.99))

    reply = runtime.handle_message(
        "whatsapp", WHO, "", image_paths=[photo], db=seeded, provider=provider
    )

    assert reply.paused is True
    assert provider.image_calls == []


# --- the shared file guard ------------------------------------------------


def test_an_oversized_file_is_refused_before_it_is_uploaded(seeded, tmp_path):
    fat = tmp_path / "huge.jpg"
    fat.write_bytes(b"0" * (media.MAX_MEDIA_BYTES + 1))
    provider = ScriptedProvider()
    provider.push_reading(ImageReading(product_id="wanas-hoodie", confidence=0.99))

    assert media.read_photo(seeded, provider, str(fat)) is None
    assert provider.image_calls == []


def test_an_unknown_extension_is_refused(seeded, tmp_path):
    odd = tmp_path / "thing.bin"
    odd.write_bytes(BLOB)
    provider = ScriptedProvider()
    provider.push_transcript("hello")

    assert media.transcribe_voice(seeded, provider, str(odd)) == ""
    assert provider.audio_calls == []
