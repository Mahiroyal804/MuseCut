"""MuseCut AI background removal — RMBG-1.4 ONNX + onnxruntime, CPU.

Round 4 (v0.6.0): the second real ML feature, same approach as
tracking (round 3) — verify a public .onnx mirror, download,
size-check, test on real footage, score only what renders.

Model: briaai/RMBG-1.4, ``onnx/model.onnx`` (176,153,355 bytes,
verified 2026-11-21 via the Hugging Face API + ``curl -sIL``;
the rembg GitHub-release URL 404s and was NOT used).
The exported graph takes a FIXED 1024x1024 RGB input
(normalised to [-1, 1]) and returns a [0, 1] alpha mask
(the graph ends in a Sigmoid — verified: output range
[0.000, 1.000] on a real street frame). Inference on this VM's
CPU costs ~4.3s/frame at 1024, so frames are segmented at a
low ``sample_fps`` (default 5) and the masks are held between
samples — fine for a mostly-static subject, documented cost.

LICENSE (honest, read from the model card 2026-11-21):
RMBG-1.4 is source-available for NON-COMMERCIAL use under
BRIA's ``bria-rmbg-1.4`` licence
(https://bria.ai/bria-huggingface-model-license-agreement/);
commercial use needs a paid BRIA licence. MuseCut uses it
locally for editing; shipping it inside a commercial product
would need that licence. (Same class of caveat as YOLOv5n's
GPL-3.0 — see ``providers.KNOWN_MODELS``.)

What this module does NOT do: run at full fps on long clips (see
the ``sample_fps`` cost above). Edge refinement lives in
``musecut.matting`` (round 6): ``make_mask_video(..., refine=True)``
refines the alpha, and ``matting.make_refined_plates`` also builds
the decontaminated RGB plate. Nothing here is faked: without
the model file on disk every entry point raises a clear
error with the download instruction.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from typing import Any, Dict, Optional

from . import providers as prov_mod
from .audio import ffmpeg_env

MODEL_NAME = "rmbg-1-4"
INPUT_SIZE = 1024  # fixed by the exported graph (512 is rejected)


class BgRemoveError(RuntimeError):
    """Raised when background removal cannot run (no model, ...)."""


def model_ready() -> bool:
    """True when onnxruntime imports AND the RMBG-1.4 file is on disk."""
    return (prov_mod.onnxruntime_available()
            and prov_mod.model_status(MODEL_NAME)["status"] == "present")


def _load_session():
    import onnxruntime as ort
    path = prov_mod.model_path(MODEL_NAME)
    if not os.path.isfile(path):
        raise BgRemoveError(
            f"background-removal model not downloaded: {path!r}. "
            f"Fetch {MODEL_NAME} from "
            f"{prov_mod.KNOWN_MODELS[MODEL_NAME]['url']} "
            f"({prov_mod.KNOWN_MODELS[MODEL_NAME]['size_bytes']} bytes) "
            f"into {prov_mod.MODEL_DIR}/ — see musecut/providers.py.")
    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


def segment_frame(session: Any, image: Any) -> Any:
    """Segment one RGB PIL image -> alpha mask (PIL, mode "L").

    The mask is returned at the INPUT image's size (the model
    runs at 1024x1024 and the mask is resized back with
    LANCZOS, which keeps edges smooth). Values 0 (background)
    .. 255 (foreground). The graph's Sigmoid output is already
    in [0, 1] — no extra sigmoid is applied.
    """
    import numpy as np
    from PIL import Image
    w, h = image.size
    im = image.resize((INPUT_SIZE, INPUT_SIZE))
    arr = (np.asarray(im, dtype=np.float32) / 255.0 - 0.5) / 0.5
    arr = np.transpose(arr, (2, 0, 1))[None]
    out = session.run(None, {"input": arr})[0][0]
    mask = out[0] if out.ndim == 3 else out
    mask = np.clip(mask, 0.0, 1.0)
    mask_img = Image.fromarray((mask * 255).astype("uint8"), mode="L")
    if (w, h) != (INPUT_SIZE, INPUT_SIZE):
        mask_img = mask_img.resize((w, h), Image.LANCZOS)
    return mask_img


def make_mask_video(src: str, dst: str, sample_fps: float = 5.0,
                    workdir: Optional[str] = None,
                    refine: bool = False,
                    refine_kwargs: Optional[Dict[str, Any]] = None) -> str:
    """Segment ``src`` into a grayscale mask video at ``dst``.

    Frames are segmented at ``sample_fps`` (default 5 — the
    model costs ~4.3s/frame on this CPU) and the masks are held
    between samples (nearest neighbour in time). The mask video
    has the source's resolution and fps and covers the source's
    full duration, so the render engine can ``alphamerge`` it
    1:1 with the clip. Returns ``dst``.

    With ``refine=True`` each mask is passed through
    ``musecut.matting.refine_alpha`` (trimap + guided filter +
    feather; decontamination is skipped here — the mask video
    carries alpha only, no RGB — use
    ``matting.make_refined_plates`` when the clean RGB plate is
    needed too).
    """
    from .render import probe_media
    from .track import _extract_frames

    if not os.path.isfile(src):
        raise BgRemoveError(f"video not found: {src!r}")
    session = _load_session()
    info = probe_media(src)
    w, h = info["width"], info["height"]
    src_fps = info["fps"]
    dur = info["duration"]
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-bgremove-")
    os.makedirs(tmp, exist_ok=True)
    masks_dir = os.path.join(tmp, "masks")
    os.makedirs(masks_dir, exist_ok=True)

    from PIL import Image
    frames = _extract_frames(src, sample_fps, tmp)
    if not frames:
        raise BgRemoveError(f"no frames extracted from {src!r}")
    n_needed = max(1, int(math.ceil(dur * sample_fps)))
    for i in range(n_needed):
        t, path = frames[min(i, len(frames) - 1)]
        img = Image.open(path).convert("RGB")
        mask = segment_frame(session, img)
        if refine:
            from . import matting as mat_mod
            # The mask video carries alpha only — decontamination
            # (which corrects RGB) is skipped here unless the caller
            # explicitly asks for it; use make_refined_plates when
            # the clean RGB plate is needed too.
            kw = dict(refine_kwargs or {})
            kw.setdefault("decontaminate", False)
            mask, _ = mat_mod.refine_alpha(img, mask, **kw)
        mask.save(os.path.join(masks_dir, f"m{i + 1:04d}.png"))

    # Assemble: hold each mask for 1/sample_fps, stretch to the
    # source fps. -stream_loop is applied by the caller in the
    # render (the mask input loops), so the video only needs to
    # cover the duration once.
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-framerate", str(sample_fps),
           "-i", os.path.join(masks_dir, "m%04d.png"),
           "-vf", f"fps={src_fps:.3f},format=gray",
           "-c:v", "libx264", "-pix_fmt", "gray", "-crf", "0",
           dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=ffmpeg_env())
    if proc.returncode != 0:
        raise BgRemoveError(
            f"mask video assembly failed: {(proc.stderr or '')[-1000:]}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise BgRemoveError("mask video assembly produced no file")
    return dst


def mask_stats(mask_path: str) -> Dict[str, Any]:
    """Cheap sanity stats for a mask PNG (used by tests)."""
    import numpy as np
    from PIL import Image
    arr = np.asarray(Image.open(mask_path).convert("L"),
                     dtype=np.float32) / 255.0
    return {"mean": round(float(arr.mean()), 4),
            "fg_frac": round(float((arr > 0.5).mean()), 4),
            "bg_frac": round(float((arr < 0.1).mean()), 4)}
