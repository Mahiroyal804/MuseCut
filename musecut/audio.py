"""MuseCut audio tools.

All processing is ffmpeg filter based; this module builds the filter
fragments the render engine stitches into its graph, plus a few
standalone helpers (beat markers, audio extraction).

* Ducking — music ducks under voice with ``sidechaincompress`` (the
  standard broadcast technique: the voice track is the sidechain).
* Voice changer — pitch/formant via ``rubberband`` (keeps tempo) or
  ``asetrate`` (classic chipmunk). Presets: chipmunk / deep / robot.
* Noise reduction — ``afftdn`` (FFT denoise, built into ffmpeg).
* Beat sync — onset detection on a decoded energy envelope, pure
  Python (no aubio dependency): decode to mono wav, RMS per 10 ms
  window, local maxima above an adaptive threshold.
* Extraction — pull the audio track out of any video file.
* Volume balancing — per-track ``loudnorm`` (EBU R128, single-pass
  linear mode) so music/VO sit at one target loudness, the way
  CapCut's "balances other clips' volume" does.
* Audio mixer (pro) — per-track 3-band EQ (lowshelf / parametric /
  highshelf), compressor (``acompressor``), and keyframed volume
  (``volume`` with ``eval=frame``) — see :func:`eq_filter`,
  :func:`compressor_filter`, :func:`volume_keyframes_filter`.
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import wave
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import keyframes as kf_mod

VOICE_CHANGE_PRESETS = {
    "chipmunk": {"pitch": 1.6},
    "deep": {"pitch": 0.7},
    "robot": {"pitch": 0.9, "robot": True},
    "slow-mo": {"pitch": 0.8, "tempo": 0.8},
}


def ffmpeg_env() -> Dict[str, str]:
    """Proxy-safe environment for ffmpeg on this VM."""
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return env


def voice_change_filter(spec: Dict[str, Any]) -> str:
    """Build a voice-change filter chain from a spec dict.

    ``{"pitch": 1.5}`` uses rubberband (tempo preserved).
    ``{"formant": "chipmunk"|"deep"|"robot"}`` uses presets.
    """
    pitch = spec.get("pitch")
    tempo = spec.get("tempo", 1.0)
    if "formant" in spec:
        preset = VOICE_CHANGE_PRESETS.get(spec["formant"])
        if preset is None:
            raise ValueError(f"unknown voice formant: {spec['formant']!r}")
        pitch = preset.get("pitch", pitch)
        tempo = preset.get("tempo", tempo)
        if preset.get("robot"):
            # Robot: pitch shift + a touch of tremolo for the metallic edge.
            return f"rubberband=pitch={pitch}:tempo={tempo},tremolo=f=55:d=0.4"
    if pitch is None:
        raise ValueError("voice_change needs 'pitch' or 'formant'")
    if not 0.25 <= pitch <= 4.0:
        raise ValueError("voice_change pitch must be 0.25..4.0")
    return f"rubberband=pitch={pitch}:tempo={tempo}"


def denoise_filter(strength: float = 1.0) -> str:
    """afftdn denoise filter; strength scales the noise floor."""
    nf = -50 + min(max(strength, 0.0), 2.0) * -10  # -50..-70 dB
    return f"afftdn=nr={12 * strength:.0f}:nf={nf:.0f}"


def ducking_filter(threshold: float = 0.02, ratio: float = 8.0,
                   attack: int = 50, release: int = 500) -> str:
    """sidechaincompress fragment (music input, voice is the sidechain)."""
    return (
        f"sidechaincompress=threshold={threshold}:ratio={ratio}:"
        f"attack={attack}:release={release}:level_sc=1.0"
    )


def balance_filter(target_i: float = -16.0, target_tp: float = -1.5,
                   target_lra: float = 11.0) -> str:
    """Per-track loudness balancing (EBU R128, single-pass loudnorm).

    ``target_i`` is the integrated loudness in LUFS (-70..-5, default
    -16 — the level streaming video uses); ``target_tp`` the true-peak
    ceiling in dBTP; ``target_lra`` the loudness range. Single-pass
    with ``linear=true`` needs no measured values, so it works inline
    in a render graph.
    """
    if not -70.0 <= target_i <= -5.0:
        raise ValueError(f"balance target_i must be -70..-5, got {target_i}")
    return (f"loudnorm=I={target_i:.1f}:TP={target_tp:.1f}:"
            f"LRA={target_lra:.1f}:linear=true")


def vocal_beautify_filter(strength: float = 1.0,
                          remove_breaths: bool = False) -> str:
    """Classical vocal-polish chain (round 10, v0.12.0) — no ML model.

    highpass (rumble) → afftdn (room noise) → deesser (sibilance) →
    presence EQ (3 kHz + air shelf) → acompressor (even dynamics).

    Measured on a synthetic voice-like probe (6 s, harmonic voice +
    sibilance bursts + -42 dB room noise, video venv): gap noise
    -42.3 → -48.8 dB (-6.5), sibilance/voice ratio -0.8 → -3.5 dB
    (-2.7), presence/total -3.9 → -3.4 dB (+0.5), loud/quiet spread
    10.4 → 4.3 dB, duration preserved, peak 0.86 → 0.43 (no
    clipping). Deliberately NO ``aexciter``: the probe showed it
    raises the gap floor +10.5 dB (it excites the noise itself) and
    pollutes the 5-8 kHz sibilance band; the EQ gives the brightness
    instead. Deesser intensity 0.6 is the measured balance point:
    0.7 eats the 2-5 kHz presence band (-4.0 dB), 0.3 barely
    de-esses (+0.2 dB sibilance ratio).

    ``strength`` 0..2 scales the chain (0 = ``anull`` no-op; 1.0 =
    the measured defaults; 2.0 = twice the EQ/de-ess). ``remove_breaths``
    prepends ``silenceremove`` (gaps below -45 dB are cut — changes
    duration, so it is OFF by default and never used in-render).
    """
    s = float(strength)
    if not 0.0 <= s <= 2.0:
        raise ValueError(f"vocal_beautify strength must be 0..2, got {strength!r}")
    if s == 0.0:
        return "anull"
    parts: List[str] = []
    if remove_breaths:
        # -40 dB: below the probe's -42 dB gaps, above quiet speech.
        parts.append("silenceremove=start_periods=1:start_threshold=-40dB:"
                     "stop_periods=-1:stop_threshold=-40dB")
    parts.append("highpass=f=80")
    parts.append(f"afftdn=nr={12 * s:.0f}:nf=-50")
    parts.append(f"deesser=i={0.6 * s:.2f}:m=0.8:f=0.6")
    parts.append(f"equalizer=f=3000:width_type=o:width=1:g={3 * s:.1f}")
    parts.append(f"highshelf=f=10000:g={2 * s:.1f}")
    parts.append("acompressor=threshold=-18dB:"
                 f"ratio={1 + 2 * s:.1f}:attack=10:release=150")
    return ",".join(parts)


def beautify_voice(src: str, dst: str, strength: float = 1.0,
                   remove_breaths: bool = False,
                   target_i: float = -16.0) -> str:
    """Standalone: polish one voice file and write it out.

    Runs :func:`vocal_beautify_filter` plus single-pass loudnorm to
    ``target_i`` LUFS and resamples to 48 kHz. Output codec follows
    the destination extension (``.wav`` → PCM, ``.mp3`` → libmp3lame,
    anything else → aac in an .m4a). Returns dst. Raises
    ``FileNotFoundError`` for a missing source and
    ``subprocess.CalledProcessError`` if ffmpeg fails.
    """
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".wav":
        codec = ["-c:a", "pcm_s16le"]
    elif ext == ".mp3":
        codec = ["-c:a", "libmp3lame", "-q:a", "4"]
    else:
        codec = ["-c:a", "aac", "-b:a", "192k"]
    filt = (vocal_beautify_filter(strength, remove_breaths) + ","
            + balance_filter(target_i, -1.5, 11.0))
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-i", src, "-vn", "-af", filt,
           "-ar", "48000"] + codec + [dst]
    subprocess.run(cmd, check=True, env=ffmpeg_env())
    return dst


def balance_loudness(src: str, dst: str, target_i: float = -16.0,
                     target_tp: float = -1.5,
                     target_lra: float = 11.0) -> str:
    """Standalone: normalize one audio file to a target loudness.

    Output codec follows the destination extension (``.wav`` → PCM,
    ``.mp3`` → libmp3lame, anything else → aac in an .m4a). Returns dst.
    """
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".wav":
        codec = ["-c:a", "pcm_s16le"]
    elif ext == ".mp3":
        codec = ["-c:a", "libmp3lame", "-q:a", "4"]
    else:
        codec = ["-c:a", "aac", "-b:a", "192k"]
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error",
           "-i", src, "-vn",
           "-af", balance_filter(target_i, target_tp, target_lra)] + codec + [dst]
    subprocess.run(cmd, check=True, env=ffmpeg_env())
    return dst


def eq_filter(spec: Dict[str, Any]) -> str:
    """Per-track 3-band EQ (pro audio mixer).

    ``spec`` keys (all gains in dB, -24..24; missing bands are skipped):
    ``low`` — lowshelf at 200 Hz; ``mid`` — parametric peaking at
    1 kHz; ``high`` — highshelf at 6 kHz.
    """
    parts: List[str] = []
    for key, filt in (("low", "lowshelf=f=200"),
                      ("mid", "equalizer=f=1000:width_type=o:width=1"),
                      ("high", "highshelf=f=6000")):
        if key in spec:
            g = float(spec[key])
            if not -24.0 <= g <= 24.0:
                raise ValueError(f"eq.{key} gain must be -24..24 dB, got {g}")
            parts.append(f"{filt}:g={g:.1f}")
    return ",".join(parts)


def compressor_filter(spec: Dict[str, Any]) -> str:
    """Per-track compressor (``acompressor``).

    ``spec``: ``threshold`` dB (-60..0, default -18), ``ratio``
    (1..20, default 4), ``attack`` ms (default 20), ``release`` ms
    (default 250), ``makeup`` dB (default 0 — converted to ffmpeg's
    linear 1..64 makeup factor).
    """
    th = float(spec.get("threshold", -18))
    ratio = float(spec.get("ratio", 4))
    if not -60.0 <= th <= 0.0:
        raise ValueError(f"compressor.threshold must be -60..0 dB, got {th}")
    if not 1.0 <= ratio <= 20.0:
        raise ValueError(f"compressor.ratio must be 1..20, got {ratio}")
    makeup = 10 ** (float(spec.get("makeup", 0)) / 20.0)
    makeup = min(max(makeup, 1.0), 64.0)
    return (
        f"acompressor=threshold={th:.1f}dB:ratio={ratio:.1f}:"
        f"attack={float(spec.get('attack', 20)):.0f}:"
        f"release={float(spec.get('release', 250)):.0f}:"
        f"makeup={makeup:.3f}"
    )


def volume_keyframes_filter(kfs: Sequence[Dict[str, Any]]) -> str:
    """Keyframed volume: a ``volume`` filter whose expression is
    evaluated per frame (``eval=frame``), so the level follows the
    keyframed curve (values 0..4, 1.0 = unchanged).

    Use as a *multiplier* after any static ``volume`` — the render
    engine applies the static volume first, then this curve.
    """
    expr = kf_mod.scalar_expr(kfs, 1.0)
    return f"volume='{kf_mod.ffmpeg_escape(expr)}':eval=frame"


def pan_filter(pan: float) -> str:
    """Stereo pan (``stereotools`` output balance).

    ``pan``: -1 = full left, 0 = centre, +1 = full right. Verified
    2026-11-20: balance_out=-1 leaves the left channel untouched
    and silences the right (and vice versa).
    """
    if not -1.0 <= pan <= 1.0:
        raise ValueError(f"pan must be -1..1, got {pan}")
    return f"stereotools=balance_out={float(pan):.2f}"


def extract_audio(src: str, dst: str, start: float = 0.0,
                  duration: Optional[float] = None,
                  sample_rate: int = 48000) -> str:
    """Extract the audio stream of ``src`` to a wav file. Returns dst."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", src]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", dst]
    subprocess.run(cmd, check=True, env=ffmpeg_env())
    return dst


def probe_audio(src: str) -> Dict[str, Any]:
    """ffprobe the audio stream: duration, sample rate, channels."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=sample_rate,channels,duration",
           "-show_entries", "format=duration",
           "-of", "json", src]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         env=ffmpeg_env(), check=True).stdout
    import json
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format", {})
    return {
        "duration": float(stream.get("duration") or fmt.get("duration") or 0),
        "sample_rate": int(stream.get("sample_rate") or 48000),
        "channels": int(stream.get("channels") or 1),
    }


def detect_beats(src: str, min_gap: float = 0.25,
                 sensitivity: float = 1.5) -> List[float]:
    """Detect beat/onset markers (seconds) in an audio file.

    Decodes to mono 16 kHz wav, computes an RMS energy envelope in
    10 ms windows, and returns local maxima that exceed
    ``sensitivity`` times the rolling median energy. Pure stdlib.
    """
    import tempfile
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        extract_audio(src, tmp, sample_rate=16000)
        with wave.open(tmp, "rb") as w:
            n = w.getnframes()
            raw = w.readframes(n)
        samples = struct.unpack(f"<{n}h", raw)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    win = 160  # 10 ms at 16 kHz
    env: List[float] = []
    for i in range(0, len(samples) - win + 1, win):
        chunk = samples[i:i + win]
        env.append(math.sqrt(sum(s * s for s in chunk) / win))
    if not env:
        return []

    # Rolling median (1 s window) as the adaptive threshold.
    beats: List[float] = []
    last_beat = -min_gap
    W = 100
    for i, e in enumerate(env):
        lo = max(0, i - W // 2)
        hi = min(len(env), i + W // 2)
        window = sorted(env[lo:hi])
        med = window[len(window) // 2] or 1.0
        if e > med * sensitivity and e >= (env[i - 1] if i else 0) \
                and e >= (env[i + 1] if i + 1 < len(env) else 0):
            t = i * 0.01
            if t - last_beat >= min_gap:
                beats.append(round(t, 3))
                last_beat = t
    return beats


def mix_levels(src: str) -> Tuple[float, float]:
    """Mean/max dB of a file via volumedetect (for mix QA)."""
    cmd = ["ffmpeg", "-hide_banner", "-i", src, "-af", "volumedetect",
           "-vn", "-sn", "-dn", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         env=ffmpeg_env())
    mean = maxv = float("nan")
    for line in (res.stderr or "").splitlines():
        if "mean_volume" in line:
            mean = float(line.split(":")[-1].strip().split()[0])
        if "max_volume" in line:
            maxv = float(line.split(":")[-1].strip().split()[0])
    return mean, maxv
