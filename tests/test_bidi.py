"""Arabic with English inside it, laid out so the customer reads what was sent.

Four things went wrong before this, all of them invisible in the stored
transcript and all of them visible on the phone: a bullet line opening with a
product name flipped to left-to-right while its neighbours stayed
right-to-left, two Latin colours either side of an Arabic comma swapped
places, and a full stop after a closing English word jumped to the far left
of the line. See `common/bidi.py` for why the algorithm does that.
"""

from __future__ import annotations

from common.bidi import FSI, PDI, RLM, shape, unshape


def test_a_latin_run_is_isolated_as_one_object():
    """`WANAS Hoodie` is one thing to lay out, not two words with a loose
    space between them that the algorithm is free to reorder."""
    assert shape("عندنا WANAS Hoodie دلوقتي") == f"{RLM}عندنا {FSI}WANAS Hoodie{PDI} دلوقتي"


def test_two_colours_keep_their_order_across_an_arabic_comma():
    """The bug this closes: `Olive، Black` was *displayed* `Black ،Olive`,
    because a neutral between two Latin runs resolves to the paragraph's
    right-to-left direction. Isolating each run makes the pair opaque, so the
    comma separates them in the order they were written."""
    out = shape("الألوان Olive، Black")
    assert out == f"{RLM}الألوان {FSI}Olive{PDI}، {FSI}Black{PDI}"


def test_a_line_opening_with_a_product_name_still_reads_right_to_left():
    """Paragraph direction comes from the first strong character, and an
    isolate's contents are skipped when it is chosen -- which is the whole
    reason a bullet list of English product names stopped coming out
    half-mirrored."""
    out = shape("• Boxy WNS Tee — 450 جنيه")
    assert out.startswith(RLM)
    assert f"{FSI}Boxy WNS Tee{PDI}" in out


def test_every_line_is_marked_not_just_the_first():
    """A message is laid out a line at a time, so a line with no Arabic left
    in it would pick its own direction and land mirrored between two that did
    not."""
    out = shape("الألوان المتاحة:\n• WANAS Hoodie — XL\n• Boxy Tee — L")
    assert all(line.startswith(RLM) for line in out.split("\n"))


def test_a_blank_line_is_left_blank():
    assert shape("تمام\n\nالسعر 450 جنيه").split("\n")[1] == ""


def test_text_with_no_arabic_is_left_exactly_alone():
    """An English-only message already lays out correctly, and invisible
    control characters must never be added to text that does not need them."""
    for text in ("Boxy WNS Tee — XL", "450", "", "  "):
        assert shape(text) == text


def test_shaping_is_idempotent():
    once = shape("عندنا WANAS Hoodie بـ 450 جنيه")
    assert shape(once) == once


def test_unshape_gives_back_exactly_what_was_written():
    """What is stored in `sessions` is the unshaped text; this is the check
    that shaping adds nothing but the invisible marks."""
    original = "عندنا WANAS Hoodie بـ 450 جنيه\n• Olive و Black\nتحب أنهي لون؟"
    assert unshape(shape(original)) == original


def test_arabic_letters_are_never_wrapped():
    assert FSI not in shape("تمام يا فندم، هبعتلك المقاسات")


def test_a_number_on_its_own_is_not_isolated():
    """Digits inside an Arabic sentence already lay out correctly, and every
    invisible character added is one more thing that can be split by a chunker
    or copied into a search box."""
    assert shape("السعر 450 جنيه") == f"{RLM}السعر 450 جنيه"


def test_a_range_is_isolated_so_it_does_not_read_backwards():
    """The fourth failure, and the one that changes a fact rather than the
    look of a line. Rule W2 gives a European digit the number type of the last
    strong character before it, so inside an Arabic sentence `2` and `4` are
    Arabic numbers -- and a hyphen between two Arabic numbers is not the number
    separator it is between two European ones. It becomes a plain neutral, takes
    the paragraph direction, and «من 2-4 أيام» is displayed "من 4-2 أيام".

    Verified in a browser by measuring each character's x-position, not by
    reading the rule: before this, `4` sat to the left of `2`.
    """
    assert shape("التوصيل بياخد من 2-4 أيام") == f"{RLM}التوصيل بياخد من {FSI}2-4{PDI} أيام"


def test_a_spaced_range_with_an_en_dash_is_isolated_too():
    assert shape("مقاسات 38 – 42 متاحة") == f"{RLM}مقاسات {FSI}38 – 42{PDI} متاحة"


def test_a_decimal_and_a_thousands_mark_are_left_alone():
    """A full stop and a comma are common separators, which the algorithm
    *does* carry between two Arabic numbers, so these already lay out right.
    Only the shapes that are actually broken get an isolate."""
    assert shape("الوزن 1.5 كيلو") == f"{RLM}الوزن 1.5 كيلو"
    assert shape("الإجمالي 1,100 جنيه") == f"{RLM}الإجمالي 1,100 جنيه"


def test_a_dash_between_two_numbers_that_are_not_a_range_is_left_alone():
    """`3 ألوان — 2 منهم` has Arabic between the numbers, so it is two separate
    numbers in a sentence and not a range at all."""
    assert FSI not in shape("عندنا 3 ألوان — 2 منهم متاح")


def test_shaping_twice_changes_nothing():
    """The docstring promised idempotence and the guard only looked for the
    isolate, so a line with no Latin run and no range -- which gets only the
    right-to-left mark -- picked up a second mark on every further pass."""
    for original in (
        "السعر 450 جنيه",
        "تمام يا فندم",
        "عندنا WANAS Hoodie دلوقتي",
        "التوصيل بياخد من 2-4 أيام",
    ):
        once = shape(original)
        assert shape(once) == once
        assert unshape(once) == original
