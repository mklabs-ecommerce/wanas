"""The dashboard's language toggle, and the one thing that rots without a test.

Arabic is the source language: every string in `dashboard.html` is written in
it, and the `EN` dictionary is keyed on the Arabic itself. A key with no
translation falls back to the Arabic, which is the right runtime behaviour --
a missed string is a cosmetic gap, never a blank screen -- and is exactly why
an omission is invisible to a person clicking around. So the check is
mechanical: extract every phrase the page will ask the dictionary for, and
fail on any that is unaccounted for.

Extraction needs to tell a string from a comment from a template's `${...}`
hole, so there is a small JS tokenizer below rather than a regex that would
quietly miss every nested template. It is not argued to be correct, it is
checked: `_tokenize` must re-emit the script byte for byte before anything
else is read from it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
DASHBOARD = _DASHBOARD_DIR / "dashboard.html"
LOGIN = _DASHBOARD_DIR / "login.html"

ARABIC = re.compile(r"[؀-ۿ]")

#: The same rule `I18N_SEGMENT` applies in the page: a translatable segment is
#: a maximal run holding at least one Arabic letter and stopping at anything
#: that is HTML or JS syntax. `test_the_runtime_rule_matches_this_one` is what
#: keeps the two implementations married.
SEGMENT_SOURCE = """[^<>="'`{}()]*[؀-ۿ][^<>="'`{}()]*"""
SEGMENT = re.compile(SEGMENT_SOURCE)

DICT_START = "const EN = {\n"
DICT_END = "};\n/* ---- 8< ---- END EN DICTIONARY ---- 8< ---- */"


# --------------------------------------------------------------------------
# a tokenizer just good enough to tell code from strings
# --------------------------------------------------------------------------

CODE, COMMENT, STRING, TEMPLATE, REGEX = "code", "comment", "string", "template", "regex"

#: A `/` following one of these closes an expression, so it is division. After
#: anything else -- or after one of the keywords below -- it opens a regex
#: literal, which may legally contain quotes and backticks and would otherwise
#: derail everything after it.
_ENDS_EXPR = set(")]}") | set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_REGEX_KEYWORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
                   "case", "do", "else", "yield", "await"}


def _regex_allowed(src: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if src[j] not in _ENDS_EXPR:
        return True
    word = re.search(r"[A-Za-z_$][\w$]*$", src[:j + 1])
    return bool(word and word.group(0) in _REGEX_KEYWORDS)


def _tokenize(src: str, start: int = 0, stop_at_brace: bool = False):
    """(kind, text) pairs. With `stop_at_brace`, stops at the `}` closing a
    template's `${` and also returns its index."""
    tokens: list[tuple[str, str]] = []
    i = start if stop_at_brace else 0
    n = len(src)
    mark = i
    depth = 0

    def flush(end: int) -> None:
        nonlocal mark
        if end > mark:
            tokens.append((CODE, src[mark:end]))
        mark = end

    while i < n:
        c = src[i]
        if stop_at_brace:
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    flush(i)
                    return tokens, i
                depth -= 1

        if c == "/" and src[i + 1:i + 2] in ("/", "*"):
            flush(i)
            if src[i + 1] == "/":
                j = src.find("\n", i)
                j = n if j == -1 else j
            else:
                j = src.find("*/", i + 2)
                j = n if j == -1 else j + 2
            tokens.append((COMMENT, src[i:j]))
            i = mark = j
            continue

        if c == "/" and _regex_allowed(src, i):
            flush(i)
            j = i + 1
            in_class = False
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "[":
                    in_class = True
                elif d == "]":
                    in_class = False
                elif d == "/" and not in_class:
                    j += 1
                    while j < n and src[j].isalpha():
                        j += 1
                    break
                elif d == "\n":
                    break
                j += 1
            tokens.append((REGEX, src[i:j]))
            i = mark = j
            continue

        if c in "'\"":
            flush(i)
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                j += 1
            tokens.append((STRING, src[i:j]))
            i = mark = j
            continue

        if c == "`":
            flush(i)
            parts = ["`"]
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    parts.append(src[j:j + 2])
                    j += 2
                    continue
                if src[j] == "`":
                    parts.append("`")
                    j += 1
                    break
                if src[j] == "$" and src[j + 1:j + 2] == "{":
                    inner, close = _tokenize(src, j + 2, True)
                    parts.append("${" + "".join(t for _, t in inner) + "}")
                    j = close + 1
                    continue
                parts.append(src[j])
                j += 1
            tokens.append((TEMPLATE, "".join(parts)))
            i = mark = j
            continue

        i += 1

    flush(n)
    return (tokens, n) if stop_at_brace else tokens


def _split_template(text: str) -> tuple[list[str], list[str]]:
    body = text[1:-1]
    literals: list[str] = []
    exprs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] == "\\":
            buf.append(body[i:i + 2])
            i += 2
            continue
        if body[i] == "$" and body[i + 1:i + 2] == "{":
            inner, close = _tokenize(body, i + 2, True)
            literals.append("".join(buf))
            buf = []
            exprs.append("".join(t for _, t in inner))
            i = close + 1
            continue
        buf.append(body[i])
        i += 1
    literals.append("".join(buf))
    return literals, exprs


# --------------------------------------------------------------------------
# reading the page
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def page() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(page: str) -> str:
    """The app script, from just after the generated dictionary. The
    dictionary's own keys are Arabic too, and they are definitions rather than
    lookups."""
    body = page[page.index(">", page.index("<script", page.index("</head>"))) + 1:
                page.rindex("</script>")]
    return body[body.index(DICT_END) + len(DICT_END):]


@pytest.fixture(scope="module")
def dictionary(page: str) -> dict[str, str]:
    """The `EN` object literal, parsed between its markers. It is generated,
    and written as strict JSON-compatible entries precisely so this test can
    read it back without evaluating JavaScript."""
    start = page.index(DICT_START) + len(DICT_START)
    end = page.index(DICT_END, start)
    return json.loads("{" + page[start:end].strip().rstrip(",") + "}")


def segments(text: str) -> set[str]:
    found = {m.group(0).strip() for m in SEGMENT.finditer(text)}
    return {s for s in found if s and ARABIC.search(s)}


def wrapped_keys(page: str, script: str) -> set[str]:
    """Every phrase the page asks the dictionary for: `tr("...")` arguments,
    the literal chunks of a ``TR`...` `` template, and the static shell's
    `data-i18n` attributes."""
    keys: set[str] = set()

    def walk(src: str) -> None:
        tokens = _tokenize(src)
        for idx, (kind, text) in enumerate(tokens):
            prev = tokens[idx - 1][1] if idx else ""
            if kind == TEMPLATE:
                literals, exprs = _split_template(text)
                if prev.endswith("TR"):
                    for literal in literals:
                        keys.update(segments(literal))
                for expr in exprs:
                    walk(expr)
            elif kind == STRING and prev.endswith("tr("):
                keys.update(segments(text[1:-1]))

    walk(script)
    for attr in ("data-i18n", "data-i18n-title"):
        keys.update(k for k in re.findall(attr + r'="([^"]+)"', page) if ARABIC.search(k))
    return keys


def test_the_tokenizer_reproduces_the_script(script):
    """Everything below reads the script through this. If it drops a byte, the
    key set it reports is fiction."""
    assert "".join(t for _, t in _tokenize(script)) == script


# --------------------------------------------------------------------------
# the segmentation rule itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunk, expected",
    [
        ("<h2>الطلبات</h2>", {"الطلبات"}),
        ('placeholder="اسم أو تليفون" value="', {"اسم أو تليفون"}),
        ("<td>الاسم</td><td>الحالة</td>", {"الاسم", "الحالة"}),
        (" ج.م", {"ج.م"}),
        ("خلص (", {"خلص"}),
        ("no arabic at all", set()),
    ],
)
def test_the_segmentation_rule(chunk, expected):
    """A run never crosses a tag boundary and never swallows an attribute's
    quotes -- otherwise the key the page looks up is not the key anybody wrote
    a translation for."""
    assert segments(chunk) == expected


def test_the_runtime_rule_matches_this_one(page):
    """Two implementations of one rule. This is the line that keeps them
    honest: edit the page's regex and this fails, rather than the
    translations quietly going unused."""
    runtime = re.search(r"const I18N_SEGMENT = /(.+?)/g;", page).group(1)
    # The page may spell the Arabic block as literal characters or as \\u
    # escapes; they are the same character class either way.
    normalised = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), runtime)
    assert normalised == SEGMENT_SOURCE


# --------------------------------------------------------------------------
# completeness
# --------------------------------------------------------------------------


def test_every_phrase_the_page_asks_for_has_an_english_translation(page, script, dictionary):
    missing = sorted(wrapped_keys(page, script) - set(dictionary))
    assert not missing, (
        f"{len(missing)} phrase(s) have no English translation. Add them to the "
        "EN dictionary in dashboard.html:\n" + "\n".join(f"  {m!r}" for m in missing)
    )


def test_no_translation_is_left_as_the_arabic(dictionary):
    """A value copied across from its key is an untranslated string that the
    completeness test above would happily pass, since the key is present.

    The size assertion is not padding: without it this test also passes on an
    empty dictionary, which is precisely the state it is meant to catch.
    """
    assert len(dictionary) > 400, "the dictionary is suspiciously small"
    untranslated = sorted(k for k, v in dictionary.items() if ARABIC.search(v) or v == k)
    assert untranslated == []


def test_the_language_button_is_labelled_in_both_languages(page):
    """The one string deliberately left bilingual: it names the two languages,
    so it reads correctly whichever is active and needs no entry."""
    assert page.count('title="العربية / English"') == 1
    assert 'aria-label="العربية / English"' in page


def test_the_dictionary_has_nothing_the_page_never_asks_for(page, script, dictionary):
    """Not a correctness failure, but dead entries are how a dictionary drifts
    away from the page it belongs to."""
    unused = sorted(set(dictionary) - wrapped_keys(page, script))
    assert not unused, "unused dictionary entries:\n" + "\n".join(f"  {u!r}" for u in unused)


# --------------------------------------------------------------------------
# what must NOT be translated
# --------------------------------------------------------------------------


def test_the_canned_customer_replies_are_never_wrapped(page):
    """`QUICK_REPLIES` is typed into the composer and sent to a customer over
    WhatsApp. The UI language is the staff member's preference; the customer
    reads Arabic either way. Wrapping these would send an Egyptian customer an
    English message because somebody's dashboard was in English."""
    block = page[page.index("const QUICK_REPLIES = ["):]
    block = block[: block.index("];")]
    assert ARABIC.search(block), "the quick replies should still be Arabic literals"
    assert "tr(" not in block and "TR`" not in block


def test_the_brand_name_is_not_translated(page):
    """It is a name, not a label: 'Wanas Gallery' in both languages."""
    assert 'class="name" dir="ltr">Wanas Gallery<' in page
    assert "ونس جاليري" not in page


def test_the_logo_is_served_not_inlined(page):
    assert '<img class="mark" src="/dashboard/logo.webp"' in page


# --------------------------------------------------------------------------
# the switch itself
# --------------------------------------------------------------------------


def test_direction_is_set_before_first_paint(page):
    """Flipping `dir` after the page has drawn swings the whole layout in
    front of the reader, so it happens in a head script, like the theme."""
    head = page[: page.index("</head>")]
    assert 'localStorage.getItem("wanas.lang")' in head
    assert 'root.dir = lang === "en" ? "ltr" : "rtl"' in head


def test_the_stylesheet_uses_logical_properties_only(page):
    """The entire right-to-left / left-to-right switch is that one `dir`
    attribute, which only holds while the CSS has no physical left/right in
    it. A single `margin-left` would silently break the English layout."""
    css = page[page.index("<style>"): page.index("</style>")]
    physical = re.findall(
        r"(?:margin|padding|border)-(?:left|right)\s*:"
        r"|(?<![\w-])(?:left|right)\s*:"
        r"|text-align\s*:\s*(?:left|right)",
        css,
    )
    assert physical == [], f"physical CSS properties break the LTR layout: {physical}"


def test_both_pages_share_one_language_key(page):
    assert '"wanas.lang"' in page
    assert '"wanas.lang"' in LOGIN.read_text(encoding="utf-8")


def test_the_login_page_translates_its_own_strings():
    """Its own small dictionary -- loading the dashboard's five hundred
    entries before anybody has logged in would be silly."""
    login = LOGIN.read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n="([^"]+)"', login))
    translated = set(re.findall(r'\n  "((?:[^"\\]|\\.)*)":', login))
    missing = sorted(k for k in keys if k not in translated)
    assert not missing, missing
