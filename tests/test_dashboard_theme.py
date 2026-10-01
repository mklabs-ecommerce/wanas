"""DASHBOARD_THEME: the skin is a switch, and `legacy` is the page untouched."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import web

THEMES = Path(web.__file__).parent / "themes"


def test_v2_is_the_default_skin():
    app = FastAPI()
    app.include_router(web.router)
    client = TestClient(app)
    page = client.get("/dashboard").text
    assert '<html data-skin="v2" ' in page
    assert '<style id="skin">' in page
    assert "family=Geist" in page and "IBM+Plex+Sans+Arabic" in page
    login = client.get("/dashboard/login").text
    assert 'data-skin="v2"' in login


def test_mklabs_is_still_one_variable_away():
    raw = web.APP_PAGE.read_text(encoding="utf-8")
    page = web.skinned(raw, "mklabs")
    assert '<html data-skin="mklabs" ' in page
    assert "Schibsted+Grotesk" in page and "Martian+Mono" in page
    assert 'data-skin="v2"' not in page


def test_legacy_is_the_page_exactly_as_it_was():
    raw = web.APP_PAGE.read_text(encoding="utf-8")
    assert web.skinned(raw, "legacy") == raw
    # A typo must not blank the dashboard, nor reach the filesystem.
    assert web.skinned(raw, "no-such-skin") == raw
    assert web.skinned(raw, "../web") == raw


def test_the_skin_uses_only_two_inks_and_no_gradients():
    css = (THEMES / "mklabs.css").read_text(encoding="utf-8")
    assert "linear-gradient" not in css
    assert "#1E3FD0" in css and "#D93A28" in css  # cobalt + stamp red
    assert "#D8C3A0" in css and "#FAFAF7" in css and "#151515" in css


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", block))


def test_text_pairings_meet_wcag_aa_in_both_themes():
    css = (THEMES / "mklabs.css").read_text(encoding="utf-8")
    light_block, dark_block = css.split('[data-theme="dark"] {', 1)
    light = _tokens(light_block)
    dark = {**light, **_tokens(dark_block.split("}", 1)[0])}
    light_ink = {"ink": "#151515", "ink-3": light["ink-3"], "clay": "#1E3FD0", "rose": light["rose"]}
    for name, colour in light_ink.items():
        assert _contrast(colour, "#FAFAF7") >= 4.5, name  # on a label
    assert _contrast(light["ink-3"], "#D8C3A0") >= 4.5  # muted text on kraft
    assert _contrast("#FFFFFF", "#1E3FD0") >= 4.5  # white on a cobalt button
    for name in ("ink", "ink-2", "ink-3", "clay", "rose", "amber"):
        assert _contrast(dark[name], dark["surface"]) >= 4.5, name
    assert _contrast("#151515", dark["clay"]) >= 4.5  # print on a dark-mode button


# --------------------------------------------------------------------------
# v2
# --------------------------------------------------------------------------


def _v2_tokens():
    css = (THEMES / "v2.css").read_text(encoding="utf-8")
    light_block, dark_block = css.split('[data-theme="dark"] {', 1)
    light = _tokens(light_block.split("}", 1)[0])
    dark = {**light, **_tokens(dark_block.split("}", 1)[0])}
    return css, light, dark


def test_v2_keeps_the_brand_inks():
    css, light, _ = _v2_tokens()
    assert light["cobalt"] == "#1E3FD0" and light["stamp-red"] == "#D93A28"
    assert light["label"] == "#FAFAF7" and light["print"] == "#151515"
    # The kraft ground and its tag/stamp dressing are what v2 drops.
    assert "#D8C3A0" not in css and "clip-path" not in css


def test_v2_text_pairings_meet_wcag_aa_in_both_themes():
    _, light, dark = _v2_tokens()
    for theme in (light, dark):
        for ink in ("ink", "ink-2", "ink-3", "clay", "rose", "teal", "amber"):
            for ground in ("surface", "surface-2", "ground"):
                assert _contrast(theme[ink], theme[ground]) >= 4.5, (ink, ground)
        # Each tinted chip: its ink on its own tint.
        for hue in ("clay", "rose", "teal", "amber"):
            assert _contrast(theme[hue], theme[f"{hue}-tint"]) >= 4.5, hue
    assert _contrast("#FFFFFF", light["cobalt"]) >= 4.5  # a primary button
    assert _contrast("#0F0F10", dark["clay"]) >= 4.5  # its dark-mode twin
    # A count badge: white on the stamp, bold and at least 10.5px.
    assert _contrast("#FFFFFF", light["stamp-red"]) >= 4.0
    # The rail is ink-black in both themes; its muted labels still read.
    for theme in (light, dark):
        assert _contrast(theme["rail-ink"], theme["rail-bg"]) >= 4.5
        assert _contrast(theme["rail-ink-3"], theme["rail-bg"]) >= 4.5


def test_v2_is_scoped_to_its_own_skin():
    """Every rule hangs off `:root[data-skin="v2"]`, so mklabs and legacy can
    never inherit a v2 layout by accident."""
    css = (THEMES / "v2.css").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = re.sub(r"@media[^{]*\{", "", body)
    for selector_list in re.findall(r"([^{}]+)\{", body):
        for selector in selector_list.split(","):
            selector = selector.strip()
            if not selector:
                continue
            assert selector.startswith(':root[data-skin="v2"]'), selector


def test_the_tab_bar_is_hidden_unless_a_skin_shows_it():
    raw = web.APP_PAGE.read_text(encoding="utf-8")
    assert '<nav id="tabbar"' in raw
    assert "#tabbar { display: none; }" in raw
