"""MuseCut vocal separation — UVR MDX-Net (UVR-MDX-NET-Inst_HQ_3), CPU.

Round 7 (v0.9.0): VERIFIED on a real known mix (real voiceover +
real music bed, 12 s): instrumental vs bed corr 0.976, vocals vs
voiceover corr 0.989, cross-correlations 0.026 / 0.027, chunk
junctions hold (0.982-0.985 / 0.996-0.997). See
``~/workspace/your_files/musecut-round7-vocalsep-proof.md``.

Method — pinned to the actual UVR source
(Anjok07/ultimatevocalremovergui, master branch), NOT guessed:
``lib_v5/tfc_tdf_v3.py`` ``STFT`` + ``separate.py``
``SeperateMDX.demix`` / ``run_model``. Round 6 failed three times
by treating the model output as a *mask* (complex-ratio-mask,
numpy-STFT mask) or by feeding it a scipy STFT with wrong
parameters (default nperseg=256, wrong window/boundary). The
correct interpretation is:

* STFT: ``torch.stft(n_fft=6144, hop=1024, periodic Hann,
  center=True, return_complex=False)``. Channels are
  ``[L-real, L-imag, R-real, R-imag]`` (the real/imag dim is
  per stereo channel — ``x.permute([0, 3, 1, 2])`` then reshape).
  Only the first 3072 of 3073 bins are kept (``x[..., :dim_f, :]``).
* The lowest 3 frequency bins of the INPUT spectrogram are zeroed
  (``spek[:, :, :3, :] *= 0``) before the model runs.
* The model output is the TARGET STEM's complex spectrogram
  directly — same 4-channel layout — NOT a mask. Inverse STFT
  gives the waveform; the result is multiplied by ``compensate``
  (1.035 for this model). No input normalisation anywhere.
* For ``UVR-MDX-NET-Inst_HQ_3`` the primary stem is
  Instrumental; vocals are ``mix - instrumental`` (UVR's
  ``mix.T - source.T`` when ``invert_spect`` is off).
* Chunking (``overlap == "Default"``): ``chunk_size =
  hop * (segment_size - 1)`` = 261120 samples (5.92 s),
  ``step = chunk_size - n_fft`` = 254976, output chunks are
  Hann-windowed (``np.hanning`` — symmetric) and overlap-added
  with a divider, then ``trim = n_fft // 2`` samples are cut from
  each end of the concatenated result.

No torch on this VM: the STFT/ISTFT are re-implemented in numpy
with the exact torch semantics (zero centre-pad, periodic Hann,
overlap-add with window-envelope normalisation, centre trim).
Round-trip ``istft(stft(x))`` reproduces ``x`` at corr 0.99995.

Model: ``UVR-MDX-NET-Inst_HQ_3.onnx``, 66,759,214 bytes,
sha256 ``317554b07fe1ea5279a77f2b1520a41ea4b93432560c4ffd08792c30fddf9adc``.
The HuggingFace mirror (``TRvlvr/model_repo``) now returns 401
("Invalid username or password"), so the download used the
canonical GitHub release instead:
``https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Inst_HQ_3.onnx``
(same size + hash — verified). The model file lives in
``musecut/models/`` (durable; ``~/.cache`` gets wiped by VM
replacements).

Cost: ~15 s of CPU inference per 5.92 s chunk on this VM, so a
60 s clip takes ~3 min. No GPU path yet.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import wave
from typing import Optional, Tuple

from .audio import ffmpeg_env

MODEL_FILE = "UVR-MDX-NET-Inst_HQ_3.onnx"
MODEL_SIZE = 66759214
MODEL_SHA256 = ("317554b07fe1ea5279a77f2b1520a41ea4b93432560c4ffd08792"
                "c30fddf9adc")
MODEL_URL = ("https://github.com/TRvlvr/model_repo/releases/download/"
             "all_public_uvr_models/UVR-MDX-NET-Inst_HQ_3.onnx")

SR = 44100
N_FFT = 6144
HOP = 1024
DIM_F = 3072
DIM_T = 256
TRIM = N_FFT // 2
CHUNK = HOP * (DIM_T - 1)          # 261120 samples = 5.92 s per chunk
GEN = CHUNK - 2 * TRIM             # 254976 samples kept per chunk
COMPENSATE = 1.035

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_MODEL_DIR = os.path.join(_ROOT, "models")
CACHE_MODEL_DIR = os.path.expanduser("~/.cache/musecut-models")


class VocalSepError(RuntimeError):
    """Raised when vocal separation cannot run (no model, bad input)."""


def model_path(model: Optional[str] = None) -> str:
    """Filesystem path of the MDX model (workspace copy preferred)."""
    if model:
        return model
    cache = os.path.join(CACHE_MODEL_DIR, MODEL_FILE)
    if os.path.isfile(cache):
        return cache
    return os.path.join(WORKSPACE_MODEL_DIR, MODEL_FILE)


def model_ready(model: Optional[str] = None) -> bool:
    """True when onnxruntime imports AND the model file is on disk."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    path = model_path(model)
    return os.path.isfile(path) and os.path.getsize(path) == MODEL_SIZE


def verify_model(path: str) -> bool:
    """True when ``path`` is the expected model (size + sha256)."""
    if not os.path.isfile(path) or os.path.getsize(path) != MODEL_SIZE:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest() == MODEL_SHA256


def _load_session(model: Optional[str] = None):
    import onnxruntime as ort
    path = model_path(model)
    if not os.path.isfile(path):
        raise VocalSepError(
            f"vocal-separation model not downloaded: {path!r}. "
            f"Fetch {MODEL_FILE} ({MODEL_SIZE} bytes, sha256 "
            f"{MODEL_SHA256[:16]}...) from {MODEL_URL} into "
            f"{WORKSPACE_MODEL_DIR}/ — see musecut/vocalsep.py.")
    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


def _hann_periodic(n: int):
    """Periodic Hann window (torch.hann_window(periodic=True))."""
    import numpy as np
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


def stft(x):
    """Stereo waveform (2, T) -> spectrogram (4, 3072, frames).

    Matches ``torch.stft(n_fft=6144, hop=1024, window=periodic
    Hann, center=True, return_complex=False)`` + UVR's reshape:
    channels [L-real, L-imag, R-real, R-imag], first 3072 bins.
    """
    import numpy as np
    x = np.asarray(x, dtype=np.float32)
    if x.ndim != 2 or x.shape[0] != 2:
        raise ValueError(f"stft needs stereo (2, T), got shape {x.shape}")
    t = x.shape[1]
    win = _hann_periodic(N_FFT).astype(np.float32)
    padded = np.pad(x, ((0, 0), (TRIM, TRIM)))
    n_frames = 1 + t // HOP
    idx = np.arange(N_FFT)[None, :] + (np.arange(n_frames) * HOP)[:, None]
    frames = padded[:, idx] * win[None, None, :]
    spec = np.fft.rfft(frames, n=N_FFT, axis=-1)[:, :, :DIM_F]
    spec = spec.transpose(0, 2, 1)                     # (2, 3072, frames)
    out = np.empty((4, DIM_F, n_frames), dtype=np.float32)
    out[0] = spec[0].real
    out[1] = spec[0].imag
    out[2] = spec[1].real
    out[3] = spec[1].imag
    return out


def istft(s):
    """Spectrogram (4, 3072, frames) -> stereo waveform (2, T).

    Matches ``torch.istft(n_fft=6144, hop=1024, window=periodic
    Hann, center=True)`` with default length ``(frames-1)*hop``:
    zero-pad the 3073rd bin, overlap-add with window-envelope
    normalisation, centre trim of ``n_fft // 2`` each side.
    """
    import numpy as np
    s = np.asarray(s, dtype=np.float32)
    if s.ndim != 3 or s.shape[0] != 4 or s.shape[1] != DIM_F:
        raise ValueError(f"istft needs (4, 3072, frames), got {s.shape}")
    n_frames = s.shape[2]
    win = _hann_periodic(N_FFT).astype(np.float32)
    cx = np.zeros((2, N_FFT // 2 + 1, n_frames), dtype=np.complex64)
    cx[0, :DIM_F] = s[0] + 1j * s[1]
    cx[1, :DIM_F] = s[2] + 1j * s[3]
    frames = np.fft.irfft(cx, n=N_FFT, axis=1) * win[None, :, None]
    total = (n_frames - 1) * HOP + N_FFT
    buf = np.zeros((2, total), dtype=np.float64)
    env = np.zeros(total, dtype=np.float64)
    w2 = win.astype(np.float64) ** 2
    for t in range(n_frames):
        sl = slice(t * HOP, t * HOP + N_FFT)
        buf[:, sl] += frames[:, :, t]
        env[sl] += w2
    out = np.zeros_like(buf)
    nz = env > 1e-8
    out[:, nz] = buf[:, nz] / env[nz]
    return out[:, TRIM:TRIM + (n_frames - 1) * HOP].astype(np.float32)


def _run_chunk(session, mix_part):
    """One 5.92 s chunk: (2, CHUNK) -> instrumental (2, CHUNK)."""
    spec = stft(mix_part)
    spec[:, :3, :] = 0.0                    # UVR zeroes the lowest bins
    out = session.run(None, {"input": spec[None, :, :, :]})[0][0]
    return istft(out)


def demix(session, mix, overlap=None):
    """Full mix (2, T) -> instrumental (2, T), UVR demix loop.

    ``overlap=None`` is UVR's ``"Default"`` (step = CHUNK - N_FFT);
    a float 0..0.99 gives ``step = int((1 - overlap) * CHUNK)``.
    """
    import numpy as np
    mix = np.asarray(mix, dtype=np.float32)
    if mix.ndim != 2 or mix.shape[0] != 2:
        raise ValueError(f"demix needs stereo (2, T), got {mix.shape}")
    if overlap is None:
        step = CHUNK - N_FFT
    else:
        overlap = float(overlap)
        if not 0.0 <= overlap < 1.0:
            raise ValueError(f"overlap must be 0..0.99, got {overlap}")
        step = int((1.0 - overlap) * CHUNK)
    t = mix.shape[1]
    pad = GEN + TRIM - (t % GEN)
    mixture = np.concatenate(
        (np.zeros((2, TRIM), dtype=np.float32), mix,
         np.zeros((2, pad), dtype=np.float32)), axis=1)
    total = mixture.shape[1]
    result = np.zeros((1, 2, total), dtype=np.float32)
    divider = np.zeros((1, 2, total), dtype=np.float32)
    for i in range(0, total, step):
        start, end = i, min(i + CHUNK, total)
        actual = end - start
        window = np.hanning(actual)              # symmetric, as in UVR
        window = np.tile(window[None, None, :], (1, 2, 1))
        part = mixture[:, start:end]
        if end != i + CHUNK:
            part = np.concatenate(
                (part, np.zeros((2, (i + CHUNK) - end), dtype=np.float32)),
                axis=-1)
        tar = _run_chunk(session, part)[None, :, :]
        tar[..., :actual] *= window
        divider[..., start:end] += window
        result[..., start:end] += tar[..., :end - start]
    out = np.divide(result, divider, out=np.zeros_like(result),
                    where=divider > 0)
    out = out[:, :, TRIM:-TRIM][:, :, :t]
    return (out * COMPENSATE)[0]


def _decode_to_wav(src: str, dst: str) -> None:
    """ffmpeg-decode any audio file to stereo 44.1 kHz PCM wav."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-i", src, "-vn", "-ac", "2", "-ar", str(SR),
           "-c:a", "pcm_s16le", dst]
    subprocess.run(cmd, check=True, env=ffmpeg_env())


def _read_wav(path: str):
    """PCM16 wav -> float32 stereo (2, T), stdlib only."""
    import numpy as np
    with wave.open(path, "rb") as w:
        nch, width, n = w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise VocalSepError(f"expected 16-bit wav, got {width * 8}-bit: "
                            f"{path!r}")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    data = data.reshape(-1, nch).T
    if nch == 1:
        data = np.repeat(data, 2, axis=0)
    elif nch > 2:
        data = data[:2]
    return np.ascontiguousarray(data)


def _write_wav(path: str, data) -> None:
    """Float32 stereo (2, T) -> PCM16 wav (clips at +-1, as UVR does)."""
    import numpy as np
    data = np.asarray(data, dtype=np.float32)
    pcm = (np.clip(data.T, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def separate(src_wav: str, workdir: str,
             model: Optional[str] = None,
             overlap: Optional[float] = None
             ) -> Tuple[str, str]:
    """Separate ``src_wav`` into vocals + instrumental.

    Decodes to stereo 44.1 kHz, runs the UVR MDX-Net demix loop
    (instrumental stem), and derives vocals as ``mix - inst``.
    Writes ``vocals.wav`` and ``instrumental.wav`` into ``workdir``
    and returns ``(vocals_path, instrumental_path)``.

    Raises ``FileNotFoundError`` for a missing source and
    ``VocalSepError`` when the model file is absent.
    """
    if not os.path.isfile(src_wav):
        raise FileNotFoundError(f"no such audio file: {src_wav!r}")
    os.makedirs(workdir, exist_ok=True)
    session = _load_session(model)
    decoded = os.path.join(workdir, "_decoded.wav")
    _decode_to_wav(src_wav, decoded)
    mix = _read_wav(decoded)
    inst = demix(session, mix, overlap=overlap)
    voc = mix - inst
    voc_path = os.path.join(workdir, "vocals.wav")
    inst_path = os.path.join(workdir, "instrumental.wav")
    _write_wav(voc_path, voc)
    _write_wav(inst_path, inst)
    try:
        os.remove(decoded)
    except OSError:
        pass
    return voc_path, inst_path
