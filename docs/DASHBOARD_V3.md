# Dashboard v3 — "Bloom"

`DASHBOARD_THEME=v3` (the default). `v2`, `mklabs` and `legacy` remain one
variable away; none needs a code change.

Two references, kept in `docs/design/refs/`:

| reference | taken from it |
|---|---|
| `layout-ref.jpg` ("Mediline") | the structure: a sidebar drawn as an icon strip plus a wide labelled menu with chevrons, the current item a rounded tab cut out of the menu in the page's ground colour; a top bar with search, language, theme, notifications and the person signed in; KPI cards as number, label, round icon and a quieter line; soft corners, generous spacing; its slate dark version as the model for dark mode |
| `colors-ref.jpg` ("Lector") | the colours: magenta-pink first (`#E9407A`), purple, sky and orange, as gradients on the KPI cards (taken a step deeper so white text on them passes AA), soft multi-colour charts, coloured status pills, a pale pink-grey ground |

## Files

- `dashboard/themes/v3.css` — tokens and component styles, every rule scoped
  to `:root[data-skin="v3"]`. Text pairings meet WCAG AA in both themes.
- `dashboard/themes/v3.js` — motion and the top-bar furniture. Presentation
  only: it moves the rail's existing controls (search, language, theme, the
  avatar) into the top bar as the same nodes with the same handlers, adds a
  bell that is the review queue's badge, and animates. No fetches, no storage,
  no library. `web.skinned` injects `<name>.js` for any skin that ships one.

## Sidebar

On a desktop the rail is shut to its icon strip: logo mark, icons, counts as
small badges, item names as tooltips. Hover (after a short intent delay) or
keyboard focus opens the labelled menu over the page -- the page never
reflows -- and leaving closes it. The pin keeps it open and widens the page's
column; it is remembered per signed-in user. The current item is a tab of
page ground cut out of the rail with curved corners, reaching the page's
edge open or shut; the menu scrolls without drawing a scrollbar. Phones keep
the bottom tab bar.

## Motion

- A page *opening* is drawn: cards rise in with a stagger, line charts stroke
  across (along the time axis, which runs left to right in both languages),
  bars grow with a stagger, donuts sweep round, KPI numbers count up from 0,
  axis labels and legends fade in after the draw, status pills pop.
- A chart below the fold holds its first frame and draws when it scrolls into
  view.
- Anything re-rendered later on the same visit (the inbox poll, a refresh after
  an action) is not replayed: numbers tween from what they showed, charts swap.
  New conversations and new messages in the open thread slide in.
- Theme switch crossfades (View Transitions, skipped after 1.2s if stalled).
- Only `transform`, `opacity` and an SVG stroke are animated; animations use
  `fill: backwards`, so nothing ever blocks a click. `prefers-reduced-motion`
  makes everything instant.

## Roles

A moderator's overview has no money cards; the skin hides an empty grid slot
and lets the card beside it take the row, so the page never shows a hole.
The server-side money guard is unchanged.
