"""A customer's photo of a pink tee was answered «ده Gray، والـ Gray متوفر».

The vision reading carried no colour at all: the model matched the product,
the colour lived only in a free description, and `photo_context` dropped that
description whenever a product matched -- so the agent picked a colour itself.
The reading now names the colour it saw, mapped by family (never by nearness)
onto the product's own colours, and a colour it is unsure of is asked about.
"""

from __future__ import annotations

from assistant import media
from assistant.providers.base import ImageReading
from domain.models import Product


def _tops() -> Product:
    return Product(
        product_id="rehla-original-tops",
        name="Rehla Original Tops",
        colors=["Black⬛️", "Gray", "Pink🌸", "White"],
    )


def _reading(color: str, confidence: float = 0.9) -> ImageReading:
    return ImageReading(
        product_id="rehla-original-tops", confidence=0.9, color=color, color_confidence=confidence
    )


def test_pink_maps_to_the_pink_variant_never_gray():
    assert media.photo_color(_reading("pink"), _tops()) == "Pink🌸"
    assert media.photo_color(_reading("وردي"), _tops()) == "Pink🌸"
    note = media.photo_context(_reading("pink"), _tops())
    assert "Pink" in note and "Gray" not in note


def test_gray_still_maps_to_gray():
    assert media.photo_color(_reading("light grey"), _tops()) == "Gray"


def test_a_colour_the_product_lacks_is_not_snapped_to_a_neighbour():
    note = media.photo_context(_reading("lavender"), _tops())
    assert media.photo_color(_reading("lavender"), _tops()) is None
    assert "Gray" not in note and "Pink" not in note
    assert "مش من ألوان" in note


def test_an_unsure_colour_is_asked_about_not_asserted():
    note = media.photo_context(_reading("pink", confidence=0.3), _tops())
    assert "اسأل الزبون" in note
    assert "color=" not in note


def test_openrouter_reads_the_colour_fields():
    from assistant.providers.base import parse_image_color

    assert parse_image_color({"color": " pink ", "color_confidence": "0.8"}) == ("pink", 0.8)
    assert parse_image_color({}) == ("", 0.0)
