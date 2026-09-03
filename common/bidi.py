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

None of that is the model's fault and none of it is reliably fixable by
asking it nicely, so it is fixed here instead -- the same "a prompt
instruction is a preference, a deterministic pass is a guarantee" split the
rest of this codebase runs on. The prompt still asks for Arabic-first lines,
because text that needs no repair is better than text that got repaired.

**How.** Every Latin run is wrapped in FIRST STRONG ISOLATE ... POP
DIRECTIONAL ISOLATE (U+2068 / U+2069). An isolate is one opaque neutral
object to the text around it while staying left-to-right inside itself, and
its contents are skipped when the paragraph direction is chosen -- so a line
opening with a product name is still laid out right-to-left with the rest of
the message. Each non-empty line then gets a RIGHT-TO-LEFT MARK, which states
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
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[ /&'’.+\-][A-Za-z0-9]+)*")


def shape(text: str) -> str:
    """Return `text` laid out to survive a bidirectional renderer.

    Idempotent, and a no-op on anything with no Arabic in it or that has
    already been shaped.
    """
    if not text or FSI in text or not _ARABIC.search(text):
        return text

    lines = []
    for line in text.split("\n"):
        isolated = _LATIN_RUN.sub(lambda m: f"{FSI}{m.group(0)}{PDI}", line)
        lines.append(f"{RLM}{isolated}" if isolated.strip() else isolated)
    return "\n".join(lines)


def unshape(text: str) -> str:
    """Strip what `shape` added -- for comparing a sent message with its source."""
    return (text or "").replace(FSI, "").replace(PDI, "").replace(RLM, "")
