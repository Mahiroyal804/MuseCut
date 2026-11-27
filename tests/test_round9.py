"""Round-9 tests (v0.15.0).

PRIMARY: beautify 1 -> 2 via YOLO person-region gating
(``musecut.beautify`` + ``musecut.track`` yolov5n, the verified
round-3 model). The round-7 caveat — a colour-only skin detector
cannot tell skin from a skin-coloured wall (9.3% false positives
on the faceless van clip) — is closed by restricting smoothing to
detected person boxes (conf >= 0.4, expanded 8%, feathered 5 px).

Measurements (all re-measured this round, video venv):
* van clip (faceless): YOLO finds no person at conf >= 0.4 on
  -ss grabs at 0.2/0.7/1.2/1.7 s; gated plates are 19/20
  byte-identical to the source frames. The one exception is a
  0.407-confidence driver sliver (34x82 px through a car window,
  changed frac 0.0001) — a real person barely visible, not a wall
  false positive.
* person clip (mixkit_11677_720.mp4, woman flipping a tyre in a
  gym): person detected every sampled frame (conf 0.60-0.79);
  gated smoothing keeps the inside-box skin reduction (6.3%, the
  same as ungated) while pixels outside the expanded box are
  byte-identical.
* synthetic skin patch (round-7 patch, same seed): covering box
  gives the same -33% as ungated; an empty box list is a no-op.

SECONDARY: text animations 1 -> 2. The round-1 note
("fade/slide/pop only; CapCut has many more incl. loop/out") is
closed: round 8 added pulse (loop) + fade-out (out) with render
tests, and round 9 adds loop-bounce (a second loop member — the
text bobs +/-8 px at 2 Hz). 9 animation types now cover the
in / loop / out families, all render-tested.

Timing note (same as round 7): the plate pipeline samples at
``sample_fps`` with ffmpeg's ``fps`` filter, which lands one
source frame (1/29.97 s) late — the rendered frame at time t is
the source frame at t+0.033 s. Plate-level tests compare plates
against the exact extracted frames, so the offset does not apply
there.
"""

import glob
import os
import subprocess
import sys
import unittest

import numpy as np
from PIL import Image

from musecut import beautify as beau_mod
from musecut.render import Renderer, probe_media
from musecut.timeline import TEXT_ANIMATIONS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round9")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")
PERSON_CLIP = os.path.join(POOL, "mixkit_11677_720.mp4")

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


def _grab(src, t, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", str(t), "-i", src, "-frames:v", "1", out_png])
    assert res.returncode == 0, res.stderr
    return out_png


def _gray(png_path):
    return np.asarray(Image.open(png_path).convert("L")).astype(np.float32)


def _frame(video, n, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-i", video, "-vf", f"select='eq(n\\,{n})'",
                "-frames:v", "1", out_png])
    assert res.returncode == 0, res.stderr
    return out_png


def _bright_centroid(png_path, thresh=128):
    a = _gray(png_path)
    ys, xs = np.where(a > thresh)
    if len(xs) == 0:
        return None
    return float(xs.mean()), float(ys.mean())


def _skin_patch():
    """The round-7 synthetic patch, same seed: noisy skin left,
    blue non-skin right, dark 'eye' stripe inside the skin."""
    rng = np.random.default_rng(7)
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    base = np.array([120, 150, 200], dtype=np.float32)  # BGR skin
    noise = rng.normal(0, 18, (200, 100, 3))
    img[:, :100] = np.clip(base + noise, 0, 255).astype(np.uint8)
    img[:, 100:] = (255, 30, 30)  # BGR blue — not skin
    img[90:110, 20:80] = (30, 30, 30)  # dark "eye" stripe in skin
    return img, beau_mod.skin_mask(img)


def _changed_frac(a, b):
    return float(np.any(a != b, axis=-1).mean())


def _outside_union(shape, boxes, expand=0.08, margin=5):
    """Boolean mask: True outside the expanded union of ALL boxes.

    YOLO can double-detect one person (two overlapping boxes whose
    IoU is below the 0.45 NMS threshold — e.g. [584,287,898,702]
    and [596,304,1276,716] for the same woman). The gate is the
    union, so the test must use the union too — boxes[0] alone
    would wrongly count the second box's region as "outside".
    """
    h, w = shape[:2]
    outside = np.ones((h, w), dtype=bool)
    for b in boxes:
        x1, y1, x2, y2 = [int(v) for v in b]
        bw, bh = x2 - x1, y2 - y1
        ex, ey = int(bw * expand) + margin, int(bh * expand) + margin
        outside[max(0, y1 - ey):min(h, y2 + ey),
                max(0, x1 - ex):min(w, x2 + ex)] = False
    return outside


class Round9Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        cls.van2s = os.path.join(MEDIA, "van2s.mp4")
        if not os.path.isfile(cls.van2s) and os.path.isfile(VAN_CLIP):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "2", "-i", VAN_CLIP, "-t", "2", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", cls.van2s])
            assert res.returncode == 0, res.stderr
        cls.person2s = os.path.join(MEDIA, "person2s.mp4")
        if not os.path.isfile(cls.person2s) and os.path.isfile(
                PERSON_CLIP):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "1", "-i", PERSON_CLIP, "-t", "2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-an", cls.person2s])
            assert res.returncode == 0, res.stderr

    # ---- person_region_mask ----------------------------------------
    @NEEDS_CV2
    def test_person_region_mask_empty_and_box(self):
        gate = beau_mod.person_region_mask((200, 200), [])
        self.assertEqual(gate.shape, (200, 200))
        self.assertTrue(bool((gate == 0).all()))
        gate = beau_mod.person_region_mask((200, 200),
                                           [[50, 50, 150, 150]],
                                           expand=0.0, feather=0)
        self.assertEqual(float(gate[100, 100]), 1.0)
        self.assertEqual(float(gate[10, 10]), 0.0)
        self.assertEqual(float(gate[49, 100]), 0.0)
        # feathered edge: values strictly between 0 and 1 exist
        gate_f = beau_mod.person_region_mask((200, 200),
                                             [[50, 50, 150, 150]],
                                             expand=0.0, feather=5)
        mid = ((gate_f > 0) & (gate_f < 1)).mean()
        self.assertGreater(float(mid), 0.0)
        # expand grows the 1.0 region by ~8% of the box size
        gate_e = beau_mod.person_region_mask((200, 200),
                                             [[50, 50, 150, 150]],
                                             expand=0.08, feather=0)
        self.assertEqual(float(gate_e[42, 100]), 1.0)  # 50-8
        self.assertEqual(float(gate_e[41, 100]), 0.0)
        with self.assertRaises(ValueError):
            beau_mod.person_region_mask((200, 200), [[0, 0, 10, 10]],
                                        expand=-0.1)

    # ---- detect_person_boxes ---------------------------------------
    @NEEDS_CV2
    def test_detect_person_boxes_van_empty_person_found(self):
        van = cv2.imread(_grab(self.van2s, 1.0,
                               os.path.join(WORK, "r9-van1.jpg")))
        self.assertEqual(beau_mod.detect_person_boxes(van), [])
        per = cv2.imread(_grab(self.person2s, 0.5,
                               os.path.join(WORK, "r9-per1.jpg")))
        boxes = beau_mod.detect_person_boxes(per)
        self.assertGreaterEqual(len(boxes), 1)
        x1, y1, x2, y2 = boxes[0]
        self.assertGreater(x2 - x1, 100)
        self.assertGreater(y2 - y1, 200)

    # ---- beautify_frame: gated patch --------------------------------
    @NEEDS_CV2
    def test_beautify_frame_gated_patch_same_reduction(self):
        img, mask = _skin_patch()
        ungated = beau_mod.beautify_frame(img, strength=0.7)
        gated = beau_mod.beautify_frame(img, strength=0.7,
                                        person_boxes=[[0, 0, 200, 200]])
        self.assertTrue(np.array_equal(gated, ungated))
        m_u = beau_mod.measure_beautify(img, ungated, mask)
        m_g = beau_mod.measure_beautify(img, gated, mask)
        self.assertAlmostEqual(m_g["reduction_pct"],
                               m_u["reduction_pct"], places=1)
        self.assertGreater(m_g["reduction_pct"], 20.0)

    @NEEDS_CV2
    def test_beautify_frame_empty_boxes_is_noop(self):
        img, _ = _skin_patch()
        out = beau_mod.beautify_frame(img, strength=0.7,
                                      person_boxes=[])
        self.assertTrue(np.array_equal(out, img))
        # and None keeps the legacy ungated behaviour
        out2 = beau_mod.beautify_frame(img, strength=0.7,
                                       person_boxes=None)
        self.assertFalse(np.array_equal(out2, img))

    # ---- beautify_frame: gated real person frame --------------------
    @NEEDS_CV2
    def test_beautify_frame_gated_person_frame(self):
        per = cv2.imread(_grab(self.person2s, 0.5,
                               os.path.join(WORK, "r9-per2.jpg")))
        boxes = beau_mod.detect_person_boxes(per)
        self.assertGreaterEqual(len(boxes), 1)
        out = beau_mod.beautify_frame(per, strength=0.7,
                                      person_boxes=boxes)
        mask = beau_mod.skin_mask(per)
        h, w = mask.shape
        inbox = np.zeros((h, w), dtype=bool)
        for b in boxes:
            x1, y1, x2, y2 = [int(v) for v in b]
            inbox[y1:y2, x1:x2] = True
        # inside-box skin is smoothed exactly as much as ungated
        m_in = beau_mod.measure_beautify(per, out, mask & inbox)
        self.assertGreater(m_in["reduction_pct"], 5.0)
        # outside the EXPANDED union of boxes (+5 px feather
        # margin) nothing changed at all — the orange wall / floor
        # stays identical
        outside = _outside_union(per.shape, boxes)
        self.assertTrue(np.array_equal(per[outside], out[outside]))

    # ---- beautify_clip: gated plates -------------------------------
    @NEEDS_CV2
    def test_beautify_clip_gated_van_plates(self):
        out = os.path.join(WORK, "beau-van-gated.mp4")
        wd = os.path.join(WORK, "beau-van-gated-wd")
        if not os.path.isfile(out):
            beau_mod.beautify_clip(self.van2s, out, strength=0.7,
                                   workdir=wd)
        self.assertAlmostEqual(probe_media(out)["duration"], 2.0,
                               delta=0.15)
        fs = sorted(glob.glob(os.path.join(wd, "f*.png")))
        ps = sorted(glob.glob(os.path.join(wd, "plates", "p*.png")))
        self.assertGreaterEqual(len(ps), 19)
        same = 0
        worst = 0.0
        for f, p in zip(fs, ps):
            d = _changed_frac(cv2.imread(f), cv2.imread(p))
            worst = max(worst, d)
            if d == 0.0:
                same += 1
        # 19/20 plates byte-identical; the one exception is a
        # 0.407-confidence driver sliver through a car window
        # (changed frac 0.0001) — a real person barely visible, not
        # a wall false positive. Ungated changed 6.3% of pixels.
        self.assertGreaterEqual(same, 19)
        self.assertLessEqual(worst, 0.001)

    @NEEDS_CV2
    def test_beautify_clip_gated_person_plates(self):
        out = os.path.join(WORK, "beau-per-gated.mp4")
        wd = os.path.join(WORK, "beau-per-gated-wd")
        if not os.path.isfile(out):
            beau_mod.beautify_clip(self.person2s, out, strength=0.7,
                                   workdir=wd)
        self.assertAlmostEqual(probe_media(out)["duration"], 2.0,
                               delta=0.15)
        fs = sorted(glob.glob(os.path.join(wd, "f*.png")))
        ps = sorted(glob.glob(os.path.join(wd, "plates", "p*.png")))
        self.assertGreaterEqual(len(ps), 19)
        changed_inside = 0
        for f, p in zip(fs, ps):
            src = cv2.imread(f)
            plate = cv2.imread(p)
            boxes = beau_mod.detect_person_boxes(src)
            self.assertGreaterEqual(len(boxes), 1)
            h, w = src.shape[:2]
            outside = _outside_union(src.shape, boxes)
            self.assertTrue(np.array_equal(src[outside],
                                           plate[outside]))
            inbox = ~_outside_union(src.shape, boxes, expand=0.0,
                                     margin=0)
            if _changed_frac(src[inbox], plate[inbox]) > 0.005:
                changed_inside += 1
        # smoothing happened inside the box on (nearly) every frame
        self.assertGreaterEqual(changed_inside, len(ps) - 2)

    # ---- loop-bounce animation --------------------------------------
    def _text_proj(self, animation, y="center", x="center"):
        return {"version": 1, "name": "r9-text", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": "color:#000000", "duration": 2,
                           "muted": True}],
                "texts": [{"text": "BOUNCE", "start": 0, "duration": 2,
                           "font": "Anton", "size": 100,
                           "color": "#FFFFFF", "x": x, "y": y,
                           "animation": animation}]}

    def _render(self, proj, name):
        out = os.path.join(WORK, name)
        Renderer(proj, workdir=os.path.join(WORK, "render-" + name)
                 ).render(out, quiet=True)
        return out

    def test_loop_bounce_animation_registered(self):
        self.assertIn("loop-bounce", TEXT_ANIMATIONS)
        self.assertIn("pulse", TEXT_ANIMATIONS)
        self.assertIn("fade-out", TEXT_ANIMATIONS)

    def test_loop_bounce_renders(self):
        """loop-bounce bobs +/-8 px at 2 Hz: at t=0.133 the offset
        is ~+8, at t=0.367 it is ~-8, so the text centroid moves
        ~16 px between the two frames."""
        out = self._render(self._text_proj("loop-bounce"),
                           "loop-bounce.mp4")
        c1 = _bright_centroid(_frame(
            out, 4, os.path.join(WORK, "lb-4.png")))
        c2 = _bright_centroid(_frame(
            out, 11, os.path.join(WORK, "lb-11.png")))
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        d = c1[1] - c2[1]
        self.assertGreaterEqual(d, 10, f"centroid moved {d:.1f}px")
        self.assertLessEqual(d, 22, f"centroid moved {d:.1f}px")

    # ---- round-9 capability pin -------------------------------------
    def test_round9_capabilities(self):
        from musecut import ai as ai_mod
        caps = ai_mod.capabilities()
        self.assertEqual(caps["face_effect"]["status"],
                         "done" if HAS_CV2 else "planned")
        self.assertEqual(caps["ai_outpainting"]["status"],
                         "partial" if HAS_CV2 else "planned")
        # (round 10) vocal_beautify shipped as a classical chain.
        self.assertEqual(caps["vocal_beautify"]["status"], "done")

    # ---- version pin -------------------------------------------------
    def test_version_pin_0_11_0(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        from musecut import __version__
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())


if __name__ == "__main__":
    unittest.main()
