"""Laying out a message that is Arabic with English inside it.

Almost every reply this shop sends is bidirectional. The prompt keeps product
names, sizes and colours in Latin on purpose -- `WANAS Hoodie`, `XL`, `Olive`
are what is printed on the label and what the customer searches for -- so an
Arabic sentence with Latin islands in it is the *normal* case here, not an
edge one.

Left to the plain Unicode bidirectional algorithm, that goes wrong in three
ways a customer can see:

* **A line that starts with a Latin word takes left-to-right paragraph
  direction.** The renderer picks the direction from the first strong
  character (UAX #9, rule P2), so `• Boxy WNS Tee — 450 جنيه` is laid out
  left-aligned in the middle of a right-aligned message. Every bullet line
  that opens with a product name comes out mirrored from its neighbours,
  which is what a jagged, half-flipped list actually is.
* **A neutral between two Latin runs resolves to the paragraph's direction.**
  In an Arabic message `Olive، Black` is *displayed* `Black ،Olive`: the two
  colours swap places and the comma lands on the wrong one. The customer
  reads a different answer from the one that was sent.
* **Trailing punctuation jumps.** A full stop after a closing Latin word is a
  neutral at the end of an RTL paragraph, so it is pushed to the far left of
  the line, away from the sentence it ends.
* **A range reads backwards.** «التوصيل بياخد من 2-4 أيام» is *displayed*
  "من 4-2 أيام". Rule W2 gives a European digit the number type of the last
  strong character before it, so in Arabic text `2` and `4` resolve as Arabic
  numbers -- and a hyphen between two Arabic numbers is not the number
  separator it is between two European ones. It becomes a plain neutral, takes
  the paragraph direction, and the two ends of the range swap. This one is not
  cosmetic: it is a delivery window, a price or a measurement stating the
  opposite of what was written.

None of that is the model's fault and none of it is reliably fixable by
asking it nicely, so it is fixed here instead -- the same "a prompt
instruction is a preference, a deterministic pass is a guarantee" split the
rest of this codebase runs on. The prompt still asks for Arabic-first lines,
because text that needs no repair is better than text that got repaired.

**How.** Every Latin run *and every number* is wrapped in FIRST STRONG
ISOLATE ... POP DIRECTIONAL ISOLATE (U+2068 / U+2069). An isolate is one
opaque neutral object to the text around it while staying left-to-right
inside itself, and its contents are skipped when the paragraph direction is
chosen -- so a line opening with a product name is still laid out
right-to-left with the rest of the message, and a number inside one has no
preceding Arabic letter to take its type from. Each non-empty line then gets
a RIGHT-TO-LEFT MARK, which states
the direction outright for the lines that have no Arabic left in them at all
(`• WANAS Hoodie — XL`) and would otherwise flip on their own.

**Where it does and does not apply.** Only to text that actually contains
Arabic: an all-English message is already laid out correctly, and invisible
control characters should never be added to text that does not need them.
And only at the send boundary -- what is stored in `sessions` stays exactly
what was written, so the dashboard, the tests and every search over the
transcript see plain text.

The characters are invisible and have been in Unicode since 6.3 (2013);
WhatsApp and Instagram both lay text out with the platform text engine, which
implements them. A renderer that did not would drop them, not draw them.
"""

from __future__ import annotations

import re

#: U+2068 FIRST STRONG ISOLATE / U+2069 POP DIRECTIONAL ISOLATE.
FSI = "⁨"
PDI = "⁩"
#: U+200F RIGHT-TO-LEFT MARK -- a zero-width strong RTL character.
RLM = "‏"

#: Arabic proper, Arabic Supplement, and the presentation forms a copied
#: string can arrive in. Enough to answer "is this line Arabic", which is all
#: it is used for.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

#: A Latin run: a word, plus the words joined to it by a single space or one
#: of the connectors a product name uses. Greedy across spaces on purpose --
#: `WANAS Hoodie` is one object to lay out, not two, and isolating each word
#: separately would leave the space between them free to be reordered.
_LATIN_RUN = r"[A-Za-z][A-Za-z0-9]*(?:[ /&'’.+\-][A-Za-z0-9]+)*"

#: ASCII and Arabic-Indic digits.
_DIGIT = "0-9٠-٩۰-۹"

#: A range, and *only* a range -- `2-4`, `38 – 42`. It is the one number shape
#: the plain algorithm gets wrong, and the reason is rule W2: a European digit
#: takes its number type from the last strong character before it, so inside an
#: Arabic sentence `2` and `4` resolve as *Arabic* numbers. A hyphen between two
#: European numbers is a number separator (W4) and survives; between two Arabic
#: ones it is not. It falls through to an ordinary neutral, takes the
#: paragraph's right-to-left direction, and the two ends swap -- «التوصيل بياخد
#: من 2-4 أيام» is *displayed* "من 4-2 أيام". That is a delivery window, a price
#: or a measurement saying the opposite of what was written.
#:
#: A lone number is deliberately left alone, and so are `1.5` and `1,100`: a
#: full stop and a comma are common separators, which W4 *does* carry between
#: two Arabic numbers, so those already lay out correctly. Every invisible
#: character added is one more thing to be split by a chunker or pasted into a
#: search box, so this adds them only where the text is otherwise wrong.
_NUMBER_RANGE = rf"[{_DIGIT}]+(?:\s*[-‐‑‒–—―]\s*[{_DIGIT}]+)+"

#: Latin first on purpose: `L 590` is one run to the Latin pattern (a size and
#: the price beside it belong together), and only a token that starts with a
#: digit can fall through to the range pattern.
_RUN = re.compile(rf"{_LATIN_RUN}|{_NUMBER_RANGE}")


def shape(text: str) -> str:
    """Return `text` laid out to survive a bidirectional renderer.

    Idempotent, and a no-op on anything with no Arabic in it or that has
    already been shaped.
    """
    # RLM as well as FSI: a line with no Latin run and no range gets only the
    # mark, so checking for the isolate alone left exactly those lines
    # re-shapable, and a second pass prepended a second mark.
    if not text or FSI in text or RLM in text or not _ARABIC.search(text):
        return text

    lines = []
    for line in text.split("\n"):
        isolated = _RUN.sub(lambda m: f"{FSI}{m.group(0)}{PDI}", line)
        lines.append(f"{RLM}{isolated}" if isolated.strip() else isolated)
    return "\n".join(lines)


def unshape(text: str) -> str:
    """Strip what `shape` added -- for comparing a sent message with its source."""
    return (text or "").replace(FSI, "").replace(PDI, "").replace(RLM, "")
