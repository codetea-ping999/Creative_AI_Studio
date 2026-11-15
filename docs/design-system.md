# Creative AI Studio Design System

## Design intent

Creative AI Studio is a quiet, precise production workbench. Quality comes from
information hierarchy, alignment, legible state, and predictable interaction —
not decorative effects.

The selected direction is **Quiet Creative Workbench**. Generated media is the
most visually expressive content on the screen; the application shell remains
neutral and restrained.

## Foundations

### Color roles

All UI colors must use semantic custom properties from `apps/web/src/styles.css`.
Do not put color literals in components.

- `--page-bg`: application canvas
- `--surface-base`: primary working surface
- `--surface-raised`: selected or emphasized surface
- `--surface-soft`: quiet hover and grouped-control surface
- `--border-soft`: default divider and component edge
- `--border-strong`: selected or emphasized edge
- `--text-primary`: primary content
- `--text-soft`: supporting content
- `--text-dim`: labels and tertiary metadata
- `--accent`: the only general-purpose accent
- `--success`, `--warning`, `--danger`: semantic state only

Use color together with text, a border, or another non-color cue for state.

### Typography

- UI: system sans stack, optimized for native rendering and Japanese text
- IDs, paths, model values, metrics: system monospace stack
- Workspace title: 24px / 650
- Panel title: 16px / 650
- Subsection title: 14px / 650
- Body and controls: 14px / 400–600
- Caption and metadata: 12px / 500–650

Avoid display-sized dashboard headings. Line length should remain below roughly
72 characters for explanatory copy.

### Spacing

Use the 4px scale only:

- `4px`: icon/label micro gap
- `8px`: compact control gap
- `12px`: control padding and row gap
- `16px`: component padding
- `20px`: panel padding
- `24px`: section separation
- `32px`: major workspace separation

### Radius

- Small control or badge: `4px`
- Input, button, row: `6px`
- Panel and modal: `8px`

Pill shapes are reserved for short status indicators and segmented controls.

### Depth

- Default depth is created with surface contrast and 1px borders.
- Cards and panels do not use shadows.
- Shadows are reserved for future dialogs, menus, and popovers.
- Do not use backdrop blur, glass surfaces, glow, or gradients.

## Components

### Buttons

- Height: 36px standard, 32px compact
- Primary: solid accent fill; one primary action per local task group
- Secondary: surface fill with border
- Hover: small color/border change only
- Focus: visible 2px outline with offset
- Disabled: reduced contrast and `not-allowed` cursor

### Fields

- Labels remain visible; placeholders are examples, not labels.
- Inputs and selects use the same height, radius, border, and focus treatment.
- Textareas resize vertically and keep a practical minimum height.
- Advanced controls use progressive disclosure instead of crowding quick mode.

### Status indicators

- Always include a text label.
- Use a compact dot + label treatment for live or running state.
- Success, warning, and danger colors are not used decoratively.

### Panels and rows

- A panel groups a distinct task, not an arbitrary piece of content.
- Nested hierarchy uses dividers and inset surfaces before another bordered box.
- Repeated assets use list rows with media thumbnails, not equal-weight cards.

### Empty, loading, error, and disabled states

- Empty states explain what action will populate the surface.
- Running jobs retain context and show status text.
- Errors use `role="alert"` and do not replace the user’s input.
- Disabled controls remain readable and indicate why through adjacent state text.

## Interaction

- Transitions: 120–180ms, color and border only.
- Do not translate or scale controls on hover.
- No continuous or entrance animation.
- Respect `prefers-reduced-motion`.
- Every interactive element needs a visible `:focus-visible` state.
- Icon-only controls require `aria-label`; prefer text actions when space permits.

## Responsive behavior

- `1440px`: full sidebar, two-column monitoring/history workspace
- `1280px`: full sidebar, compact two-column workspace
- `768px`: sidebar becomes an in-flow workspace header; content is one column
- `390px`: all fields and actions stack; media rows use a vertical thumbnail
- No page-level horizontal scrolling at supported widths.

## Prohibited patterns

- Purple-to-blue or decorative gradients
- Glassmorphism, backdrop blur, glow, neon
- Decorative box shadows
- Oversized hero headings
- Equal-weight cards for every datum
- Excessive whitespace or excessive rounding
- Emoji as icons
- Decorative charts and invented KPIs
- Hover lift/translate animation
- Unscoped colors or spacing values inside React components

## Arcane Tech theme (魔法工学) and playful layer

Added after v1.1.0 at the owner's request: the studio should feel exciting,
with a Japanese tokusatsu / anime / light-novel flavour. The references are
mood only. Never use a real work's name, logo, characters, catchphrases or
signature motifs.

- **Theme**: `魔法工学` is the default of three themes (魔法工学 / ライト /
  ダーク), stored under `creative-ai-studio.theme`. It sets
  `data-theme="dark"` plus `data-flair="hud"`, so every dark rule still applies
  and `apps/web/src/theme-hud.css` only adds the HUD layer: indigo surfaces,
  cyan lines with a gold second accent, corner brackets on panels, monospace
  eyebrows, slanted primary buttons and a faint scanline.
- **Moments**: a short cut-in (`CastOverlay`) when a generation starts (展開！),
  finishes (完成！) or raises the creator level (レベルアップ！). It is
  decorative (`aria-hidden`, no pointer events); the same news is always in the
  status text. While a job runs, the latest job panel shows a spell-circle
  progress ring. Neither appears in the light or dark theme, and both stop under
  `prefers-reduced-motion`.
- **Creator rank**: `lib/creatorRank.ts` turns finished generations into a level
  and a light-novel-style title, shown in the sidebar in every theme. New titles
  need an English entry in `i18n/en.ts` (a test checks this).
- Keep the playful layer to these places. Forms, model setup and error text stay
  plain so the studio remains quick to operate.
