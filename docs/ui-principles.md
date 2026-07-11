# UI Principles

## 1. Keep the creative loop visible

The default screen should make the sequence obvious without a tutorial:

1. choose media and project;
2. configure and queue a generation;
3. monitor the latest job;
4. compare recent assets;
5. inspect, review, reuse, or export the selected asset.

## 2. Let generated media carry the personality

The application chrome must stay neutral. Generated images, videos, waveforms,
and project content are allowed to be expressive; navigation and controls are
not competing artwork.

## 3. Prefer rows and dividers over card collections

Use a bordered panel only for a distinct task or workspace. Within a panel, use
rows, definition lists, and dividers so related data reads as one system.

## 4. Show the next meaningful action

At any state, place the next likely action near its object:

- queue generation near the prompt;
- cancel near a running job;
- search and select within the gallery;
- reuse, load, and export beside the selected asset;
- feedback beside quality metadata.

## 5. Use progressive disclosure

Quick mode exposes only high-frequency controls. Advanced model parameters,
lineage, raw snapshots, and review details remain available without dominating
the primary creation path.

## 6. Design for long sessions

- Maintain WCAG AA contrast where practical.
- Use a compact but not cramped scale.
- Keep state labels textual and scannable.
- Avoid motion that demands attention.
- Preserve entered content across media and project changes.

## 7. Validate the rendered result

For UI changes, inspect at 390px, 768px, 1280px, and 1440px. Review hierarchy,
alignment, clipping, focus, empty/running/error states, and long content. Code
review alone is not visual acceptance.
