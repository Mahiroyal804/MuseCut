"""Lip sync (Wav2Lip) — drive a face's lips from an audio track.

The model is the genuine Wav2Lip generator exported to ONNX at 256x256
(``musecut/models/wav2lip_256.onnx``, 214,402,122 bytes, sha256
``bfeb0ab1ef3097f456f6fdcd506d3b32ee8a42f762a6722b42d0f1ca5b64e83c``),
a public mirror (``vnalex/wav2lip-256-onnx`` on HuggingFace) of the
archived ``instant-high/wav2lip-onnx-256`` export, itself adapted from
``Kedreamix/Linly-Talker``. The mirror README documents the exact
inputs the model expects: mel spectrogram ``[1, 1, 80, 16]`` and face
``[1, 6, 256, 256]`` (3 channels masked face + 3 channels reference
face); output is the full face ``[1, 3, 256, 256]`` in [0, 1]. Both
were verified here by loading the file in onnxruntime and running it
before any wiring (see the round-13 probe log).

Pipeline (no new heavy dependency — numpy + cv2 + onnxruntime, all
already in the video venv):
  audio -> 16 kHz mono -> Wav2Lip mel (numpy STFT, reflect padding,
  Slaney melbank, n_fft=800/hop=200/win=800, 80 mels, fmin=55,
  fmax=7600, 20*log10, ref 20 dB, min -100 dB, symmetric normalise
  to [-4, 4]) -> 16-frame mel chunks, one per video frame
  face  -> Haar-cascade detection -> padded 256x256 crop -> masked
  (lower half zeroed) + reference stacked as 6 channels
  model -> regenerated 256x256 face -> feathered lower-half paste
  back into the original frame (upper face stays the original
  pixels; only the mouth/jaw region is replaced, with a 20 px
  vertical feather ramp so no box edge shows).

Honest scope (measured, not assumed):
  - ~1 s of CPU per frame on this VM (1.5 shared CPUs), so a 2 s
    clip at 25 fps costs ~1 min and a long clip costs hours — the
    module takes ``seconds`` to cap the proof length and documents
    the cost; it does not pretend full-length clips are practical.
  - The regenerated lower half is slightly softer than the
    original and can carry a faint smudge under the lip on some
    frames (visible in the round-13 probe frame check). Identity,
    hair and background are preserved — the model only redraws
    the mouth region.
  - Face detection is a Haar cascade: it needs a roughly frontal,
    well-lit face of >=60 px. No face found -> LipSyncError, never
    a silent pass-through. On a clip, a frame with no face (B-roll
    after a cut) passes through untouched — the first clip proof
    pasted a stale box onto the cityscape before this was fixed.
    Haar can also miss a real face on heavily composited frames
    (the reel at 5 s: only an 87 px false positive over a keyboard
    overlay, speaker missed) — pick a clean segment instead.
  - The mel path was cross-checked three ways in the round-13
    probe: numpy STFT (reflect) vs torch STFT max|d| = 0.060,
    mean|d| = 0.001; librosa vs torch corr 0.987. The numpy path
    used here is the one proven equivalent to the torch path that
    produced the verified outputs. librosa is installed in the
    video venv as a cross-check tool, not a runtime dependency.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import wave
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

MODEL_FILE = "wav2lip_256.onnx"
MODEL_SIZE = 214402122
MODEL_SHA256 = ("bfeb0ab1ef3097f456f6fdcd506d3b32ee8a42f762a6722b42d0f1ca5b64e83c")
MODEL_URL = "https://huggingface.co/vnalex/wav2lip-256-onnx"

SR = 16000
N_FFT, HOP, WIN = 800, 200, 800
N_MELS, FMIN, FMAX = 80, 55, 7600
MEL_STEP = 16
MEL_FPS = SR // HOP  # 80 mel frames per second
DEFAULT_FPS = 25
# Feather ramp for the paste: rows 120..140 of the 256 crop blend
# from original (above) to generated (below).
FEATHER_TOP, FEATHER_BOTTOM = 120, 140


class LipSyncError(Exception):
    """Raised when lip sync cannot run (no model, no face, bad audio)."""


def _require_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise LipSyncError(
            "lip sync needs OpenCV (cv2) — not installed in this python"
        ) from exc
    if not hasattr(cv2, "CascadeClassifier"):
        raise LipSyncError(
            "lip sync needs cv2.CascadeClassifier — this cv2 build has none "
            "(use the video venv, ~/workspace/.venv-video/bin/python)"
        )
    return cv2


def _require_ort():
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise LipSyncError(
            "lip sync needs onnxruntime — not installed in this python"
        ) from exc
    return ort


def model_path() -> str:
    """Where the model lives — same resolution as providers: the
    durable workspace copy first, then the cache."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ws_path = os.path.join(here, "models", MODEL_FILE)
    if os.path.isfile(ws_path):
        return ws_path
    return os.path.join(os.path.expanduser("~/.cache/musecut-models"),
                        MODEL_FILE)


_session = None


def _load_session():
    global _session
    if _session is not None:
        return _session
    ort = _require_ort()
    path = model_path()
    if not os.path.isfile(path):
        raise LipSyncError(f"lip-sync model missing: {path!r}")
    if os.path.getsize(path) != MODEL_SIZE:
        raise LipSyncError(
            f"lip-sync model size mismatch: {os.path.getsize(path)} != "
            f"{MODEL_SIZE} — re-download from {MODEL_URL}")
    _session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return _session


# ---------------------------------------------------------------- mel ---

def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    f = np.asarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, min_log_mel = 1000.0, 1000.0 / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = f >= min_log_hz
    return np.where(log_t, min_log_mel + np.log(f / min_log_hz) / logstep, mels)


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = m * f_sp
    min_log_hz, min_log_mel = 1000.0, 1000.0 / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = m >= min_log_mel
    return np.where(log_t, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def _mel_filterbank() -> np.ndarray:
    """Slaney melbank (80, 401) — matches librosa.filters.mel defaults."""
    fftfreqs = np.linspace(0, SR / 2, 1 + N_FFT // 2)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(FMIN), _hz_to_mel(FMAX), N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[None, :] - fftfreqs[:, None]
    weights = np.zeros((len(fftfreqs), N_MELS))
    for i in range(N_MELS):
        weights[:, i] = np.maximum(0, np.minimum(-ramps[:, i] / fdiff[i],
                                                 ramps[:, i + 2] / fdiff[i + 1]))
    enorm = 2.0 / (mel_f[2:N_MELS + 2] - mel_f[:N_MELS])
    return (weights * enorm).T


def load_wav_16k(path: str) -> np.ndarray:
    """Load a WAV as float32 mono at 16 kHz (stdlib wave; PCM16/32)."""
    if not os.path.isfile(path):
        raise LipSyncError(f"audio not found: {path!r}")
    try:
        with wave.open(path, "rb") as w:
            n, sr, ch, sw = (w.getnframes(), w.getframerate(),
                             w.getnchannels(), w.getsampwidth())
            raw = w.readframes(n)
    except (wave.Error, EOFError) as exc:
        raise LipSyncError(f"cannot read WAV {path!r}: {exc}") from exc
    if sw == 2:
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        y = np.frombuffer(raw, dtype=np.float32).copy()
    else:
        raise LipSyncError(f"unsupported WAV sample width {sw} in {path!r}")
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        idx = np.linspace(0, len(y) - 1, max(1, int(len(y) * SR / sr)))
        y = np.interp(idx, np.arange(len(y)), y).astype(np.float32)
    if len(y) == 0:
        raise LipSyncError(f"empty audio: {path!r}")
    return y


def mel_spectrogram(wav: np.ndarray) -> np.ndarray:
    """Wav2Lip mel: (80, T) float32 in [-4, 4].

    Numpy STFT with reflect padding (centre=True), Hann window,
    magnitude, Slaney melbank, 20*log10, ref 20 dB, min -100 dB,
    symmetric normalisation. Cross-checked against torch STFT
    (max|d| 0.060) and librosa (corr 0.987) in the round-13 probe.
    """
    pad = N_FFT // 2
    yp = np.pad(wav, (pad, pad), mode="reflect")
    win = np.hanning(WIN)
    n_frames = 1 + (len(yp) - WIN) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(yp, WIN)[::HOP][:n_frames]
    spec = np.fft.rfft(frames * win, n=N_FFT, axis=1)
    mag = np.abs(spec).T  # (401, T)
    mel = _mel_filterbank() @ mag
    db = 20.0 * np.log10(np.maximum(mel, 1e-10)) - 20.0
    return (np.clip((db + 100.0) / 100.0, 0, 1) * 8.0 - 4.0).astype(np.float32)


def mel_chunks(mel: np.ndarray, fps: float, n_frames: int) -> List[np.ndarray]:
    """One 16-frame mel chunk per video frame (Wav2Lip chunking)."""
    chunks = []
    for i in range(n_frames):
        start = int(i * MEL_FPS / fps)
        chunk = mel[:, start:start + MEL_STEP]
        if chunk.shape[1] < MEL_STEP:
            chunk = np.pad(chunk, ((0, 0), (0, MEL_STEP - chunk.shape[1])))
        chunks.append(chunk)
    return chunks


# --------------------------------------------------------------- face ---

def detect_face(img_bgr: np.ndarray) -> Tuple[int, int, int, int]:
    """Haar-cascade face box, padded 12%, as (x0, y0, x1, y1).

    The LARGEST detected face wins: on busy composited frames the
    cascade can fire a small false positive (the round-13 clip
    proof first picked an 87 px box over a keyboard overlay while
    the speaker's ~200 px face went undetected), and the speaker
    is the largest face in a talking-head shot.
    """
    cv2 = _require_cv2()
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    casc = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = casc.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
    if len(faces) == 0:
        raise LipSyncError("no face detected (Haar cascade found none)")
    x, y, w, h = [int(v) for v in max(faces, key=lambda f: f[2] * f[3])]
    pad = int(0.12 * w)
    return (max(0, x - pad), max(0, y - pad),
            min(img_bgr.shape[1], x + w + pad),
            min(img_bgr.shape[0], y + h + pad))


def face_input(face256_bgr: np.ndarray) -> np.ndarray:
    """256x256 BGR crop -> (1, 6, 256, 256) float32 [0, 1].

    Channels 0-2: masked face (lower half zeroed); channels 3-5:
    the reference face. This is the exact input layout the ONNX
    model documents.
    """
    rgb = np.ascontiguousarray(
        face256_bgr[:, :, ::-1].astype(np.float32) / 255.0)
    masked = rgb.copy()
    masked[128:, :, :] = 0.0
    six = np.concatenate([masked, rgb], axis=2)
    return np.transpose(six, (2, 0, 1))[None, :, :, :].astype(np.float32)


def lipsync_face(face256_bgr: np.ndarray, mel_chunk: np.ndarray,
                 session: Any = None) -> np.ndarray:
    """One model call: 256x256 BGR face + (80, 16) mel -> 256x256 BGR out."""
    sess = session or _load_session()
    in_mel, in_face = sess.get_inputs()[0].name, sess.get_inputs()[1].name
    out = sess.run(None, {in_mel: mel_chunk[None, None, :, :].astype(np.float32),
                          in_face: face_input(face256_bgr)})[0]
    rgb = (out[0].transpose(1, 2, 0) * 255.0).clip(0, 255).astype(np.uint8)
    return rgb[:, :, ::-1]  # back to BGR


def paste_face(frame_bgr: np.ndarray, box: Tuple[int, int, int, int],
               out256_bgr: np.ndarray) -> np.ndarray:
    """Paste the generated face back, feathered lower half only.

    The upper face keeps the original pixels; the mouth/jaw region
    is replaced with a vertical feather ramp (rows 120..140 of the
    256 crop) so no box edge shows. Outside the box the frame is
    byte-identical.
    """
    cv2 = _require_cv2()
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    gen = cv2.resize(out256_bgr, (w, h))
    alpha = np.zeros((256, 1), dtype=np.float32)
    alpha[FEATHER_TOP:FEATHER_BOTTOM, 0] = np.linspace(
        0, 1, FEATHER_BOTTOM - FEATHER_TOP, dtype=np.float32)
    alpha[FEATHER_BOTTOM:, 0] = 1.0
    alpha = cv2.resize(alpha, (w, h))[:, :, None]
    region = frame_bgr[y0:y1, x0:x1].astype(np.float32)
    blended = region * (1.0 - alpha) + gen.astype(np.float32) * alpha
    out = frame_bgr.copy()
    out[y0:y1, x0:x1] = blended.clip(0, 255).astype(np.uint8)
    return out


# -------------------------------------------------------------- render ---

def _assemble_plate(frames_dir: str, dst: str, fps: float,
                    wav_path: Optional[str]) -> str:
    """Assemble processed PNG frames into a plate video (crf 18,
    yuv420p — same pattern as superres/inpaint plates), with the
    driving audio muxed in when given so the deliverable carries
    sound. The plate is an intermediate; the deliverable itself is
    rendered through the real engine by the caller."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-framerate", str(fps),
           "-i", os.path.join(frames_dir, "p%04d.png")]
    if wav_path:
        cmd += ["-i", wav_path]
    cmd += ["-vf", f"fps={fps:.3f},format=yuv420p",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]
    if wav_path:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [dst]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise LipSyncError(
            f"lip-sync plate assembly failed for {dst}: "
            f"{(proc.stderr or '')[-1000:]}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise LipSyncError(f"lip-sync plate assembly produced no file: {dst}")
    return dst


def _render_plate(plate: str, dst: str, w: int, h: int, fps: float,
                  dur: float, tmp: str, name: str) -> str:
    """Render the plate through the real engine (single clip, cover)."""
    from .render import Renderer
    from .timeline import new_project
    proj = new_project(name, w, h, int(round(fps)))
    proj["clips"] = [{"src": plate, "duration": dur, "fit": "cover"}]
    Renderer(proj, workdir=tmp).render(dst)
    return dst


def lipsync_image(photo: str, wav: str, dst: str, fps: float = DEFAULT_FPS,
                  seconds: Optional[float] = None,
                  workdir: Optional[str] = None,
                  name: str = "lipsync") -> str:
    """Animate a still photo's lips from an audio track.

    Detects the face once, runs the model per frame at ``fps`` for
    ``seconds`` (default: the audio length), pastes each generated
    face back into the photo, and renders through the real engine
    with the audio attached. Returns ``dst``.
    """
    cv2 = _require_cv2()
    if not os.path.isfile(photo):
        raise LipSyncError(f"photo not found: {photo!r}")
    img = cv2.imread(photo)
    if img is None:
        raise LipSyncError(f"cannot read photo: {photo!r}")
    h, w = img.shape[:2]
    wav_arr = load_wav_16k(wav)
    dur = len(wav_arr) / SR if seconds is None else min(seconds, len(wav_arr) / SR)
    if dur <= 0:
        raise LipSyncError("audio too short to lip-sync")
    n_frames = max(1, int(math.ceil(dur * fps)))
    dur = n_frames / fps
    mel = mel_spectrogram(wav_arr)
    chunks = mel_chunks(mel, fps, n_frames)
    box = detect_face(img)
    face256 = cv2.resize(img[box[1]:box[3], box[0]:box[2]], (256, 256))
    session = _load_session()
    root = os.path.expanduser("~/workspace/musecut/.tmp")
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-lipsync-", dir=root)
    os.makedirs(tmp, exist_ok=True)
    frames_dir = os.path.join(tmp, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    for i, chunk in enumerate(chunks):
        out256 = lipsync_face(face256, chunk, session)
        frame = paste_face(img, box, out256)
        cv2.imwrite(os.path.join(frames_dir, f"p{i + 1:04d}.png"), frame)
    plate = os.path.join(tmp, "lipsync_plate.mp4")
    _assemble_plate(frames_dir, plate, fps, wav)
    return _render_plate(plate, dst, w, h, fps, dur, tmp, name)


def lipsync_clip(video: str, wav: str, dst: str, fps: float = DEFAULT_FPS,
                 seconds: Optional[float] = None,
                 workdir: Optional[str] = None,
                 detect_every: int = 5,
                 name: str = "lipsync") -> str:
    """Lip-sync a talking-head clip from an audio track.

    Frames are extracted at ``fps`` (full rate — holding frames
    would break sync), the face is re-detected every
    ``detect_every`` frames (last box reused in between), each
    frame is run through the model and pasted back, and the result
    is rendered through the real engine with the audio attached.
    ``seconds`` caps the length (default: the shorter of the video
    and the audio). Returns ``dst``.
    """
    from .render import probe_media
    from .track import _extract_frames
    cv2 = _require_cv2()
    if not os.path.isfile(video):
        raise LipSyncError(f"video not found: {video!r}")
    info = probe_media(video)
    w, h = info["width"], info["height"]
    src_dur = info["duration"]
    if w <= 0 or h <= 0 or src_dur <= 0:
        raise LipSyncError(f"cannot probe usable video: {video!r}")
    wav_arr = load_wav_16k(wav)
    audio_dur = len(wav_arr) / SR
    dur = min(src_dur, audio_dur)
    if seconds is not None:
        dur = min(dur, seconds)
    if dur <= 0:
        raise LipSyncError("nothing to lip-sync (zero duration)")
    n_frames = max(1, int(math.ceil(dur * fps)))
    dur = n_frames / fps
    mel = mel_spectrogram(wav_arr)
    chunks = mel_chunks(mel, fps, n_frames)
    session = _load_session()
    root = os.path.expanduser("~/workspace/musecut/.tmp")
    tmp = workdir or tempfile.mkdtemp(prefix="musecut-lipsync-", dir=root)
    os.makedirs(tmp, exist_ok=True)
    # Trim first: _extract_frames pulls dur*fps frames for the WHOLE
    # file, so a long reel would extract tens of thousands of frames.
    trimmed = os.path.join(tmp, "trimmed.mp4")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-v", "error",
         "-i", video, "-t", f"{dur:.3f}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", trimmed],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise LipSyncError(
            f"lip-sync trim failed for {video!r}: {(proc.stderr or '')[-500:]}")
    frames = _extract_frames(trimmed, fps, tmp)
    if not frames:
        raise LipSyncError(f"no frames extracted from {video!r}")
    frames_dir = os.path.join(tmp, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    box: Optional[Tuple[int, int, int, int]] = None
    face_found = False
    for i in range(n_frames):
        _, path = frames[min(i, len(frames) - 1)]
        img = cv2.imread(path)
        if img is None:
            raise LipSyncError(f"cannot read extracted frame: {path}")
        if i % detect_every == 0 or box is None:
            try:
                box = detect_face(img)
                face_found = True
            except LipSyncError:
                if box is None:
                    raise
                # No face on this frame (e.g. B-roll after a cut):
                # pass the frame through untouched. Never paste a
                # stale box onto a new scene — the round-13 clip
                # proof first did exactly that (a softened patch on
                # the cityscape, box diff 1.83) before this fix.
                face_found = False
        if not face_found:
            cv2.imwrite(os.path.join(frames_dir, f"p{i + 1:04d}.png"), img)
            continue
        assert box is not None
        face256 = cv2.resize(img[box[1]:box[3], box[0]:box[2]], (256, 256))
        out256 = lipsync_face(face256, chunks[i], session)
        frame = paste_face(img, box, out256)
        cv2.imwrite(os.path.join(frames_dir, f"p{i + 1:04d}.png"), frame)
    plate = os.path.join(tmp, "lipsync_plate.mp4")
    _assemble_plate(frames_dir, plate, fps, wav)
    return _render_plate(plate, dst, w, h, fps, dur, tmp, name)


def measure_lipsync(photo: str, wav: str) -> Dict[str, Any]:
    """Probe metrics for one photo + audio pair (no render).

    determinism: same mel twice -> mouth mean abs diff (must be ~0).
    audio_variance: mouth diff between two different audio times
    (must be clearly > 0 — the mouth follows the audio).
    speech_vs_silence: mouth diff speech vs flat -4 mel.
    """
    cv2 = _require_cv2()
    if not os.path.isfile(photo):
        raise LipSyncError(f"photo not found: {photo!r}")
    img = cv2.imread(photo)
    box = detect_face(img)
    face256 = cv2.resize(img[box[1]:box[3], box[0]:box[2]], (256, 256))
    wav_arr = load_wav_16k(wav)
    mel = mel_spectrogram(wav_arr)
    chunks = mel_chunks(mel, DEFAULT_FPS, 30)
    session = _load_session()

    def mouth_diff(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.abs(a[170:235, 90:170].astype(np.float32)
                            - b[170:235, 90:170].astype(np.float32)).mean())

    o1 = lipsync_face(face256, chunks[5], session)
    o1b = lipsync_face(face256, chunks[5], session)
    o2 = lipsync_face(face256, chunks[15], session)
    silent = np.full((80, MEL_STEP), -4.0, dtype=np.float32)
    o_sil = lipsync_face(face256, silent, session)
    return {
        "determinism_mouth_diff": mouth_diff(o1, o1b),
        "audio_variance_mouth_diff": mouth_diff(o1, o2),
        "speech_vs_silence_mouth_diff": mouth_diff(o2, o_sil),
        "mel_shape": list(mel.shape),
        "mel_range": [float(mel.min()), float(mel.max())],
    }
