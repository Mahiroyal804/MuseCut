"""Round-7 tests (v0.9.0): vocal separation — UVR MDX-Net.

Covers ``musecut.vocalsep`` (round 7, 2026-11-21):
* numpy STFT/ISTFT reproduces the torch semantics UVR relies on
  (periodic Hann, centre=True zero-pad, envelope-normalised OLA):
  round-trip is ~identity, and the 4-channel layout is
  [L-real, L-imag, R-real, R-imag] (an L-only signal leaves the
  R channels exactly zero).
* The real-separation test builds a KNOWN mix from a real
  voiceover segment + a real music bed, runs one 5.92 s chunk
  through the model, and measures Pearson correlations of each
  stem against each known source. Verified numbers (2026-11-21):
  corr(inst, bed) 0.985 / corr(inst, vo) 0.016,
  corr(voc, vo) 0.994 / corr(voc, bed) 0.002.
* The model test SKIPS cleanly when the model file (or the
  source audio) is absent; the STFT/validation tests need no model.

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round7_vocalsep -v
"""

import hashlib
import os
import subprocess
import unittest
import wave

import numpy as np

from musecut import vocalsep as vs_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round7")
VO_SRC = os.path.expanduser(
    "~/workspace/youtube-finance/us45/voiceover/seg01.wav")
BED_SRC = os.path.expanduser(
    "~/workspace/youtube-finance/dollar-music/bed-calm-explainer.mp3")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _corr(a, b):
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def _rms(a):
    return float(np.sqrt(np.mean(np.asarray(a, dtype=np.float64) ** 2)))


class VocalsepTest(unittest.TestCase):
    # ---- STFT / ISTFT (no model needed) ---------------------------
    def test_stft_istft_roundtrip(self):
        rng = np.random.default_rng(0)
        x = rng.standard_normal((2, vs_mod.CHUNK)).astype(np.float32) * 0.3
        rt = vs_mod.istft(vs_mod.stft(x))
        self.assertEqual(rt.shape, x.shape)
        self.assertGreater(_corr(rt, x), 0.999)
        self.assertLess(float(np.max(np.abs(rt - x))), 0.05)

    def test_stft_channel_layout(self):
        # L-only impulse: R channels must be exactly zero, and the
        # L-real / L-imag pair must carry the signal.
        x = np.zeros((2, vs_mod.CHUNK), dtype=np.float32)
        x[0, 100000] = 1.0
        s = vs_mod.stft(x)
        self.assertEqual(s.shape, (4, vs_mod.DIM_F, vs_mod.DIM_T))
        self.assertEqual(float(np.abs(s[2:]).max()), 0.0)  # R: silent
        self.assertGreater(float(np.abs(s[0]).max()), 0.0)  # L-real
        self.assertGreater(float(np.abs(s[1]).max()), 0.0)  # L-imag
        # A pure tone lands on its bin: 440 Hz at 44.1 kHz, bin 440 /
        # (44100 / 6144) = bin 61.3 -> peak at bin 61.
        t = np.arange(vs_mod.CHUNK, dtype=np.float32) / vs_mod.SR
        tone = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        st = vs_mod.stft(np.stack([tone, tone]))
        mag = np.sqrt(st[0].astype(np.float64) ** 2
                      + st[1].astype(np.float64) ** 2)
        peak_bin = int(np.argmax(mag[:, 128]))
        self.assertEqual(peak_bin, 61)

    def test_stft_istft_validation(self):
        with self.assertRaises(ValueError):
            vs_mod.stft(np.zeros((1, 1000), dtype=np.float32))
        with self.assertRaises(ValueError):
            vs_mod.istft(np.zeros((4, 100, 10), dtype=np.float32))

    def test_demix_validation(self):
        sess = None  # validation happens before the session is used
        with self.assertRaises(ValueError):
            vs_mod.demix(sess, np.zeros((1, 1000), dtype=np.float32))
        with self.assertRaises(ValueError):
            vs_mod.demix(sess, np.zeros((2, 1000), dtype=np.float32),
                         overlap=1.5)

    # ---- model file ------------------------------------------------
    def test_model_file_hash(self):
        path = vs_mod.model_path()
        if not os.path.isfile(path):
            self.skipTest("model file absent: " + path)
        self.assertEqual(os.path.getsize(path), vs_mod.MODEL_SIZE)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        self.assertEqual(h.hexdigest(), vs_mod.MODEL_SHA256)

    # ---- separate() validation (no model needed) -------------------
    def test_separate_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            vs_mod.separate("nope.wav", os.path.join(WORK, "x"))

    def test_separate_missing_model(self):
        if not os.path.isfile(VO_SRC):
            self.skipTest("voiceover source missing: " + VO_SRC)
        with self.assertRaises(vs_mod.VocalSepError):
            vs_mod.separate(VO_SRC, os.path.join(WORK, "x"),
                            model=os.path.join(WORK, "no-such-model.onnx"))

    # ---- real separation, one chunk --------------------------------
    def test_real_separation_single_chunk(self):
        if not vs_mod.model_ready():
            self.skipTest("model file absent: " + vs_mod.model_path())
        if not os.path.isfile(VO_SRC) or not os.path.isfile(BED_SRC):
            self.skipTest("source audio missing")
        os.makedirs(WORK, exist_ok=True)
        vo44 = os.path.join(WORK, "vo44.wav")
        bed44 = os.path.join(WORK, "bed44.wav")
        for src, dst in ((VO_SRC, vo44), (BED_SRC, bed44)):
            if not os.path.isfile(dst):
                res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                            "-i", src, "-ac", "2", "-ar", "44100",
                            "-c:a", "pcm_s16le", dst])
                self.assertEqual(res.returncode, 0, res.stderr)
        vo = vs_mod._read_wav(vo44)[:, :vs_mod.CHUNK]
        bed = vs_mod._read_wav(bed44)[:, 44100 * 10:44100 * 10 + vs_mod.CHUNK]
        self.assertEqual(vo.shape[1], vs_mod.CHUNK)
        self.assertEqual(bed.shape[1], vs_mod.CHUNK)
        vo = vo * (0.12 / _rms(vo))
        bed = bed * (0.08 / _rms(bed))
        mix = vo + bed
        # keep the written mix unclipped: _write_wav clips at +-1,
        # so scale everything down together if the peak exceeds 0.95
        peak = float(np.abs(mix).max())
        if peak > 0.95:
            scale = 0.95 / peak
            vo, bed, mix = vo * scale, bed * scale, mix * scale
        mix_path = os.path.join(WORK, "mix1chunk.wav")
        vs_mod._write_wav(mix_path, mix)

        voc_path, inst_path = vs_mod.separate(mix_path, WORK)
        self.assertTrue(os.path.isfile(voc_path))
        self.assertTrue(os.path.isfile(inst_path))
        inst = vs_mod._read_wav(inst_path)
        voc = vs_mod._read_wav(voc_path)
        self.assertEqual(inst.shape, mix.shape)
        self.assertEqual(voc.shape, mix.shape)
        # stems reconstruct the mix (vocals are defined as mix - inst)
        self.assertLess(float(np.max(np.abs((inst + voc) - mix))), 1e-3)
        # the separation itself, measured against the known sources
        c_ib = _corr(inst, bed)
        c_iv = _corr(inst, vo)
        c_vv = _corr(voc, vo)
        c_vb = _corr(voc, bed)
        self.assertGreater(c_ib, 0.7, f"corr(inst, bed)={c_ib:.3f}")
        self.assertLess(c_iv, 0.3, f"corr(inst, vo)={c_iv:.3f}")
        self.assertGreater(c_vv, 0.7, f"corr(voc, vo)={c_vv:.3f}")
        self.assertLess(c_vb, 0.3, f"corr(voc, bed)={c_vb:.3f}")
        # and clearly above the unseparated mix baseline
        self.assertGreater(c_ib, _corr(mix, bed) + 0.2)
        self.assertGreater(c_vv, _corr(mix, vo) + 0.05)


if __name__ == "__main__":
    unittest.main()
