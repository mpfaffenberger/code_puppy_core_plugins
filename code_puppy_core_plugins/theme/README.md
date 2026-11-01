# theme — `/theme` for Code Puppy

A friendlier `/theme` command with an **interactive picker**, **live preview**,
and **four layers of theming** — banner headers, body text, inline markup,
AND the whole terminal window background/foreground/ANSI palette via OSC
escape sequences (mure-style).

## What you get

```
/theme
```
Launches a paginated split-panel TUI with 16 themes (7 neon/dark themes, 7 palette-first/light themes, surprise, reset). Five themes are shown per page; use Up/Down for item navigation and PgUp/PgDn to change pages.


## What gets themed

| Level | Surface | Themed? |
|-------|---------|---------|
| 0 | Banner headers (`[ THINKING ]`, etc.) | ✅ |
| 1 | Body text (`emit_info`/`warning`/`success`/`error`/`debug` + diffs) | ✅ |
| 2 | Inline Rich markup (`[bold cyan]`, `[dim cyan]`, `[magenta]`...) | ✅ |
| 3 | **Entire terminal window** (bg/fg/16-color ANSI palette via OSC 10/11/4) | ✅ |

**Semantic colors are preserved** at Level 2 (red errors stay angry, yellow warnings warn). Level 3 OSC remap is broader — it changes how *your terminal itself* interprets every ANSI color, so e.g. an `ls` ran after `/theme tokyo-night` will also look Tokyo Night. Pure terminal-level magic.

Tokyo Night is applied by default on first run. Themes persist to your Code Puppy config and survive restarts; existing or explicitly restored choices are never overwritten by the default. The OSC palette auto-resets on Code Puppy exit (via `atexit`) so you never get a stuck-pink terminal.

## Bundled themes

| # | Theme | Vibe |
|---|-------|------|
| 1 | 🌊 Ocean | cool blues, cyans & teals |
| 2 | 🌲 Forest | earthy greens & olives |
| 3 | 🌅 Sunset | warm reds, oranges & gold |
| 4 | 🪩 Vaporwave | neon pinks & purples |
| 5 | 🫧 Bubblegum Pink | sweet pinks, candy neon & glow |
| 6 | 🐶 Purple Puppy | vivid violet fur, white muzzle & pink tongue |
| 7 | 🐱 Catppuccin Mocha | soothing pastel dark (mure default) |
| 8 | ☕ Catppuccin Latte | soothing pastel light |
| 9 | 🌃 Tokyo Night | neon-on-navy night |
| 10 | [CRT] Green Screen | llxprt's radioactive green phosphor CRT |
| 11 |  Deep Black | inky noir with subtle neon edge |
| 12 |  Solarized Light | classic warm beige with calm accents |
| 13 |  GitHub Light | crisp white, familiar code colors |
| 14 |  Rose Pine Dawn | soft pastel rose light |
| 15 |  Surprise Me | a fresh random remix every time |
| 16 |  Restore Defaults | back to Code Puppy + terminal factory |

Green Screen ports the canonical phosphor colors from [vybestack/llxprt-code](https://github.com/vybestack/llxprt-code), whose theme is licensed under Apache-2.0.

## Power-user shortcuts

```
/theme 5            apply theme #5 (Bubblegum Pink)
/theme bubblegum    apply by alias (also: pink, puppy, purple, mocha,
                    latte, tokyo, green, crt, solarized, github, rose-pine)
/theme tokyo-night  apply by canonical name
/theme surprise     re-roll a random palette
/theme default      restore Code Puppy + terminal factory colors
/theme reset        alias of /theme default
/theme show         dump current banner + content style mappings
```

## Architecture (Zen-compliant, one job per file)

| File | Responsibility |
|------|----------------|
| `themes.py` | Theme catalog + banner/content/remap/palette construction |
| `bundled_palettes.py` | Hex palette data (Catppuccin, Tokyo Night, Deep Black, +5 originals) |
| `content_styles.py` | Mutates `RichConsoleRenderer` style maps (Level 1) |
| `rich_themes.py` | Monkey-patches `Console.get_style()` for inline remap (Level 2) |
| `osc_palette.py` | OSC escape sequences for terminal-wide bg/fg/ANSI palette (Level 3) |
| `picker.py` | prompt_toolkit split-panel TUI with live preview |
| `register_callbacks.py` | `/theme` command handler, glue only |
| `catalog.py` | Pure, read-only catalog for ACP clients (wire dicts, semantic roles, active-theme read) |
| `__init__.py` | Re-applies persisted (L1 + L2 + L3) on plugin load |

## How each level works (the spicy bits)

**Level 1** — mutates `code_puppy.messaging.rich_renderer.DEFAULT_STYLES` and walks live `RichConsoleRenderer._styles` via `gc.get_objects()`.

**Level 2** — Rich's stock `Theme` only intercepts named styles, not base colors inside `[bold cyan]`. So we monkey-patch `Console.get_style()` on every live `Console`: every resolved `Style` flows throughd if `.color`/`.bgcolor` matches our remap, we swap. Tracked in a `WeakKeyDictionary` for clean swap-without-leak.

**Level 3** — fires xterm OSC sequences (`\\033]10;#fg\\007`, `\\033]11;#bg\\007`, `\\033]4;N;#hex\\007`) which modern terminals honor terminal-wide. Auto-resets on exit via `atexit`. Persists across restarts via JSON in `puppy.cfg`. Supported in iTerm2, Terminal.app, Alacritty, kitty, ode, GNOME Terminal, Windows Terminal. Unsupported terminals silently ignore.

Plays nice with `/colors` — same color pool, same config keys for banners.

## Theme catalog

`catalog.py` is a pure, read-only catalog for ACP clients. It never applies a
palette, emits OSC sequences, or writes config. Its only config access is the
read in `read_active_theme_id()`.

| Function | Purpose |
|----------|---------|
| `build_catalog()` | Returns 15 fresh wire dicts: 14 curated themes in menu order, then `default`. `surprise` is excluded. |
| `semantic_roles(palette)` | Maps a `bg`/`fg`/`ansi` palette to the semantic roles below. |
| `appearance_for(bg_hex)` | Returns `dark` when the WCAG relative luminance of `bg` is below 0.179, else `light`. |
| `active_theme_id(raw)` | Returns `raw` if it is a catalog id, else `None`. |
| `read_active_theme_id()` | Reads `ACTIVE_THEME_CONFIG_KEY` and returns `active_theme_id(...)`. Never raises. |

### Role mapping

| Role | Source | AA adjustment |
|------|--------|---------------|
| `background` | `bg` | None (base color) |
| `surface` | `bg` blended 8% toward `fg` | None (exempt) |
| `text` | `fg` | Yes |
| `mutedText` | `ansi[8]` | Yes |
| `accent` | `ansi[4]` | Yes |
| `success` | `ansi[2]` | Yes |
| `error` | `ansi[1]` | Yes |
| `warning` | `ansi[3]` | Yes |
| `running` | `ansi[6]` | Yes |
| `diffAdd` | `ansi[2]` | Yes |
| `diffRemove` | `ansi[1]` | Yes |

AA adjustment: if a role has less than 4.5:1 contrast against `background`, it
is blended toward `#ffffff` on dark themes or `#000000` on light themes in 5%
steps. The first step that reaches 4.5:1 is used. The input palette is never
modified, and output values are lowercase `#rrggbb`.
