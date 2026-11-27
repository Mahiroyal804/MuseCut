"""MuseCut AI matting — classical edge refinement for the RMBG matte.

Round 6 (v0.8.0): the RMBG-1.4 matte (round 4) is the matting path,
but it had no edge-refinement controls — that is what this module
adds, with classical methods only (no ML model):

* **trimap** — definite foreground (alpha > 0.95, eroded) /
  definite background (alpha < 0.05, dilated) / unknown band
  between them, built from the matte itself.
* **guided filter** (He et al. 2013, luma guide) — pulls the alpha
  edge onto the image's real luminance edge. Implemented with
  ``scipy.ndimage.uniform_filter`` box filters (numpy/scipy only —
  no OpenCV on this VM's system python).
* **feather** — Gaussian blur of the alpha, applied ONLY inside
  the unknown band (definite fg/bg are untouched).
* **decontamination** — for unknown-band pixels, estimate the local
  foreground and background colours (box means over the definite
  regions) and un-premultiply: F = (I - (1-a) B) / a. This removes
  the background-colour spill (halo) on semi-transparent edge
  pixels.

Measured on a real street frame (2026-11-21, van clip t=0.5s,
RMBG matte vs refined, defaults guided_radius=8 / guided_eps=0.001
/ trimap_radius=3 / feather 0 / decontaminate on): edge alignment
corr(|grad alpha|, |grad luma|) 0.378 -> 0.494 (+31%), fringe
pixels after compositing on magenta 7,096 -> 5,518 (-22%).
Tradeoff, stated plainly: the semi-transparent transition widens
slightly (100,145 -> 111,954 pixels, +12%) because the guided
filter follows the image's soft luminance ramps — the edge is
better ALIGNED and has less halo, not narrower. The optional
``hardness`` control (sigmoid steepening in the unknown band)
trades the other way: at hardness=8, semi 100,145 -> 75,894
(-24%), alignment 0.459 (+21%), fringe 6,578 (-7%).
The width metric (semi / boundary) is NOT used — refinement
smooths the ragged raw edge so the perimeter shrinks, which
would make a width ratio meaningless.

Honest scope: this refines the matte's edge; it cannot invent hair
detail the model never saw, and masks still update at ``sample_fps``
(stepped edges on fast subjects — same caveat as bgremove).
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from typing import Any, Dict, Optional, Tuple

from .audio import ffmpeg_env

DEFAULT_GUIDED_RADIUS = 8
DEFAULT_GUIDED_EPS = 0.001
DEFAULT_TRIMAP_RADIUS = 3
DEFAULT_FEATHER_PX = 0


def _as_float01(arr: Any) -> Any:
    import numpy as np
    a = np.asarray(arr, dtype=np.float32)
    if a.max() > 1.5:
        a = a / 255.0
    return np.clip(a, 0.0, 1.0)


def trimap_from_alpha(alpha: Any, radius: int = DEFAULT_TRIMAP_RADIUS,
                      fg_thr: float = 0.95, bg_thr: float = 0.05) -> Any:
    """Alpha (float [0,1] or uint8) -> trimap uint8 (0 bg / 128 ? / 255 fg).

    Definite fg = alpha > fg_thr eroded by ``radius``; definite bg =
    alpha < bg_thr eroded by ``radius``; everything else is unknown.
    Both sides are ERODED (not dilated) so the unknown band always
    straddles the edge — dilating the background would swallow the
    semi-transparent ring and misclassify it as definite bg.
    """
    import numpy as np
    from scipy import ndimage as ndi
    if radius < 0:
        raise ValueError("trimap radius must be >= 0")
    a = _as_float01(alpha)
    fg = a > fg_thr
    bg = a < bg_thr
    if radius > 0:
        fg = ndi.binary_erosion(fg, iterations=radius)
        bg = ndi.binary_erosion(bg, iterations=radius)
    out = np.full(a.shape, 128, dtype=np.uint8)
    out[bg] = 0
    out[fg] = 255
    return out


def _box(a: Any, r: int) -> Any:
    from scipy import ndimage as ndi
    return ndi.uniform_filter(a, size=2 * r + 1, mode="nearest")


def guided_filter(guide: Any, p: Any, radius: int, eps: float) -> Any:
    """Guided filter (He et al.), single-channel guide, float [0,1].

    ``q = a * guide + b`` with a, b from local linear regression in
    windows of ``radius``. ``eps`` is the regularisation: smaller
    follows the guide edge more aggressively (0.001 measured best
    on real footage); larger smooths more.
    """
    import numpy as np
    if radius < 1:
        raise ValueError("guided radius must be >= 1")
    if eps <= 0:
        raise ValueError("guided eps must be > 0")
    I = _as_float01(guide).astype(np.float32)
    p = _as_float01(p).astype(np.float32)
    mean_I = _box(I, radius)
    mean_p = _box(p, radius)
    corr_I = _box(I * I, radius)
    corr_Ip = _box(I * p, radius)
    var_I = corr_I - mean_I * mean_I
    cov_Ip = corr_Ip - mean_I * mean_p
    a = cov_Ip / (var_I + eps)
    b = mean_p - a * mean_I
    return np.clip(_box(a, radius) * I + _box(b, radius), 0.0, 1.0)


def refine_alpha(image: Any, alpha: Any, feather_px: int = DEFAULT_FEATHER_PX,
                 guided_radius: int = DEFAULT_GUIDED_RADIUS,
                 guided_eps: float = DEFAULT_GUIDED_EPS,
                 trimap_radius: int = DEFAULT_TRIMAP_RADIUS,
                 decontaminate: bool = True,
                 hardness: float = 0.0) -> Tuple[Any, Any]:
    """Refine one matte against its source frame.

    ``image`` / ``alpha`` are PIL images (RGB / L) or float arrays.
    Returns ``(refined_alpha, clean_rgb)`` as PIL images (L / RGB);
    ``clean_rgb`` is None when ``decontaminate`` is False. The clean
    RGB equals the source except in the unknown band, where edge
    pixels are un-premultiplied against the local background colour.

    Steps, in order: guided filter (align alpha with the real
    luminance edge) -> ``hardness`` (optional sigmoid steepening in
    the unknown band only; 0 = off, 8 measured to cut semi pixels
    -24% at the cost of halo gain) -> feather (Gaussian blur, only
    in the unknown band) -> hard constraints (definite fg/bg are
    exactly 1/0) -> decontamination.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi
    if feather_px < 0:
        raise ValueError("feather_px must be >= 0")
    if hardness < 0:
        raise ValueError("hardness must be >= 0")
    img = image.convert("RGB") if hasattr(image, "convert") else image
    alp = alpha.convert("L") if hasattr(alpha, "convert") else alpha
    rgb = np.asarray(img, dtype=np.float32) / 255.0
    a = np.asarray(alp, dtype=np.float32) / 255.0
    luma = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
            + 0.114 * rgb[:, :, 2]).astype(np.float32)
    tm = trimap_from_alpha(a, trimap_radius)
    unknown = tm == 128
    fg_known = tm == 255
    bg_known = tm == 0
    # 1. guided filter — align alpha with the real luminance edge.
    a = guided_filter(luma, a, guided_radius, guided_eps)
    # 2. hardness — optional sigmoid steepening, unknown band only.
    if hardness > 0:
        steep = 1.0 / (1.0 + np.exp(-hardness * (a - 0.5)))
        a = np.where(unknown, steep, a)
    # 3. feather — blur only inside the unknown band.
    if feather_px > 0:
        blurred = ndi.gaussian_filter(a, sigma=feather_px / 2.0)
        a = np.where(unknown, blurred, a)
    # 4. hard constraints — definite fg/bg are exactly 1/0.
    a = np.where(fg_known, 1.0, np.where(bg_known, 0.0, a))
    a = np.clip(a, 0.0, 1.0)
    clean = None
    if decontaminate:
        r = 12
        fg_m = fg_known.astype(np.float32)
        bg_m = bg_known.astype(np.float32)
        fg_sum = np.stack([_box(rgb[:, :, c] * fg_m, r)
                           for c in range(3)], axis=-1)
        fg_cnt = np.maximum(_box(fg_m, r), 1e-6)
        bg_sum = np.stack([_box(rgb[:, :, c] * bg_m, r)
                           for c in range(3)], axis=-1)
        bg_cnt = np.maximum(_box(bg_m, r), 1e-6)
        f_est = fg_sum / fg_cnt[..., None]
        b_est = bg_sum / bg_cnt[..., None]
        aa = np.clip(a, 0.05, 1.0)[..., None]
        f_un = np.clip((rgb - (1.0 - aa) * b_est) / aa, 0.0, 1.0)
        clean_rgb = np.where(unknown[..., None], f_un, rgb)
        clean = Image.fromarray((clean_rgb * 255).astype(np.uint8),
                                mode="RGB")
    return (Image.fromarray((a * 255).astype(np.uint8), mode="L"), clean)


def measure_matte(image: Any, alpha: Any) -> Dict[str, float]:
    """Edge-quality metrics for one matte (same definitions as the
    round-6 experiment): semi-transparent pixel count, edge
    alignment corr(|grad alpha|, |grad luma|) in the unknown band,
    and fringe pixels after compositing on magenta. Lower semi /
    fringe and higher alignment are better."""
    import numpy as np
    from scipy import ndimage as ndi
    img = image.convert("RGB") if hasattr(image, "convert") else image
    alp = alpha.convert("L") if hasattr(alpha, "convert") else alpha
    rgb = np.asarray(img, dtype=np.float32) / 255.0
    a = np.asarray(alp, dtype=np.float32) / 255.0
    luma = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
            + 0.114 * rgb[:, :, 2]).astype(np.float32)
    tm = trimap_from_alpha(a, DEFAULT_TRIMAP_RADIUS)
    unknown = tm == 128
    fg = a > 0.95
    semi = int(((a > 0.05) & (a < 0.95)).sum())
    ga = np.hypot(*np.gradient(a))
    gl = np.hypot(*np.gradient(luma))
    x, y = ga[unknown], gl[unknown]
    corr = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 else 0.0
    comp = a[..., None] * rgb + (1 - a[..., None]) * np.array(
        [1.0, 0.0, 1.0], dtype=np.float32)
    ring = ndi.binary_dilation(fg, iterations=2) & ~fg
    magenta = ((comp[:, :, 0] > 0.8) & (comp[:, :, 1] < 0.3)
               & (comp[:, :, 2] > 0.8))
    fringe = int((ring & ~magenta).sum())
    return {"semi": semi, "align_corr": round(corr, 3),
            "fringe": fringe}


def make_refined_plates(src: str, mask_dst: str, rgb_dst: str,
                        sample_fps: float = 5.0,
                        workdir: Optional[str] = None,
                        **refine_kwargs: Any) -> Tuple[str, str]:
    """Segment ``src`` with RMBG and refine every sampled matte.

    Builds TWO videos, frame-aligned: ``mask_dst`` (grayscale refined
    alpha, crf 0) and ``rgb_dst`` (decontaminated RGB plate, crf 18).
    Render the plate through the engine with ``background_remove``
    using the refined mask and the edge pixels come out
    colour-corrected. Same sampling cost as ``bgremove`` (masks held
    between samples). Returns ``(mask_dst, rgb_dst)``.
    """
    from . import bgremove as bg_mod
    from .render import probe_media
    from .track import _extract_frames
    if not os.path.isfile(src):
        raise bg_mod.BgRemoveError(f"video not found: {src!r}")
    session = bg_mod._load_session()
    info = probe_media(src)
    w, h = info["width"], info["height"]
    src_fps = info["fps"]
    dur = info["duration"]
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-matting-")
    os.makedirs(tmp, exist_ok=True)
    masks_dir = os.path.join(tmp, "masks")
    rgb_dir = os.path.join(tmp, "rgb")
    os.makedirs(masks_dir, exist_ok=True)
    os.makedirs(rgb_dir, exist_ok=True)
    from PIL import Image
    frames = _extract_frames(src, sample_fps, tmp)
    if not frames:
        raise bg_mod.BgRemoveError(f"no frames extracted from {src!r}")
    n_needed = max(1, int(math.ceil(dur * sample_fps)))
    for i in range(n_needed):
        t, path = frames[min(i, len(frames) - 1)]
        img = Image.open(path).convert("RGB")
        mask = bg_mod.segment_frame(session, img)
        refined, clean = refine_alpha(img, mask, **refine_kwargs)
        refined.save(os.path.join(masks_dir, f"m{i + 1:04d}.png"))
        (clean if clean is not None else img).save(
            os.path.join(rgb_dir, f"r{i + 1:04d}.png"))
    for dst, d, pix_fmt, crf in (
            (mask_dst, masks_dir, "gray", "0"),
            (rgb_dst, rgb_dir, "yuv420p", "18")):
        pat = "m%04d.png" if dst == mask_dst else "r%04d.png"
        cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
               "-framerate", str(sample_fps),
               "-i", os.path.join(d, pat),
               "-vf", f"fps={src_fps:.3f},format={pix_fmt}",
               "-c:v", "libx264", "-pix_fmt", pix_fmt, "-crf", crf, dst]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=ffmpeg_env())
        if proc.returncode != 0:
            raise bg_mod.BgRemoveError(
                f"refined plate assembly failed for {dst}: "
                f"{(proc.stderr or '')[-1000:]}")
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            raise bg_mod.BgRemoveError(
                f"refined plate assembly produced no file: {dst}")
    return mask_dst, rgb_dst
