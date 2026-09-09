# wallgen

Generate themed desktop wallpapers at your display's exact native resolution, set
them automatically, and refresh the set daily. Roughly **one cent per 4K image**.

```bash
wallgen "misty scandinavian pine forest at dawn" -n 6 --set
```

---

## Why it exists

Most AI wallpaper tools either need a local GPU or render at 1024×1024 and upscale,
which looks soft on a Retina display. `wallgen` renders **natively at your screen's
pixel dimensions** and composes specifically for a desktop.

Three things it does that a raw image API call does not:

**Renders at the real resolution.** `gpt-image-2` accepts any size whose edges divide
by 16, up to a 3840px long edge. So on a 14" MacBook Pro it renders 3024×1968 directly
— no upscaling, no softness. Displays larger than 3840px render at the cap and get a
step-wise Lanczos upscale with mild sharpening applied only in that case.

**Composes for a desktop, not a gallery.** Every prompt carries a fixed suffix
reserving calm negative space in the left third (where icons live) and the top 8%
(the menu bar), and forbidding text, logos, borders and vignettes — the things image
models reliably get wrong. This is applied in code, so hand-written prompts can't
forget it.

**Expands one theme into a coherent *set*.** A cheap text model turns `"kyoto in the
rain"` into N genuinely different scenes — different times of day, scales and
compositions — that still share a palette. The result reads as one collection rather
than six near-duplicates.

## Install

Requires macOS, [`uv`](https://astral.sh/uv), and one image provider.

```bash
git clone https://github.com/archi-max/wallgen.git
cd wallgen
./wallgen --displays        # uv fetches deps on first run
```

Optionally put it on your `PATH`:

```bash
ln -s "$PWD/wallgen" /usr/local/bin/wallgen
```

## Providers

### Azure AI Services (recommended)

Keys are read at run time via `az` — nothing is stored on disk.

```bash
az login
az cognitiveservices account deployment create \
  -n YOUR-ACCOUNT -g YOUR-RESOURCE-GROUP \
  --deployment-name gpt-image-2 --model-name gpt-image-2 \
  --model-version 2026-04-21 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 50
```

Then set the account in `~/.config/wallgen/env` (or export them):

```bash
WALLGEN_AZURE_ACCOUNT=your-account
WALLGEN_AZURE_GROUP=your-resource-group
WALLGEN_AZURE_DEPLOYMENT=gpt-image-2
```

`gpt-image-2` is deployable in `eastus2` and `westus3`. Check availability with
`az cognitiveservices model list -l eastus2 -o table`.

> **Capacity matters.** At `--sku-capacity 1` requests serialize behind 30–60s
> rate limits. wallgen waits them out and retries, but batches are slow. 50 is
> comfortable; `GlobalStandard` is pay-per-use, so idle capacity costs nothing.

### OpenAI

```bash
echo 'OPENAI_API_KEY=sk-...' >> ~/.config/wallgen/env
wallgen "theme" --provider openai
```

Renders at 1536×1024 and upscales.

### Replicate

```bash
echo 'REPLICATE_API_TOKEN=r8_...' >> ~/.config/wallgen/env
wallgen "theme" --provider replicate
```

FLUX-schnell, ~$0.003/image, ~1MP then upscaled. Cheapest option.

## Cost

Computed from the API's own `usage` report — not estimated — and printed after every
run. Measured on `gpt-image-2` at 3840×2160:

| quality | per image | notes |
|---|---|---|
| `low` (default) | **~$0.012** | genuinely good; the right default |
| `medium` | ~$0.100 | finer texture |
| `high` | ~$0.400 | rarely worth it for a wallpaper |

A daily set of 6 at `low` is about **7¢/day**.

## Usage

```
wallgen "theme" [options]

-n, --count N        how many (default 4)
-q, --quality        low (default) | medium | high
--res                auto (default, your display's native pixels) | 4k | 5k | 6k | WxH
--set                set the first result as the desktop picture, verified
--dry-run            print the prompts and exit, spend nothing
--displays           show detected displays and exit
--prompts-file F     render a hand-authored set, one prompt per line
--no-expand          use the theme verbatim instead of expanding it
--format             png (default) | jpg
--jobs N             concurrent generations (default 2)
--provider           azure (default) | openai | replicate
-o, --out DIR        output root (default ~/Pictures/Wallpapers)

rotation and housekeeping
--rotate             advance the desktop picture one step and exit
--rotate-scope       latest (default) | all
--shuffle            with --rotate, pick randomly instead of in order
--prune N            after generating, keep only the N newest sets
--list-sets          list generated sets and exit
```

Output goes to `~/Pictures/Wallpapers/<theme-slug>-<timestamp>/`, with a
`prompts.json` recording the prompts, render size and exact cost.

### Hand-authored prompt sets

`--prompts-file` skips theme expansion and renders exactly the lines you give it.
Use it when you want deliberate control over a set:

```bash
wallgen "my set" --prompts-file themes/example-concepts.txt
```

See `themes/` for examples. The composition rules are still appended automatically.

**Writing prompts that work:** state one idea as a *physical fact in the scene*,
then let style trail as a clause. "A commuter crowd drawn as identical grey cutouts
with one saturated figure turned the wrong way" produces something; "a moody
introspective commute" produces wallpaper-shaped mush. If an image needs a paragraph
of explanation to land, it has failed — a wallpaper gets about one second.

## Daily automation

Generate a fresh set each morning and rotate the desktop through it during the day:

```bash
./scripts/wallgen-setup install
```

That writes `~/.config/wallgen/daily.conf` (from `daily.conf.example`) and installs
two launchd agents:

| agent | what it does |
|---|---|
| `dev.wallgen.daily` | generates a set at `DAILY_HOUR:DAILY_MINUTE`, sets one, prunes old sets |
| `dev.wallgen.rotate` | advances the desktop picture every `ROTATE_SECONDS` |

Change what it makes by editing `~/.config/wallgen/daily.conf` — set `THEME` for
free text, or `PROMPTS_FILE` to point at your own prompt set. Re-run
`wallgen-setup install` after changing the schedule.

```bash
./scripts/wallgen-setup status      # agent state, config, recent log
./scripts/wallgen-setup uninstall   # remove agents; config and images kept
```

Logs land in `~/.config/wallgen/daily.log`. `KEEP` (default 7) bounds disk use —
without it, 6 images/day at ~6 MB is about 13 GB/year. The set currently on screen
is never pruned.

## Notes

- **Setting the wallpaper on macOS 14+.** The usual
  `tell application "System Events" to set picture` AppleScript reports success but
  does nothing — it writes only the `SystemDefault` entry while per-Space entries take
  precedence. wallgen uses `NSWorkspace.setDesktopImageURL` via the JXA ObjC bridge
  and verifies from a separate process, because reading back in the same process
  returns a stale cached path.
- **Dark interiors.** Prompts describing night interiors tend to come out too dark to
  sit behind desktop icons. Name an explicit light source.
- **Abstract nouns drift.** Words like "machine" or "giants" get resolved into fantasy
  vocabulary unless the prompt supplies a concrete contemporary referent.
- **Rate limits** are treated as a queue, not a failure: they get their own retry
  budget, separate from the small budget reserved for genuine errors.

## License

MIT
