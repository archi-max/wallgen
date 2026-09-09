#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10.2", "requests>=2.31"]
# ///
"""
wallgen — themed 4K-ish wallpaper generator for macOS.

Generates a batch of cohesive, theme-driven wallpapers via a cheap image API,
upscales them to your display's native pixel resolution, and (optionally) sets
one as your desktop picture.

Quick start:
    export OPENAI_API_KEY=sk-...
    ./wallgen "misty scandinavian pine forest at dawn" -n 6 --set

See `./wallgen --help`.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image, ImageEnhance, ImageFilter

# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "wallgen"
CONFIG_ENV = CONFIG_DIR / "env"
CONFIG_DAILY = CONFIG_DIR / "daily.conf"
DEFAULT_OUT = Path.home() / "Pictures" / "Wallpapers"

# $ per 1M image-output tokens. Cost is computed from the API's own usage
# report, so these are the only numbers that need to stay current.
OPENAI_IMAGE_PRICE_PER_MTOK = {
    "gpt-image-1-mini": 8.0,
    "gpt-image-1": 40.0,
    "gpt-image-1.5": 32.0,
}

# Azure: gpt-image-2 accepts any size whose edges are divisible by 16, with the
# longest edge capped at 3840 -- i.e. it renders true 4K natively, no upscale.
AZURE_SIZE_GRID = 16
AZURE_MAX_EDGE = 3840
AZURE_API_VERSION = "preview"

# Azure bills the same per-token image rates as OpenAI direct. Keyed by the
# *model* name; a deployment named differently falls back to a flat estimate.
AZURE_IMAGE_PRICE_PER_MTOK = {
    "gpt-image-2": 30.0,
    "gpt-image-1.5": 32.0,
    "gpt-image-1": 40.0,
    "gpt-image-1-mini": 8.0,
}

# Replicate bills by GPU-seconds; these are the vendor's published per-image
# ballpark figures and are reported as estimates, not measurements.
REPLICATE_MODELS = {
    "flux-schnell": ("black-forest-labs/flux-schnell", 0.003),
    "flux-dev": ("black-forest-labs/flux-dev", 0.025),
    "flux-1.1-pro": ("black-forest-labs/flux-1.1-pro", 0.040),
}

STYLE_SEASONING = [
    "soft volumetric light, painterly",
    "crisp high-contrast photographic",
    "muted flat-vector minimalism",
    "moody cinematic grade, deep shadows",
    "dreamy pastel gradients, airy",
    "richly textured, fine grain, tactile",
    "graphic poster art, bold shapes",
    "long-exposure, silky motion",
]

# Appended to every image prompt. Keeps the batch usable as actual wallpaper:
# no text to misspell, no busy corners fighting the menu bar / Dock / icons.
WALLPAPER_RULES = (
    "Desktop wallpaper composition. Absolutely no text, letters, numbers, "
    "logos, watermarks, signatures, UI, or frames. Wide landscape framing with "
    "generous calm negative space in the upper-left and lower-left thirds so "
    "desktop icons stay readable; keep the visual interest off-center and away "
    "from the top 8 percent of the frame. Edge-to-edge scene with no borders, "
    "no vignette bars, no collage or split panels. Cohesive limited palette, "
    "clean rendering, high detail, no clutter."
)

PROMPT_SYSTEM = """You write prompts for an image model that produces desktop wallpapers.

Given a THEME, return {n} distinct wallpaper concepts that clearly belong to the same set:
one recognisable theme, one coherent colour palette, but genuinely different subjects,
times of day, scales, and compositions. Do not just restate the theme {n} times.

Each prompt must be 30-60 words, concrete and visual: name the subject, the light,
the palette, the depth cues, and the medium/render style. Never mention text, words,
captions, logos, UI, borders, or aspect ratio -- those are handled separately.

Return strict JSON: {{"palette": "<3-5 word palette description>", "prompts": ["...", "..."]}}"""


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def die(msg: str, code: int = 1):
    print(f"\033[31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str):
    print(f"\033[2m{msg}\033[0m", file=sys.stderr)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return (s[:48] or "wallpaper").rstrip("-")


def load_env_files():
    """Layer config sources; the real environment always wins.

    daily.conf is included so a hand-run of `wallgen` picks up the same provider
    settings the launchd job uses, instead of only working under the wrappers.
    """
    for path in (CONFIG_ENV, CONFIG_DAILY, Path.cwd() / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and v and not os.environ.get(k):
                os.environ[k] = v


def az_key(account: str, group: str) -> str:
    """Pull the account key straight from Azure CLI (cached for the process)."""
    cached = os.environ.get("_WALLGEN_AZ_KEY")
    if cached:
        return cached
    try:
        p = subprocess.run(
            ["az", "cognitiveservices", "account", "keys", "list",
             "-n", account, "-g", group, "--query", "key1", "-o", "tsv"],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        die("azure cli (`az`) not found -- install it or use --provider openai")
    if p.returncode != 0:
        die(f"az could not read the key for {account}/{group}:\n{p.stderr.strip()[:400]}\n"
            "Try `az login`, or override with --azure-account / --azure-group.")
    key = p.stdout.strip()
    if not key:
        die(f"az returned an empty key for {account}/{group}")
    os.environ["_WALLGEN_AZ_KEY"] = key
    return key


def az_endpoint(account: str, group: str) -> str:
    cached = os.environ.get("_WALLGEN_AZ_ENDPOINT")
    if cached:
        return cached.rstrip("/")
    p = subprocess.run(
        ["az", "cognitiveservices", "account", "show", "-n", account, "-g", group,
         "--query", "properties.endpoint", "-o", "tsv"],
        capture_output=True, text=True, timeout=60,
    )
    if p.returncode != 0 or not p.stdout.strip():
        die(f"az could not read the endpoint for {account}/{group}: {p.stderr.strip()[:300]}")
    ep = p.stdout.strip().rstrip("/")
    os.environ["_WALLGEN_AZ_ENDPOINT"] = ep
    return ep


# ----------------------------------------------------------------------------
# display resolution
# ----------------------------------------------------------------------------

RES_PRESETS = {
    "4k": (3840, 2160),
    "4k-16-10": (3840, 2400),
    "5k": (5120, 2880),
    "6k": (6016, 3384),
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
}


def detect_displays() -> list[tuple[int, int]]:
    """Native pixel resolutions of attached displays, largest first."""
    try:
        raw = subprocess.run(
            ["system_profiler", "-json", "SPDisplaysDataType"],
            capture_output=True, text=True, timeout=25,
        ).stdout
        data = json.loads(raw)
    except Exception:
        return []

    found: list[tuple[int, int]] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and ("resolution" in k.lower() or "pixels" in k.lower()):
                    m = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", v)
                    if m:
                        found.append((int(m.group(1)), int(m.group(2))))
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    # dedupe, biggest area first
    return sorted(set(found), key=lambda wh: -wh[0] * wh[1])


def resolve_target(spec: str) -> tuple[int, int]:
    spec = spec.strip().lower()
    if spec in RES_PRESETS:
        return RES_PRESETS[spec]
    if spec == "auto":
        got = detect_displays()
        if got:
            w, h = got[0]
            if w * h < 1920 * 1080:      # implausible parse -> fall back
                return RES_PRESETS["4k"]
            return w, h
        info("could not detect display resolution, falling back to 3840x2160")
        return RES_PRESETS["4k"]
    m = re.fullmatch(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", spec)
    if m:
        return int(m.group(1)), int(m.group(2))
    die(f"unrecognised --res {spec!r}. Use auto, {', '.join(RES_PRESETS)}, or WxH.")


# ----------------------------------------------------------------------------
# prompt expansion
# ----------------------------------------------------------------------------

def expand_theme(theme: str, n: int, model: str, api_key: str, base_url: str | None = None,
                 azure: bool = False) -> list[str]:
    """Turn one theme into n distinct wallpaper prompts using a cheap text model."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM.format(n=n)},
            {"role": "user", "content": f"THEME: {theme}"},
        ],
        "response_format": {"type": "json_object"},
    }
    # gpt-5 / o-series use max_completion_tokens and reject temperature != 1
    if re.match(r"^(gpt-5|gpt-image|o[134])", model):
        body["max_completion_tokens"] = 4000
    else:
        body["max_tokens"] = 2000
        body["temperature"] = 1.0

    if azure:
        url = f"{base_url}/openai/v1/chat/completions?api-version={AZURE_API_VERSION}"
        headers = {"api-key": api_key, "Content-Type": "application/json"}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"prompt expansion failed ({r.status_code}): {r.text[:300]}")

    content = r.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
        prompts = [p.strip() for p in parsed["prompts"] if isinstance(p, str) and p.strip()]
    except Exception as e:
        raise RuntimeError(f"could not parse expansion JSON: {e}\n{content[:300]}")
    if not prompts:
        raise RuntimeError("expansion returned no prompts")

    # Pad by cycling if the model under-delivered; trim if it over-delivered.
    while len(prompts) < n:
        prompts.append(prompts[len(prompts) % len(prompts[:n])])
    return prompts[:n]


def literal_prompts(theme: str, n: int) -> list[str]:
    """--no-expand path: reuse the theme verbatim, varying only the style seasoning."""
    seasoning = random.sample(STYLE_SEASONING, k=min(n, len(STYLE_SEASONING)))
    while len(seasoning) < n:
        seasoning.append(random.choice(STYLE_SEASONING))
    return [f"{theme}. {s}." for s in seasoning]


# ----------------------------------------------------------------------------
# providers
# ----------------------------------------------------------------------------

class RateLimited(Exception):
    """429 from the provider; carries the server-advised wait in seconds."""
    def __init__(self, retry_after: float = 20.0):
        super().__init__(f"rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass
class GenResult:
    png: bytes
    cost: float
    cost_is_estimate: bool


def openai_size_for(target: tuple[int, int]) -> str:
    """gpt-image-* only offers 1:1 and 3:2 in both orientations."""
    w, h = target
    ratio = w / h
    if ratio > 1.15:
        return "1536x1024"
    if ratio < 0.87:
        return "1024x1536"
    return "1024x1024"


def gen_openai(prompt: str, model: str, quality: str, target, api_key: str) -> GenResult:
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": openai_size_for(target),
        "quality": quality,
        "output_format": "png",
    }
    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body, timeout=600,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
    data = r.json()

    item = data["data"][0]
    if "b64_json" in item:
        png = base64.b64decode(item["b64_json"])
    else:  # some deployments hand back a URL instead
        png = requests.get(item["url"], timeout=300).content

    usage = data.get("usage") or {}
    out_tok = usage.get("output_tokens") or usage.get("image_tokens") or 0
    rate = OPENAI_IMAGE_PRICE_PER_MTOK.get(model)
    if out_tok and rate:
        return GenResult(png, out_tok / 1_000_000 * rate, False)
    return GenResult(png, 0.0, True)


def azure_native_size(target: tuple[int, int]) -> tuple[int, int]:
    """Largest 16-divisible size matching the target aspect, within Azure's 3840 cap.

    If the display is bigger than 3840 on its long edge (5K/6K), we render at the
    cap and let finish() upscale the remainder.
    """
    tw, th = target
    scale = min(1.0, AZURE_MAX_EDGE / max(tw, th))
    w, h = tw * scale, th * scale
    g = AZURE_SIZE_GRID
    w = max(g, int(round(w / g)) * g)
    h = max(g, int(round(h / g)) * g)
    # rounding up must not breach the cap
    while max(w, h) > AZURE_MAX_EDGE:
        if w >= h:
            w -= g
        else:
            h -= g
    return w, h


def gen_azure(prompt: str, deployment: str, quality: str, target,
              account: str, group: str) -> GenResult:
    key = az_key(account, group)
    url = f"{az_endpoint(account, group)}/openai/v1/images/generations?api-version={AZURE_API_VERSION}"
    w, h = azure_native_size(target)
    body = {
        "model": deployment,
        "prompt": prompt,
        "n": 1,
        "size": f"{w}x{h}",
        "quality": quality,
        "output_format": "png",
    }
    r = requests.post(
        url, headers={"api-key": key, "Content-Type": "application/json"},
        json=body, timeout=900,
    )
    if r.status_code == 429:
        wait = r.headers.get("retry-after") or r.headers.get("Retry-After") or "20"
        raise RateLimited(float(re.sub(r"[^0-9.]", "", wait) or 20))
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
    data = r.json()

    item = data["data"][0]
    png = base64.b64decode(item["b64_json"]) if "b64_json" in item \
        else requests.get(item["url"], timeout=300).content

    usage = data.get("usage") or {}
    tok = (usage.get("output_tokens_details") or {}).get("image_tokens") or usage.get("output_tokens") or 0
    rate = AZURE_IMAGE_PRICE_PER_MTOK.get(deployment) or AZURE_IMAGE_PRICE_PER_MTOK.get(
        re.sub(r"-\d+$", "", deployment))
    if tok and rate:
        return GenResult(png, tok / 1_000_000 * rate, False)
    return GenResult(png, 0.0, True)


def gen_replicate(prompt: str, model: str, target, api_key: str) -> GenResult:
    slug, est = REPLICATE_MODELS[model]
    w, h = target
    ratio = w / h
    aspect = "16:9" if ratio > 1.6 else "3:2" if ratio > 1.15 else "1:1" if ratio > 0.87 else "2:3"
    payload = {
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect,
            "output_format": "png",
            "num_outputs": 1,
        }
    }
    if model != "flux-1.1-pro":
        payload["input"]["megapixels"] = "1"
        payload["input"]["go_fast"] = True

    r = requests.post(
        f"https://api.replicate.com/v1/models/{slug}/predictions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Prefer": "wait=60",
        },
        json=payload, timeout=300,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
    pred = r.json()

    # Prefer: wait usually returns terminal state; poll if not.
    deadline = time.time() + 300
    while pred.get("status") in ("starting", "processing") and time.time() < deadline:
        time.sleep(2)
        pred = requests.get(
            pred["urls"]["get"], headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        ).json()
    if pred.get("status") != "succeeded":
        raise RuntimeError(f"prediction {pred.get('status')}: {str(pred.get('error'))[:300]}")

    out = pred["output"]
    url = out[0] if isinstance(out, list) else out
    return GenResult(requests.get(url, timeout=300).content, est, True)


# ----------------------------------------------------------------------------
# upscale / finish
# ----------------------------------------------------------------------------

def finish(png: bytes, target: tuple[int, int], sharpen: float, saturation: float) -> Image.Image:
    """Cover-crop to the target aspect, then step-upscale to native pixels.

    Sharpening only fires when we actually upscaled -- a natively-rendered image
    needs no help, and unsharp-masking it just adds halos.
    """
    img = Image.open(io.BytesIO(png)).convert("RGB")
    tw, th = target
    target_ratio = tw / th

    # 1. crop to aspect (centre, biased slightly up -- skies read better than dirt)
    w, h = img.size
    if w / h > target_ratio:
        new_w = round(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif w / h < target_ratio:
        new_h = round(w / target_ratio)
        top = int((h - new_h) * 0.40)
        img = img.crop((0, top, w, top + new_h))

    upscaled = img.width < tw
    # 2. step-upscale in <=1.5x hops -- fewer ringing artefacts than one big jump
    while img.width < tw:
        step = min(tw, int(img.width * 1.5))
        img = img.resize((step, max(1, round(step / target_ratio))), Image.LANCZOS)
    if img.size != (tw, th):
        img = img.resize((tw, th), Image.LANCZOS)

    # 3. recover the micro-contrast that resampling costs
    if sharpen > 0 and upscaled:
        img = img.filter(ImageFilter.UnsharpMask(radius=2.0, percent=int(sharpen * 100), threshold=3))
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    return img


def _osa_js(script: str, timeout: int = 60) -> tuple[int, str, str]:
    p = subprocess.run(["osascript", "-l", "JavaScript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def current_wallpaper() -> str:
    """Path of the picture actually showing on the main screen."""
    rc, out, _ = _osa_js(
        'ObjC.import("AppKit");'
        'ObjC.unwrap($.NSWorkspace.sharedWorkspace'
        '.desktopImageURLForScreen($.NSScreen.mainScreen).path);'
    )
    return out if rc == 0 else ""


def set_wallpaper(path: Path) -> bool:
    """Set the desktop picture on every screen, then verify it actually took.

    The classic `System Events ... set picture` AppleScript is broken on macOS 14+:
    it writes only the SystemDefault entry, the per-Space entries win, and the
    wallpaper silently does not change while AppleScript still reports success.
    NSWorkspace.setDesktopImageURL is the supported API and does stick.

    The read-back has to happen in a *separate* osascript process -- querying it
    in the same process that just wrote returns the stale cached value.
    """
    script = f'''
    ObjC.import("AppKit");
    var url = $.NSURL.fileURLWithPath({json.dumps(str(path))});
    var ws = $.NSWorkspace.sharedWorkspace;
    var screens = $.NSScreen.screens;
    var n = screens.count, done = 0;
    for (var i = 0; i < n; i++) {{
        if (ws.setDesktopImageURLForScreenOptionsError(url, screens.objectAtIndex(i), $(), null)) done++;
    }}
    done + "/" + n;
    '''
    try:
        rc, out, err = _osa_js(script)
    except Exception as e:
        info(f"could not set wallpaper: {e}")
        return False
    if rc != 0:
        info(f"could not set wallpaper: {err[:200]}")
        return False

    want = str(path)
    for _ in range(6):                       # WallpaperAgent needs a moment
        if current_wallpaper() == want:
            return True
        time.sleep(0.4)
    info(f"wallpaper API reported {out} screens set, but the desktop still shows "
         f"{current_wallpaper() or 'something else'}")
    return False


# ----------------------------------------------------------------------------
# set management: rotate / prune


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


SET_STAMP_RE = re.compile(r"-(\d{8})-(\d{4})$")


def set_age_key(d: Path) -> tuple:
    """Sort key for a set directory: the trailing -YYYYmmdd-HHMM stamp.

    Sorting on the directory name alone would order by theme slug first, which
    silently picks the alphabetically-last theme as "newest". Fall back to mtime
    for directories that do not carry a stamp.
    """
    m = SET_STAMP_RE.search(d.name)
    if m:
        return (1, m.group(1) + m.group(2))
    return (0, f"{d.stat().st_mtime:020.0f}")


def list_sets(root: Path) -> list[Path]:
    """Generated set directories under root, newest first."""
    if not root.is_dir():
        return []
    sets = [d for d in root.iterdir() if d.is_dir() and any(
        f.suffix.lower() in IMAGE_SUFFIXES for f in d.iterdir())]
    return sorted(sets, key=set_age_key, reverse=True)


def set_images(d: Path) -> list[Path]:
    return sorted(f for f in d.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)


def rotate(root: Path, scope: str = "latest", shuffle: bool = False) -> bool:
    """Advance the desktop picture to the next image and set it.

    Position is derived from what is currently on screen rather than stored in a
    state file, so the rotation stays correct even if the wallpaper is changed by
    hand or a new set lands between ticks.
    """
    sets = list_sets(root)
    if not sets:
        info(f"no generated sets under {root}")
        return False

    pool: list[Path] = []
    for d in (sets[:1] if scope == "latest" else sets):
        pool.extend(set_images(d))
    if not pool:
        info(f"no images found under {root}")
        return False

    current = current_wallpaper()
    try:
        idx = [str(p) for p in pool].index(current)
        nxt = pool[(idx + 1) % len(pool)]
    except ValueError:
        nxt = pool[0]                      # not one of ours -> start at the top
    if shuffle and len(pool) > 1:
        nxt = random.choice([p for p in pool if str(p) != current])

    if set_wallpaper(nxt.resolve()):
        print(f"wallpaper -> {nxt.parent.name}/{nxt.name}")
        return True
    return False


def prune(root: Path, keep: int) -> int:
    """Delete all but the `keep` newest sets. Returns how many were removed."""
    import shutil
    sets = list_sets(root)
    doomed = sets[keep:]
    current = current_wallpaper()
    removed = 0
    for d in doomed:
        # never delete the set the desktop is currently showing
        if current and Path(current).parent == d:
            info(f"keeping {d.name} (currently on screen)")
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    if removed:
        print(f"pruned {removed} old set(s), kept {keep}")
    return removed


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wallgen",
        description="Generate a themed set of 4K-ish wallpapers for macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  wallgen "misty scandinavian pine forest at dawn" -n 6 --set
  wallgen "brutalist concrete architecture, fog" -n 4 -q high
  wallgen "deep space nebula, teal and magenta" --provider replicate -n 10
  wallgen "kyoto in the rain" --dry-run          # see prompts, spend nothing
  wallgen "cyberpunk alley" --res 5k --format jpg

cost per image (gpt-image-2 at 3840x2160, measured from the API's usage report):
  low     ~$0.012        medium  ~$0.100        high    more still
alternatives:
  openai    gpt-image-1-mini   ~$0.003 low / ~$0.013 medium  (1536x1024, upscaled)
  replicate flux-schnell       ~$0.003 flat
""",
    )
    p.add_argument("theme", nargs="?", help="the theme, e.g. \"foggy redwood forest\"")
    p.add_argument("-n", "--count", type=int, default=None,
                   help="how many wallpapers (default 4; with --prompts-file, defaults to "
                        "every prompt in the file and only ever trims)")
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT),
                   help=f"output root (default {DEFAULT_OUT})")
    p.add_argument("--res", default="auto",
                   help="auto | 4k | 5k | 6k | 1440p | WxH (default auto = your display's native pixels)")
    p.add_argument("--provider", choices=["azure", "openai", "replicate"], default="azure",
                   help="azure (default; key pulled via `az`) | openai | replicate")
    p.add_argument("--azure-account", default=os.environ.get("WALLGEN_AZURE_ACCOUNT"),
                   help="Azure AI Services account name (or set WALLGEN_AZURE_ACCOUNT)")
    p.add_argument("--azure-group", default=os.environ.get("WALLGEN_AZURE_GROUP"),
                   help="Azure resource group (or set WALLGEN_AZURE_GROUP)")
    p.add_argument("--azure-deployment", default=os.environ.get("WALLGEN_AZURE_DEPLOYMENT"),
                   help="image model deployment name (or set WALLGEN_AZURE_DEPLOYMENT)")
    p.add_argument("-m", "--model", default=None,
                   help="azure: deployment name (default gpt-image-2); "
                        "openai: gpt-image-1-mini (default) | gpt-image-1.5 | gpt-image-1; "
                        "replicate: flux-schnell (default) | flux-dev | flux-1.1-pro")
    p.add_argument("-q", "--quality", choices=["low", "medium", "high"], default="low",
                   help="azure/openai only. Measured on gpt-image-2 at 4K: "
                        "low ~$0.012/img, medium ~$0.100/img, high more still (default low)")
    p.add_argument("--text-model", default=None,
                   help="model/deployment used to expand the theme "
                        "(default: gpt-5.5 on azure, gpt-4o-mini otherwise)")
    p.add_argument("--no-expand", action="store_true",
                   help="use the theme verbatim instead of expanding it into distinct prompts")
    p.add_argument("--prompts-file",
                   help="render a hand-authored set: one prompt per line (blank lines and "
                        "# comments ignored). Skips expansion; theme is used only for the "
                        "output folder name. -n trims the list if given.")
    p.add_argument("--format", choices=["png", "jpg"], default="png")
    p.add_argument("--sharpen", type=float, default=0.55,
                   help="post-upscale unsharp amount, 0 to disable (default 0.55)")
    p.add_argument("--saturation", type=float, default=1.0, help="1.0 = untouched")
    p.add_argument("--set", dest="do_set", action="store_true",
                   help="set the first generated image as the desktop picture")
    p.add_argument("--jobs", type=int, default=2,
                   help="concurrent generations (default 2; raise once Azure capacity allows)")
    p.add_argument("--dry-run", action="store_true", help="print the prompts and exit, no spend")
    p.add_argument("--displays", action="store_true", help="show detected displays and exit")

    g = p.add_argument_group("rotation and housekeeping")
    g.add_argument("--rotate", action="store_true",
                   help="do not generate: advance the desktop picture to the next "
                        "image in the newest set and exit")
    g.add_argument("--rotate-scope", choices=["latest", "all"], default="latest",
                   help="rotate within the newest set only (default) or across every set")
    g.add_argument("--shuffle", action="store_true",
                   help="with --rotate, pick at random instead of in order")
    g.add_argument("--prune", type=int, metavar="N",
                   help="after generating, keep only the N newest sets and delete the rest")
    g.add_argument("--list-sets", action="store_true", help="list generated sets and exit")
    return p


def main():
    load_env_files()
    args = build_parser().parse_args()

    if args.displays:
        got = detect_displays()
        print("detected displays (native pixels):")
        for w, h in got or []:
            print(f"  {w}x{h}   aspect {w/h:.3f}")
        if not got:
            print("  none detected")
        print(f"\n--res auto would use: {'x'.join(map(str, resolve_target('auto')))}")
        return

    root = Path(args.out).expanduser()

    if args.list_sets:
        sets = list_sets(root)
        if not sets:
            print(f"no sets under {root}")
        for d in sets:
            imgs = set_images(d)
            print(f"  {d.name:<46} {len(imgs):>2} images")
        return

    if args.rotate:
        sys.exit(0 if rotate(root, args.rotate_scope, args.shuffle) else 1)

    if not args.theme:
        build_parser().print_help()
        sys.exit(2)
    if args.count is not None and args.count < 1:
        die("-n must be >= 1")

    default_model = {"azure": args.azure_deployment or "gpt-image-2",
                     "openai": "gpt-image-1-mini",
                     "replicate": "flux-schnell"}[args.provider]
    model = args.model or default_model
    if args.provider == "replicate" and model not in REPLICATE_MODELS:
        die(f"unknown replicate model {model!r}; pick one of {', '.join(REPLICATE_MODELS)}")

    target = resolve_target(args.res)

    # --- keys -------------------------------------------------------------
    openai_key = os.environ.get("OPENAI_API_KEY")
    replicate_key = os.environ.get("REPLICATE_API_TOKEN")
    az_acct, az_grp = args.azure_account, args.azure_group
    use_azure_text = args.provider == "azure"

    if not args.dry_run:
        if args.provider == "azure":
            if not az_acct or not az_grp:
                die("Azure account/group not configured. Run `wallgen-setup`, or set "
                    "WALLGEN_AZURE_ACCOUNT and WALLGEN_AZURE_GROUP in ~/.config/wallgen/env, "
                    "or pass --azure-account/--azure-group.")
            az_key(az_acct, az_grp)          # fail fast with a useful message
        elif args.provider == "openai" and not openai_key:
            die("OPENAI_API_KEY not set. Put it in ~/.config/wallgen/env or export it.")
        elif args.provider == "replicate" and not replicate_key:
            die("REPLICATE_API_TOKEN not set. Put it in ~/.config/wallgen/env or export it.")

        # prompt expansion needs *some* text model
        if not args.no_expand and not use_azure_text and not openai_key:
            info("no OPENAI_API_KEY for prompt expansion -- using the literal theme")
            args.no_expand = True

    text_model = (args.text_model or os.environ.get("WALLGEN_TEXT_MODEL")
                  or ("gpt-5.5" if use_azure_text else "gpt-4o-mini"))

    # --- prompts ----------------------------------------------------------
    if args.prompts_file:
        pf = Path(args.prompts_file).expanduser()
        if not pf.is_file():
            die(f"--prompts-file not found: {pf}")
        prompts = [ln.strip() for ln in pf.read_text().splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
        if not prompts:
            die(f"--prompts-file {pf} has no usable lines")
        # -n only trims a hand-authored set; it never pads it with repeats
        if args.count is not None and args.count < len(prompts):
            prompts = prompts[:args.count]
        args.count = len(prompts)
        info(f"using {len(prompts)} hand-authored prompts from {pf.name}")
    elif args.no_expand or (args.dry_run and not openai_key and args.provider != "azure"):
        args.count = args.count or 4
        prompts = literal_prompts(args.theme, args.count)
    else:
        args.count = args.count or 4
        info(f"expanding theme into {args.count} prompts via {text_model} ...")
        try:
            if use_azure_text:
                prompts = expand_theme(args.theme, args.count, text_model,
                                       az_key(az_acct, az_grp),
                                       base_url=az_endpoint(az_acct, az_grp), azure=True)
            else:
                prompts = expand_theme(args.theme, args.count, text_model, openai_key)
        except Exception as e:
            info(f"expansion failed ({e}); falling back to the literal theme")
            prompts = literal_prompts(args.theme, args.count)

    full = [f"{p.rstrip('. ')}. {WALLPAPER_RULES}" for p in prompts]

    if args.dry_run:
        print(f"\ntheme:  {args.theme}")
        print(f"target: {target[0]}x{target[1]}  ({args.provider}/{model})\n")
        for i, p in enumerate(prompts, 1):
            print(f"\033[1m{i}.\033[0m {p}\n")
        return

    # --- generate ---------------------------------------------------------
    stamp = time.strftime("%Y%m%d-%H%M")
    outdir = root / f"{slugify(args.theme)}-{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    if args.provider == "azure":
        nat = azure_native_size(target)
        how = "natively" if nat == target or max(target) > AZURE_MAX_EDGE else f"at {nat[0]}x{nat[1]}"
        extra = "" if max(target) <= AZURE_MAX_EDGE else f", then upscaled to {target[0]}x{target[1]}"
        info(f"rendering {how}{extra}  (quality={args.quality})")

    info(f"generating {args.count} x {target[0]}x{target[1]} via {args.provider}/{model} -> {outdir}")

    def one(idx_prompt):
        i, prompt = idx_prompt
        # A 429 is a queue, not a failure: it gets its own generous budget so a
        # slow deployment can never exhaust the retries meant for real errors.
        errors = 0
        for _ in range(40):
            try:
                if args.provider == "azure":
                    res = gen_azure(prompt, model, args.quality, target, az_acct, az_grp)
                elif args.provider == "openai":
                    res = gen_openai(prompt, model, args.quality, target, openai_key)
                else:
                    res = gen_replicate(prompt, model, target, replicate_key)

                img = finish(res.png, target, args.sharpen, args.saturation)
                path = outdir / f"{i:02d}.{args.format}"
                if args.format == "jpg":
                    img.save(path, "JPEG", quality=95, subsampling=0, optimize=True)
                else:
                    img.save(path, "PNG", optimize=False)
                mb = path.stat().st_size / 1e6
                print(f"  \033[32mok\033[0m  {path.name}  {img.width}x{img.height}  {mb:.1f} MB")
                return res.cost, res.cost_is_estimate, path
            except RateLimited as e:
                # Deployment capacity is shared across jobs; wait it out rather
                # than burning a retry. Jitter so parallel jobs don't resync.
                wait = min(120.0, e.retry_after + 2) + random.uniform(0, 3)
                info(f"  #{i} rate limited, waiting {wait:.0f}s")
                time.sleep(wait)
            except Exception as e:
                msg = str(e)[:200]
                errors += 1
                if errors > 3:
                    print(f"  \033[31mfail\033[0m #{i}: {msg}")
                    return 0.0, False, None
                info(f"  retry #{i} after error: {msg}")
                time.sleep(2 * errors)
        print(f"  \033[31mfail\033[0m #{i}: still rate limited after 40 attempts")
        return 0.0, False, None

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(one, enumerate(full, 1)))

    made = [r[2] for r in results if r and r[2]]
    total = sum(r[0] for r in results if r)
    estimated = any(r[1] for r in results if r)

    if not made:
        die("no wallpapers were generated")

    prefix = "~" if estimated else ""
    print(f"\n{len(made)}/{args.count} generated in {outdir}")
    print(f"cost: {prefix}${total:.3f}" + ("  (estimate)" if estimated else ""))

    (outdir / "prompts.json").write_text(json.dumps(
        {"theme": args.theme, "provider": args.provider, "model": model,
         "quality": args.quality, "target": list(target),
         "rendered_at": list(azure_native_size(target)) if args.provider == "azure" else None,
         "cost_usd": round(total, 4), "prompts": prompts},
        indent=2))

    if args.prune is not None and args.prune > 0:
        prune(root, args.prune)

    if args.do_set:
        if set_wallpaper(made[0].resolve()):
            print(f"desktop set to {made[0].name} (verified)")
        else:
            print("could not set the desktop picture; open the folder and pick one manually")

    print(f"\ntip: System Settings > Wallpaper > Add Folder > {outdir}  to rotate through the set")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
