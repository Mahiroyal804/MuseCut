"""Round-12 tests (v0.15.0): classical outpainting — mirror-seeded Telea.

Covers ``musecut.outpaint`` + ``musecut.ai.ai_outpainting``
(round 12, 2026-11-21):
* The canvas extends by ``pad`` (int or (l, r, t, b)); the new
  border is a mirror of the edge content, with a Telea seam band
  (``seam_px``, default 12) softening the mirror kink.
* The original interior outside the seam band is byte-identical
  to the source (measured 0.0000 mean abs diff on the real 720p
  frame).
* The mirror seed keeps real texture: on the real frame the
  outpainted strip's gradient energy is ~40 vs ~5 for a Telea-only
  fill (an 8x wash) — the test pins the ratio, not the exact
  numbers, so it survives re-encodes.
* The seam band is not a no-op: on a synthetic diagonal stripe
  the band differs from the pure mirror (the V kink is visibly
  rounded in the frame check) — pinned as band-vs-mirror diff > 0.
* Honest limits pinned in the module, not the tests: Telea ripples
  on a perfect linear ramp (band 2nd-diff 1.64 vs 0.64 mirror),
  and structured objects crossing the edge mirror into symmetric
  doubles — that is why this scores 1, never 2.
* Clip path: a small clip outpaints and renders through the real
  engine (ffprobe-verified size + duration); output is video-only,
  like inpainting. Missing source raises FileNotFoundError;
  ``ai.ai_outpainting`` without ``pad`` raises ValueError;
  capabilities report ai_outpainting "partial" (cv2) / "planned".

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round12 -v
"""

import os
import unittest

import numpy as np

from musecut import __version__
from musecut import ai as ai_mod
from musecut import outpaint as op_mod
from musecut.render import probe_media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round12")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
TYRE_CLIP = os.path.join(POOL, "mixkit_11677_720.mp4")
FRAME720 = os.path.expanduser(
    "~/workspace/your_files/musecut-tests/round11/frame720.png")

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

NEEDS_CV2 = unittest.skipUnless(
    HAS_CV2,
    "cv2 not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")


def _stripe_frame():
    """Synthetic 400x300: black field, one white diagonal stripe
    (20px) crossing the left edge — the mirror makes a sharp V
    kink at the seam, the Telea band rounds it."""
    img = np.zeros((300, 400, 3), dtype=np.uint8)
    cv2.line(img, (0, 60), (399, 240), (255, 255, 255), 20)
    return img


def _strip_grad(gray):
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return float(np.mean(np.abs(gx) + np.abs(gy)))


class TestOutpaintFrame(unittest.TestCase):
    def setUp(self):
        os.makedirs(WORK, exist_ok=True)
        os.makedirs(MEDIA, exist_ok=True)

    @NEEDS_CV2
    def test_frame_size_and_pad_forms(self):
        img = np.zeros((150, 200, 3), dtype=np.uint8)
        # Uniform pad must be >= the default seam (12) or the seam
        # validation raises — see test_pad_validation.
        out = op_mod.outpaint_frame(img, 20)
        self.assertEqual(out.shape, (190, 240, 3))
        out2 = op_mod.outpaint_frame(img, (20, 30, 15, 40))
        self.assertEqual(out2.shape, (205, 250, 3))
        # seam_px=0: pure mirror, still the right size
        out3 = op_mod.outpaint_frame(img, 10, seam_px=0)
        self.assertEqual(out3.shape, (170, 220, 3))

    @NEEDS_CV2
    def test_frame_interior_identical_real_frame(self):
        if not os.path.isfile(FRAME720):
            self.skipTest("round-11 frame missing: " + FRAME720)
        img = cv2.imread(FRAME720)
        self.assertEqual(img.shape, (720, 1280, 3))
        out = op_mod.outpaint_frame(img, (160, 160, 90, 90))
        self.assertEqual(out.shape, (900, 1600, 3))
        m = op_mod.measure_outpaint(img, out, (160, 160, 90, 90))
        self.assertEqual(m["output_size"], [1600, 900])
        self.assertTrue(m["interior_identical"],
                        f"interior diff {m['interior_mean_abs_diff']}")
        self.assertEqual(m["interior_mean_abs_diff"], 0.0)

    @NEEDS_CV2
    def test_seam_band_changes_mirror(self):
        # The Telea band must actually do something: on the stripe
        # frame the band differs from the pure mirror (the V kink
        # is rounded — visible in the round-12 probe frame check).
        img = _stripe_frame()
        out = op_mod.outpaint_frame(img, (100, 100, 60, 60))
        m = op_mod.measure_outpaint(img, out, (100, 100, 60, 60))
        self.assertGreater(
            m["seam_band_vs_mirror_mean_abs_diff"], 0.5,
            "seam band is a no-op vs pure mirror")
        # ...and the interior still survives byte-identical.
        self.assertTrue(m["interior_identical"])

    @NEEDS_CV2
    def test_texture_kept_vs_telea_only(self):
        # Mirror seed keeps real texture; a Telea-only fill is a
        # wash. Pinned as a ratio so the test survives re-encodes.
        if not os.path.isfile(FRAME720):
            self.skipTest("round-11 frame missing: " + FRAME720)
        img = cv2.imread(FRAME720)
        out = op_mod.outpaint_frame(img, (160, 160, 90, 90))
        m = op_mod.measure_outpaint(img, out, (160, 160, 90, 90))
        # Telea-only baseline: black border, inpaint the whole border.
        h, w = img.shape[:2]
        blank = np.zeros((900, 1600, 3), dtype=np.uint8)
        blank[90:90 + h, 160:160 + w] = img
        mask = np.zeros((900, 1600), dtype=np.uint8)
        mask[:90, :] = 255
        mask[90 + h:, :] = 255
        mask[:, :160] = 255
        mask[:, 160 + w:] = 255
        telea = cv2.inpaint(blank, mask, 3, cv2.INPAINT_TELEA)
        telea_strip = _strip_grad(
            cv2.cvtColor(telea[90 + 20:90 + h - 20, 10:140],
                         cv2.COLOR_BGR2GRAY))
        self.assertGreater(m["outpainted_strip_gradient_energy"], 20.0,
                           "mirror-seeded strip lost its texture")
        self.assertLess(telea_strip, 10.0,
                        "Telea-only baseline is not a wash — probe stale?")
        self.assertGreater(
            m["outpainted_strip_gradient_energy"], 3.0 * telea_strip,
            "mirror seed must keep >=3x the texture of Telea-only")

    @NEEDS_CV2
    def test_pad_validation(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            op_mod.outpaint_frame(img, 0)
        with self.assertRaises(ValueError):
            op_mod.outpaint_frame(img, -5)
        with self.assertRaises(ValueError):
            op_mod.outpaint_frame(img, (10, 0, 0, 0), seam_px=12)
        with self.assertRaises(ValueError):
            op_mod.outpaint_frame(img, "wide")

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            op_mod.outpaint_image("/no/such/file.png", "/tmp/x.png", 10)

    @NEEDS_CV2
    def test_ai_outpainting_image(self):
        if not os.path.isfile(FRAME720):
            self.skipTest("round-11 frame missing: " + FRAME720)
        dst = os.path.join(WORK, "frame_outpainted.png")
        res = ai_mod.ai_outpainting(FRAME720, dst, pad=(160, 160, 90, 90))
        self.assertEqual(res, dst)
        arr = cv2.imread(dst)
        self.assertEqual(arr.shape, (900, 1600, 3))

    def test_ai_outpainting_needs_pad(self):
        with self.assertRaises(ValueError):
            ai_mod.ai_outpainting("in.png", "out.png")

    @NEEDS_CV2
    def test_outpaint_clip_renders(self):
        if not os.path.isfile(TYRE_CLIP):
            self.skipTest("footage clip missing: " + TYRE_CLIP)
        # 1 s 640x360 source, outpainted +40px each side.
        import subprocess
        src = os.path.join(MEDIA, "clip640_1s.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-v", "error",
             "-i", TYRE_CLIP, "-t", "1", "-vf", "scale=640:360",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", src],
            check=True)
        dst = os.path.join(WORK, "clip_outpainted.mp4")
        res = op_mod.outpaint_clip(src, dst, 40, sample_fps=10.0)
        self.assertEqual(res, dst)
        info = probe_media(dst)
        self.assertEqual((info["width"], info["height"]), (720, 440))
        self.assertAlmostEqual(info["duration"], 1.0, delta=0.15)

    # ---- capabilities pin -------------------------------------------
    def test_capabilities_partial(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["ai_outpainting"]["status"],
                         "partial" if HAS_CV2 else "planned")

    # ---- version pin -------------------------------------------------
    def test_version_unified(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())


if __name__ == "__main__":
    unittest.main()
