"""Eye correction (round 14): redirect a talking head's gaze toward the camera.

Pipeline (no new heavy dependency — numpy + cv2 + onnxruntime, all
already in the video venv):

1. Face box from the Haar cascade (largest face wins — the same
   pattern as ``musecut.lipsync.detect_face``; the round-13 fix for
   the small false-positive box).
2. Dense landmarks from Google's MediaPipe Face Landmarker (478
   points, 256x256) via the verified ONNX mirror
   ``yakhyo/mediapipe-face-mesh-onnx`` (GitHub releases, tag
   ``weights``): ``face_landmarker_Nx3x256x256.onnx`` (4,864,717 B,
   sha256 pinned below) + its bundled BlazeFace detector is NOT
   used — Haar supplies the box, and the ROI follows MediaPipe's
   public ``detection_to_roi`` recipe: a SQUARE region, long side
   of the box scaled 1.5x (margin 0.25), sampled straight to the
   model size. The mesh model was verified by this round's probe:
   on the real gate photo the 478-point mesh sits exactly on the
   face (frame-checked), iris centres land on the irises, and the
   measured gaze offsets are small and consistent between the two
   eyes (left (-0.031, -0.077), right (-0.023, -0.073) eye-widths
   — the subject looks at the camera). Inference is ~0.10 s/face
   on this CPU.
3. Gaze = iris-centre offset from eye-centre, in eye-width units
   (iris centres: landmarks 468 left / 473 right; eye centre is
   the corners midpoint in x and the upper/lower eyelid midpoint
   in y — the corners midpoint sits below the midline and biases
   the vertical offset by ~-0.05). The correction warps each iris
   toward the eye centre with a smoothstep falloff outside the
   iris disk's destination footprint (uniform shift across the
   disk, zero one iris radius past it), so the iris translates
   rigidly and only the surrounding sclera stretches — no hole,
   no crease.
4. Video: landmarks are sampled at ``sample_fps`` and the OFFSETS
   are moving-averaged (window 3) before the deadband decision,
   so a borderline offset cannot flicker the warp on and off
   frame to frame; frames with no detected face pass through
   untouched (the round-13 lip-sync B-roll rule).

Verification is by NCC template matching, not by re-running the
landmarker: the landmarker is SHAPE-LOCKED for iris-only changes
(a +0.50 eye-width warp moved its reported offset by only 0.02,
non-monotonically — it reads gaze mostly from the eye contour
and head pose, which the warp deliberately does not touch),
while NCC measured the same warps at +6.0/+7.0 px (expect 6.3)
and +13.0/+13.0 px (expect 12.6) with scores 0.997-0.999. The
landmarker offsets are still reported alongside, but they are
not the proof.
- This corrects GAZE DIRECTION (iris position), not head pose.
  A frontality gate skips the warp when the head is turned: the
  yaw proxy (|nose-tip x - mid-eye x| / inter-ocular distance)
  must be <= 0.20, else the frame is left alone. Centring the
  iris on a turned head looks wrong, so the gate is the fix.
- The landmarker measures gaze with a per-face error of ~±0.06
  eye-widths (a camera-looking face reads ~0.00 on the gate
  photo but L -0.065 / R +0.055 on the reel speaker — both eyes
  outward, which no real gaze does). So a DEADBAND of 0.10
  eye-widths applies: offsets below it are left alone, and the
  feature only corrects gazes that are clearly off-camera. A
  sanity bound skips eyes with |offset| > 0.50 (a blink frame
  once read y = -3.35 — landmark garbage) or eyes narrower than
  20 px. Glasses glare degrades the measurement further (the
  round-13 clip speaker's right iris sits under a blue glare).
- The shift is clamped to 0.35 eye-widths per axis; an iris
  pinned at the very edge of the eye cannot be dragged to the
  centre without distorting the eye, so extreme gazes correct
  only part-way (the clamp is reported in the measurement).
- The warp is geometric, not generative: it moves real pixels.
  NCC measures the applied shift exactly (the correction moves
  the iris by the measured offset, to the pixel). The closed loop
  is limited by the measurement, not the warp: the landmarker
  under-reads a synthetic iris-only shift by ~3-4x, so on a
  synthetic +0.25 warp the iris comes only ~1 px back of the
  ~6 px it went out — a documented limit, not a warp error.
- Heads tilted more than ~15 degrees degrade the mesh (the
  mesh model itself is roll-sensitive — the mirror author's own
  finding); tilted-head frames are left alone when the mesh
  score is low.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- constants

LANDMARKER_FILE = "face_landmarker_Nx3x256x256.onnx"
LANDMARKER_SIZE = 4864717
LANDMARKER_SHA256 = (
    "111795f8703cdeb6d0c68a9f3cc966a0f23f8786bb00f4577a11f461fc4276ac"
)
LANDMARKER_URL = (
    "https://github.com/yakhyo/mediapipe-face-mesh-onnx/releases/"
    "download/weights/face_landmarker_Nx3x256x256.onnx"
)
BLAZEFACE_FILE = "face_detection_short_range.onnx"
BLAZEFACE_SIZE = 470549
BLAZEFACE_SHA256 = (
    "2f2689b040becf555706d2cb978d2f0e3296ea82413734fba9a856c66c5f2b17"
)
BLAZEFACE_URL = (
    "https://github.com/yakhyo/mediapipe-face-mesh-onnx/releases/"
    "download/weights/face_detection_short_range.onnx"
)
MODEL_LICENSE = "Apache-2.0 (Google MediaPipe weights via PINTO0309's ONNX conversion; yakhyo mirror)"
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")

# MediaPipe Face Landmarker indices (478-point layout).
L_IRIS_C, R_IRIS_C = 468, 473
L_IRIS_RING = (469, 470, 471, 472)
R_IRIS_RING = (474, 475, 476, 477)
L_OUT, L_IN = 33, 133
R_IN, R_OUT = 362, 263
# Eyelid midpoints (y of the eye centre — the corners midpoint sits
# below the eye's midline and biases the vertical offset by ~-0.05;
# the lid-midpoint definition measures a camera-looking face at
# y ≈ -0.03 instead of -0.08).
L_UP, L_LO = 159, 145
R_UP, R_LO = 386, 374
NOSE_TIP = 1

DEFAULT_SAMPLE_FPS = 10.0
DEFAULT_MAX_SHIFT = 0.35      # eye-widths per axis
DEFAULT_FRONTALITY = 0.20     # max |yaw proxy| before the gate skips
DEFAULT_DEADBAND = 0.10       # eye-widths: offsets below this are NOT
# corrected — the landmarker measures gaze with a per-face error of
# ~±0.06 eye-widths (a camera-looking face reads L -0.065 / R +0.055
# on the reel speaker, ~0.00 on the gate photo), so correcting
# sub-0.10 offsets would move centred irises the wrong way.
DEFAULT_MIN_EYE_WIDTH = 20.0  # px: smaller eyes are not measured
DEFAULT_MAX_OFFSET = 0.50      # eye-widths: beyond this the landmarks
# are garbage (a blink frame read y = -3.35) — skip the eye.
DEFAULT_SMOOTH_WINDOW = 3     # moving-average window over sampled frames
DEFAULT_PLATE_CRF = "18"


class EyeCorrectionError(Exception):
    """Raised when eye correction cannot run on the given input."""


# ---------------------------------------------------------------- utilities

def _require_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - venv-only path
        raise EyeCorrectionError(
            "eye correction needs cv2 — run with "
            "~/workspace/.venv-video/bin/python") from exc
    return cv2


def _require_ort() -> Any:
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - venv-only path
        raise EyeCorrectionError(
            "eye correction needs onnxruntime — run with "
            "~/workspace/.venv-video/bin/python") from exc
    return ort


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path() -> str:
    """Filesystem path of the landmarker ONNX (workspace copy first)."""
    ws = os.path.join(MODEL_DIR, LANDMARKER_FILE)
    if os.path.isfile(ws):
        return ws
    return os.path.join(
        os.path.expanduser("~/.cache/musecut/models"), LANDMARKER_FILE)


def verify_model() -> Dict[str, Any]:
    """Check the landmarker file is the verified mirror (size + sha256)."""
    path = model_path()
    if not os.path.isfile(path):
        return {"status": "missing", "path": path, "url": LANDMARKER_URL}
    size = os.path.getsize(path)
    sha = _sha256(path)
    if size != LANDMARKER_SIZE or sha != LANDMARKER_SHA256:
        return {"status": "mismatch", "path": path, "size_bytes": size,
                "sha256": sha, "url": LANDMARKER_URL}
    return {"status": "verified", "path": path, "size_bytes": size,
            "sha256": sha, "url": LANDMARKER_URL,
            "license": MODEL_LICENSE}


# ---------------------------------------------------------------- sessions

_session = None


def _load_session() -> Any:
    """Lazy singleton landmarker session (CPU)."""
    global _session
    if _session is not None:
        return _session
    ort = _require_ort()
    info = verify_model()
    if info["status"] != "verified":
        raise EyeCorrectionError(
            f"face landmarker model not verified: {info['status']} "
            f"({info['path']}); fetch it from {LANDMARKER_URL}")
    _session = ort.InferenceSession(
        info["path"], providers=["CPUExecutionProvider"])
    return _session


def _detect_face_box(img_bgr: Any) -> Optional[Tuple[int, int, int, int]]:
    """Haar-cascade face box (largest wins), or None when no face.

    Same pattern as ``musecut.lipsync.detect_face`` — the largest
    detected face wins, so a small false positive on a busy frame
    never beats the speaker. Returns (x0, y0, x1, y1) padded 12%.
    """
    cv2 = _require_cv2()
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    casc = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = casc.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = [int(v) for v in max(faces, key=lambda f: f[2] * f[3])]
    pad = int(0.12 * w)
    return (max(0, x - pad), max(0, y - pad),
            min(img_bgr.shape[1], x + w + pad),
            min(img_bgr.shape[0], y + h + pad))


def _roi_from_box(box: Tuple[int, int, int, int],
                  margin: float = 0.25) -> Tuple[float, float, float]:
    """MediaPipe detection_to_roi: square, long side scaled 1.5x.

    Returns (center_x, center_y, side) in image pixels. Roll is 0 —
    Haar supplies no eye keypoints, so tilted heads rely on the
    frontality gate instead of roll normalisation.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    side = (1.0 + 2.0 * margin) * max(x1 - x0, y1 - y0)
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0, side


def _run_landmarks(img_bgr: Any, box: Tuple[int, int, int, int]) -> Any:
    """Run the landmarker on one face box; returns (478, 3) float32
    landmarks in full-image pixels + the face-presence score in [0, 1].

    The ROI is warped straight to the model's 256x256 input (one
    bilinear resample, like MediaPipe's ImageToTensorCalculator) and
    the landmarks are mapped back with the inverse affine.
    """
    import numpy as np
    cv2 = _require_cv2()
    session = _load_session()
    cx, cy, side = _roi_from_box(box)
    size = 256
    matrix = cv2.getRotationMatrix2D((cx, cy), 0.0, size / side)
    matrix[0, 2] += size / 2.0 - cx
    matrix[1, 2] += size / 2.0 - cy
    crop = cv2.warpAffine(img_bgr, matrix, (size, size))
    rgb = crop[:, :, ::-1].astype(np.float32) / 255.0
    blob = np.transpose(rgb, (2, 0, 1))[None, :, :, :]
    input_name = session.get_inputs()[0].name
    landmarks, logits = session.run(None, {input_name: blob})
    score = float(1.0 / (1.0 + np.exp(-float(logits.ravel()[0]))))
    inverse = cv2.invertAffineTransform(matrix)
    lm = landmarks[0].astype(np.float64)
    lm[:, :2] = lm[:, :2] @ inverse[:, :2].T + inverse[:, 2]
    lm[:, 2] *= side / size
    return lm.astype(np.float32), score


def detect_landmarks(img_bgr: Any) -> Optional[Tuple[Any, float]]:
    """Detect the largest face and return its (478, 3) landmarks +
    mesh score, or None when no face is found."""
    box = _detect_face_box(img_bgr)
    if box is None:
        return None
    return _run_landmarks(img_bgr, box)


# ---------------------------------------------------------------- gaze

def gaze_offsets(landmarks: Any) -> Dict[str, Any]:
    """Per-eye gaze offsets in eye-width units + a frontality proxy.

    offset = (iris centre - eye centre) / eye width, per axis.
    The eye centre is the corners midpoint in x and the
    upper/lower eyelid midpoint in y (the corners midpoint sits
    below the midline and biases the vertical offset).
    yaw = (nose tip x - mid-eye x) / inter-ocular distance; a head
    turned sideways moves the nose off the mid-eye line.
    """
    import numpy as np
    lm = landmarks
    out: Dict[str, Any] = {}
    for name, ic, oc, icn, up, lo in (
            ("left", L_IRIS_C, L_OUT, L_IN, L_UP, L_LO),
            ("right", R_IRIS_C, R_IN, R_OUT, R_UP, R_LO)):
        iris = lm[ic, :2].astype(np.float64)
        eye_c = np.array([
            (float(lm[oc, 0]) + float(lm[icn, 0])) / 2.0,
            (float(lm[up, 1]) + float(lm[lo, 1])) / 2.0])
        width = float(np.linalg.norm(lm[oc, :2] - lm[icn, :2]))
        ring = np.stack([lm[i, :2] for i in
                         (L_IRIS_RING if name == "left" else R_IRIS_RING)])
        radius = float(np.mean(np.linalg.norm(ring - iris, axis=1)))
        out[name] = {
            "iris": (float(iris[0]), float(iris[1])),
            "eye_center": (float(eye_c[0]), float(eye_c[1])),
            "eye_width": width,
            "iris_radius": radius,
            "offset": ((float(iris[0] - eye_c[0]) / width) if width else 0.0,
                       (float(iris[1] - eye_c[1]) / width) if width else 0.0),
        }
    mid_eye = (np.asarray(out["left"]["eye_center"])
               + np.asarray(out["right"]["eye_center"])) / 2.0
    iod = float(np.linalg.norm(
        np.asarray(out["left"]["eye_center"])
        - np.asarray(out["right"]["eye_center"])))
    nose_x = float(lm[NOSE_TIP, 0])
    out["yaw"] = (nose_x - float(mid_eye[0])) / iod if iod else 0.0
    out["frontal"] = abs(out["yaw"]) <= DEFAULT_FRONTALITY
    return out


def _smoothstep(t: Any) -> Any:
    """3t^2 - 2t^3 on [0, 1] — zero slope at both ends, so the warp
    meets the untouched surround without a crease."""
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


def warp_eye(img_bgr: Any, iris: Tuple[float, float], radius: float,
             shift: Tuple[float, float]) -> Any:
    """Shift the iris region by ``shift`` (pixels) with a smoothstep
    falloff outside the iris.

    The displacement is UNIFORM (1.0) across the "stadium" — the
    union of the iris disk where it is and where it is going
    (the segment from the iris centre to the shifted centre,
    dilated by the iris radius) — then falls to zero over one
    more iris radius. So the iris translates rigidly, the eye
    corners and lids (perpendicular to the shift) are untouched,
    and only the surrounding sclera stretches. (A circular flat
    region of radius r + |shift| reaches the eye corners and
    moves them with the iris — the round-14 dev probe showed the
    eye centre itself shifting +1.1 px under a +0.25 warp, which
    corrupted the gaze measurement. A falloff that starts at the
    iris edge compresses the disk instead of moving it — the same
    probe first measured a +0.25 warp as a 0.02 shift, and a
    synthetic-disk test moved the centroid only 7 px for a 10 px
    request with the disk squeezed to half its width. The flat
    region must cover where the disk is GOING, not just where it
    was — and only in the direction it is going.)
    Built with cv2.remap: dest(x) samples src(x - disp(x)), so the
    area the iris vacates is filled by stretched sclera — no hole.
    Only the eye ROI is remapped.
    """
    import numpy as np
    cv2 = _require_cv2()
    cx, cy = iris
    r = max(3.0, float(radius))
    sx, sy = float(shift[0]), float(shift[1])
    shift_mag = float(np.hypot(sx, sy))
    # Stadium: the segment centre -> centre+shift, dilated by r,
    # is flat (full shift); one more iris radius of smooth falloff.
    half = int(math.ceil(r + shift_mag + r)) + 2
    h, w = img_bgr.shape[:2]
    x0 = max(0, int(cx) - half)
    y0 = max(0, int(cy) - half)
    x1 = min(w, int(cx) + half + 1)
    y1 = min(h, int(cy) + half + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return img_bgr
    roi = img_bgr[y0:y1, x0:x1]
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    px, py = xs - cx, ys - cy
    if shift_mag < 1e-6:
        return img_bgr
    # distance from the point to the segment [0, shift] (in the
    # shift's frame), minus r: <= 0 inside the stadium.
    ux, uy = sx / shift_mag, sy / shift_mag
    along = np.clip(px * ux + py * uy, 0.0, shift_mag)
    qx, qy = along * ux, along * uy
    d_seg = np.sqrt((px - qx) ** 2 + (py - qy) ** 2)
    t = np.clip((d_seg - r) / max(1e-6, r), 0.0, 1.0)
    f = 1.0 - np.vectorize(_smoothstep)(t)
    # remap samples src at (coord - disp): content moves by +shift.
    map_x = (xs - f * sx).astype(np.float32)
    map_y = (ys - f * sy).astype(np.float32)
    warped = cv2.remap(roi, map_x - x0, map_y - y0,
                       cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    out = img_bgr.copy()
    out[y0:y1, x0:x1] = warped
    return out


def _eye_shift(gaze_eye: Dict[str, Any],
               max_shift: float = DEFAULT_MAX_SHIFT,
               target: Tuple[float, float] = (0.0, 0.0)
               ) -> Tuple[Tuple[float, float], str]:
    """Desired iris shift in eye-width units for one eye, or (0, 0)
    with the reason it was skipped.

    Gates, in order: eye too small to measure; offset beyond the
    sanity bound (landmark garbage); offset inside the deadband
    (below the landmarker noise floor — leave centred eyes alone).
    Otherwise the shift is (target - offset), clamped to
    ``max_shift`` per axis.
    """
    if gaze_eye["eye_width"] < DEFAULT_MIN_EYE_WIDTH:
        return (0.0, 0.0), "eye too small (%.1f px)" % gaze_eye["eye_width"]
    off = gaze_eye["offset"]
    if abs(off[0]) > DEFAULT_MAX_OFFSET or abs(off[1]) > DEFAULT_MAX_OFFSET:
        return (0.0, 0.0), "offset out of range %s (landmark garbage)" % (
            off,)
    want = (target[0] - off[0], target[1] - off[1])
    want = (0.0 if abs(want[0]) < DEFAULT_DEADBAND else want[0],
            0.0 if abs(want[1]) < DEFAULT_DEADBAND else want[1])
    applied = (max(-max_shift, min(max_shift, want[0])),
               max(-max_shift, min(max_shift, want[1])))
    reason = ""
    if want != applied:
        reason = "clamped to %.2f" % max_shift
    return applied, reason


def correct_frame(img_bgr: Any, landmarks: Any,
                  max_shift: float = DEFAULT_MAX_SHIFT,
                  target: Tuple[float, float] = (0.0, 0.0)
                  ) -> Tuple[Any, Dict[str, Any]]:
    """Warp both irises toward ``target`` (eye-width units, default
    the eye centre) and return (corrected image, measurement).

    The per-eye shift goes through ``_eye_shift`` (deadband, sanity
    bound, clamp); the measurement reports the requested vs applied
    shift and whether the frontality gate held.
    """
    gaze = gaze_offsets(landmarks)
    meas: Dict[str, Any] = {"frontal": gaze["frontal"], "yaw": gaze["yaw"],
                            "eyes": {}}
    out = img_bgr
    if not gaze["frontal"]:
        meas["skipped"] = "head turned (yaw %.3f > %.2f)" % (
            gaze["yaw"], DEFAULT_FRONTALITY)
        return out, meas
    for name in ("left", "right"):
        g = gaze[name]
        applied, reason = _eye_shift(g, max_shift=max_shift, target=target)
        shift_px = (applied[0] * g["eye_width"],
                    applied[1] * g["eye_width"])
        if abs(shift_px[0]) < 0.25 and abs(shift_px[1]) < 0.25:
            meas["eyes"][name] = {"offset": g["offset"], "shift_px": (0.0, 0.0),
                                  "skipped": reason or "already centred (<0.25 px)"}
            continue
        out = warp_eye(out, g["iris"], g["iris_radius"], shift_px)
        meas["eyes"][name] = {"offset": g["offset"], "shift_px": shift_px,
                              "reason": reason}
    return out, meas


def correct_image(src: str, dst: str,
                  max_shift: float = DEFAULT_MAX_SHIFT) -> Dict[str, Any]:
    """Correct the gaze in one still image; returns the measurement.

    No face found -> the source is copied unchanged and the
    measurement says so (never an error: a faceless frame is a
    normal input, not a failure).
    """
    cv2 = _require_cv2()
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    img = cv2.imread(src)
    if img is None:
        raise EyeCorrectionError(f"cannot read image: {src!r}")
    found = detect_landmarks(img)
    if found is None:
        cv2.imwrite(dst, img)
        return {"face": False, "note": "no face detected — copied unchanged"}
    landmarks, score = found
    corrected, meas = correct_frame(img, landmarks, max_shift=max_shift)
    cv2.imwrite(dst, corrected)
    meas["face"] = True
    meas["mesh_score"] = float(score)
    return meas


def ncc_iris_shift(before_bgr: Any, after_bgr: Any,
                   iris: Tuple[float, float], radius: float,
                   eye_width: float) -> Tuple[float, float, float]:
    """Measure how far the iris texture actually moved, in pixels.

    Template-matches the iris patch from ``before_bgr`` inside
    ``after_bgr`` (normalised cross-correlation over a ±0.7
    eye-width search window). Returns (dx, dy, score).

    This is the PRIMARY verifier for a correction: the landmarker
    is shape-locked for iris-only changes (a +0.50 eye-width warp
    moved its reported offset by only 0.02, non-monotonically —
    it reads gaze mostly from the eye contour and head pose, which
    the warp deliberately does not touch), while NCC measured the
    same warps at +6.0/+7.0 px (expect 6.3/6.6) and +13.0/+13.0 px
    (expect 12.6/13.3) with scores 0.997-0.999.
    """
    cv2 = _require_cv2()
    cx, cy = iris
    s = int(max(4, round(radius)))
    x0, y0 = int(round(cx)) - s, int(round(cy)) - s
    tpl = before_bgr[y0:y0 + 2 * s, x0:x0 + 2 * s]
    if tpl.size == 0:
        return (0.0, 0.0, 0.0)
    gray_t = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    gray_w = cv2.cvtColor(after_bgr, cv2.COLOR_BGR2GRAY)
    r = int(round(0.7 * eye_width))
    sx0, sy0 = max(0, x0 - r), max(0, y0 - r)
    sx1 = min(after_bgr.shape[1], x0 + 2 * s + r)
    sy1 = min(after_bgr.shape[0], y0 + 2 * s + r)
    search = gray_w[sy0:sy1, sx0:sx1]
    if search.shape[0] < gray_t.shape[0] or search.shape[1] < gray_t.shape[1]:
        return (0.0, 0.0, 0.0)
    res = cv2.matchTemplate(search, gray_t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return (float(loc[0] + sx0 - x0), float(loc[1] + sy0 - y0), float(mx))


def measure_eye_correction(before_path: str, after_path: str) -> Dict[str, Any]:
    """Independent check: re-run the landmarker on both images AND
    template-match each iris patch (NCC) to measure the real shift.

    The NCC shift is the primary evidence (it measures the pixels);
    the landmarker offsets are reported alongside as a secondary
    signal — the model is shape-locked for iris-only changes and
    must not be read as the correction proof (see
    ``ncc_iris_shift``).
    """
    cv2 = _require_cv2()
    before = cv2.imread(before_path)
    after = cv2.imread(after_path)
    if before is None:
        raise EyeCorrectionError(f"cannot read image: {before_path!r}")
    if after is None:
        raise EyeCorrectionError(f"cannot read image: {after_path!r}")
    found = detect_landmarks(before)
    if found is None:
        return {"face": False, "note": "no face in the before image"}
    landmarks, score = found
    gaze = gaze_offsets(landmarks)
    out: Dict[str, Any] = {"face": True, "mesh_score": float(score),
                           "before": {}, "after": {}, "ncc": {}}
    for eye in ("left", "right"):
        out["before"][eye] = gaze[eye]["offset"]
        dx, dy, mx = ncc_iris_shift(
            before, after, gaze[eye]["iris"], gaze[eye]["iris_radius"],
            gaze[eye]["eye_width"])
        out["ncc"][eye] = {"dx_px": dx, "dy_px": dy, "score": mx,
                           "dx_eye_widths": dx / gaze[eye]["eye_width"]
                           if gaze[eye]["eye_width"] else 0.0,
                           "dy_eye_widths": dy / gaze[eye]["eye_width"]
                           if gaze[eye]["eye_width"] else 0.0}
    found_after = detect_landmarks(after)
    if found_after is not None:
        gaze_after = gaze_offsets(found_after[0])
        out["after"] = {eye: gaze_after[eye]["offset"]
                        for eye in ("left", "right")}
        out["after"]["yaw"] = gaze_after["yaw"]
    out["before"]["yaw"] = gaze["yaw"]
    return out


# ---------------------------------------------------------------- clip

def _ffmpeg_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return env


def _assemble_plate(frames_dir: str, dst: str, sample_fps: float,
                    src_fps: float) -> str:
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-framerate", str(sample_fps),
           "-i", os.path.join(frames_dir, "p%04d.png"),
           "-vf", f"fps={src_fps:.3f},format=yuv420p",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", DEFAULT_PLATE_CRF, dst]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=_ffmpeg_env())
    if proc.returncode != 0:
        raise EyeCorrectionError(
            f"eye-correction plate assembly failed for {dst}: "
            f"{(proc.stderr or '')[-1000:]}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise EyeCorrectionError(
            f"eye-correction plate assembly produced no file: {dst}")
    return dst


def correct_clip(src: str, dst: str,
                 sample_fps: float = DEFAULT_SAMPLE_FPS,
                 max_shift: float = DEFAULT_MAX_SHIFT,
                 workdir: Optional[str] = None,
                 name: str = "eyecorrect") -> Dict[str, Any]:
    """Correct the gaze in a talking-head clip; render through the
    real engine. Returns the measurement summary.

    Frames are processed at ``sample_fps`` (default 10) and held
    between samples; the per-eye offsets are moving-averaged over
    ``DEFAULT_SMOOTH_WINDOW`` sampled frames BEFORE the deadband
    decision, so a borderline offset cannot flicker the warp on
    and off from frame to frame. Frames with no detected face, and
    frames where the frontality gate trips, pass through untouched.
    Output is video-only (silent), like the inpainting clip path.
    """
    from .render import Renderer, probe_media
    from .timeline import new_project
    from .track import _extract_frames

    cv2 = _require_cv2()
    if sample_fps <= 0:
        raise ValueError(f"sample_fps must be > 0, got {sample_fps}")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    info = probe_media(src)
    w, h = info["width"], info["height"]
    src_fps = info["fps"]
    dur = info["duration"]
    if w <= 0 or h <= 0 or dur <= 0:
        raise EyeCorrectionError(f"cannot probe usable video: {src!r}")
    root = os.path.expanduser("~/workspace/musecut/.tmp")
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-eyecorrect-", dir=root)
    os.makedirs(tmp, exist_ok=True)
    frames_dir = os.path.join(tmp, "frames")
    plates_dir = os.path.join(tmp, "plates")
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(plates_dir, exist_ok=True)
    frames = _extract_frames(src, sample_fps, tmp)
    if not frames:
        raise EyeCorrectionError(f"no frames extracted from {src!r}")
    n_needed = max(1, int(math.ceil(dur * sample_fps)))
    history: List[Dict[str, Tuple[float, float]]] = []
    corrected_n = 0
    skipped_n = 0
    for i in range(n_needed):
        _, path = frames[min(i, len(frames) - 1)]
        img = cv2.imread(path)
        if img is None:
            raise EyeCorrectionError(f"cannot read extracted frame: {path!r}")
        found = detect_landmarks(img)
        if found is None:
            skipped_n += 1
            cv2.imwrite(os.path.join(plates_dir, f"p{i + 1:04d}.png"), img)
            continue
        landmarks, _score = found
        gaze = gaze_offsets(landmarks)
        if not gaze["frontal"]:
            skipped_n += 1
            cv2.imwrite(os.path.join(plates_dir, f"p{i + 1:04d}.png"), img)
            continue
        # Smooth the OFFSETS first, then one deadband decision on
        # the smoothed value: smoothing the shifts instead flips a
        # borderline offset on and off per frame (this clip reads
        # -0.094..-0.116, straddling the 0.10 deadband) and the
        # moving average lands at a flickering partial correction.
        history.append({"left": gaze["left"]["offset"],
                        "right": gaze["right"]["offset"]})
        win = history[-DEFAULT_SMOOTH_WINDOW:]
        smooth_off = {eye: (sum(o[eye][0] for o in win) / len(win),
                            sum(o[eye][1] for o in win) / len(win))
                      for eye in ("left", "right")}
        shifts: Dict[str, Tuple[float, float]] = {}
        for eye in ("left", "right"):
            g = gaze[eye]
            probe = dict(g)
            probe["offset"] = smooth_off[eye]
            applied, _reason = _eye_shift(probe, max_shift=max_shift)
            shifts[eye] = (applied[0] * g["eye_width"],
                           applied[1] * g["eye_width"])
        out = img
        warped = False
        for eye in ("left", "right"):
            g = gaze[eye]
            if abs(shifts[eye][0]) < 0.25 and abs(shifts[eye][1]) < 0.25:
                continue
            out = warp_eye(out, g["iris"], g["iris_radius"], shifts[eye])
            warped = True
        if warped:
            corrected_n += 1
        else:
            # face found but nothing moved: the smoothed offset sat
            # inside the deadband (below the noise floor) — the
            # frame is untouched by design, and it counts as such.
            skipped_n += 1
        cv2.imwrite(os.path.join(plates_dir, f"p{i + 1:04d}.png"), out)
    plate = os.path.join(tmp, "eyecorrect_plate.mp4")
    _assemble_plate(plates_dir, plate, sample_fps, src_fps)
    proj = new_project(name, w, h, int(round(src_fps)))
    proj["clips"] = [{"src": plate, "duration": dur, "fit": "cover"}]
    Renderer(proj, workdir=tmp).render(dst)
    return {"frames_total": n_needed, "frames_corrected": corrected_n,
            "frames_untouched": skipped_n, "sample_fps": sample_fps,
            "duration": dur}
