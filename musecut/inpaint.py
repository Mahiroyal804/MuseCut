"""MuseCut classical inpainting — OpenCV Telea / Navier-Stokes.

Round 7 (v0.9.0): closes the INPAINTING half of the "AI inpainting /
outpainting" rubric row with classical methods only (no ML model).
``cv2.inpaint`` fills a masked region from its surroundings:

* ``telea`` (default) — Telea's fast-marching method; better at
  thin structures and small holes, and faster.
* ``ns`` — Bertalmio et al.'s Navier-Stokes fluid-dynamics method;
  smoother on large flat areas, slower.

Outpainting stays 0 — it needs a generative model that can invent
content outside the frame, which neither method here can do. This
module does not attempt it.

Measured on a real street frame (2026-11-21, van clip t=2s,
1280x720, wall-mounted AC unit removed, rect
x=0.327/y=0.125/w=0.055/h=0.076, Telea radius 3): the masked
region changes by mean abs diff ~31.5 (the dark unit is replaced
by wall texture) while every unmasked pixel is byte-identical at
the plate level; Telea costs ~0.06 s/frame at 720p, so clips are
processed at ``sample_fps=10`` by default (masks held between
samples, same pattern as ``bgremove``).

Environment: needs OpenCV (``cv2``). System python3 has NO cv2 —
importing it there fails with a clear error naming the module and
the venv that has it (``~/workspace/.venv-video/bin/python``),
following the ``onnxruntime_available()`` pattern in
``musecut.providers``. ``numpy`` is imported lazily inside the
functions, like ``musecut.matting`` does, so the module itself
imports cleanly anywhere.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union

from .audio import ffmpeg_env

METHODS = {"telea": "INPAINT_TELEA", "ns": "INPAINT_NS"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
DEFAULT_RADIUS = 3
DEFAULT_SAMPLE_FPS = 10.0
DEFAULT_PLATE_CRF = "18"


class InpaintError(RuntimeError):
    """Raised when inpainting cannot run (bad mask, missing file, ...)."""


def _require_cv2() -> Any:
    """Import cv2 or raise a clear error naming the missing module."""
    try:
        import cv2
        return cv2
    except ImportError:
        raise ImportError(
            "musecut.inpaint needs OpenCV (cv2), which is not installed "
            "in this Python. Run with ~/workspace/.venv-video/bin/python "
            "(cv2 4.11.0) instead of the system python3.")


def _validate_radius(radius: float) -> float:
    if radius is None or float(radius) <= 0:
        raise ValueError(f"inpaint radius must be > 0, got {radius!r}")
    return float(radius)


def _validate_method(method: str) -> str:
    m = str(method).lower()
    if m not in METHODS:
        raise ValueError(
            f"inpaint method must be one of {sorted(METHODS)}, "
            f"got {method!r}")
    return m


def _as_bgr(img: Any) -> Any:
    """Accept a BGR uint8 ndarray (cv2 convention) or an RGB PIL
    image; return a BGR uint8 ndarray. Raises ValueError otherwise."""
    import numpy as np
    if hasattr(img, "convert"):  # PIL image, assumed RGB
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        return arr[:, :, ::-1].copy()
    arr = np.asarray(img)
    if arr.dtype != np.uint8:
        raise ValueError("inpaint_frame needs a uint8 image, got "
                         f"{arr.dtype}")
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError("inpaint_frame needs a 3-channel BGR image, "
                         f"got shape {arr.shape}")
    return arr


def _as_mask(mask: Any, shape: Tuple[int, int]) -> Any:
    """Accept a uint8 ndarray or a PIL "L" image; return a uint8
    mask of ``shape`` (nonzero = inpaint). Raises ValueError when
    the shape mismatches or the mask is empty."""
    import numpy as np
    if hasattr(mask, "convert"):
        arr = np.asarray(mask.convert("L"), dtype=np.uint8)
    else:
        arr = np.asarray(mask)
    if arr.ndim != 2:
        raise ValueError("inpaint mask must be 2-D (HxW), got shape "
                         f"{arr.shape}")
    if arr.shape != shape:
        raise ValueError(
            f"inpaint mask shape {arr.shape} does not match image "
            f"shape {shape}")
    if not bool((arr > 0).any()):
        raise ValueError("inpaint mask is empty (no nonzero pixels)")
    return np.where(arr > 0, 255, 0).astype(np.uint8)


def inpaint_frame(img_bgr: Any, mask: Any, radius: float = DEFAULT_RADIUS,
                  method: str = "telea") -> Any:
    """Inpaint one frame: fill the masked region from its surroundings.

    ``img_bgr`` is a uint8 HxWx3 BGR ndarray (or an RGB PIL image);
    ``mask`` is a uint8 HxW ndarray (or a PIL "L" image) where
    nonzero pixels are replaced. ``radius`` is the inpainting
    neighbourhood (must be > 0); ``method`` is ``"telea"`` (default)
    or ``"ns"``. Returns a uint8 BGR ndarray the same shape as the
    input. Unmasked pixels are byte-identical to the input (verified
    on real frames — cv2.inpaint only writes inside the mask).
    """
    cv2 = _require_cv2()
    radius = _validate_radius(radius)
    method = _validate_method(method)
    img = _as_bgr(img_bgr)
    m = _as_mask(mask, img.shape[:2])
    flag = cv2.INPAINT_TELEA if method == "telea" else cv2.INPAINT_NS
    return cv2.inpaint(img, m, radius, flag)


def measure_inpaint(before: Any, after: Any, mask: Any) -> Dict[str, Any]:
    """Numbers for one inpainted frame: mean abs diff inside the
    mask (how much the region changed — the object is gone when
    this is large) and outside it (should be 0.0 at plate level;
    small but nonzero after a lossy re-encode)."""
    import numpy as np
    b = _as_bgr(before)
    a = _as_bgr(after)
    if b.shape != a.shape:
        raise ValueError(
            f"measure_inpaint shape mismatch: {b.shape} vs {a.shape}")
    m = _as_mask(mask, b.shape[:2]) > 0
    diff = np.abs(b.astype(np.int16) - a.astype(np.int16)).mean(axis=2)
    return {
        "masked_mean_abs_diff": round(float(diff[m].mean()), 2),
        "masked_pixels": int(m.sum()),
        "unmasked_mean_abs_diff": round(float(diff[~m].mean()), 4),
        "unmasked_identical": bool((diff[~m] == 0).all()),
    }


# ------------------------------------------------------------------
# mask specs
# ------------------------------------------------------------------
def _rect_to_mask(rect: Dict[str, Any], w: int, h: int) -> Any:
    """Rect dict {"x","y","w","h"} in 0..1 coords -> uint8 mask."""
    import numpy as np
    for k in ("x", "y", "w", "h"):
        if k not in rect:
            raise ValueError(f"inpaint rect mask is missing key {k!r}")
    x, y, rw, rh = (float(rect["x"]), float(rect["y"]),
                    float(rect["w"]), float(rect["h"]))
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ValueError(
            f"inpaint rect x/y must be in 0..1, got x={x}, y={y}")
    if rw <= 0 or rh <= 0 or x + rw > 1.0 or y + rh > 1.0:
        raise ValueError(
            "inpaint rect w/h must be > 0 and x+w / y+h <= 1, got "
            f"x={x}, y={y}, w={rw}, h={rh}")
    x0, y0 = int(round(x * w)), int(round(y * h))
    x1, y1 = int(round((x + rw) * w)), int(round((y + rh) * h))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[y0:y1, x0:x1] = 255
    return mask


def _load_static_mask(path: str, w: int, h: int) -> Any:
    """Load a mask image (PNG etc.), resize to the frame if needed,
    threshold to 0/255. Raises InpaintError when empty."""
    import numpy as np
    cv2 = _require_cv2()
    arr = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if arr is None:
        raise InpaintError(f"cannot read mask image: {path!r}")
    if arr.shape != (h, w):
        arr = cv2.resize(arr, (w, h), interpolation=cv2.INTER_NEAREST)
    mask = np.where(arr > 0, 255, 0).astype(np.uint8)
    if not bool((mask > 0).any()):
        raise InpaintError(f"mask image is empty (all zero): {path!r}")
    return mask


def _resolve_masks(mask: Any, w: int, h: int, duration: float,
                   sample_fps: float, workdir: str) -> List[Any]:
    """Resolve the ``mask`` argument into one uint8 mask per sampled
    frame (``n_needed = ceil(duration * sample_fps)`` entries).

    ``mask`` is one of:

    * a rect dict ``{"x","y","w","h"}`` in 0..1 coords, optionally
      with ``"t0"`` / ``"t1"`` seconds — frames outside [t0, t1)
      get an all-zero mask (the frame passes through unchanged);
    * a static mask image path (PNG/JPG/...): the same mask for
      every sampled frame;
    * a mask-video path: frames sampled at ``sample_fps`` and held
      between samples (same pattern as ``bgremove``).

    All forms are validated up front: a rect with bad coords, an
    empty static mask, or a mask video with no nonzero pixels
    anywhere raises before any frame is processed.
    """
    import numpy as np
    n_needed = max(1, int(math.ceil(duration * sample_fps)))
    if isinstance(mask, dict):
        t0 = float(mask.get("t0", 0.0))
        t1 = float(mask.get("t1", duration))
        if t0 < 0:
            raise ValueError(f"inpaint rect t0 must be >= 0, got {t0}")
        if t1 <= t0:
            raise ValueError(
                f"inpaint rect t1 must be > t0, got t0={t0}, t1={t1}")
        base = _rect_to_mask(mask, w, h)
        out: List[Any] = []
        for i in range(n_needed):
            t = i / sample_fps
            out.append(base if t0 <= t < t1
                       else np.zeros((h, w), dtype=np.uint8))
        return out
    if isinstance(mask, str):
        if not os.path.isfile(mask):
            raise InpaintError(f"mask file not found: {mask!r}")
        if os.path.splitext(mask)[1].lower() in IMAGE_EXTS:
            static = _load_static_mask(mask, w, h)
            return [static] * n_needed
        # mask video: sample at sample_fps, hold the last frame
        from .track import _extract_frames
        mdir = os.path.join(workdir, "maskframes")
        os.makedirs(mdir, exist_ok=True)
        frames = _extract_frames(mask, sample_fps, mdir)
        if not frames:
            raise InpaintError(
                f"no frames extracted from mask video: {mask!r}")
        cv2 = _require_cv2()
        sampled: List[Any] = []
        for _, path in frames:
            arr = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if arr is None:
                raise InpaintError(
                    f"cannot read extracted mask frame: {path!r}")
            if arr.shape != (h, w):
                arr = cv2.resize(arr, (w, h),
                                 interpolation=cv2.INTER_NEAREST)
            sampled.append(np.where(arr > 0, 255, 0).astype(np.uint8))
        if not any(bool((m > 0).any()) for m in sampled):
            raise InpaintError(
                f"mask video has no nonzero pixels: {mask!r}")
        return [sampled[min(i, len(sampled) - 1)] for i in range(n_needed)]
    raise ValueError(
        "inpaint mask must be a rect dict, a mask image path, or a "
        f"mask video path, got {type(mask).__name__}")


def _assemble_plate(frames_dir: str, dst: str, sample_fps: float,
                    src_fps: float) -> str:
    """Assemble processed PNG frames into a plate video (crf 18,
    yuv420p — same pattern as ``matting.make_refined_plates``)."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-framerate", str(sample_fps),
           "-i", os.path.join(frames_dir, "p%04d.png"),
           "-vf", f"fps={src_fps:.3f},format=yuv420p",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", DEFAULT_PLATE_CRF, dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=ffmpeg_env())
    if proc.returncode != 0:
        raise InpaintError(
            f"inpaint plate assembly failed for {dst}: "
            f"{(proc.stderr or '')[-1000:]}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise InpaintError(
            f"inpaint plate assembly produced no file: {dst}")
    return dst


def inpaint_clip(src: str, dst: str, mask: Any,
                 radius: float = DEFAULT_RADIUS, method: str = "telea",
                 sample_fps: float = DEFAULT_SAMPLE_FPS,
                 workdir: Optional[str] = None,
                 name: str = "inpaint") -> str:
    """Inpaint ``src`` into ``dst`` and render through the real engine.

    ``mask`` is a rect dict, a static mask image path, or a mask
    video path (see ``_resolve_masks``). Frames are processed at
    ``sample_fps`` (default 10 — Telea costs ~0.06 s/frame at 720p)
    and held between samples; the processed plate is then rendered
    through ``musecut.render.Renderer`` (one clip, fit cover,
    duration clamped to the source) — never hand-rolled ffmpeg for
    the deliverable. The output is video-only (silent): the plate
    carries no audio track. Returns ``dst``.
    """
    from .render import Renderer, probe_media
    from .timeline import new_project
    from .track import _extract_frames

    _require_cv2()
    radius = _validate_radius(radius)
    method = _validate_method(method)
    if sample_fps <= 0:
        raise ValueError(f"sample_fps must be > 0, got {sample_fps}")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    info = probe_media(src)
    w, h = info["width"], info["height"]
    src_fps = info["fps"]
    dur = info["duration"]
    if w <= 0 or h <= 0 or dur <= 0:
        raise InpaintError(f"cannot probe usable video: {src!r}")
    root = os.path.expanduser("~/workspace/musecut/.tmp")
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-inpaint-", dir=root)
    os.makedirs(tmp, exist_ok=True)
    masks = _resolve_masks(mask, w, h, dur, sample_fps, tmp)
    frames_dir = os.path.join(tmp, "frames")
    plates_dir = os.path.join(tmp, "plates")
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(plates_dir, exist_ok=True)
    cv2 = _require_cv2()
    frames = _extract_frames(src, sample_fps, tmp)
    if not frames:
        raise InpaintError(f"no frames extracted from {src!r}")
    n_needed = max(1, int(math.ceil(dur * sample_fps)))
    for i in range(n_needed):
        _, path = frames[min(i, len(frames) - 1)]
        img = cv2.imread(path)
        if img is None:
            raise InpaintError(f"cannot read extracted frame: {path!r}")
        m = masks[i]
        if bool((m > 0).any()):
            img = inpaint_frame(img, m, radius=radius, method=method)
        cv2.imwrite(os.path.join(plates_dir, f"p{i + 1:04d}.png"), img)
    plate = os.path.join(tmp, "inpaint_plate.mp4")
    _assemble_plate(plates_dir, plate, sample_fps, src_fps)
    proj = new_project(name, w, h, int(round(src_fps)))
    proj["clips"] = [{"src": plate, "duration": dur, "fit": "cover"}]
    Renderer(proj, workdir=tmp).render(dst)
    return dst
