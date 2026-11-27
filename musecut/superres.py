"""MuseCut super resolution — Real-ESRGAN x4plus ONNX.

Round 11 (v0.13.0): closes the SUPER RESOLUTION Export row
(0 -> 2). The model is the genuine Real-ESRGAN x4plus
(RRDBNet, 16.7M params), from the Qualcomm AI Hub public asset
mirror — the first verified public Real-ESRGAN ``.onnx`` found
after the round-6 hunt came up empty (Upscayl is NCNN-only,
upstream ships ``.pth``, the xinntao HuggingFace repo 401s,
sberbank-ai's weights are ``.pth`` on Google Drive):

* float: ``.../real_esrgan_x4plus/releases/v0.67.0/real_esrgan_x4plus-onnx-float.zip``
  (59.2 MB zip; ``real_esrgan_x4plus.onnx`` 3,060,406 B sha256
  ``31188dbf…bfd5d`` + external weights ``real_esrgan_x4plus.data``
  66,737,664 B sha256 ``28fada12…e3287`` — 16.7M float32 params,
  the real x4plus size)
* w8a8 (quantized): ``.../real_esrgan_x4plus-onnx-w8a8.zip``
  (25.0 MB zip; ``.onnx`` 1,064,253 B, ``.data`` 66,737,664 B
  sha256 ``de022841…debea`` — a different file from the float
  weights, verified by sha)

Verified on a real 720p frame (2026-11-21, mixkit tyre clip
t=2s, 512x512 centre crop downscaled to 128x128, then 4x up):
input ``image`` is fixed 1x3x128x128 (float 0..1 RGB, or uint8
RGB for w8a8), output ``upscaled_image`` 1x3x512x512. Float:
PSNR 29.01 dB vs lanczos 30.70 dB — the GAN trades PSNR for
perceptual detail (the Real-ESRGAN paper's own trade-off) — but
gradient energy 5.751 vs ground truth 5.763 (lanczos 4.127),
and the frame check reads as genuinely sharp, close to the
ground truth, with no checkerboard or hallucinated texture.
w8a8: PSNR 28.45 dB, gradient energy 7.976 (+38% vs GT — visibly
crunchier, blotchy dark areas), mean abs diff vs the float
output 2.82. So the float model is the default; w8a8 is the
``fast=True`` option (documented quality cost).

Cost (measured on this VM, 1.5 shared CPUs): float ~4.3 s per
128x128 tile at 2 threads (1 thread 5.7 s, 4 threads 6.4 s —
contention); w8a8 ~1.6 s/tile at 2 threads. A 720p frame is
60 tiles (77 with the 16 px overlap) — ~4.5 min float,
~2 min w8a8 per frame. Stills and short clips are practical;
a full-length 720p clip is hours — that cost is stated here,
not hidden. Frames are tiled with a 16 px overlap and a
64 px feather ramp in output space, so tile seams do not show.
Clips are processed at ``sample_fps`` (default 10, held between
samples — the same pattern as ``inpaint``/``bgremove``) and
rendered through the real engine; output is video-only (the
plate carries no audio).

Environment: needs onnxruntime (1.30.0 in the video venv — the
exact version the Qualcomm export targets) + OpenCV (cv2).
System python3 has neither — importing them fails with a clear
error naming the venv, following the ``onnxruntime_available()``
pattern in ``musecut.providers``.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .audio import ffmpeg_env

TILE = 128
SCALE = 4
OVERLAP = 16
DEFAULT_SAMPLE_FPS = 10.0
DEFAULT_PLATE_CRF = "18"

FLOAT_ONNX = "RealESRGAN_x4plus.onnx"
FLOAT_DATA = "real_esrgan_x4plus.data"
W8A8_DIR = "realesrgan_w8a8"
W8A8_ONNX = "real_esrgan_x4plus.onnx"


class SuperResError(RuntimeError):
    """Raised when super resolution cannot run (missing model, ...)."""


def _models_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "models")


def _model_paths(fast: bool) -> Tuple[str, str]:
    """(onnx path, weights path) for the float or w8a8 variant."""
    base = _models_dir()
    if fast:
        d = os.path.join(base, W8A8_DIR)
        return (os.path.join(d, W8A8_ONNX),
                os.path.join(d, FLOAT_DATA))
    return (os.path.join(base, FLOAT_ONNX),
            os.path.join(base, FLOAT_DATA))


def _require_ort() -> Any:
    try:
        import onnxruntime as ort
        return ort
    except ImportError:
        raise ImportError(
            "musecut.superres needs onnxruntime, which is not "
            "installed in this Python. Run with "
            "~/workspace/.venv-video/bin/python (onnxruntime 1.30.0) "
            "instead of the system python3.")


def _require_cv2() -> Any:
    try:
        import cv2
        return cv2
    except ImportError:
        raise ImportError(
            "musecut.superres needs OpenCV (cv2), which is not "
            "installed in this Python. Run with "
            "~/workspace/.venv-video/bin/python (cv2 4.11.0) "
            "instead of the system python3.")


def _load_session(fast: bool = False) -> Any:
    """Load the Real-ESRGAN session (2 threads — measured fastest
    on this VM for both variants). Raises SuperResError with the
    exact download instruction when the model files are missing."""
    ort = _require_ort()
    onnx_path, data_path = _model_paths(fast)
    if not os.path.isfile(onnx_path) or not os.path.isfile(data_path):
        from . import providers as prov_mod
        info = prov_mod.KNOWN_MODELS["realesrgan-x4plus"]
        raise SuperResError(
            "Real-ESRGAN x4plus model files not found: "
            f"{onnx_path!r} + {data_path!r}. Download the Qualcomm "
            "AI Hub public asset "
            f"{info['url']} and place the .onnx + .data files in "
            "musecut/models/ (w8a8 variant in "
            "musecut/models/realesrgan_w8a8/).")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(
        onnx_path, opts, providers=["CPUExecutionProvider"])


def _tile_origins(length: int) -> List[int]:
    """Tile start positions covering [0, length) with TILE-wide
    tiles. Tiles always overlap (the feather blend needs a real
    overlap — abutting tiles would leave a zero-weight line at
    the seam), so the origins are spread evenly: n =
    ceil((length-TILE) / (TILE-OVERLAP)) + 1 tiles, step =
    (length-TILE) / (n-1) <= TILE-OVERLAP. The first origin is 0
    and the last is length-TILE, so the edges are covered
    exactly."""
    if length <= TILE:
        return [0]
    span = length - TILE
    stride = TILE - OVERLAP
    n = (span + stride - 1) // stride + 1
    if n <= 1:
        return [0]
    step = span / (n - 1)
    origins = sorted({int(round(i * step)) for i in range(n)})
    origins[0] = 0
    origins[-1] = span
    return origins


def _feather_weight(size: int, ramp: int, first: bool,
                    last: bool) -> Any:
    """1-D feather ramp for one output tile axis: 1.0 at the frame
    edge, ramping 0->1 over ``ramp`` px at an overlapped edge."""
    import numpy as np
    w = np.ones(size, dtype=np.float32)
    if not first:
        w[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float32)
    if not last:
        w[-ramp:] *= np.linspace(1.0, 0.0, ramp, dtype=np.float32)
    return w


def _run_tile(session: Any, tile_rgb: Any, fast: bool) -> Any:
    """Run one 128x128 RGB uint8 tile through the model; return a
    512x512x3 RGB uint8 ndarray."""
    import numpy as np
    if fast:
        x = np.transpose(tile_rgb, (2, 0, 1))[None, :, :, :]
        x = np.ascontiguousarray(x, dtype=np.uint8)
    else:
        x = tile_rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, :, :, :]
        x = np.ascontiguousarray(x, dtype=np.float32)
    out = session.run(None, {"image": x})[0]
    arr = np.transpose(out[0], (1, 2, 0))
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    return arr


def upscale_frame(img_bgr: Any, session: Any = None,
                  fast: bool = False) -> Any:
    """Upscale one BGR uint8 frame 4x with tiled Real-ESRGAN.

    ``img_bgr`` is a uint8 HxWx3 BGR ndarray (or an RGB PIL
    image). Returns a uint8 BGR ndarray of shape (4H, 4W, 3).
    Tile seams are feather-blended (16 px input overlap, 64 px
    output ramp). Frames smaller than 128 px in a dimension are
    reflect-padded to a full tile and cropped back.
    """
    import numpy as np
    cv2 = _require_cv2()
    if hasattr(img_bgr, "convert"):  # PIL image, assumed RGB
        arr = np.asarray(img_bgr.convert("RGB"), dtype=np.uint8)
        img = arr[:, :, ::-1].copy()
    else:
        img = np.asarray(img_bgr)
    if img.dtype != np.uint8 or img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(
            "upscale_frame needs a uint8 3-channel BGR image, got "
            f"shape {img.shape} dtype {img.dtype}")
    if session is None:
        session = _load_session(fast)
    h, w = img.shape[:2]
    pad_h = max(0, TILE - h)
    pad_w = max(0, TILE - w)
    if pad_h or pad_w:
        img = cv2.copyMakeBorder(
            img, 0, pad_h, 0, pad_w, cv2.BORDER_REFLECT)
        h, w = img.shape[:2]
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    out_h, out_w = h * SCALE, w * SCALE
    acc = np.zeros((out_h, out_w, 3), dtype=np.float32)
    wsum = np.zeros((out_h, out_w), dtype=np.float32)
    xs, ys = _tile_origins(w), _tile_origins(h)
    ramp = OVERLAP * SCALE
    for iy, y0 in enumerate(ys):
        for ix, x0 in enumerate(xs):
            tile = rgb[y0:y0 + TILE, x0:x0 + TILE]
            up = _run_tile(session, tile, fast).astype(np.float32)
            wx = _feather_weight(SCALE * TILE, ramp,
                                 ix == 0, ix == len(xs) - 1)
            wy = _feather_weight(SCALE * TILE, ramp,
                                 iy == 0, iy == len(ys) - 1)
            wt = wy[:, None] * wx[None, :]
            oy, ox = y0 * SCALE, x0 * SCALE
            acc[oy:oy + SCALE * TILE, ox:ox + SCALE * TILE] += (
                up * wt[:, :, None])
            wsum[oy:oy + SCALE * TILE, ox:ox + SCALE * TILE] += wt
    out = acc / np.maximum(wsum[:, :, None], 1e-6)
    out = np.clip(out, 0, 255).astype(np.uint8)
    # crop the reflect padding back off (in output space)
    orig_h = img.shape[0] - pad_h
    orig_w = img.shape[1] - pad_w
    out = out[:orig_h * SCALE, :orig_w * SCALE]
    return cv2.cvtColor(out, cv2.COLOR_RGB2BGR)


def measure_superres(lr_bgr: Any, sr_bgr: Any, gt_bgr: Any) -> Dict[str, Any]:
    """Numbers for one upscaled frame: output size (must be 4x the
    LR), PSNR of SR vs ground truth and of a lanczos 4x baseline
    vs ground truth, and gradient energy (sharpness) of SR / GT /
    lanczos. A GAN SR model typically scores LOWER PSNR than
    lanczos while matching GT sharpness — report both, judge by
    the frame check, not by PSNR alone."""
    import numpy as np
    cv2 = _require_cv2()
    lr = np.asarray(lr_bgr)
    sr = np.asarray(sr_bgr)
    gt = np.asarray(gt_bgr)
    if sr.shape[0] != lr.shape[0] * SCALE or sr.shape[1] != lr.shape[1] * SCALE:
        raise ValueError(
            f"superres output must be 4x the LR input: LR {lr.shape} "
            f"-> SR {sr.shape}")
    if sr.shape != gt.shape:
        raise ValueError(
            f"SR shape {sr.shape} != ground-truth shape {gt.shape}")
    base = cv2.resize(lr, (gt.shape[1], gt.shape[0]),
                      interpolation=cv2.INTER_LANCZOS4)

    def psnr(a: Any, b: Any) -> float:
        mse = float(np.mean(
            (a.astype(np.float64) - b.astype(np.float64)) ** 2))
        return 10 * math.log10(255.0 ** 2 / mse) if mse > 0 else float("inf")

    def grad_energy(img: Any) -> float:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64)
        return float(np.abs(np.diff(g, axis=1)).mean()
                     + np.abs(np.diff(g, axis=0)).mean())

    return {
        "output_shape": [int(sr.shape[1]), int(sr.shape[0])],
        "psnr_sr_vs_gt": round(psnr(sr, gt), 2),
        "psnr_lanczos_vs_gt": round(psnr(base, gt), 2),
        "grad_energy_sr": round(grad_energy(sr), 3),
        "grad_energy_gt": round(grad_energy(gt), 3),
        "grad_energy_lanczos": round(grad_energy(base), 3),
        "mean_abs_diff_sr_vs_gt": round(float(np.abs(
            sr.astype(np.int16) - gt.astype(np.int16)).mean()), 2),
    }


def superres_image(src: str, dst: str, fast: bool = False,
                   workdir: Optional[str] = None) -> str:
    """Upscale a still image 4x with Real-ESRGAN. ``src`` is an
    image path (PNG/JPG/...); ``dst`` is written as PNG. Returns
    ``dst``."""
    cv2 = _require_cv2()
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    img = cv2.imread(src)
    if img is None:
        raise SuperResError(f"cannot read image: {src!r}")
    session = _load_session(fast)
    out = upscale_frame(img, session, fast=fast)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if not cv2.imwrite(dst, out):
        raise SuperResError(f"cannot write image: {dst!r}")
    return dst


def _assemble_plate(frames_dir: str, dst: str, sample_fps: float,
                    src_fps: float) -> str:
    """Assemble processed PNG frames into a plate video (crf 18,
    yuv420p — same pattern as ``inpaint._assemble_plate``)."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-framerate", str(sample_fps),
           "-i", os.path.join(frames_dir, "p%04d.png"),
           "-vf", f"fps={src_fps:.3f},format=yuv420p",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", DEFAULT_PLATE_CRF, dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=ffmpeg_env())
    if proc.returncode != 0:
        raise SuperResError(
            f"superres plate assembly failed for {dst}: "
            f"{(proc.stderr or '')[-1000:]}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise SuperResError(
            f"superres plate assembly produced no file: {dst}")
    return dst


def superres_clip(src: str, dst: str, sample_fps: float = DEFAULT_SAMPLE_FPS,
                  fast: bool = False, workdir: Optional[str] = None,
                  name: str = "superres") -> str:
    """Upscale a video clip 4x with Real-ESRGAN and render it
    through the real engine.

    Frames are processed at ``sample_fps`` (default 10 — a 720p
    frame costs ~4.5 min float / ~2 min w8a8 on this CPU, so full
    fps is not practical here) and held between samples; the
    upscaled plate is then rendered through
    ``musecut.render.Renderer`` (one clip, fit cover, duration
    clamped to the source) — never hand-rolled ffmpeg for the
    deliverable. The output is video-only (silent): the plate
    carries no audio track. Returns ``dst``.
    """
    from .render import Renderer, probe_media
    from .timeline import new_project
    from .track import _extract_frames

    _require_cv2()
    if sample_fps <= 0:
        raise ValueError(f"sample_fps must be > 0, got {sample_fps}")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    info = probe_media(src)
    w, h = info["width"], info["height"]
    src_fps = info["fps"]
    dur = info["duration"]
    if w <= 0 or h <= 0 or dur <= 0:
        raise SuperResError(f"cannot probe usable video: {src!r}")
    root = os.path.expanduser("~/workspace/musecut/.tmp")
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-superres-", dir=root)
    os.makedirs(tmp, exist_ok=True)
    session = _load_session(fast)
    frames = _extract_frames(src, sample_fps, tmp)
    if not frames:
        raise SuperResError(f"no frames extracted from {src!r}")
    n_needed = max(1, int(math.ceil(dur * sample_fps)))
    plates_dir = os.path.join(tmp, "plates")
    os.makedirs(plates_dir, exist_ok=True)
    cv2 = _require_cv2()
    for i in range(n_needed):
        _, path = frames[min(i, len(frames) - 1)]
        img = cv2.imread(path)
        if img is None:
            raise SuperResError(
                f"cannot read extracted frame: {path!r}")
        out = upscale_frame(img, session, fast=fast)
        cv2.imwrite(os.path.join(plates_dir, f"p{i + 1:04d}.png"), out)
    plate = os.path.join(tmp, "superres_plate.mp4")
    _assemble_plate(plates_dir, plate, sample_fps, src_fps)
    proj = new_project(name, w * SCALE, h * SCALE, int(round(src_fps)))
    proj["clips"] = [{"src": plate, "duration": dur, "fit": "cover"}]
    Renderer(proj, workdir=tmp).render(dst)
    return dst
