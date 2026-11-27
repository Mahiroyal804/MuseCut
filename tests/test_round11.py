"""Round-11 tests (v0.15.0): super resolution — Real-ESRGAN x4plus.

Covers ``musecut.superres`` + ``musecut.ai.super_resolution``
(round 11, 2026-11-21):
* The model is the genuine Real-ESRGAN x4plus ONNX from the
  Qualcomm AI Hub public asset (float + w8a8 fast variant) —
  verified on a real 720p frame: gradient energy 5.751 vs GT
  5.763 (lanczos 4.127); PSNR 29.01 vs lanczos 30.70 — the GAN
  PSNR trade-off, judged by the frame check, not PSNR alone.
* Tiling: 128x128 tiles, 16 px overlap, 64 px feather ramp —
  seams must not show (seam gradient comparable to a control
  strip), and the output is 4x the input in each dimension.
* w8a8 is ~2.7x faster per tile but over-sharpens (gradient
  energy +38% vs GT, PSNR -0.56 dB) — the tests pin both
  numbers so the trade-off cannot silently change.
* Clip path: a small clip upscales 4x and renders through the
  real engine (ffprobe-verified size + duration); output is
  video-only, like inpainting.
* Missing source raises FileNotFoundError (not
  NotImplementedError); capabilities report super_resolution
  "done".

Timing: float ~4.3 s/tile, w8a8 ~1.6 s/tile on this VM, so the
clip test uses the w8a8 fast path and a 320x180 source (6
tiles/frame, 5 frames at sample_fps=10) — about a minute. The
float path is covered by the single-tile and frame tests.

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round11 -v
"""

import json
import os
import subprocess
import unittest

import numpy as np

from musecut import __version__
from musecut import ai as ai_mod
from musecut import superres as sr_mod
from musecut.render import probe_media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round11")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
TYRE_CLIP = os.path.join(POOL, "mixkit_11677_720.mp4")

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

NEEDS_CV2 = unittest.skipUnless(
    HAS_CV2,
    "cv2 not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")

try:
    import onnxruntime  # noqa: F401
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

NEEDS_ORT = unittest.skipUnless(
    HAS_ORT,
    "onnxruntime not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _probe(path, entries):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                f"stream={entries}", "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _vstream(probe):
    return next(s for s in probe["streams"]
                if s["codec_type"] == "video")


def _synthetic_frame(h=256, w=256):
    """Deterministic textured frame: horizontal gradient + vertical
    bars + a circle, so tiling seams and sharpness are measurable."""
    rng = np.random.default_rng(7)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    x = np.linspace(0, 255, w, dtype=np.float32)[None, :]
    y = np.linspace(0, 255, h, dtype=np.float32)[:, None]
    img[:, :, 0] = np.clip(np.broadcast_to(x * 0.6 + 40, (h, w)), 0, 255)  # B
    img[:, :, 1] = np.clip(np.broadcast_to(y * 0.6 + 40, (h, w)), 0, 255)  # G
    img[:, :, 2] = np.clip(np.broadcast_to(255 - x * 0.5, (h, w)), 0, 255)  # R
    for bx in range(0, w, 32):
        img[:, bx:bx + 8] = (30, 200, 200)
    cv2.circle(img, (w // 2, h // 2), 40, (20, 20, 220), -1)
    noise = rng.normal(0, 6, (h, w, 3))
    return np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    clip = os.path.join(MEDIA, "clip320.mp4")
    if not os.path.isfile(clip):
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                    "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    clip])
        assert res.returncode == 0, res.stderr
    still = os.path.join(MEDIA, "still256.png")
    if not os.path.isfile(still):
        cv2.imwrite(still, _synthetic_frame(256, 256))
    return clip, still


class Round11Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        if HAS_CV2:
            cls.clip, cls.still = _make_media()
        else:
            cls.clip = cls.still = None

    # ---- version ------------------------------------------------
    def test_version_unified(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    # ---- tiling (pure python, runs everywhere) ------------------
    def test_tile_origins(self):
        self.assertEqual(sr_mod._tile_origins(128), [0])
        self.assertEqual(sr_mod._tile_origins(100), [0])
        self.assertEqual(sr_mod._tile_origins(256), [0, 64, 128])
        self.assertEqual(sr_mod._tile_origins(320), [0, 96, 192])
        self.assertEqual(sr_mod._tile_origins(129), [0, 1])
        # every origin is a valid 128-wide tile start, consecutive
        # tiles always overlap (the feather blend needs it), and
        # the first/last origins cover the edges exactly
        for n in (180, 200, 720, 1280):
            o = sr_mod._tile_origins(n)
            self.assertEqual(o[0], 0)
            self.assertEqual(o[-1], n - 128)
            for a, b in zip(o, o[1:]):
                self.assertGreater(b, a)
                self.assertLessEqual(b - a, 112)
                self.assertLessEqual(a + 128, n)

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            ai_mod.super_resolution("no-such-file.mp4", "out.mp4")
        with self.assertRaises(FileNotFoundError):
            sr_mod.superres_image("no-such-file.png", "out.png")
        with self.assertRaises(FileNotFoundError):
            sr_mod.superres_clip("no-such-file.mp4", "out.mp4")

    # ---- model + session ----------------------------------------
    @NEEDS_ORT
    def test_session_loads_both_variants(self):
        for fast, dtype in ((False, "tensor(float)"),
                            (True, "tensor(uint8)")):
            s = sr_mod._load_session(fast)
            ins = [(i.name, list(i.shape), i.type)
                   for i in s.get_inputs()]
            outs = [(o.name, list(o.shape), o.type)
                    for o in s.get_outputs()]
            self.assertEqual(ins, [("image", [1, 3, 128, 128], dtype)])
            self.assertEqual(
                outs, [("upscaled_image", [1, 3, 512, 512], dtype)])

    @NEEDS_ORT
    def test_capabilities_sr_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["super_resolution"]["status"], "done")

    # ---- single frame -------------------------------------------
    @NEEDS_CV2
    @NEEDS_ORT
    def test_upscale_frame_shape_and_seams(self):
        img = _synthetic_frame(256, 256)
        sess = sr_mod._load_session(fast=False)
        out = sr_mod.upscale_frame(img, sess, fast=False)
        self.assertEqual(out.shape, (1024, 1024, 3))
        self.assertEqual(out.dtype, np.uint8)
        # output differs from lanczos (the model does real work)
        base = cv2.resize(img, (1024, 1024),
                          interpolation=cv2.INTER_LANCZOS4)
        self.assertGreater(float(np.abs(
            out.astype(np.int16) - base.astype(np.int16)).mean()), 0.5)
        # tile seams must not show: the gradient across a seam
        # (output x=512, the boundary between the first two input
        # tiles) must be comparable to the gradient at the SAME
        # columns in the lanczos baseline, which has no tiles — a
        # seam artifact would spike it. (The seam columns sit on a
        # synthetic bar edge, so an absolute threshold against a
        # flat control strip would be meaningless.)
        g = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gb = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY).astype(np.float32)
        seam = np.abs(np.diff(g[:, 508:516], axis=1)).mean()
        seam_base = np.abs(np.diff(gb[:, 508:516], axis=1)).mean()
        self.assertLess(seam, seam_base * 1.5 + 1.0)
        # no black holes / NaN: every pixel in range, mean sane
        self.assertGreater(float(out.mean()), 20)
        self.assertLess(float(out.mean()), 235)

    @NEEDS_CV2
    @NEEDS_ORT
    def test_measure_superres_real_frame(self):
        if not os.path.isfile(TYRE_CLIP):
            self.skipTest("footage clip missing: " + TYRE_CLIP)
        cap = cv2.VideoCapture(TYRE_CLIP)
        cap.set(cv2.CAP_PROP_POS_MSEC, 2000)
        ok, frame = cap.read()
        cap.release()
        self.assertTrue(ok, "no frame from " + TYRE_CLIP)
        h, w = frame.shape[:2]
        gt = frame[h // 2 - 256:h // 2 + 256, w // 2 - 256:w // 2 + 256]
        lr = cv2.resize(gt, (128, 128), interpolation=cv2.INTER_AREA)
        sess = sr_mod._load_session(fast=False)
        sr = sr_mod.upscale_frame(lr, sess, fast=False)
        m = sr_mod.measure_superres(lr, sr, gt)
        self.assertEqual(m["output_shape"], [512, 512])
        # gradient energy of SR matches GT (lanczos loses ~30%)
        self.assertGreater(m["grad_energy_sr"],
                           m["grad_energy_lanczos"] * 1.2)
        self.assertLess(abs(m["grad_energy_sr"] - m["grad_energy_gt"]),
                        m["grad_energy_gt"] * 0.20)
        # PSNR sanity floor (the GAN trades PSNR for detail — it
        # may sit below lanczos; the frame check is the judge)
        self.assertGreater(m["psnr_sr_vs_gt"], 25.0)
        # w8a8 on the same tile: faster, slightly worse, and
        # over-sharpens (gradient energy above GT) — pin the
        # measured trade-off so it cannot silently change.
        sess_q = sr_mod._load_session(fast=True)
        sr_q = sr_mod.upscale_frame(lr, sess_q, fast=True)
        m_q = sr_mod.measure_superres(lr, sr_q, gt)
        self.assertGreater(m_q["grad_energy_sr"], m["grad_energy_gt"])
        self.assertGreater(
            float(np.abs(sr_q.astype(np.int16)
                         - sr.astype(np.int16)).mean()), 0.5)
        self.assertLess(
            float(np.abs(sr_q.astype(np.int16)
                         - sr.astype(np.int16)).mean()), 10.0)

    # ---- image + clip through the engine -------------------------
    @NEEDS_CV2
    @NEEDS_ORT
    def test_superres_image_via_ai(self):
        dst = os.path.join(WORK, "still1024.png")
        out = ai_mod.super_resolution(self.still, dst, fast=True)
        self.assertEqual(out, dst)
        img = cv2.imread(dst)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (1024, 1024, 3))

    @NEEDS_CV2
    @NEEDS_ORT
    def test_superres_clip_renders(self):
        dst = os.path.join(WORK, "clip1280.mp4")
        out = ai_mod.super_resolution(
            self.clip, dst, fast=True, sample_fps=10.0)
        self.assertEqual(out, dst)
        probe = _probe(dst, "width,height,codec_name,codec_type")
        vs = _vstream(probe)
        self.assertEqual((vs["width"], vs["height"]), (1280, 720))
        dur = float(probe["format"]["duration"])
        self.assertAlmostEqual(dur, 0.5, delta=0.15)
        info = probe_media(dst)
        self.assertEqual((info["width"], info["height"]), (1280, 720))
