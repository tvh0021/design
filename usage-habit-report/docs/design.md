# Dashboard design note

`usage-habit-report serve` renders one dense, calm page from the local
store. It should read like a ledger or an instrument panel: figures first,
rules only where they separate meaning, colour only where it carries
information. This note is the source of truth for the tokens in
`assets/dashboard.css`; change the note first, then the CSS.

## Rules

- One ink, one paper. Text, rules, and the activity ramp are all the ink at
  different strengths. Nothing else is grey.
- One hue per tool, and nothing else is coloured. Claude Code, Codex and
  Antigravity each own a hue. A hue appears on a tool's name, on its row
  marker, on a stepped bar that belongs to that tool, and on the activity
  map when the map is filtered to that tool.
  Model radar backgrounds also identify the model provider: Claude uses the
  Claude Code hue, GPT uses the Codex hue, and Gemini uses the Antigravity hue.
  Models from other providers use neutral ink. No hue is used for emphasis,
  state, or decoration.
- Figures are tabular in a monospace from the system stack. No webfonts and
  no network requests of any kind; the page works offline.
- Light and dark follow `prefers-color-scheme`. Both schemes meet WCAG AA
  (4.5:1) for every text colour on the paper.
- No animation on load. Transitions, where present, are switched off under
  `prefers-reduced-motion`.
- Sentence case everywhere. Plain verbs. No headings that restate the page.

## Avoid

Checked against this list before and after coding:

- warm-cream paper with terracotta accents (the paper here is a neutral
  off-white and the tool hues sit apart from it)
- near-black with acid green
- hairlines around everything (rules appear under column headers and above
  totals only)
- identical rounded cards with soft grey shadows (sections are separated by
  space and one rule, never boxed)
- all-caps eyebrow labels (labels are sentence case at body size)
- middle-dot meta strings (facts are written as short sentences or table cells)
- arrows on links
- gradient washes (the ramp is stepped, never a gradient)
- any palette borrowed from T3 Code

## Colour

Hex values are final. The CSS exposes them as custom properties with the
same names.

| Token            | Light     | Dark      | Use                                     |
|------------------|-----------|-----------|-----------------------------------------|
| `--paper`        | `#F3F2EE` | `#191917` | page background                         |
| `--ink`          | `#1E1D1A` | `#E6E4DD` | text, strongest ramp step               |
| `--muted`        | `#5F5E58` | `#A3A198` | secondary text, axis labels, captions   |
| `--rule`         | `#D3D1C9` | `#3A3934` | the few horizontal rules, empty cells   |
| `--claude-code`  | `#A8402A` | `#EE8E73` | Claude Code                             |
| `--codex`        | `#2A5BB8` | `#86ACF2` | Codex                                   |
| `--antigravity`  | `#4A741C` | `#A3CB65` | Antigravity                             |
| `--focus`        | `#2A5BB8` | `#86ACF2` | keyboard focus ring (2px solid, offset) |

Contrast against the paper (WCAG 2 ratio):

| Token            | Light | Dark |
|------------------|-------|------|
| `--ink`          | 15.1  | 13.8 |
| `--muted`        | 5.8   | 6.8  |
| `--claude-code`  | 5.5   | 7.3  |
| `--codex`        | 5.7   | 7.7  |
| `--antigravity`  | 4.9   | 9.5  |

The focus ring reuses the Codex hue because a ring is not a tool marker: it
is a 2px outline offset 2px from the element, which no tool marker ever is.
It is the only place a tool hue appears without meaning "that tool".

### Activity ramp

Cell strength is one hue mixed into the paper. The hue is the ink, or the
filtered tool's hue when the map shows one tool. Six steps:

| Level | Mix (hue into paper) | Meaning                                  |
|-------|----------------------|------------------------------------------|
| 0     | none, 1px rule inset | no requests in that hour on that weekday |
| 1     | 14 %                 | lowest fifth of non-empty cells          |
| 2     | 30 %                 | second fifth                             |
| 3     | 48 %                 | third fifth                              |
| 4     | 68 %                 | fourth fifth                             |
| 5     | 90 %                 | top fifth                                |

The mix is `color-mix(in oklab, var(--ramp) N%, var(--paper))`. Level
thresholds are quintiles of the non-empty cells so the map shows the shape
of a habit rather than one heavy hour swamping the rest; the legend prints
the token value at which each step begins.

### Secondary panels

The panels under the totals reuse the tokens above; nothing new is coloured.

- **Model radar charts** lead the task-kind panel. Every model has its own
  chart, including models folded into the exact-count table. Three columns
  at desktop width become two below 1000px and one below 680px. Each chart
  has the same task axes, with a separate linear scale that places its largest
  topic at the outer ring. Ring labels show actual request percentages at
  quarter-maximum intervals. Its heading includes the request count and the
  outer ring's percentage. Models are grouped as Anthropic, OpenAI, Google,
  then other providers. Claude models use Fable, Opus, Sonnet, Haiku order
  within each major generation, newest generation first. OpenAI uses Astra,
  Sol, Terra, Luna order; Google uses Argon, Pro, Flash order. Minor versions
  sort newest first within each tier. Unknown model families follow known tiers.
  The background mixes 12% provider hue into paper, and the plotted area mixes
  24% hue over that background. Text stays ink for contrast. Axis labels show
  percentages; SVG descriptions include exact counts. Unknown models use the
  neutral background. There is no animation or external chart dependency.
- **Model by task kind's exact-count table** sits in an expandable disclosure
  below the charts and uses the same six-step ramp with the same quintile
  rule, over request counts. It is always the ink, even when the page is
  filtered to one tool: hue marks a tool, and the matrix rows are models.
  The matrix is set one type step larger than the other ledgers (`--t-lead`,
  16px) with 10px row padding, so its rows are about 20 % taller.
  Each cell is a 12px swatch beside its figure rather than a shaded
  background, so every figure stays ink on paper at full contrast (ink on a
  48 % or 68 % step would fall under 4.5:1).
- **Stepped bars** are a row of discrete 6 by 10px units with a 2px gap,
  never a continuous or gradient fill. A share bar is 20 units (5 % each);
  unfilled units are a 1px `--rule` inset, like an empty map cell. A count
  bar (the subagent histogram) draws only its filled units, scaled to the
  largest bucket. Filled units are the ink at the 68 % ramp step, or the
  tool's hue at full strength when the bar belongs to one tool (cache-read
  ratio, the per-project tool mix). The tool mix is a row marker: ten units
  split by each tool's share of the project's requests.
- **Quiet tables.** The condensed spend table is set in `--t-small` with
  period labels in `--muted`, so it reads as a reference under the map, not
  a second headline. Sub-lists inside a section (longest and costliest
  sessions) are labelled with a table `caption` in `--muted` at body size,
  not with a heading.
- Session titles may wrap (up to 48ch) inside an otherwise single-line table.

## Type

The whole page is set in the system monospace so figures and labels share
one rhythm:

```
ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", "DejaVu Sans Mono", monospace
```

`font-variant-numeric: tabular-nums` on the root. Sizes are a short scale
in rem on a 16px root; line heights are unitless.

| Token       | Size      | Line height | Use                                  |
|-------------|-----------|-------------|--------------------------------------|
| `--t-small` | 0.75rem   | 1.4         | axis labels, legend, footnotes       |
| `--t-body`  | 0.875rem  | 1.5         | everything by default                |
| `--t-lead`  | 1.0rem    | 1.4         | section titles, window totals        |
| `--t-title` | 1.25rem   | 1.3         | page title (the only one this size)  |

Weight is 400 everywhere except section titles and the totals row (600).
No italics.

## Space

A 4px base. Tokens: `--s-1` 4px, `--s-2` 8px, `--s-3` 12px, `--s-4` 16px,
`--s-6` 24px, `--s-8` 32px, `--s-12` 48px.

- Page padding `--s-6`; `--s-4` under 600px.
- Sections are separated by `--s-12` of space and a single `--rule` line.
- Within a section, title to body is `--s-3`; table cells have `--s-2`
  vertical padding; map cells have a 2px gap so the grid reads as one
  instrument, not 168 tiles.
- No maximum content width: the page spans the viewport, with `--s-6` padding on each side.

## Layout

Top to bottom, one column:

1. Header: page title, one sentence of provenance (sessions counted, store
   read at). Then the controls: window (1 week, 4 weeks, 12 weeks, all time) and
   tool filter (all, each tool), as link groups. The current choice is
   underlined with the ink and carries `aria-current="page"`.
2. `section#activity`: the activity map. Rows are weekdays Monday to Sunday,
   columns are hours 0 to 23 in local time. Cells are focusable and carry
   the exact figures in a `title`; hovering or focusing a cell also writes
   them to a readout line under the map (`aria-live="polite"`). The legend
   sits right of the readout.
3. `section#totals`: window totals as a ledger: one row per tool, one total
   row, columns requests, uncached input, cache read, cache write, output,
   est. cost. The total row matches `query summary --since <window>`.
4. Ticket 10 panels, each a `section` with an id and a title, in this order:
   `#models` (model by task kind), `#agents` (multi-agent habit),
   `#projects` (per-project time and cost), `#sessions` (longest and
   costliest), `#cache` (cache-read ratio), `#spend` (condensed spend table).
   `html::section()` renders the frame; a panel supplies only its body.
5. Footer: rate provenance and the render time.

Under 720px the map keeps all 24 columns and shrinks the cells; the hour
axis then labels every third hour. Tables scroll horizontally inside their
section rather than wrapping.

### Subscription value page

`/value` (public: `value.html`) reuses the header, type, colour and ledger
styles of the main page and adds nothing new. Its controls carry one group,
"Page", that links the two pages; the current one is underlined and carries
`aria-current="page"`. Top to bottom:

1. `section#value`: one sentence of method, the combined headline and any
   pool or live-limit finding in `.facts` (a leading tool name takes the tool's
   hue), then a ledger with a row per tool at its recent pace (plan, price,
   estimated cost a month, times the price with a count bar, the per-window
   costs, days of history), a total row, and one note on `≥`. Each tool's
   own headline is left out because the ledger already says it.
2. One `section#value-<tool>` per tool: a one-line note (price, request
   dates, logged and live readings), the per-window ledger (`≥` lower bounds;
   pools follow the plan's own windows), then in a `.stack`: per window a
   drift sentence and table when two or more months qualify, with two count
   bars per row (cost at the month's prices and at the reference model's,
   on one scale); the closed-windows table, whose mean-peak column is a
   20-unit share bar, and a quiet by-month table; the credits ledger
   (`ledger quiet`) when credits moved; the month table with a count bar on
   times the price and pace and mean rows (the mean only with two or more
   months); and the averaged runs as `ledger quiet` tables inside one closed
   `<details>`. Every bar in a section is that tool's hue.
3. `section#value-coverage`, "Coverage and method": three short paragraphs
   on runs, sources, and drift. It renders on an empty store too, so its
   fixed words reach the snapshot's template vocabulary.

The utilization and credits findings are left off the page, since their
tables carry the same figures; the text report and MCP still print them.

No activity map, so the page carries no script.

## Keyboard and motion

- Every control is a real link or button. Focus is a 2px `--focus` outline
  offset 2px, never removed.
- Map cells have `tabindex="0"` so the figures are reachable without a
  mouse; the readout is live so a screen reader hears the change.
- No transitions on page load. The only transition is the cell outline on
  hover (80ms), and it is removed under `prefers-reduced-motion: reduce`.

## Copy

- Sentence case. "Estimated API cost", not "ESTIMATED API COST".
- State the window and the clock in plain words: "4 weeks to Tue 20 Oct,
  00:56 local time".
- Empty store: "No usage recorded yet. Run `usage-habit-report scan` to read
  your logs."
- Costs are "estimated" and the footer names the source and fetch time.


Project and session time figures use cached active time with gaps longer than
15 minutes removed. Parent time includes linked child activity; totals count
that tree once. The longest-session list sorts by active time. Layout, tokens,
and request/cost window semantics stay the same.

## Public export

`snapshot` uses the same page model, queries, templates, and embedded assets
as `serve`. Its public audience changes navigation to relative HTML files
and removes private fields before rendering. A note near the top states the
generation date and window. Project names are pseudonyms without path
tooltips, except exact paths opted into `[public_projects]`: those rows carry
the GitHub URL separately and render its last segment as a repository link.
Shared URLs do not merge project rows. Private dashboard names and tooltips
stay unchanged. The project table has no active-time column. The sessions
section is one summary line (session, project and fan-out counts) with no
per-session rows. The agents panel keeps the histogram and model mix but
drops the heaviest fan-outs and the per-tool coverage and lineage notes.
Active time appears nowhere in the export. Estimated dollar figures round independently to whole dollars,
including exact aggregates computed before rounding. The spend note explains
why rounded rows may not add up. All existing visual tokens stay the same.

With `[publish] dir` configured, each local dashboard start regenerates the
public export before the server binds its port. `serve --no-publish` skips
this step. The CLI catches up the store, renders into a temporary sibling
directory, and checks the output for private values before replacing the
published directory. A privacy failure preserves the previous files. Other
publish failures also leave the dashboard available. The server's connections
remain read-only after startup. The standalone `publish` command runs the same
step; git commits and pushes follow the rules documented in the README.
