"""Round-10 tests (v0.15.0): vocal beautify — classical voice polish.

Covers ``musecut.audio.vocal_beautify_filter`` /
``beautify_voice`` and ``musecut.ai.vocal_beautify`` (round 10,
2026-11-21):
* The filter string is the measured chain (highpass + afftdn +
  deesser + presence EQ + compressor); no ``aexciter`` — the probe
  showed it raises the gap floor +10.5 dB and pollutes the
  sibilance band. Strength 0 is an ``anull`` no-op; out-of-range
  strengths raise ValueError.
* A synthetic voice-like probe (deterministic seed: harmonic
  voice with a 3 kHz formant bump + sibilance bursts + -42 dB
  room noise, 4 phrases alternating loud/quiet) is polished and
  measured: gap-vs-quiet noise -5.0 dB, sibilance/voice -2.7 dB, presence
  +0.5 dB, loud/quiet spread 10.4 -> 4.3 dB, duration preserved,
  no clipping. (Gap noise is measured relative to the quiet-phrase
  level — the honest ratio once loudnorm re-gains the file.)
* The real-voiceover test runs the same chain on a real TTS
  voiceover segment and checks duration, peak and loudness
  (skips cleanly when the source is absent).
* In-render wiring: a project with an ``audio[].vocal_beautify``
  voice track renders, keeps its audio, and the timeline
  validation accepts/rejects the field.
* ``remove_breaths`` (silenceremove) shortens a gappy clip and
  is OFF by default.

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round10 -v
"""

import json
import os
import subprocess
import unittest
import wave

import numpy as np

from musecut import ai as ai_mod
from musecut import audio as audio_mod
from musecut import timeline as timeline_mod
from musecut import __version__
from musecut.render import Renderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round10")
MEDIA = os.path.join(WORK, "media")
VO_SRC = os.path.expanduser(
    "~/workspace/youtube-finance/us45/voiceover/seg01.wav")
SR = 48000


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _db(v):
    return 20 * np.log10(v + 1e-12)


def _write_wav(path, x, sr=SR):
    pcm = (np.clip(x, -0.99, 0.99) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if ch == 2:
        x = x.reshape(-1, 2).mean(axis=1)
    return x, sr


def _band_rms(x, sr, f_lo, f_hi):
    spec = np.fft.rfft(x * np.hanning(len(x)))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    mask = (freqs >= f_lo) & (freqs < f_hi)
    return float(np.sqrt(np.mean(np.abs(spec[mask]) ** 2))) if mask.any() else 0.0


def _seg_rms(x, sr, t0, t1):
    seg = x[int(t0 * sr):int(t1 * sr)]
    return float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0


def _probe_signal():
    """Deterministic voice-like probe (same generator as the
    round-10 scratch probe in /tmp/r10probe/probe.py): 6 s, 4
    phrases of 1.0 s voiced + 0.5 s gap, alternating loud/quiet;
    harmonic voice (f0 130 Hz, harmonics to 5.2 kHz, 3 kHz formant
    bump) + 5-8 kHz sibilance bursts at each phrase start + -42 dB
    white room noise + 60 Hz hum."""
    rng = np.random.default_rng(7)
    t = np.arange(int(6.0 * SR)) / SR
    sig = np.zeros_like(t)
    for p in range(4):
        t0 = p * 1.5
        voiced = (t >= t0) & (t < t0 + 1.0)
        amp = 0.5 if p % 2 == 0 else 0.15
        env = np.zeros_like(t)
        env[voiced] = amp
        harm = sum((1.0 / k) * (1 + 2 * np.exp(-((130 * k - 3000) / 800.0) ** 2))
                   * np.sin(2 * np.pi * 130 * k * t) for k in range(1, 41))
        sig += env * harm * 0.5
        sib = (t >= t0) & (t < t0 + 0.15)
        noise = rng.standard_normal(len(t))
        spec = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(len(t), 1 / SR)
        spec[(freqs < 5000) | (freqs > 8000)] = 0
        band = np.fft.irfft(spec, len(t))
        band /= (np.max(np.abs(band)) + 1e-9)
        sig += sib * band * 0.30
    sig += rng.standard_normal(len(t)) * 0.003
    sig += 0.01 * np.sin(2 * np.pi * 60 * t)
    return sig


def _measure(x, sr=SR):
    """(gap-vs-quiet dB, sibilance/voice dB, presence/total dB, spread dB).

    The gap metric is gap RMS *relative to the quiet-phrase RMS*:
    it is invariant to the final loudnorm gain (a constant gain
    shifts both equally), and it is the ratio a listener hears —
    noise in the gaps against the quietest speech. The compressor
    pulls loud phrases down further than afftdn pulls the gap down,
    so an absolute gap number is meaningless once loudnorm re-gains
    the file; the relative number is the honest one.
    """
    gap = _db(float(np.mean([_seg_rms(x, sr, a, b) for a, b in
                             [(1.05, 1.45), (2.55, 2.95),
                              (4.05, 4.45), (5.55, 5.95)]])))
    loud = np.mean([_seg_rms(x, sr, p * 1.5 + 0.3, p * 1.5 + 0.9) for p in (0, 2)])
    quiet = np.mean([_seg_rms(x, sr, p * 1.5 + 0.3, p * 1.5 + 0.9) for p in (1, 3)])
    gap_rel = gap - _db(quiet)
    sib = float(np.mean([
        _db(_band_rms(x[int((p * 1.5) * sr):int((p * 1.5 + 0.15) * sr)], sr, 5000, 8000))
        - _db(_band_rms(x[int((p * 1.5) * sr):int((p * 1.5 + 0.15) * sr)], sr, 300, 3000))
        for p in range(4)]))
    pres = float(np.mean([
        _db(_band_rms(x[int((p * 1.5 + 0.3) * sr):int((p * 1.5 + 0.9) * sr)], sr, 2000, 5000))
        - _db(_band_rms(x[int((p * 1.5 + 0.3) * sr):int((p * 1.5 + 0.9) * sr)], sr, 100, 12000))
        for p in range(4)]))
    return gap_rel, sib, pres, _db(loud) - _db(quiet)


def _loudnorm_i(path):
    """Integrated loudness (LUFS) via ffmpeg loudnorm JSON."""
    res = _run(["ffmpeg", "-hide_banner", "-i", path,
                "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    text = res.stderr
    start = text.rfind("{")
    # The JSON block is followed by trailing ffmpeg lines; parse
    # only the first complete object.
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    return float(data["input_i"])


class Round10Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        cls.probe = os.path.join(MEDIA, "probe.wav")
        if not os.path.isfile(cls.probe):
            _write_wav(cls.probe, _probe_signal())
        # 2 s testsrc2 video for the in-render test.
        cls.clip = os.path.join(MEDIA, "clip2s.mp4")
        if not os.path.isfile(cls.clip):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.clip])
            assert res.returncode == 0, res.stderr

    # ---- filter string ---------------------------------------------
    def test_filter_string(self):
        f = audio_mod.vocal_beautify_filter()
        for part in ("highpass=f=80", "afftdn", "deesser",
                     "equalizer=f=3000", "highshelf", "acompressor"):
            self.assertIn(part, f)
        # No exciter: the probe showed it raises the gap floor
        # +10.5 dB and pollutes the sibilance band.
        self.assertNotIn("aexciter", f)
        self.assertEqual(audio_mod.vocal_beautify_filter(0.0), "anull")
        with self.assertRaises(ValueError):
            audio_mod.vocal_beautify_filter(2.5)
        with self.assertRaises(ValueError):
            audio_mod.vocal_beautify_filter(-0.5)
        # Strength scales the chain.
        f2 = audio_mod.vocal_beautify_filter(2.0)
        self.assertIn("deesser=i=1.20", f2)
        self.assertIn("g=6.0", f2)

    # ---- probe measurements ----------------------------------------
    def test_probe_measurements(self):
        """Every stage of the chain moves its metric the right way."""
        out = os.path.join(WORK, "probe_beautified.wav")
        audio_mod.beautify_voice(self.probe, out)
        x0, _ = _read_wav(self.probe)
        x1, sr = _read_wav(out)
        self.assertEqual(sr, 48000)
        self.assertAlmostEqual(len(x1) / sr, len(x0) / SR, delta=0.05)
        self.assertLess(float(np.max(np.abs(x1))), 0.99)
        g0, s0, p0, d0 = _measure(x0)
        g1, s1, p1, d1 = _measure(x1)
        self.assertLessEqual(g1 - g0, -3.0,
                             "gap-vs-quiet-noise must drop >= 3 dB")
        self.assertLessEqual(s1 - s0, -1.0, "sibilance ratio must drop >= 1 dB")
        self.assertGreaterEqual(p1 - p0, 0.0, "presence must not drop")
        self.assertLessEqual(d1, 6.0, "loud/quiet spread must even out")
        self.assertLess(d1, d0 - 3.0, "spread must shrink >= 3 dB")

    # ---- ai.py function + capabilities ------------------------------
    def test_ai_function(self):
        out = os.path.join(WORK, "probe_ai.wav")
        ret = ai_mod.vocal_beautify(self.probe, out)
        self.assertEqual(ret, out)
        self.assertTrue(os.path.isfile(out))
        with self.assertRaises(FileNotFoundError):
            ai_mod.vocal_beautify(os.path.join(WORK, "nope.wav"),
                                  os.path.join(WORK, "x.wav"))
        caps = ai_mod.capabilities()
        self.assertEqual(caps["vocal_beautify"]["status"], "done")

    def test_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            audio_mod.beautify_voice(os.path.join(WORK, "nope.wav"),
                                     os.path.join(WORK, "x.wav"))

    # ---- real voiceover ---------------------------------------------
    def test_real_voiceover(self):
        if not os.path.isfile(VO_SRC):
            self.skipTest("voiceover source missing: " + VO_SRC)
        out = os.path.join(WORK, "vo_beautified.wav")
        audio_mod.beautify_voice(VO_SRC, out)
        x0, sr0 = _read_wav(VO_SRC)
        x1, sr1 = _read_wav(out)
        self.assertEqual(sr1, 48000)
        self.assertAlmostEqual(len(x1) / sr1, len(x0) / sr0, delta=0.1)
        self.assertLess(float(np.max(np.abs(x1))), 0.99)
        # Loudnorm target is -16 LUFS (single-pass, allow +/-2).
        loud = _loudnorm_i(out)
        self.assertAlmostEqual(loud, -16.0, delta=2.0)

    # ---- remove_breaths ----------------------------------------------
    def test_remove_breaths(self):
        """Default keeps duration; opt-in silenceremove cuts gaps."""
        out_keep = os.path.join(WORK, "probe_keep.wav")
        out_cut = os.path.join(WORK, "probe_cut.wav")
        audio_mod.beautify_voice(self.probe, out_keep)
        audio_mod.beautify_voice(self.probe, out_cut, remove_breaths=True)
        x0, _ = _read_wav(self.probe)
        xk, _ = _read_wav(out_keep)
        xc, _ = _read_wav(out_cut)
        self.assertAlmostEqual(len(xk) / SR, len(x0) / SR, delta=0.05)
        # The probe has 4 x 0.5 s gaps; cutting them shortens it.
        self.assertLess(len(xc) / SR, len(x0) / SR - 0.5)

    # ---- timeline validation ------------------------------------------
    def test_timeline_validation(self):
        base = {"version": 1, "name": "r10", "width": 640, "height": 360,
                "fps": 30, "clips": [{"src": self.clip, "duration": 2.0}],
                "audio": [{"src": self.probe, "role": "voice",
                           "vocal_beautify": True}]}
        timeline_mod.validate_timeline(dict(base))
        base["audio"][0]["vocal_beautify"] = {"strength": 1.5}
        timeline_mod.validate_timeline(dict(base))
        bad = dict(base)
        bad["audio"] = [{"src": self.probe, "role": "voice",
                         "vocal_beautify": {"strength": 3.0}}]
        with self.assertRaises(timeline_mod.TimelineError):
            timeline_mod.validate_timeline(bad)
        bad2 = dict(base)
        bad2["audio"] = [{"src": self.probe, "role": "voice",
                          "vocal_beautify": "yes"}]
        with self.assertRaises(timeline_mod.TimelineError):
            timeline_mod.validate_timeline(bad2)

    # ---- in-render wiring ----------------------------------------------
    def test_render_with_vocal_beautify(self):
        proj = {"version": 1, "name": "r10-render", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": self.clip, "duration": 2.0}],
                "audio": [{"src": self.probe, "role": "voice",
                           "duration": 2.0, "vocal_beautify": True}]}
        out = os.path.join(WORK, "render-beautified.mp4")
        Renderer(proj, workdir=os.path.join(WORK, "render-vb")
                 ).render(out, quiet=True)
        self.assertTrue(os.path.isfile(out))
        res = _run(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-show_entries",
                    "stream=codec_type,codec_name",
                    "-of", "json", out])
        self.assertEqual(res.returncode, 0, res.stderr)
        info = json.loads(res.stdout)
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0,
                               delta=0.15)
        types = [s["codec_type"] for s in info["streams"]]
        self.assertIn("video", types)
        self.assertIn("audio", types)
        # The voice survives the in-render chain: decode the audio
        # and check a voiced window is not silent.
        wav = os.path.join(WORK, "render-vb.wav")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-i", out, "-vn", "-c:a", "pcm_s16le", wav])
        self.assertEqual(res.returncode, 0, res.stderr)
        x, sr = _read_wav(wav)
        self.assertGreater(_seg_rms(x, sr, 0.3, 0.9), 0.01)

    # ---- version pin ----------------------------------------------------
    def test_version_pin(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())


if __name__ == "__main__":
    unittest.main()
