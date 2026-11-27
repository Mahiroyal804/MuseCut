"""Round-7 tests (v0.9.0) — SECONDARY track: classical inpainting +
classical beautify (no ML model).

* ``musecut.inpaint`` — cv2 Telea / Navier-Stokes frame inpainting
  and clip rendering through the real engine. The proof object is
  the wall-mounted AC unit in the van clip (static, so a fixed
  rect mask works — the traffic moves too fast for a static rect).
* ``musecut.beautify`` — YCbCr+RGB skin mask + bilateral filter
  applied to skin pixels only. Honest partial for face effect:
  the van clip has no face; the classical detector flags 9.2% of
  the frame (beige wall / road) as skin, which the tests assert
  as the known false-positive limit rather than hiding it.

Real footage: the same minivan clip as rounds 3-6
(pexels_1192116.mp4, 2 s slice from t=2 s).

Timing note: the plate pipeline samples at ``sample_fps`` with
ffmpeg's ``fps`` filter, which lands one source frame (1/29.97 s)
late — the rendered frame at time t is the source frame at
t+0.033 s. Clip tests therefore compare against the source frame
at t+0.033. Same sampling behaviour as ``bgremove``/``matting``.
"""

import json
import os
import subprocess
import sys
import unittest

import numpy as np
from PIL import Image

from musecut import beautify as beau_mod
from musecut import inpaint as inp_mod
from musecut.render import probe_media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# cv2 lives in the video venv only (~/workspace/.venv-video/bin/python);
# the system python3 has no cv2. The 9 tests that call cv2-backed
# functions SKIP cleanly there (the full suite stays green under the
# system python); they run for real under the video venv. The
# cv2-missing-error test below is the one that runs everywhere.
try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

NEEDS_CV2 = unittest.skipUnless(
    HAS_CV2,
    "cv2 not installed in this Python — run with "
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


def _grab(src, t, dst):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", str(t), "-i", src, "-frames:v", "1", dst])
    assert res.returncode == 0, res.stderr
    return dst


WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round7")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")

# Wall-mounted AC unit in the van clip (1280x720 frame): the unit
# spans x 418-488, y 90-145 (measured from a zoomed frame grab).
AC_RECT = {"x": 0.327, "y": 0.125, "w": 0.055, "h": 0.076}
AC_PX = (slice(90, 145), slice(418, 488))  # (y, x) pixel slices
AC_CENTER = (117, 453)  # (y, x) — the dark fan circle


def _skin_patch():
    """Synthetic 200x200: noisy skin left, blue non-skin right,
    dark 'eye' stripe inside the skin. Returns (img_bgr, skin_mask)."""
    rng = np.random.default_rng(7)
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    base = np.array([120, 150, 200], dtype=np.float32)  # BGR skin
    noise = rng.normal(0, 18, (200, 100, 3))
    img[:, :100] = np.clip(base + noise, 0, 255).astype(np.uint8)
    img[:, 100:] = (255, 30, 30)  # BGR blue — not skin
    img[90:110, 20:80] = (30, 30, 30)  # dark "eye" stripe in skin
    return img, beau_mod.skin_mask(img)


class Round7InpaintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        cls.van2s = os.path.join(MEDIA, "van2s.mp4")
        if not os.path.isfile(cls.van2s) and os.path.isfile(VAN_CLIP):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "2", "-i", VAN_CLIP, "-t", "2", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", "-an", cls.van2s])
            assert res.returncode == 0, res.stderr
        # Static PNG mask: same AC rect, white on black.
        cls.png_mask = os.path.join(MEDIA, "ac_mask.png")
        if not os.path.isfile(cls.png_mask):
            m = np.zeros((720, 1280), dtype=np.uint8)
            m[AC_PX] = 255
            Image.fromarray(m).save(cls.png_mask)
        if not HAS_CV2:
            # The renders below need cv2; the tests that use them
            # skip without it, so do not fail setUpClass here.
            return
        # Shared renders (one per mask form; each ~12 s).
        cls.out_rect = os.path.join(WORK, "inpaint-rect.mp4")
        if not os.path.isfile(cls.out_rect):
            inp_mod.inpaint_clip(cls.van2s, cls.out_rect, AC_RECT,
                                 workdir=os.path.join(WORK, "inp-rect"))
        cls.out_png = os.path.join(WORK, "inpaint-png.mp4")
        if not os.path.isfile(cls.out_png):
            # sample_fps=10 (the default) so the rendered frame at
            # 0.5 s is the source frame at 0.533 s, like the rect
            # render — at sample_fps=5 the held plate frame would be
            # the source frame at 0.433 s instead and the moving car
            # would break the unmasked comparison below.
            inp_mod.inpaint_clip(cls.van2s, cls.out_png, cls.png_mask,
                                 workdir=os.path.join(WORK, "inp-png"))
        cls.out_range = os.path.join(WORK, "inpaint-range.mp4")
        if not os.path.isfile(cls.out_range):
            rect = dict(AC_RECT, t0=1.0, t1=2.0)
            inp_mod.inpaint_clip(cls.van2s, cls.out_range, rect,
                                 sample_fps=5.0,
                                 workdir=os.path.join(WORK, "inp-range"))
        cls.out_beau = os.path.join(WORK, "beautify-gated.mp4")
        if not os.path.isfile(cls.out_beau):
            # (round 9) re-rendered with the person gate (the default
            # since v0.11.0): the old "beautify.mp4" was the ungated
            # round-7 render and must not be reused.
            beau_mod.beautify_clip(cls.van2s, cls.out_beau, strength=0.7,
                                   workdir=os.path.join(WORK, "beau"))

    # ---- inpaint_frame: fills the mask, preserves the rest ---------
    @NEEDS_CV2
    def test_inpaint_frame_fills_and_preserves(self):
        # smooth horizontal gradient + a black "object" square
        grad = np.tile(np.linspace(100, 180, 200, dtype=np.uint8),
                       (200, 1))
        img = np.stack([grad, grad, grad], axis=-1).astype(np.uint8)
        img[80:120, 80:120] = 0
        mask = np.zeros((200, 200), dtype=np.uint8)
        mask[80:120, 80:120] = 255
        for method in ("telea", "ns"):
            out = inp_mod.inpaint_frame(img, mask, method=method)
            self.assertEqual(out.shape, img.shape)
            self.assertEqual(out.dtype, np.uint8)
            m = inp_mod.measure_inpaint(img, out, mask)
            self.assertGreater(m["masked_mean_abs_diff"], 10.0)
            self.assertTrue(m["unmasked_identical"])
            self.assertEqual(m["unmasked_mean_abs_diff"], 0.0)
        # PIL inputs work too (RGB image + L mask)
        pil_out = inp_mod.inpaint_frame(
            Image.fromarray(img[:, :, ::-1]),
            Image.fromarray(mask))
        self.assertEqual(pil_out.shape, img.shape)

    # ---- inpaint_frame: validation ---------------------------------
    @NEEDS_CV2
    def test_inpaint_frame_validation(self):
        img = np.zeros((50, 50, 3), dtype=np.uint8)
        mask = np.zeros((50, 50), dtype=np.uint8)
        mask[10:20, 10:20] = 255
        with self.assertRaises(ValueError):
            inp_mod.inpaint_frame(img, mask, method="nope")
        with self.assertRaises(ValueError):
            inp_mod.inpaint_frame(img, mask, radius=0)
        with self.assertRaises(ValueError):
            inp_mod.inpaint_frame(img, mask, radius=-2)
        with self.assertRaises(ValueError):  # empty mask
            inp_mod.inpaint_frame(img, np.zeros((50, 50), np.uint8))
        with self.assertRaises(ValueError):  # shape mismatch
            inp_mod.inpaint_frame(img, np.zeros((40, 40), np.uint8))
        with self.assertRaises(ValueError):  # not 3-channel
            inp_mod.inpaint_frame(np.zeros((50, 50), np.uint8), mask)

    # ---- cv2 missing: clear error naming the module ---------------
    def test_inpaint_cv2_missing_error(self):
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import numpy as np\n"
            "from musecut import inpaint as i, beautify as b\n"
            "try:\n"
            "    i.inpaint_frame(np.zeros((10,10,3), np.uint8),"
            " np.ones((10,10), np.uint8))\n"
            "    print('NO-ERROR')\n"
            "except ImportError as e:\n"
            "    print('ImportError:', e)\n" % ROOT)
        res = _run(["/usr/bin/python3", "-c", script])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ImportError:", res.stdout)
        self.assertIn("cv2", res.stdout)
        self.assertIn(".venv-video", res.stdout)
        self.assertNotIn("NO-ERROR", res.stdout)

    # ---- inpaint_clip: validation ----------------------------------
    @NEEDS_CV2
    def test_inpaint_clip_validation(self):
        with self.assertRaises(FileNotFoundError):
            inp_mod.inpaint_clip("nope.mp4", "out.mp4", AC_RECT)
        with self.assertRaises(ValueError):
            inp_mod.inpaint_clip(self.van2s, "out.mp4", AC_RECT,
                                 method="nope")
        with self.assertRaises(ValueError):
            inp_mod.inpaint_clip(self.van2s, "out.mp4", AC_RECT,
                                 radius=0)
        with self.assertRaises(ValueError):  # rect out of bounds
            inp_mod.inpaint_clip(self.van2s, "out.mp4",
                                 {"x": 0.9, "y": 0.1, "w": 0.2,
                                  "h": 0.1})
        with self.assertRaises(ValueError):  # bad time range
            inp_mod.inpaint_clip(self.van2s, "out.mp4",
                                 dict(AC_RECT, t0=1.5, t1=1.0))
        with self.assertRaises(ValueError):  # mask of unknown type
            inp_mod.inpaint_clip(self.van2s, "out.mp4", 12345)
        empty_png = os.path.join(MEDIA, "empty_mask.png")
        Image.fromarray(np.zeros((720, 1280), dtype=np.uint8)).save(
            empty_png)
        with self.assertRaises(inp_mod.InpaintError):
            inp_mod.inpaint_clip(self.van2s, "out.mp4", empty_png)

    # ---- inpaint_clip: the AC unit is gone, surroundings intact ----
    @NEEDS_CV2
    def test_inpaint_clip_removes_ac_unit(self):
        probe = _probe(self.out_rect,
                       "codec_type,codec_name,width,height")
        v = _vstream(probe)
        self.assertEqual((v["width"], v["height"]), (1280, 720))
        dur = probe_media(self.out_rect)["duration"]
        self.assertAlmostEqual(dur, 2.0, delta=0.15)
        # rendered frame at 0.5 s == source frame at 0.533 s (see the
        # one-frame fps-filter offset in the module docstring)
        out_f = _grab(self.out_rect, 0.5, os.path.join(WORK, "r7o.png"))
        src_f = _grab(self.van2s, 0.533,
                      os.path.join(WORK, "r7s.png"))
        import cv2
        orig = cv2.imread(src_f)
        out = cv2.imread(out_f)
        mask = np.zeros((720, 1280), dtype=np.uint8)
        mask[AC_PX] = 255
        m = inp_mod.measure_inpaint(orig, out, mask)
        # the dark unit is replaced by wall texture: big change
        self.assertGreater(m["masked_mean_abs_diff"], 15.0)
        # surroundings intact modulo encode noise (matched frame)
        self.assertLess(m["unmasked_mean_abs_diff"], 4.0)
        # the fan circle itself: dark (44) -> wall (87)
        gray_o = cv2.cvtColor(orig, cv2.COLOR_BGR2GRAY)
        gray_i = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        self.assertLess(int(gray_o[AC_CENTER]), 60)
        self.assertGreater(int(gray_i[AC_CENTER]), 75)

    # ---- inpaint_clip: static PNG mask works the same --------------
    @NEEDS_CV2
    def test_inpaint_clip_static_png_mask(self):
        probe = _probe(self.out_png, "codec_type,width,height")
        v = _vstream(probe)
        self.assertEqual((v["width"], v["height"]), (1280, 720))
        out_f = _grab(self.out_png, 0.5, os.path.join(WORK, "r7po.png"))
        src_f = _grab(self.van2s, 0.533, os.path.join(WORK, "r7ps.png"))
        import cv2
        orig = cv2.imread(src_f)
        out = cv2.imread(out_f)
        mask = np.zeros((720, 1280), dtype=np.uint8)
        mask[AC_PX] = 255
        m = inp_mod.measure_inpaint(orig, out, mask)
        self.assertGreater(m["masked_mean_abs_diff"], 15.0)
        self.assertLess(m["unmasked_mean_abs_diff"], 4.0)

    # ---- inpaint_clip: rect t0/t1 limits the effect in time -------
    @NEEDS_CV2
    def test_inpaint_clip_time_range(self):
        import cv2
        mask = np.zeros((720, 1280), dtype=np.uint8)
        mask[AC_PX] = 255
        # before t0=1.0: the AC unit is still there
        out_f = _grab(self.out_range, 0.5,
                      os.path.join(WORK, "r7ro.png"))
        src_f = _grab(self.van2s, 0.533, os.path.join(WORK, "r7rs.png"))
        m = inp_mod.measure_inpaint(cv2.imread(src_f),
                                    cv2.imread(out_f), mask)
        self.assertLess(m["masked_mean_abs_diff"], 4.0)
        # inside [1.0, 2.0): the unit is gone
        out_f = _grab(self.out_range, 1.5,
                      os.path.join(WORK, "r7ro2.png"))
        src_f = _grab(self.van2s, 1.533,
                      os.path.join(WORK, "r7rs2.png"))
        m = inp_mod.measure_inpaint(cv2.imread(src_f),
                                    cv2.imread(out_f), mask)
        self.assertGreater(m["masked_mean_abs_diff"], 15.0)

    # ---- beautify_frame: skin smooths, rest is identical -----------
    @NEEDS_CV2
    def test_beautify_frame_smooths_skin_only(self):
        img, mask = _skin_patch()
        self.assertGreater(int(mask.sum()), 5000)  # most of left half
        self.assertFalse(mask[100, 150])  # blue is not skin
        self.assertFalse(mask[100, 50])   # eye stripe is not skin
        out = beau_mod.beautify_frame(img, strength=0.7)
        self.assertEqual(out.shape, img.shape)
        m = beau_mod.measure_beautify(img, out, mask)
        self.assertLess(m["skin_local_std_after"],
                        m["skin_local_std_before"])
        self.assertGreater(m["reduction_pct"], 20.0)
        self.assertTrue(m["nonskin_identical"])
        # the eye stripe (dark, inside the skin region) is untouched
        self.assertTrue(np.array_equal(out[90:110, 20:80],
                                       img[90:110, 20:80]))
        # strength 0 is a no-op; no skin at all is a no-op too
        self.assertTrue(np.array_equal(
            beau_mod.beautify_frame(img, strength=0.0), img))
        blue = np.zeros((50, 50, 3), dtype=np.uint8)
        blue[:] = (255, 30, 30)
        self.assertTrue(np.array_equal(
            beau_mod.beautify_frame(blue, strength=1.0), blue))

    # ---- beautify_frame: validation --------------------------------
    @NEEDS_CV2
    def test_beautify_frame_validation(self):
        img = np.zeros((20, 20, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            beau_mod.beautify_frame(img, strength=-0.1)
        with self.assertRaises(ValueError):
            beau_mod.beautify_frame(img, strength=1.5)
        with self.assertRaises(ValueError):
            beau_mod.beautify_clip(self.van2s, "out.mp4", strength=2.0)

    # ---- beautify_clip: renders; honest numbers on a faceless clip
    @NEEDS_CV2
    def test_beautify_clip_renders(self):
        probe = _probe(self.out_beau, "codec_type,width,height")
        v = _vstream(probe)
        self.assertEqual((v["width"], v["height"]), (1280, 720))
        dur = probe_media(self.out_beau)["duration"]
        self.assertAlmostEqual(dur, 2.0, delta=0.15)
        out_f = _grab(self.out_beau, 0.5, os.path.join(WORK, "r7bo.png"))
        src_f = _grab(self.van2s, 0.533, os.path.join(WORK, "r7bs.png"))
        import cv2
        m = beau_mod.measure_beautify(cv2.imread(src_f),
                                      cv2.imread(out_f))
        # no face in this clip: the detector flags skin-COLOURED
        # surfaces (beige wall / road) — 9.2% here, not ~0 and not
        # ~50%; the test pins the honest range, not a fake precision
        self.assertGreater(m["skin_frac"], 0.02)
        self.assertLess(m["skin_frac"], 0.20)
        # (round 9) the person gate means those flagged pixels are
        # NOT smoothed any more: the residual reduction (2.5%
        # measured 2026-11-21) is the re-encode floor of the plate
        # + engine passes, not beautify — the ungated round-7
        # render measured > 5%. The exact 0-pixel proof is the
        # plate-level test in test_round9.py.
        self.assertLess(m["reduction_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
