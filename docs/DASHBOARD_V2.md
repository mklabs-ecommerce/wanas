# Dashboard v2 — "Ink rail"

`DASHBOARD_THEME=v2` (the default until v3, see DASHBOARD_V3.md). `mklabs` and `legacy` remain one variable
away. v2 is a skin (`dashboard/themes/v2.css`): it changes how the dashboard
looks and lays out, never what it does. Roles, routes, the moderator's money
boundary (`dashboard/money.py`, server-side) and every API are unchanged.

## What the audit found (mklabs, 2026-10)

Walked every section as owner and as page moderator, light and dark, Arabic
(RTL) and English (LTR), desktop (1440) and phone (390).

- **Hierarchy** — every list page printed its title twice (top bar, then the
  same heading again above the filters), pushing content down a screen on a
  phone. KPI numbers in a wide monospace competed with the tables beneath.
- **Visual noise** — kraft ground with a dot texture, clipped-corner "tags",
  dashed tear lines and outlined upper-case "stamps" on every status: the
  decoration out-shouted the data, and 10.5px tracked upper-case chips were
  hard to read.
- **Density / tables** — the orders table (14 columns) overflowed sideways on
  a 1440px screen; on a phone every table needed horizontal scrolling to
  reach the total.
- **Mobile** — KPI tiles stacked one per row (four screens before the first
  order); navigation only through a hamburger with no way to close it by
  tapping outside; the chat header's name, chips and "Take over" overlapped.
- **Inbox** — Arabic messages in the English dashboard took the page's LTR
  direction, so «بكام؟» rendered with its question mark at the wrong end.
- **i18n** — the inbox search, the reply box, the command palette, the inbox
  empty state and the Send button stayed Arabic in English.
- **Buttons** — icon buttons in the rail footer were squeezed to nothing by a
  padding rule (theme / logout invisible).

## Direction

One canvas, white panels, an **ink-black rail in both themes** (the brand's
print colour, and the one bold move), and only two inks on top: **cobalt
#1E3FD0** for what you can press or what is selected, **stamp red #D93A28**
for what is waiting on a person (badges, the "needs a person" edge).
Green and amber survive only as meaning (done / waiting). Geist for Latin and
every number (tabular), IBM Plex Sans Arabic for Arabic.

- Top bar carries the page title; the duplicate heading is gone.
- KPI tiles: a small tone square on the label, the number as the focus;
  two-up on a phone.
- Tables: quiet sentence-case headers, tighter cells; on a phone each row is a
  card with every cell captioned by its column (`data-label`, copied from the
  header by `labelTableCells`).
- Inbox: WhatsApp-like — customer bubbles white at the start, bot bubbles
  cobalt-tint, staff bubbles solid cobalt, system notes dashed; each bubble
  takes its direction from its own text; pill composer.
- Phone: a bottom tab bar (Today, Inbox, Review, Orders, More — filtered by
  permission), hidden inside a chat; the rail opens as a sheet from "More" and
  closes on a tap outside.
- Light and dark from the system by default, the rail toggle remembers a
  choice per user (unchanged behaviour).

Contrast for every text pairing is checked in `tests/test_dashboard_theme.py`.
