"""MuseCut timeline model.

A MuseCut project is a plain JSON document. This module defines the
schema, validates it, and offers a small builder API so agents (and the
MCP server) can assemble timelines without hand-writing JSON.

Top-level project shape::

    {
      "version": 1,
      "name": "my-video",
      "width": 1280, "height": 720, "fps": 30,
      "background": "#000000",
      "clips": [ ... ],        # main video track, played in order
      "overlays": [ ... ],     # PiP / stickers / image overlays
      "texts": [ ... ],        # text overlays with animation
      "audio": [ ... ],        # voice / music / sfx tracks
      "captions": { ... },     # word-level captions (viral style)
      "endcard": { ... }       # optional end card
    }

Clip fields (all optional except ``src`` unless noted)::

    src            path to a video/image file, or "color:#RRGGBB" / "testsrc"
    start          seconds into the source file (default 0)
    duration       seconds to use (default: rest of source)
    speed          constant speed factor, e.g. 2.0 = 2x (default 1.0)
    speed_curve    [{"time": 0.0, "speed": 1.0}, ...] piecewise speed map
    reverse        true to play the clip backwards
    freeze_at      timestamp in source; hold that frame for `duration`
    transition_in  {"type": "crossfade"|"dip-to-black"|"whoosh"|"wipe"|"none",
                   "duration": 0.5} — round 5 adds "slide" (slideleft),
                   "zoom" (zoomin), "dissolve", "circle" (circleopen),
                   "pixelize" (glitch-style) and "dip-to-white"
                   (fadewhite); all are native ffmpeg xfade transitions.
    keyframes      {"zoom": {"from": 1.0, "to": 1.25},
                   "pan": {"from": [0.5, 0.5], "to": [0.4, 0.5]}}
    effects        {"filter": "cinematic"|"vivid"|"bw"|"vintage"|"warm"|"cool"|...,
                   "brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                   "hue": 0, "sharpen": 0.0, "blur": 0.0,
                   "vignette": 0.0, "grain": 0.0}
    chroma_key     {"color": "0x00FF00", "similarity": 0.3, "blend": 0.1}
    stabilize      true to run vidstab (two-pass)
    mask           {"shape": "circle"|"rect"|"linear"|"diagonal",
                   "x": 0.5, "y": 0.5, "size": 0.4, "feather": 0.05,
                   "angle": 45, "effect": "blur"|"darken"|"desaturate"}
                   "diagonal" draws a straight seam through (x, y) at
                   `angle` degrees from horizontal (-180..180; 90 =
                   vertical seam, white left of x like "linear").
                   "desaturate" turns the masked-OUT area B&W instead
                   of blurring/darkening it.
    fit            "contain" (default: scale + pad bars) | "cover"
                   (scale + centre-crop to fill the canvas) | "blur"
                   (blurred-canvas fill: the clip centred on a blurred
                   copy of itself) | "none" (no sizing at all — the
                   clip stays at native resolution; ONLY valid with
                   transform keyframes (position/scale), which do the
                   sizing. This is what smart_reframe uses: the
                   keyframe scale IS the cover scale, so the position
                   keyframes' math (see musecut.track) lines up
                   exactly. Using "cover" + a scale keyframe would
                   double-scale — verified by grid diagnostic
                   2026-11-20). Ignored when legacy zoom keyframes
                   are present (zoompan does its own cover).
    effects.glitch true | {"strength": 0.5, "strobe": 4} — RGB-split +
                   slice-displacement glitch; strobe N blacks out
                   every Nth frame (0 = off, 1..60)
    deflicker      true | {"size": 5, "mode": "am"} — remove temporal
                   luminance flicker (timelapse / auto-exposure);
                   size 2..129 frames, mode am/gm/hm/qm/cm/pm/median
    effects.flip   true | "horizontal"|"vertical"|"both" — mirror the
                   frame (true = horizontal); the mirror a camera on
                   the other side of the axis of action would see
    locked         true — pin the clip: no transform keyframes
                   (position/scale/rotation/zoom/pan) may move it
    blend          "screen"|"multiply"|"overlay"|... (full-frame blend overlay)
    volume         clip audio volume 0.0-1.0 (default 1.0)
    muted          true to drop the clip's own audio

Object lock (overlays / texts — timeline binding, NOT tracking)::

    lock_to        {"clip": 0, "dx": 0, "dy": -40} — bind the element
                   to a main-track clip: its top-left corner follows
                   the anchor clip's keyframed position point, plus
                   the dx/dy pixel offset. The anchor's authored
                   keyframes drive it; nothing is detected in the
                   footage. An element cannot be both locked and
                   lock_to-bound.
    locked         true — pin the element to its declared x/y

Audio track fields::

    src            path to an audio (or video) file
    role           "voice" | "music" | "sfx" (default "music")
    start          seconds into the timeline where it begins (default 0)
    offset         seconds into the source file (default 0)
    duration       seconds to use (default: rest of source / timeline)
    volume         0.0-1.0 (default 1.0)
    loop           true to loop the source to fill `duration`
    ducking        {"sidechain": "voice", "threshold": 0.02, "ratio": 8}
                   (music only: duck under the voice track)
    denoise        true to apply afftdn noise reduction
    vocal_beautify true (or {"strength": 0..2, "remove_breaths": bool})
                   to polish a voice track in-render (highpass + afftdn
                   + deesser + presence EQ + compressor; includes its
                   own denoise, so it replaces `denoise` on the track)
    voice_change   {"pitch": 1.5} or {"formant": "chipmunk"|"deep"|"robot"}
    fade_in / fade_out   seconds

Text overlay fields::

    text           the string (any UTF-8; '%' is safe — drawtext uses
                   expansion=none)
    start, duration
    font           "Anton" | "BebasNeue" | "Montserrat" | "Oswald" |
                   "ArchivoBlack" | "Inter" | path to a .ttf/.otf
    size           px (default 64)
    color          "#FFFFFF" (default)
    highlight      "#FFD60A" (accent colour for the animation)
    x, y           "center" | number | "left"/"right" (default center/lower)
    animation      "fade" | "slide-up" | "slide-down" | "slide-left" |
                   "slide-right" | "pop" | "pulse" | "loop-bounce" |
                   "fade-out" | "none"
                   (slides move in over 0.3 s; pulse is a loop
                   animation (alpha breathing); loop-bounce is a loop
                   animation (vertical bob +/-8 px at 2 Hz); fade-out
                   is an out animation)

Caption fields::

    words          [{"word": "hello", "start": 0.12, "end": 0.34}, ...]
    style          "viral-gold" (Anton, gold active word, lower third)
    font_size      px (default scales with canvas height)
    position       "lower" (default, ASS Alignment 2) | "mid"
                   (Alignment 5, centre of frame) | "top" (Alignment 8)
    word_colors    {"WORD": "#RRGGBB"} — the active word takes this
                   colour when it matches a key (case-insensitive),
                   instead of ktv_color / the default gold
    animation      "none" (default) | "pop" | "slide" | "typewriter"
                   — native ASS caption animations (see captions.py)
    word_bloom     {"WORD": "#RRGGBB"} — per-word glow outline for
                   the active word (overrides bloom_color for that
                   word); only the active word glows, inactive words
                   keep a plain black outline

Project-level letterbox (top-level ``letterbox``)::

    true                          0.125 top + 0.125 bottom bars
    {"top": 0.125, "bottom": 0.125}   fractions of the frame height,
                                  each 0..0.4. Applied to the main
                                  video only — the end card is not
                                  letterboxed.

End card fields::

    duration       seconds (default 5)
    title, subtitle
    background     "#0B0B0B" (default)
    thumbnail      optional path to an image shown on the card

Export block (top-level ``export``)::

    codec          "h264" (default) | "h265" | "prores422" | "prores422lt" |
                   "prores422hq" | "prores4444" | "prores422proxy" | "av1" |
                   "gif" | "wav" | "mp3" (audio-only)
    bitrate_ladder {"sd": "2M", "hd": "4M", "fullhd": "8M", "2k": "16M",
                   "4k": "32M", "gop": 60, "profile": "high"}
                   per-resolution target bitrates (CapCut's server-driven
                   Hw model, done locally); used by h264/h265/av1 only.
                   Without a ladder the encoder uses ``crf``.

Canvas (top-level ``canvas`` — background behind the main track)::

    {"type": "color", "color": "#FF0000"}
    {"type": "image", "image": "bg.png"}
    {"type": "blur", "blur": 25}   # blurred copy of the clip itself

Clip extras::

    motion_blur    true | {"frames": 8} — tmix motion blur (2..16 frames)
    effects        ... + "exposure" (-3..3 stops), "highlights" (-1..1),
                   "shadows" (-1..1), "clarity" (-1..1), "dehaze" (0..1),
                   "fade" (0..1), "lut" (path to a .cube LUT file)

Full transform keyframes (pro — Premiere-style, per clip)::

    keyframes      {"position": [{"time": 0.0, "value": [0.5, 0.5]}, ...],
                   "scale": [{"time": 0.0, "value": 1.0}, ...],
                   "rotation": [{"time": 0.0, "value": 0.0}, ...],  # degrees
                   "opacity": [{"time": 0.0, "value": 1.0}, ...],
                   "anchor": [0.5, 0.5]}   # position reference point
                   (legacy "zoom"/"pan" from/to keyframes still work)
                   Every keyframe may carry "easing": "linear"
                   (default) | "ease-in" | "ease-out" | "ease-in-out"
                   — it shapes the segment STARTING at that keyframe.
                   (Audio volume_keyframes support easing too.)

Nested sequences / compound clips (pro)::

    clip {"nested": "path/to/other.json"}        # a whole project as a clip
    clip {"nested": "sequence:intro"}           # top-level "sequences" entry
    clip {"nested": {...inline project dict...}}
    Top-level "sequences": {"intro": {...project...}} — named reusable
    sequences. Nesting depth is capped at 3.

Markers (pro — editorial metadata, no render effect)::

    "markers": [{"time": 1.5, "name": "hook"}, ...]

Adjustment layers (pro — effects over a time range of the final picture)::

    "adjustment_layers": [{"start": 2.0, "duration": 3.0,
                           "effects": {"brightness": 0.1, ...}}, ...]

Audio track extras (pro mixer)::

    eq             {"low": dB, "mid": dB, "high": dB}  (-24..24 each)
    compressor     {"threshold": -18, "ratio": 4, "attack": 20,
                   "release": 250, "makeup": 0}
    volume_keyframes  [{"time": 0.0, "value": 1.0}, ...]  (multiplier)
    pan            -1..1 (full left .. centre .. full right)
    balance        true | {"target_i": -16, "target_tp": -1.5,
                   "target_lra": 11} — loudnorm per-track balancing

Text extras::

    glow           {"color": "#FFD60A", "strength": 0.5}
    bubble         {"color": "#000000", "alpha": 0.6, "padding": 12}
    underline      true (width estimated from the text; exact in captions)
    bold_width     0..10 (fake-bold double draw)
    3d             true | {"depth": 6, "depth_color": "#222222"} — extruded
                   3D text: the text is drawn `depth` times (1..20),
                   each copy offset 1px down-right in `depth_color`,
                   then the main text on top (round 3)
    shape          {"type": "arc"|"circle", "radius": 300, "angle": 180}
                   — text laid out along an arc (upright glyphs, one
                   drawtext per character); "circle" is a full 360°
                   ring. `angle` is the arc span in degrees (30..360,
                   arc only); `radius` in px (50..3000, default
                   min(W,H)*0.35). Underline/bold are ignored for
                   shape text (round 3)
    template       "title-bold"|"lower-third"|"quote"|"punch"|
                   "caption-clean" (round 5) — a preset text style:
                   font/size/colour/position/animation/glow/bubble
                   baked in; any field the text sets itself wins.
                   `size_frac` (0.005..0.5 of frame height) sizes a
                   templated text; top-level `text_templates` adds
                   project-level templates or overrides a built-in.

Cover templates (top-level ``cover`` — the video's designed first
frame, round 3)::

    {"template": "bold"|"split"|"minimal", "title": "BIG TITLE",
     "subtitle": "smaller line", "duration": 3.0,
     "background": "#0B0B0B", "accent": "#FFD60A"}
    Rendered as a colour clip of `duration` at the head of the
    timeline with the title/subtitle composed per template:
    "bold" = giant centred title + accent subtitle below;
    "split" = big title top-left + accent divider + subtitle
    bottom-left; "minimal" = small title + subtitle lower-left,
    mostly empty frame.

Export extras (round 3)::

    hdr            true — HDR10 export (PQ / BT.2020, 10-bit,
                   mastering-display + MaxCLL metadata). Only with
                   codec "h265". Converts the SDR timeline into an
                   HDR10 container — it does NOT invent HDR
                   highlights; a graded HDR source is still owed.
    alpha          true — keep the alpha channel through the graph
                   and encode it. Only with codec "prores4444"
                   (yuva444p12le — ProRes 4444 is 12-bit). HEVC-with-alpha is BLOCKED on
                   this build (libx265 lists no alpha pixel formats;
                   VP9 alpha writes the tag but no alpha plane
                   round-trips — verified 2026-11-20), so it is
                   refused here instead of silently dropping alpha.

Caption extras::

    ktv_color, ktv_outline_color, ktv_shadow_color   karaoke colours
    underline, bold, italic                          ASS style flags
    bloom_color, bloom_strength (0..1)               text glow
    bubble, bubble_color                             opaque box behind text
    active_size_ratio (1.0..2.0, round 5)            two-tier sizing:
                   the highlighted word renders at font_size *
                   ratio while the rest of the line stays at
                   font_size — the elite-reel small-setup + big-punch
                   line, inside one caption event.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from . import keyframes as kf_mod

VERSION = 1

FONT_ALIASES = {
    "Anton": "Anton-Regular.ttf",
    "BebasNeue": "BebasNeue-Regular.ttf",
    "Montserrat": "Montserrat-VF.ttf",
    "Oswald": "Oswald-VF.ttf",
    "ArchivoBlack": "ArchivoBlack-Regular.ttf",
    "Inter": "Inter-VF.ttf",
}

TRANSITION_TYPES = {"none", "crossfade", "dip-to-black", "whoosh", "wipe", "fade",
                    # (round 5, v0.7.0) native ffmpeg xfade mappings —
                    # slide=slideleft, zoom=zoomin, dissolve=dissolve,
                    # circle=circleopen, pixelize=pixelize (glitch-style),
                    # dip-to-white=fadewhite, radial=radial (clock wipe,
                    # round 31, v0.18.0). See render.XFADE_MAP.
                    "slide", "zoom", "dissolve", "circle", "pixelize",
                    "dip-to-white", "radial"}
FILTER_PRESETS = {
    "none", "cinematic", "vivid", "bw", "vintage", "warm", "cool",
    "sharpen", "soft", "dramatic",
}
BLEND_MODES = {
    "screen", "multiply", "overlay", "darken", "lighten", "difference",
    "addition", "average", "hardlight", "softlight",
}
EXPORT_CODECS = {
    "h264", "h265", "prores422", "prores422lt", "prores422hq",
    "prores4444", "prores422proxy", "av1", "gif", "wav", "mp3",
    "dnxhd",
}
CANVAS_TYPES = {"color", "image", "blur"}
LADDER_KEYS = {"sd", "hd", "fullhd", "2k", "4k"}
AUDIO_ROLES = {"voice", "music", "sfx"}
TEXT_ANIMATIONS = {"none", "fade", "slide-up", "slide-down",
                   "slide-left", "slide-right", "pop", "pulse",
                   "loop-bounce", "fade-out"}
MASK_SHAPES = {"circle", "rect", "linear", "diagonal"}
MASK_EFFECTS = {"blur", "darken", "desaturate"}
FIT_MODES = {"contain", "cover", "blur", "none"}
COVER_TEMPLATES = {"bold", "split", "minimal"}
TEXT_SHAPES = {"arc", "circle"}
CAPTION_POSITIONS = {"lower", "mid", "top"}
CAPTION_ANIMATIONS = {"none", "pop", "slide", "typewriter"}

# Caption styles (round 6, v0.8.0) — named presets for
# ``captions.style``. A style is the DEFAULT for the fields it sets;
# any field the project sets itself wins. ``viral-gold`` is the
# historical default (Anton, gold active word, white rest, lower
# third) — a project with no style renders exactly as before.
# ``minimal`` is the clean sans look (Inter, white active, grey
# rest, no decoration); ``news`` is the broadcast lower third
# (Oswald bold, white on a dark bubble). New styles are added here
# and validated by name in ``validate_timeline``.
CAPTION_STYLES: Dict[str, Dict[str, Any]] = {
    "viral-gold": {"font": "Anton", "active_color": "#FFD60A",
                   "inactive_color": "#FFFFFF", "position": "lower",
                   "bubble": False, "bubble_color": "#000000",
                   "bold": False},
    "minimal": {"font": "Inter", "active_color": "#FFFFFF",
                "inactive_color": "#9A9A9A", "position": "lower",
                "bubble": False, "bubble_color": "#000000",
                "bold": False},
    "news": {"font": "Oswald", "active_color": "#FFFFFF",
             "inactive_color": "#FFFFFF", "position": "lower",
             "bubble": True, "bubble_color": "#111111", "bold": True},
}
DEFLICKER_MODES = {"am", "gm", "hm", "qm", "cm", "pm", "median"}
# Flip modes (round 52, v0.20.0) — mirror the frame: "horizontal"
# (left-right), "vertical" (top-bottom), "both".
FLIP_MODES = {"horizontal", "vertical", "both"}

# Text templates (round 5, v0.7.0) — CapCut's preset text styles as
# config. A text with "template" takes the template's fields as
# defaults; any field the text sets itself wins. "size_frac" is a
# fraction of the frame height, resolved at render time so the same
# template works at 720p and 1080p. Project-level "text_templates"
# (top-level dict) adds new templates or overrides a built-in one
# with the same name.
TEXT_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # Giant centred title, gold glow, pops in — the cover/title card.
    "title-bold": {"font": "Anton", "size_frac": 0.13,
                   "color": "#FFFFFF", "x": "center", "y": "center",
                   "animation": "pop",
                   "glow": {"color": "#FFD60A", "strength": 0.5}},
    # News-style lower third: small white text, left, dark bubble.
    "lower-third": {"font": "Inter", "size_frac": 0.045,
                    "color": "#FFFFFF", "x": "left", "y": "lower",
                    "animation": "fade",
                    "bubble": {"color": "#000000", "alpha": 0.6}},
    # Centred statement line, mid-frame — the quote/reveal card.
    "quote": {"font": "Montserrat", "size_frac": 0.055,
              "color": "#FFFFFF", "x": "center", "y": "center",
              "animation": "fade"},
    # One-word punch: huge gold Anton, hard pop + strong glow.
    "punch": {"font": "Anton", "size_frac": 0.16,
              "color": "#FFD60A", "x": "center", "y": "center",
              "animation": "pop",
              "glow": {"color": "#FFD60A", "strength": 0.8}},
    # Small clean subtitle line, lower third, no decoration.
    "caption-clean": {"font": "Inter", "size_frac": 0.035,
                      "color": "#FFFFFF", "x": "center", "y": "lower",
                      "animation": "fade"},
}

# Fields a text template may set (built-in or project-level).
TEXT_TEMPLATE_FIELDS = {
    "font", "size", "size_frac", "color", "highlight", "x", "y",
    "animation", "glow", "bubble", "bold_width", "underline",
    "underline_color", "3d",
}


def resolve_text_template(text: Dict[str, Any],
                          project_templates: Optional[Dict[str, Any]] = None
                          ) -> Dict[str, Any]:
    """Return a copy of ``text`` with its ``template`` defaults applied.

    Built-in templates come from :data:`TEXT_TEMPLATES`; project-level
    ``text_templates`` override a built-in of the same name or add a
    new one. Fields the text sets itself always win over the template.
    A text without ``template`` is returned unchanged (as a copy).
    """
    name = text.get("template")
    if not name:
        return dict(text)
    merged = dict(TEXT_TEMPLATES)
    for k, v in (project_templates or {}).items():
        merged[str(k)] = v
    tmpl = merged.get(str(name))
    if tmpl is None:
        raise TimelineError(f"unknown text template: {name!r} "
                            f"(want one of {sorted(merged)})")
    out = dict(tmpl)
    out.update(text)
    return out


class TimelineError(ValueError):
    """Raised when a timeline document is invalid."""


def default_font_path(font: str) -> str:
    """Resolve a font name/alias/path to an absolute .ttf path."""
    if os.path.isfile(font):
        return os.path.abspath(font)
    name = FONT_ALIASES.get(font, font)
    candidates = [
        os.path.expanduser(f"~/.fonts/{name}"),
        f"/usr/share/fonts/truetype/{name}",
        os.path.expanduser(f"~/.local/share/fonts/{name}"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Last resort: let ffmpeg/fontconfig find it by family name.
    return font


def new_project(name: str = "untitled", width: int = 1280,
                height: int = 720, fps: int = 30) -> Dict[str, Any]:
    """Return a blank project dict."""
    if width <= 0 or height <= 0:
        raise TimelineError("width and height must be positive")
    if fps <= 0 or fps > 120:
        raise TimelineError("fps must be between 1 and 120")
    return {
        "version": VERSION,
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "background": "#000000",
        "clips": [],
        "overlays": [],
        "texts": [],
        "audio": [],
        "captions": None,
        "endcard": None,
        "export": None,
        "canvas": None,
        "sequences": {},
        "markers": [],
        "adjustment_layers": [],
    }


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise TimelineError(msg)


def validate_timeline(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a project document in place; returns it for chaining.

    Raises TimelineError with a human-readable message on the first problem.
    """
    _check(isinstance(doc, dict), "project must be a JSON object")
    _check(doc.get("version", VERSION) == VERSION,
           f"unsupported timeline version {doc.get('version')!r}")
    for k in ("width", "height", "fps"):
        _check(_is_number(doc.get(k)), f"project.{k} must be a number")
    _check(doc["width"] > 0 and doc["height"] > 0,
           "project width/height must be positive")
    _check(0 < doc["fps"] <= 120, "project fps must be 1..120")

    lb = doc.get("letterbox")
    if lb is not None:
        if isinstance(lb, bool):
            pass  # true = 0.125 top + 0.125 bottom
        elif isinstance(lb, dict):
            for k in lb:
                _check(k in ("top", "bottom"),
                       f"letterbox key unknown: {k!r} (want top/bottom)")
            for k in ("top", "bottom"):
                if lb.get(k) is not None:
                    _check(_is_number(lb[k]) and 0 <= lb[k] <= 0.4,
                           f"letterbox.{k} must be a fraction 0..0.4")
        else:
            _check(False,
                   "letterbox must be true or {\"top\": .., \"bottom\": ..}")

    cvr = doc.get("cover")
    if cvr is not None:
        _check(isinstance(cvr, dict), "cover must be an object")
        _check(cvr.get("template", "bold") in COVER_TEMPLATES,
               f"cover.template unknown: {cvr.get('template')!r} "
               f"(want one of {sorted(COVER_TEMPLATES)})")
        _check(isinstance(cvr.get("title"), str) and cvr["title"],
               "cover.title is required")
        if cvr.get("subtitle") is not None:
            _check(isinstance(cvr["subtitle"], str),
                   "cover.subtitle must be a string")
        if cvr.get("duration") is not None:
            _check(_is_number(cvr["duration"]) and cvr["duration"] > 0,
                   "cover.duration must be > 0")
        for k in ("background", "accent"):
            if cvr.get(k) is not None:
                _check(isinstance(cvr[k], str),
                       f"cover.{k} must be a colour string")

    exp = doc.get("export")
    if exp is not None:
        _check(isinstance(exp, dict), "export must be an object")
        if exp.get("codec"):
            _check(exp["codec"] in EXPORT_CODECS,
                   f"export.codec unknown: {exp['codec']!r}")
        if exp.get("hdr") is not None:
            _check(isinstance(exp["hdr"], bool),
                   "export.hdr must be true or false")
            _check(exp.get("codec", "h264") == "h265",
                   "export.hdr (HDR10) needs codec \"h265\" — "
                   f"got {exp.get('codec', 'h264')!r}")
        if exp.get("alpha") is not None:
            _check(isinstance(exp["alpha"], bool),
                   "export.alpha must be true or false")
            _check(exp.get("codec", "h264") == "prores4444",
                   "export.alpha needs codec \"prores4444\" — HEVC-with-"
                   "alpha is blocked on this build (libx265 lists no "
                   "alpha pixel formats; verified 2026-11-20), so "
                   "alpha is only encoded as ProRes 4444")
        lad = exp.get("bitrate_ladder")
        if lad is not None:
            _check(isinstance(lad, dict),
                   "export.bitrate_ladder must be an object")
            for k, v in lad.items():
                if k in LADDER_KEYS:
                    _check(isinstance(v, (int, float, str)),
                           f"export.bitrate_ladder.{k} must be a bitrate")
                elif k == "gop":
                    _check(isinstance(v, int) and v > 0,
                           "export.bitrate_ladder.gop must be a positive int")
                elif k == "profile":
                    _check(isinstance(v, str),
                           "export.bitrate_ladder.profile must be a string")
                else:
                    _check(False,
                           f"export.bitrate_ladder key unknown: {k!r}")

    cv = doc.get("canvas")
    if cv is not None:
        _check(isinstance(cv, dict), "canvas must be an object")
        _check(cv.get("type", "color") in CANVAS_TYPES,
               f"canvas.type unknown: {cv.get('type')!r}")
        if cv.get("blur") is not None:
            _check(_is_number(cv["blur"]) and 0 <= cv["blur"] <= 50,
                   "canvas.blur must be 0..50")

    seqs = doc.get("sequences")
    if seqs is not None:
        _check(isinstance(seqs, dict), "sequences must be an object")
        for name, sub in seqs.items():
            _check(isinstance(sub, dict),
                   f"sequences[{name!r}] must be a project object")
            try:
                validate_timeline(sub)
            except TimelineError as exc:
                raise TimelineError(f"sequences[{name!r}]: {exc}")

    for i, m in enumerate(doc.get("markers") or []):
        _check(isinstance(m, dict), f"markers[{i}] must be an object")
        _check(_is_number(m.get("time")) and m["time"] >= 0,
               f"markers[{i}].time must be a number >= 0")
        _check(isinstance(m.get("name"), str) and m["name"],
               f"markers[{i}].name is required")

    for i, al in enumerate(doc.get("adjustment_layers") or []):
        _check(isinstance(al, dict),
               f"adjustment_layers[{i}] must be an object")
        _check(_is_number(al.get("start", 0)) and al.get("start", 0) >= 0,
               f"adjustment_layers[{i}].start must be >= 0")
        _check(_is_number(al.get("duration"))
               and al["duration"] > 0,
               f"adjustment_layers[{i}].duration must be > 0")
        _check(isinstance(al.get("effects"), dict),
               f"adjustment_layers[{i}].effects must be an object")

    clips = doc.get("clips") or []
    _check(isinstance(clips, list), "clips must be a list")
    for i, c in enumerate(clips):
        _validate_clip(c, f"clips[{i}]")

    for i, o in enumerate(doc.get("overlays") or []):
        _check(isinstance(o, dict), f"overlays[{i}] must be an object")
        _check("src" in o, f"overlays[{i}].src is required")
        _check(_is_number(o.get("start", 0)), f"overlays[{i}].start must be a number")
        _check(_is_number(o.get("duration", 0)) and o.get("duration", 0) > 0,
               f"overlays[{i}].duration must be > 0")
        if o.get("blend"):
            _check(o["blend"] in BLEND_MODES,
                   f"overlays[{i}].blend unknown: {o['blend']!r}")
        _validate_lock(o, f"overlays[{i}]", len(clips))

    # (round 5) top-level text_templates: project-level preset text
    # styles. Each entry is a dict of text fields (see
    # TEXT_TEMPLATE_FIELDS); it may override a built-in template or
    # add a new one. Validated here so texts[].template can resolve.
    project_templates: Dict[str, Any] = {}
    if doc.get("text_templates") is not None:
        tt = doc["text_templates"]
        _check(isinstance(tt, dict),
               "text_templates must be an object")
        for name, tmpl in tt.items():
            _check(isinstance(tmpl, dict),
                   f"text_templates[{name!r}] must be an object")
            for key in tmpl:
                _check(key in TEXT_TEMPLATE_FIELDS,
                       f"text_templates[{name!r}] has unknown field: "
                       f"{key!r}")
            if tmpl.get("size_frac") is not None:
                _check(_is_number(tmpl["size_frac"])
                       and 0.005 <= tmpl["size_frac"] <= 0.5,
                       f"text_templates[{name!r}].size_frac must be "
                       "0.005..0.5")
            if tmpl.get("animation") is not None:
                _check(tmpl["animation"] in TEXT_ANIMATIONS,
                       f"text_templates[{name!r}].animation unknown: "
                       f"{tmpl['animation']!r}")
        project_templates = tt

    for i, t in enumerate(doc.get("texts") or []):
        _check(isinstance(t, dict), f"texts[{i}] must be an object")
        _check(isinstance(t.get("text"), str) and t["text"],
               f"texts[{i}].text is required")
        _check(_is_number(t.get("start", 0)), f"texts[{i}].start must be a number")
        _check(_is_number(t.get("duration", 0)) and t.get("duration", 0) > 0,
               f"texts[{i}].duration must be > 0")
        if t.get("animation"):
            _check(t["animation"] in TEXT_ANIMATIONS,
                   f"texts[{i}].animation unknown: {t['animation']!r}")
        if t.get("template") is not None:
            # (round 5) must name a built-in template or a
            # project-level one; resolution happens at render time.
            _check(isinstance(t["template"], str),
                   f"texts[{i}].template must be a string")
            known = set(TEXT_TEMPLATES) | set(project_templates)
            _check(t["template"] in known,
                   f"texts[{i}].template unknown: {t['template']!r} "
                   f"(want one of {sorted(known)})")
        if t.get("size_frac") is not None:
            _check(_is_number(t["size_frac"])
                   and 0.005 <= t["size_frac"] <= 0.5,
                   f"texts[{i}].size_frac must be 0.005..0.5")
        _validate_lock(t, f"texts[{i}]", len(clips))
        if t.get("glow") is not None:
            _check(isinstance(t["glow"], dict),
                   f"texts[{i}].glow must be an object")
        if t.get("bubble") is not None:
            _check(isinstance(t["bubble"], dict),
                   f"texts[{i}].bubble must be an object")
        if t.get("bold_width") is not None:
            _check(_is_number(t["bold_width"])
                   and 0 <= t["bold_width"] <= 10,
                   f"texts[{i}].bold_width must be 0..10")
        if t.get("3d") is not None:
            d3 = t["3d"]
            if isinstance(d3, bool):
                pass  # true = defaults (depth 6, #222222)
            elif isinstance(d3, dict):
                if d3.get("depth") is not None:
                    _check(isinstance(d3["depth"], int)
                           and not isinstance(d3["depth"], bool)
                           and 1 <= d3["depth"] <= 20,
                           f"texts[{i}].3d.depth must be an int 1..20")
                if d3.get("depth_color") is not None:
                    _check(isinstance(d3["depth_color"], str),
                           f"texts[{i}].3d.depth_color must be a string")
            else:
                _check(False,
                       f"texts[{i}].3d must be true or an object")
        if t.get("shape") is not None:
            sh = t["shape"]
            _check(isinstance(sh, dict),
                   f"texts[{i}].shape must be an object")
            _check(sh.get("type", "arc") in TEXT_SHAPES,
                   f"texts[{i}].shape.type unknown: {sh.get('type')!r} "
                   f"(want one of {sorted(TEXT_SHAPES)})")
            if sh.get("radius") is not None:
                _check(_is_number(sh["radius"])
                       and 50 <= sh["radius"] <= 3000,
                       f"texts[{i}].shape.radius must be 50..3000 px")
            if sh.get("angle") is not None:
                _check(_is_number(sh["angle"])
                       and 30 <= sh["angle"] <= 360,
                       f"texts[{i}].shape.angle must be 30..360 degrees")

    for i, a in enumerate(doc.get("audio") or []):
        _check(isinstance(a, dict), f"audio[{i}] must be an object")
        _check("src" in a, f"audio[{i}].src is required")
        if a.get("role"):
            _check(a["role"] in AUDIO_ROLES,
                   f"audio[{i}].role unknown: {a['role']!r}")
        if a.get("ducking"):
            _check(isinstance(a["ducking"], dict),
                   f"audio[{i}].ducking must be an object")
        if a.get("denoise") is not None:
            _check(isinstance(a["denoise"], bool),
                   f"audio[{i}].denoise must be true/false")
        if a.get("vocal_beautify") is not None:
            vb = a["vocal_beautify"]
            _check(isinstance(vb, (bool, dict)),
                   f"audio[{i}].vocal_beautify must be true or an object")
            if isinstance(vb, dict):
                if "strength" in vb:
                    _check(_is_number(vb["strength"])
                           and 0 <= vb["strength"] <= 2,
                           f"audio[{i}].vocal_beautify.strength must be 0..2")
                if "remove_breaths" in vb:
                    _check(isinstance(vb["remove_breaths"], bool),
                           f"audio[{i}].vocal_beautify.remove_breaths must be true/false")
        if a.get("voice_change"):
            vc = a["voice_change"]
            _check(isinstance(vc, dict), f"audio[{i}].voice_change must be an object")
            _check("pitch" in vc or "formant" in vc,
                   f"audio[{i}].voice_change needs pitch or formant")
        if a.get("balance") is not None:
            b = a["balance"]
            _check(isinstance(b, (bool, dict)),
                   f"audio[{i}].balance must be true or an object")
        if a.get("eq") is not None:
            eq = a["eq"]
            _check(isinstance(eq, dict),
                   f"audio[{i}].eq must be an object")
            for band in ("low", "mid", "high"):
                if band in eq:
                    _check(_is_number(eq[band])
                           and -24 <= eq[band] <= 24,
                           f"audio[{i}].eq.{band} must be -24..24 dB")
        if a.get("compressor") is not None:
            co = a["compressor"]
            _check(isinstance(co, dict),
                   f"audio[{i}].compressor must be an object")
            if "threshold" in co:
                _check(_is_number(co["threshold"])
                       and -60 <= co["threshold"] <= 0,
                       f"audio[{i}].compressor.threshold must be -60..0 dB")
            if "ratio" in co:
                _check(_is_number(co["ratio"]) and 1 <= co["ratio"] <= 20,
                       f"audio[{i}].compressor.ratio must be 1..20")
        if a.get("volume_keyframes") is not None:
            try:
                kf_mod.validate_keyframes(
                    a["volume_keyframes"],
                    f"audio[{i}].volume_keyframes")
            except ValueError as exc:
                raise TimelineError(str(exc))
            for p in a["volume_keyframes"]:
                _check(0 <= p["value"] <= 4,
                       f"audio[{i}].volume_keyframes values must be 0..4")
        if a.get("pan") is not None:
            _check(_is_number(a["pan"]) and -1 <= a["pan"] <= 1,
                   f"audio[{i}].pan must be -1..1")

    caps = doc.get("captions")
    if caps is not None:
        _check(isinstance(caps, dict), "captions must be an object")
        words = caps.get("words") or []
        _check(isinstance(words, list), "captions.words must be a list")
        prev_end = 0.0
        for j, w in enumerate(words):
            _check(isinstance(w, dict), f"captions.words[{j}] must be an object")
            _check(isinstance(w.get("word"), str) and w["word"],
                   f"captions.words[{j}].word is required")
            _check(_is_number(w.get("start")) and _is_number(w.get("end")),
                   f"captions.words[{j}] needs numeric start/end")
            _check(0 <= w["start"] < w["end"],
                   f"captions.words[{j}] has bad timing")
            _check(w["start"] >= prev_end - 0.001,
                   f"captions.words[{j}] overlaps the previous word")
            prev_end = w["end"]

        for key in ("ktv_color", "ktv_outline_color", "ktv_shadow_color",
                    "bloom_color", "bubble_color"):
            if caps.get(key) is not None:
                _check(isinstance(caps[key], str),
                       f"captions.{key} must be a colour string")
        if caps.get("position") is not None:
            _check(caps["position"] in CAPTION_POSITIONS,
                   f"captions.position unknown: {caps['position']!r} "
                   f"(want one of {sorted(CAPTION_POSITIONS)})")
        if caps.get("style") is not None:
            _check(caps["style"] in CAPTION_STYLES,
                   f"captions.style unknown: {caps['style']!r} "
                   f"(want one of {sorted(CAPTION_STYLES)})")
        if caps.get("word_colors") is not None:
            wc = caps["word_colors"]
            _check(isinstance(wc, dict),
                   "captions.word_colors must be an object")
            for k, v in wc.items():
                _check(isinstance(k, str) and isinstance(v, str)
                       and len(v) == 7 and v.startswith("#"),
                       "captions.word_colors must map a word to a "
                       f"\"#RRGGBB\" colour string, got {k!r}: {v!r}")
        if caps.get("bloom_strength") is not None:
            _check(_is_number(caps["bloom_strength"])
                   and 0 <= caps["bloom_strength"] <= 1,
                   "captions.bloom_strength must be 0..1")
        if caps.get("animation") is not None:
            _check(caps["animation"] in CAPTION_ANIMATIONS,
                   f"captions.animation unknown: {caps['animation']!r} "
                   f"(want one of {sorted(CAPTION_ANIMATIONS)})")
        if caps.get("active_size_ratio") is not None:
            # (round 5) two-tier caption sizing: the highlighted
            # word renders at font_size * ratio (1.0 = off).
            _check(_is_number(caps["active_size_ratio"])
                   and 1.0 <= caps["active_size_ratio"] <= 2.0,
                   "captions.active_size_ratio must be 1.0..2.0")
        if caps.get("word_bloom") is not None:
            wb = caps["word_bloom"]
            _check(isinstance(wb, dict),
                   "captions.word_bloom must be an object")
            for k, v in wb.items():
                _check(isinstance(k, str) and isinstance(v, str)
                       and len(v) == 7 and v.startswith("#"),
                       "captions.word_bloom must map a word to a "
                       f"\"#RRGGBB\" colour string, got {k!r}: {v!r}")

    ec = doc.get("endcard")
    if ec is not None:
        _check(isinstance(ec, dict), "endcard must be an object")
        _check(_is_number(ec.get("duration", 5)) and ec.get("duration", 5) > 0,
               "endcard.duration must be > 0")

    return doc


def _validate_lock(el: Dict[str, Any], where: str, n_clips: int) -> None:
    """Validate an element's object-lock fields.

    ``locked: true`` pins the element: it may not also carry
    ``lock_to`` (a lock binding moves it) and — for clips, checked
    in ``_validate_clip`` — it may not carry transform keyframes.
    ``lock_to: {"clip": i, "dx": px, "dy": px}`` binds an overlay /
    text to a main-track clip: its top-left corner follows the
    anchor clip's keyframed position point, plus the pixel offset.
    This is a TIMELINE lock, not motion tracking: it follows the
    anchor's authored keyframes, it does not detect anything in the
    footage (see ai.py — track_object stays Planned).
    """
    locked = el.get("locked")
    if locked is not None:
        _check(isinstance(locked, bool),
               f"{where}.locked must be true or false")
    lock_to = el.get("lock_to")
    if lock_to is not None:
        _check(isinstance(lock_to, dict),
               f"{where}.lock_to must be an object")
        ci = lock_to.get("clip")
        _check(isinstance(ci, int) and not isinstance(ci, bool)
               and 0 <= ci < n_clips,
               f"{where}.lock_to.clip must be a main-track clip index "
               f"(0..{max(n_clips - 1, 0)}), got {ci!r}")
        for k in ("dx", "dy"):
            if lock_to.get(k) is not None:
                _check(_is_number(lock_to[k]),
                       f"{where}.lock_to.{k} must be a number (pixels)")
        _check(not locked,
               f"{where} cannot be both locked and lock_to-bound")


def _validate_clip(c: Dict[str, Any], where: str) -> None:
    _check(isinstance(c, dict), f"{where} must be an object")
    _check("src" in c or "nested" in c,
           f"{where} needs src or nested")
    nested = c.get("nested")
    if nested is not None:
        if isinstance(nested, dict):
            try:
                validate_timeline(nested)
            except TimelineError as exc:
                raise TimelineError(f"{where}.nested: {exc}")
        elif isinstance(nested, str):
            _check(nested and (nested.startswith("sequence:")
                               or nested.endswith(".json")),
                   f"{where}.nested must be a .json path, "
                   f"'sequence:<name>', or an inline project")
        else:
            _check(False, f"{where}.nested must be a path or a project")
    if c.get("speed") is not None:
        _check(_is_number(c["speed"]) and 0.05 <= c["speed"] <= 16,
               f"{where}.speed must be 0.05..16")
    if c.get("speed_curve"):
        sc = c["speed_curve"]
        _check(isinstance(sc, list) and len(sc) >= 2,
               f"{where}.speed_curve needs >= 2 points")
        for p in sc:
            _check(_is_number(p.get("time")) and _is_number(p.get("speed"))
                   and 0.05 <= p["speed"] <= 16,
                   f"{where}.speed_curve point is invalid")
    if c.get("transition_in"):
        t = c["transition_in"]
        _check(isinstance(t, dict), f"{where}.transition_in must be an object")
        _check(t.get("type", "none") in TRANSITION_TYPES,
               f"{where}.transition_in.type unknown: {t.get('type')!r}")
        _check(_is_number(t.get("duration", 0.5)) and t.get("duration", 0.5) >= 0,
               f"{where}.transition_in.duration must be >= 0")
    if c.get("fit") is not None:
        _check(c["fit"] in FIT_MODES,
               f"{where}.fit unknown: {c['fit']!r} "
               f"(want one of {sorted(FIT_MODES)})")
        if c["fit"] == "none":
            # "none" leaves the clip at native resolution — only
            # meaningful when transform keyframes do the sizing.
            kf = c.get("keyframes") or {}
            _check(bool(kf.get("position") or kf.get("scale")),
                   f"{where}.fit \"none\" needs keyframes.position or "
                   f"keyframes.scale (nothing else sizes the clip)")
    if c.get("background_remove") is not None:
        # (round 4, v0.6.0) AI background removal (RMBG-1.4).
        # The mask video is merged at native resolution before
        # fit, so speed/reverse/freeze would break mask alignment
        # and are refused. "transparent" keeps the alpha plane:
        # the transform-keyframes block composites onto a black
        # canvas, which would destroy it, and fit "contain" pads
        # with opaque bars — so transparent needs fit "cover".
        br = c["background_remove"]
        _check(isinstance(br, dict),
               f"{where}.background_remove must be an object")
        _check(isinstance(br.get("mask"), str) and br["mask"]
               and os.path.isfile(br["mask"]),
               f"{where}.background_remove.mask must be an existing "
               f"mask-video file (build one with "
               f"musecut.bgremove.make_mask_video)")
        bg = br.get("background", "#00FF00")
        if isinstance(bg, str) and bg.startswith("#"):
            _check(len(bg) == 7 and all(ch in "0123456789abcdefABCDEF"
                                        for ch in bg[1:]),
                   f"{where}.background_remove.background must be "
                   f"'transparent', a #RRGGBB colour, or an image "
                   f"file path")
        elif bg == "transparent":
            pass
        else:
            _check(isinstance(bg, str) and os.path.isfile(bg),
                   f"{where}.background_remove.background must be "
                   f"'transparent', a #RRGGBB colour, or an existing "
                   f"image file")
        _check(not c.get("speed") or float(c["speed"]) == 1.0,
               f"{where}.background_remove cannot be combined with "
               f"speed != 1 (mask alignment)")
        _check(not c.get("reverse") and not c.get("speed_curve")
               and c.get("freeze_at") is None,
               f"{where}.background_remove cannot be combined with "
               f"reverse / speed_curve / freeze_at (mask alignment)")
        if bg == "transparent":
            kf = c.get("keyframes") or {}
            _check(not (kf.get("position") or kf.get("scale")
                        or kf.get("opacity")),
                   f"{where}.background_remove with background "
                   f"'transparent' cannot be combined with transform "
                   f"keyframes (they composite onto a black canvas)")
            _check(c.get("fit", "contain") == "cover",
                   f"{where}.background_remove with background "
                   f"'transparent' needs fit \"cover\" (\"contain\" "
                   f"pads with opaque bars)")
    if c.get("effects"):
        fx = c["effects"]
        _check(isinstance(fx, dict), f"{where}.effects must be an object")
        if fx.get("filter"):
            _check(fx["filter"] in FILTER_PRESETS,
                   f"{where}.effects.filter unknown: {fx['filter']!r}")
        if fx.get("glitch") is not None:
            g = fx["glitch"]
            if isinstance(g, bool):
                pass  # true = defaults (strength 0.5, no strobe)
            elif isinstance(g, dict):
                if g.get("strength") is not None:
                    _check(_is_number(g["strength"])
                           and 0 <= g["strength"] <= 1,
                           f"{where}.effects.glitch.strength must be 0..1")
                if g.get("strobe") is not None:
                    _check(isinstance(g["strobe"], int)
                           and not isinstance(g["strobe"], bool)
                           and 0 <= g["strobe"] <= 60,
                           f"{where}.effects.glitch.strobe must be an "
                           f"int 0..60")
            else:
                _check(False,
                       f"{where}.effects.glitch must be true or an object")
        if fx.get("deflicker") is not None:
            df = fx["deflicker"]
            if isinstance(df, bool):
                pass  # true = defaults (size 5, mode "am")
            elif isinstance(df, dict):
                if df.get("size") is not None:
                    _check(isinstance(df["size"], int)
                           and not isinstance(df["size"], bool)
                           and 2 <= df["size"] <= 129,
                           f"{where}.effects.deflicker.size must be an "
                           f"int 2..129")
                if df.get("mode") is not None:
                    _check(df["mode"] in DEFLICKER_MODES,
                           f"{where}.effects.deflicker.mode unknown: "
                           f"{df['mode']!r} (want one of "
                           f"{sorted(DEFLICKER_MODES)})")
            else:
                _check(False,
                       f"{where}.effects.deflicker must be true or an "
                       f"object")
        if fx.get("flip") is not None:
            fl = fx["flip"]
            if isinstance(fl, bool):
                pass  # true = horizontal
            elif isinstance(fl, str):
                _check(fl in FLIP_MODES,
                       f"{where}.effects.flip unknown: {fl!r} "
                       f"(want one of {sorted(FLIP_MODES)})")
            else:
                _check(False,
                       f"{where}.effects.flip must be true or one of "
                       f"{sorted(FLIP_MODES)}")
        for key, lo, hi in (("exposure", -3, 3), ("highlights", -1, 1),
                            ("shadows", -1, 1), ("clarity", -1, 1),
                            ("dehaze", 0, 1), ("fade", 0, 1)):
            if key in fx:
                _check(_is_number(fx[key]) and lo <= fx[key] <= hi,
                       f"{where}.effects.{key} must be {lo}..{hi}")
        if fx.get("lut") is not None:
            _check(isinstance(fx["lut"], str)
                   and fx["lut"].lower().endswith(".cube"),
                   f"{where}.effects.lut must be a .cube file path")
    if c.get("motion_blur") is not None:
        mb = c["motion_blur"]
        if isinstance(mb, dict):
            _check(_is_number(mb.get("frames", 8))
                   and 2 <= mb["frames"] <= 16,
                   f"{where}.motion_blur.frames must be 2..16")
        else:
            _check(isinstance(mb, bool),
                   f"{where}.motion_blur must be true or an object")
    if c.get("locked"):
        # A locked clip is pinned: no transform keyframes may move it.
        kf = c.get("keyframes") or {}
        for prop in ("position", "scale", "rotation", "zoom", "pan"):
            _check(prop not in kf,
                   f"{where} is locked and cannot carry "
                   f"keyframes.{prop}")
    if c.get("chroma_key"):
        ck = c["chroma_key"]
        _check(isinstance(ck, dict), f"{where}.chroma_key must be an object")
    if c.get("mask"):
        m = c["mask"]
        _check(isinstance(m, dict), f"{where}.mask must be an object")
        _check(m.get("shape", "circle") in MASK_SHAPES,
               f"{where}.mask.shape unknown: {m.get('shape')!r}")
        _check(m.get("effect", "blur") in MASK_EFFECTS,
               f"{where}.mask.effect unknown: {m.get('effect')!r}")
        if m.get("angle") is not None:
            _check(_is_number(m["angle"]) and -180 <= m["angle"] <= 180,
                   f"{where}.mask.angle must be -180..180 degrees")
    if c.get("keyframes"):
        kf = c["keyframes"]
        _check(isinstance(kf, dict), f"{where}.keyframes must be an object")
        if "zoom" in kf:
            z = kf["zoom"]
            _check(_is_number(z.get("from", 1.0)) and _is_number(z.get("to", 1.0)),
                   f"{where}.keyframes.zoom needs from/to numbers")
        if "anchor" in kf:
            a = kf["anchor"]
            _check(isinstance(a, (list, tuple)) and len(a) == 2
                   and all(_is_number(x) and 0 <= x <= 1 for x in a),
                   f"{where}.keyframes.anchor must be [x, y] in 0..1")
        for prop in ("position", "scale", "rotation", "opacity"):
            if prop in kf:
                try:
                    kf_mod.validate_keyframes(kf[prop],
                                              f"{where}.keyframes.{prop}",
                                              pair=(prop == "position"))
                except ValueError as exc:
                    raise TimelineError(str(exc))
                if prop in ("scale", "opacity"):
                    for p in kf[prop]:
                        _check(0 <= p["value"] <= 8 if prop == "scale"
                               else 0 <= p["value"] <= 1,
                               f"{where}.keyframes.{prop} values must be "
                               f"{'0..8' if prop == 'scale' else '0..1'}")


def load_project(path: str) -> Dict[str, Any]:
    """Load and validate a timeline JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    return validate_timeline(doc)


def save_project(doc: Dict[str, Any], path: str) -> None:
    """Validate and save a timeline JSON file."""
    validate_timeline(doc)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


def timeline_duration(doc: Dict[str, Any]) -> float:
    """Total timeline length in seconds (main track + end card)."""
    total = 0.0
    for c in doc.get("clips") or []:
        d = c.get("duration")
        if d is None:
            continue  # unknown until probed; render resolves it
        total += d / (c.get("speed") or 1.0)
        t = (c.get("transition_in") or {}).get("duration", 0) or 0
        total -= min(t, total)
    ec = doc.get("endcard")
    if ec:
        total += ec.get("duration", 5)
    # (round 3) the cover clip plays at the head of the timeline.
    cvr = doc.get("cover")
    if cvr:
        total += float(cvr.get("duration", 3.0) or 3.0)
    return total


def list_media_files(doc: Dict[str, Any]) -> List[str]:
    """All media paths referenced by the project (for existence checks)."""
    files: List[str] = []
    for c in doc.get("clips") or []:
        nested = c.get("nested")
        if isinstance(nested, str) and nested.endswith(".json"):
            files.append(nested)
            continue
        src = c.get("src", "")
        if src and not src.startswith(("color:", "testsrc")):
            files.append(src)
    for o in doc.get("overlays") or []:
        files.append(o.get("src", ""))
    for a in doc.get("audio") or []:
        files.append(a.get("src", ""))
    ec = doc.get("endcard") or {}
    if ec.get("thumbnail"):
        files.append(ec["thumbnail"])
    cv = doc.get("canvas") or {}
    if cv.get("type") == "image" and cv.get("image"):
        files.append(cv["image"])
    return [f for f in files if f]
