# Image assets for the README

The main `README.md` references the files in this folder. The `.svg` files
shipped here are **placeholders** so the README renders complete on GitHub out
of the box. Replace them with real captures when you can — you can keep the same
filenames (the README needs no edits) or swap to `.png`/`.webp` and update the
`<img src=…>` paths.

## Shot list

| File | What it should show | How to capture |
|------|--------------------|----------------|
| `banner.svg` | Project hero banner. The placeholder is a finished graphic — keep it, or drop in a designed banner (recommended size ~1200×300). | Design tool / keep as-is |
| `architecture.svg` | The layers + build pipeline. This one is a **real diagram**, not a screenshot — keep it or edit the SVG. | Edit SVG directly |
| `screenshot-targets.svg` | The **Targets** tab — the capability board with lit / outlined / hatched cells. | Run the app, open **Targets**, screenshot the board |
| `screenshot-config.svg` | The **Config** tab — server/key/branding fields with the live `custom_.txt` preview visible. | Open **Config**, fill a few fields, screenshot |
| `screenshot-toolchains.svg` | The left-rail **toolchain panel** — tools with sizes, versions, install/remove buttons. | Screenshot the left rail with a few tools installed |
| `screenshot-console.svg` | The **Build** tab mid-run — streaming log lines in the console. | Start a build (or a dry-run **Preview plan**), screenshot the console |

## Tips for good screenshots

- Use a real config so the fields aren't empty, but **redact your real server IP, key, and passwords** before publishing.
- Capture at 2× / Retina if you can — crisp on GitHub.
- Recommended width ~800–1000px so the two-column tables in the README line up.
- Optional: crop to just the panel (not the whole browser chrome) for a cleaner look; the placeholders include fake window chrome you can mimic or drop.
