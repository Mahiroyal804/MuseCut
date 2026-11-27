"""MuseCut video effects.

Every effect here maps to a real ffmpeg filter chain — nothing is
faked. Named filter presets are the same "one-tap filter" idea CapCut
ships, implemented with eq/colorbalance/hue/unsharp/vignette/noise.

Available building blocks:

* :func:`color_filter` — brightness/contrast/saturation/HSL + presets,
  plus the CapCut adjust sliders: exposure, highlights, shadows,
  clarity, dehaze, fade (ffmpeg-backed approximations, see the
  function docstring — none of them is faked, each maps to a real
  filter).
* :func:`motion_blur_filter` — tmix motion blur on moving clips.
* :func:`blur_canvas_filter` — blurred-background canvas (blur a
  scaled-up copy of the clip and put the clip on top of it).
* :func:`grade_filter` — cinematic grade + vignette + grain (the "pro"
  treatment: teal-shadow/warm-highlight colour balance, vignette,
  film grain).
* :func:`chroma_key_filter` — green-screen removal (chromakey).
* :func:`mask_filter` — shape masks (circle/rect/linear/diagonal) via
  geq + maskedmerge; the masked area gets blurred, darkened, or
  desaturated (B&W).
* :func:`deflicker_filter` — remove temporal luminance flicker
  (timelapse / auto-exposure flutter) with ffmpeg ``deflicker``.
* :func:`glitch_filter` — RGB-shift + slice displacement + noise,
  with an optional strobe (every Nth frame black).
* :func:`zoompan_filter` — Ken Burns zoom/pan keyframes.
* :func:`lut3d_filter` — apply a .cube 3D LUT (pro colour).
* :func:`rotate_keyframe_filter` / :func:`opacity_keyframe_filter`
  — per-frame keyframing of rotation and opacity (Premiere-style
  transform keyframes; scale/position run as a label stage in the
  renderer, see ``render._video_leg``).
* :func:`stabilize_detect_cmd` / :func:`stabilize_filter` — vidstab
  two-pass stabilization.
* :func:`blend_filter` — full-frame blend modes for overlays.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence

from . import keyframes as kf_mod

# ---------------------------------------------------------------- presets
FILTER_PRESETS: Dict[str, str] = {
    # name -> ffmpeg filter chain (applied at canvas size)
    "cinematic": (
        "colorbalance=rs=-0.08:gs=-0.03:bs=0.10:rm=0.02:gm=0.00:bm=-0.06:"
        "rh=0.06:gh=0.02:bh=-0.08,"
        "eq=saturation=1.08:contrast=1.05"
    ),
    "vivid": "eq=saturation=1.35:contrast=1.08:brightness=0.02",
    "bw": "hue=s=0,eq=contrast=1.05",
    "vintage": (
        "curves=vintage,"
        "eq=saturation=0.85:contrast=0.95:brightness=0.03,"
        "noise=alls=6:allf=t"
    ),
    "warm": "colorbalance=rs=0.10:gs=0.03:bs=-0.08,eq=saturation=1.05",
    "cool": "colorbalance=rs=-0.08:gs=0.00:bs=0.10,eq=saturation=1.02",
    "dramatic": "eq=contrast=1.18:saturation=1.10:brightness=-0.02,vignette=PI/4",
    "soft": "eq=saturation=0.92:contrast=0.94,gblur=sigma=0.6",
    "sharpen": "unsharp=5:5:0.8:5:5:0.0",
}

BLEND_MODES = {
    "screen", "multiply", "overlay", "darken", "lighten", "difference",
    "addition", "average", "hardlight", "softlight",
}


def color_filter(effects: Dict[str, Any]) -> str:
    """Build the colour part of a clip's filter chain.

    Accepts a preset name plus numeric overrides: brightness (-1..1),
    contrast (0..2), saturation (0..3), hue (-180..180), sharpen (0..2),
    blur (sigma, 0..5), and the CapCut adjust sliders:

    * exposure (-3..3 stops) — ffmpeg ``exposure`` filter.
    * highlights / shadows (-1..1) — a ``curves`` lift of the top /
      bottom quarter of the luma range. Approximation: CapCut's
      sliders are per-zone tone maps; this is the same idea with a
      two-point curve.
    * clarity (-1..1) — local contrast via ``unsharp`` (positive =
      crisper, negative = softer).
    * dehaze (0..1) — contrast + saturation lift with a slight
      S-curve; an approximation of a haze-removal operator.
    * fade (0..1) — lifted blacks + reduced contrast/saturation
      (the faded-film look).
    """
    parts: List[str] = []
    preset = effects.get("filter", "none")
    if preset and preset != "none":
        chain = FILTER_PRESETS.get(preset)
        if chain is None:
            raise ValueError(f"unknown filter preset: {preset!r}")
        parts.append(chain)

    eq_bits = []
    if "brightness" in effects:
        eq_bits.append(f"brightness={float(effects['brightness']):.3f}")
    if "contrast" in effects:
        eq_bits.append(f"contrast={float(effects['contrast']):.3f}")
    if "saturation" in effects:
        eq_bits.append(f"saturation={float(effects['saturation']):.3f}")
    if eq_bits:
        parts.append("eq=" + ":".join(eq_bits))
    if "hue" in effects and float(effects["hue"]) != 0:
        parts.append(f"hue=h={float(effects['hue']):.1f}")
    if "exposure" in effects and float(effects["exposure"]) != 0:
        parts.append(f"exposure={float(effects['exposure']):.2f}")
    hl = float(effects.get("highlights", 0) or 0)
    sh = float(effects.get("shadows", 0) or 0)
    if hl or sh:
        p25 = min(max(0.25 + sh * 0.10, 0.0), 1.0)
        p75 = min(max(0.75 + hl * 0.10, 0.0), 1.0)
        parts.append(
            f"curves=master='0/0 0.25/{p25:.3f} 0.75/{p75:.3f} 1/1'")
    if "clarity" in effects and float(effects["clarity"]) != 0:
        parts.append(f"unsharp=5:5:{float(effects['clarity']):.2f}:5:5:0.0")
    if effects.get("dehaze"):
        d = float(effects["dehaze"])
        parts.append(
            f"eq=contrast={1 + 0.25 * d:.3f}:saturation={1 + 0.15 * d:.3f}:"
            f"brightness={0.01 * d:.3f}")
    if effects.get("fade"):
        f = float(effects["fade"])
        parts.append(
            f"curves=master='0/{0.08 * f:.3f} 1/{1 - 0.05 * f:.3f}',"
            f"eq=saturation={1 - 0.2 * f:.3f}")
    if effects.get("sharpen"):
        s = float(effects["sharpen"])
        parts.append(f"unsharp=5:5:{s:.2f}:5:5:0.0")
    if effects.get("blur"):
        parts.append(f"gblur=sigma={float(effects['blur']):.2f}")
    return ",".join(parts)


def grade_filter(vignette: float = 0.0, grain: float = 0.0,
                 cinematic: bool = False) -> str:
    """Cinematic grade + vignette + film grain."""
    parts: List[str] = []
    if cinematic:
        parts.append(FILTER_PRESETS["cinematic"])
    if vignette:
        # vignette angle PI/5..PI/3; strength maps 0..1 -> a=PI/5..PI/2.2
        angle = 3.14159 / (5.0 - 2.6 * min(max(vignette, 0.0), 1.0))
        parts.append(f"vignette=a={angle:.3f}")
    if grain:
        parts.append(f"noise=alls={int(4 + 8 * min(max(grain, 0.0), 1.0))}:allf=t")
    return ",".join(parts)


def chroma_key_filter(color: str = "0x00FF00", similarity: float = 0.3,
                      blend: float = 0.1) -> str:
    """Green-screen removal. Returns the chromakey filter string."""
    return f"chromakey={color}:{similarity:.2f}:{blend:.2f}"


def mask_filter(shape: str = "circle", x: float = 0.5, y: float = 0.5,
                size: float = 0.4, feather: float = 0.05,
                angle: float = 45.0) -> str:
    """Build a geq expression that renders a white shape mask on black.

    The mask is a single-channel (gray) video the same size as the
    canvas; ``maskedmerge`` uses it to pick between the original clip
    and an effected copy (blur/darken/desaturate): the shape
    interior (white) stays original, the exterior (black) takes the
    effect. NOTE on ``maskedmerge`` input order (verified with a
    white/black probe): mask white takes the SECOND input, mask
    black the FIRST — so the renderer passes the effected copy
    first and the original second.

    ``diagonal`` draws a straight seam through the point (x, y) at
    ``angle`` degrees from horizontal. With dx=(X/W-x)*(W/H) and
    dy=(Y/H-y), the signed distance is d = dx*sin(a) - dy*cos(a);
    white where d < -feather, feathered to black at d > +feather
    (the same smoothstep shape as the ``linear`` mask). At angle=90
    the seam is vertical, i.e. white left of x like ``linear``.
    """
    feather = max(feather, 0.005)
    if shape == "circle":
        # Normalised distance from centre, aspect-corrected so the
        # shape is a circle on screen; smoothstep over `feather`.
        dx = f"(X/W-{x:.3f})*(W/H)"
        dy = f"(Y/H-{y:.3f})"
        r = f"hypot({dx},{dy})"
        expr = (
            f"if(lt({r},{size:.3f}),255,"
            f"if(gt({r},{size + feather:.3f}),0,"
            f"255*(1-({r}-{size:.3f})/{feather:.3f})))"
        )
    elif shape == "rect":
        hw, hh = size / 2, size / 2
        expr = (
            f"if(between(X/W,{x - hw:.3f},{x + hw:.3f})*"
            f"between(Y/H,{y - hh:.3f},{y + hh:.3f}),255,0)"
        )
    elif shape == "linear":
        # Horizontal wipe: white left of x, feathered edge.
        expr = (
            f"if(lt(X/W,{x - feather:.3f}),255,"
            f"if(gt(X/W,{x + feather:.3f}),0,"
            f"255*(1-(X/W-({x - feather:.3f}))/(2*{feather:.3f}))))"
        )
    elif shape == "diagonal":
        # Straight seam through (x, y) at `angle` degrees from
        # horizontal; sin/cos are baked in as literals (angle is a
        # Python-side constant, so the expression stays deterministic).
        a = math.radians(angle)
        sa, ca = math.sin(a), math.cos(a)
        dx = f"(X/W-{x:.3f})*(W/H)"
        dy = f"(Y/H-{y:.3f})"
        d = f"({dx}*{sa:.6f}-({dy})*{ca:.6f})"
        expr = (
            f"if(lt({d},-{feather:.3f}),255,"
            f"if(gt({d},{feather:.3f}),0,"
            f"255*(1-(({d})+{feather:.3f})/(2*{feather:.3f}))))"
        )
    else:
        raise ValueError(f"unknown mask shape: {shape!r}")
    # The SAME expression on all three planes: maskedmerge merges
    # per-plane, using each plane of the mask for the matching plane
    # of the inputs. Constant cb/cr=128 would blend the chroma 50/50
    # everywhere — a desaturate (chroma) effect would only ever get
    # halfway. The expression uses normalised X/W, Y/H, so it scales
    # to the half-resolution chroma planes correctly.
    return (f"geq=lum_expr='{expr}':cb_expr='{expr}':cr_expr='{expr}'")


def zoompan_filter(width: int, height: int, duration: float, fps: int,
                   zoom_from: float = 1.0, zoom_to: float = 1.25,
                   pan_from: tuple = (0.5, 0.5),
                   pan_to: tuple = (0.5, 0.5)) -> str:
    """Ken Burns zoom/pan over the clip (zoompan filter).

    ``on`` is the output frame counter; the zoom interpolates linearly
    from ``zoom_from`` to ``zoom_to`` across the clip.
    """
    total = max(int(duration * fps), 1)
    # zoompan evaluates z per output frame; 'on' counts output frames.
    z_expr = f"{zoom_from:.3f}+({zoom_to - zoom_from:.3f})*on/{total}"
    # Keep the crop window centred on the interpolated pan point.
    px = f"{pan_from[0]:.3f}+({pan_to[0] - pan_from[0]:.3f})*on/{total}"
    py = f"{pan_from[1]:.3f}+({pan_to[1] - pan_from[1]:.3f})*on/{total}"
    return (
        f"zoompan=z='{z_expr}':x='iw*{px}-(iw/zoom/2)':"
        f"y='ih*{py}-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
    )


def stabilize_detect_cmd(src: str, trf: str, start: float = 0.0,
                         duration: Optional[float] = None) -> List[str]:
    """ffmpeg command for vidstab pass 1 (writes the .trf file)."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vf", f"vidstabdetect=shakiness=5:accuracy=15:result={trf}",
            "-f", "null", "-"]
    return cmd


def stabilize_filter(trf: str) -> str:
    """vidstab pass 2 filter string."""
    return f"vidstabtransform=input={trf}:smoothing=10:optzoom=1"


def blend_filter(mode: str) -> str:
    """Full-frame blend mode filter."""
    if mode not in BLEND_MODES:
        raise ValueError(f"unknown blend mode: {mode!r}")
    return f"blend=all_mode={mode}"


def motion_blur_filter(frames: int = 8) -> str:
    """Motion blur: mix successive frames with ``tmix``.

    ``frames`` is the number of frames averaged (2..16). More frames =
    longer blur trails; it also delays the picture by ``frames - 1``
    frames, which is inherent to the technique.
    """
    n = int(frames)
    if not 2 <= n <= 16:
        raise ValueError(f"motion blur frames must be 2..16, got {n}")
    return f"tmix=frames={n}"


def deflicker_filter(size: int = 5, mode: str = "am") -> str:
    """Remove temporal luminance flicker (ffmpeg ``deflicker``).

    Smooths the frame-to-frame brightness variations you get from
    timelapse sequences, auto-exposure flutter and cheap LED lighting.
    ``size`` (2..129) is the sliding window in frames; ``mode`` is
    the averaging mode — "am" (arithmetic mean, default), "gm",
    "hm", "qm", "cm", "pm" or "median". Larger windows kill slower
    flicker but soften genuine fast brightness changes (a strobe
    cut, a flash) — size 5 is the usual starting point.
    """
    modes = {"am", "gm", "hm", "qm", "cm", "pm", "median"}
    n = int(size)
    if not 2 <= n <= 129:
        raise ValueError(f"deflicker size must be 2..129, got {size}")
    if mode not in modes:
        raise ValueError(f"deflicker mode unknown: {mode!r} "
                         f"(want one of {sorted(modes)})")
    return f"deflicker=size={n}:mode={mode}"


def flip_filter(mode: str = "horizontal") -> str:
    """Mirror the frame (round 52, v0.20.0).

    ``"horizontal"`` flips left-right (``hflip``) — the mirror a
    camera on the other side of the axis of action would see;
    ``"vertical"`` flips top-bottom (``vflip``); ``"both"`` flips
    both axes. A plain geometric mirror: no colour or timing
    change, so a flipped clip is frame-identical to its source
    under the mirror map.
    """
    modes = {"horizontal", "vertical", "both"}
    if mode not in modes:
        raise ValueError(f"flip mode unknown: {mode!r} "
                         f"(want one of {sorted(modes)})")
    if mode == "horizontal":
        return "hflip"
    if mode == "vertical":
        return "vflip"
    return "hflip,vflip"


def glitch_filter(strength: float = 0.5, strobe: int = 0) -> str:
    """Glitch / strobe effect (CapCut-style RGB-split + slice shift).

    Chain (all deterministic — no ``random`` anywhere, so renders
    are reproducible and tests can compare frames):

    * ``rgbashift`` — red shifted right, blue left by
      R = max(1, int(12*strength)) px.
    * ONE ``geq`` — slice displacement: rows where
      ``gt(mod(Y,47),23)`` are shifted horizontally by
      ``amp*sin(N*0.7)`` with amp = int(20*strength) px. If
      ``strobe`` > 0, every Nth frame goes black via a term folded
      into the luma expression: ``if(eq(mod(N,strobe),0),0,lum(...))``.
    * ``noise`` — temporal grain, ``alls=int(4+16*strength)``.

    ``strength`` 0..1, ``strobe`` 0 (off) or 1..60.
    """
    strength = min(max(float(strength), 0.0), 1.0)
    strobe = int(strobe)
    if strobe < 0:
        raise ValueError(f"glitch strobe must be >= 0, got {strobe}")
    r = max(1, int(12 * strength))
    amp = int(20 * strength)
    shift = f"{amp}*sin(N*0.7)*gt(mod(Y,47),23)"
    lum_src = f"lum(X+({shift}),Y)"
    if strobe > 0:
        lum_src = f"if(eq(mod(N,{strobe}),0),0,{lum_src})"
    geq = (
        f"geq=lum_expr='{lum_src}':"
        f"cb_expr='cb(X+({shift}),Y)':cr_expr='cr(X+({shift}),Y)'"
    )
    noise = f"noise=alls={int(4 + 16 * strength)}:allf=t"
    return f"rgbashift=rh={r}:bh=-{r},{geq},{noise}"


def blur_canvas_filter(width: int, height: int, sigma: float = 25.0) -> str:
    """Blur-background canvas: scale the frame up to fill the canvas,
    crop to canvas size, blur heavily and darken slightly.

    Apply to a *copy* of the clip (via ``split``) and overlay the
    original clip on top, centred.
    """
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma={float(sigma):.1f},"
        "eq=brightness=-0.08"
    )


def lut3d_filter(path: str) -> str:
    """Apply a .cube 3D LUT (Premiere Lumetri-style colour).

    ``path`` must exist and end in ``.cube``; the caller (render)
    checks that before building the graph.
    """
    if not path.lower().endswith(".cube"):
        raise ValueError(f"LUT must be a .cube file, got {path!r}")
    esc = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return f"lut3d=file='{esc}'"


def rotate_keyframe_filter(kfs: Sequence[Dict[str, Any]]) -> str:
    """Per-frame rotation keyframes (values in degrees, about the
    frame centre; uncovered corners are filled black)."""
    expr = kf_mod.scalar_expr(kfs, 0.0)
    return f"rotate=a='({kf_mod.ffmpeg_escape(expr)})*PI/180':fillcolor=black"


def opacity_keyframe_filter(kfs: Sequence[Dict[str, Any]]) -> str:
    """Per-frame opacity keyframes (values 0..1).

    Returns a filter chain that must run on a ``yuva420p`` stream:
    ``geq`` rewrites the alpha plane from the keyframed expression
    (``colorchannelmixer`` cannot take time expressions in this
    ffmpeg build — verified 2026-11-20). The caller composites the
    result over a background afterwards. NOTE: ``geq`` only knows
    capital ``T`` for time, so the expression is built with var="T".

    The chroma expressions are explicit (round 8): ``geq`` fills
    UNSPECIFIED planes with the luma value — a clip with opacity
    keyframes came out pink (Cb/Cr = 235 on a white frame) until
    ``cb_expr``/``cr_expr`` pinned the chroma to identity.
    """
    expr = kf_mod.scalar_expr(kfs, 1.0, var="T")
    return (
        "format=yuva420p,"
        "geq=lum_expr='lum(X,Y)':cb_expr='cb(X,Y)':"
        f"cr_expr='cr(X,Y)':alpha_expr='255*({kf_mod.ffmpeg_escape(expr)})'"
    )
