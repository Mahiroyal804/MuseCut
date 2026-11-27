"""Round-7 tests (v0.9.0).

Covers round 7 (2026-11-21):
* Ease-out precedence bug in musecut.keyframes._eased (found by
  frame-checking the whip-pan demo): the bare "1-pow(1-u,2)" return
  was used as a multiplicand in scalar_expr, so it parsed as
  "v0+(v1-v0)*1-pow(...)" — an ease-out position keyframe 0.65->0.5
  evaluated to -0.5 at t=0 (frame off-screen, 97.8% black). Fixed by
  parenthesizing the return. The exact-value render test below pins
  the correct value (scale 4.75 at u=0.5); round 2's render test only
  asserted ease-out > linear, which the buggy (larger) value also
  satisfied — that is why the bug survived since round 2.
* Whip-pan mastery demo (round 7): two-pass plate build, engine
  only — clip A whips out (ease-in pan), clip B whips in (ease-out
  settle), motion blur (tmix) applied to the panned plates, cut at
  1.0 s inside the blur window. Measured on the rendered demo in
  the static top band (buildings): A-side LapVar 114.7 -> 31.6
  (27%), B-side 31.9 vs 51.8 settled (62%), directional X/Y drop
  on both sides. Honest limit (documented in EDITING_MASTERY.md):
  one sharp frame sits exactly at the seam (n=30) because tmix
  restarts at the clip boundary — the blur brackets the cut rather
  than straddling it.

Measurements use numpy only (no cv2) so the suite still runs under
the system python. Real footage: the same minivan clip as rounds
3-6 (pexels_1192116.mp4, 2 s slices from t=2 s and t=6 s).
"""

import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

import numpy as np
from PIL import Image

from musecut import keyframes as kf_mod
from musecut.render import Renderer
from musecut.timeline import new_project

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round7")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")
PROOF = os.path.expanduser(
    "~/workspace/your_files/musecut-round7-whip-pan.mp4")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          **kw)


def _frame(video, n, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-i", video, "-vf", f"select='eq(n\\,{n})'",
                "-frames:v", "1", out_png])
    assert res.returncode == 0, res.stderr
    return out_png


def _gray(png_path):
    return np.asarray(Image.open(png_path).convert("L")).astype(np.float32)


def _lap_var(band):
    """Variance of the Laplacian (3x3) — sharpness of a band."""
    p = np.pad(band, 1, mode="edge")
    lap = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
           - 4 * p[1:-1, 1:-1])
    return float(lap.var())


def _sobel_ratio(band):
    """mean|d/dx| / mean|d/dy| — horizontal pan blur kills vertical
    edges (d/dx) more than horizontal ones, so the ratio drops."""
    p = np.pad(band, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]
          - p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]
          - p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:])
    return float(np.abs(gx).mean() / max(np.abs(gy).mean(), 1e-6))


def _band_stats(png_path):
    a = _gray(png_path)
    top = a[40:200, :]  # buildings — static in the source
    return {"lap": _lap_var(top), "xy": _sobel_ratio(top),
            "mean": float(a.mean())}


class Round7Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        cls.van2s = os.path.join(MEDIA, "van2s.mp4")
        cls.seg_b = os.path.join(MEDIA, "segB.mp4")
        if os.path.isfile(VAN_CLIP):
            for dst, ss in ((cls.van2s, 2), (cls.seg_b, 6)):
                if not os.path.isfile(dst):
                    res = _run(["ffmpeg", "-hide_banner", "-y", "-v",
                                "error", "-ss", str(ss), "-i", VAN_CLIP,
                                "-t", "2", "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-an", dst])
                    assert res.returncode == 0, res.stderr
        # White circle r=60 on black, 640x360 (easing render probe).
        circle = os.path.join(MEDIA, "circle60.png")
        if not os.path.isfile(circle):
            img = Image.new("RGB", (640, 360), (0, 0, 0))
            px = img.load()
            for y in range(360):
                for x in range(640):
                    if (x - 320) ** 2 + (y - 180) ** 2 <= 60 * 60:
                        px[x, y] = (255, 255, 255)
            img.save(circle)
        cls.circle = circle

    # ---- ease-out precedence fix -----------------------------------
    def test_ease_out_expr_parenthesised(self):
        kfs = [{"time": 0.0, "value": 0.0, "easing": "ease-out"},
               {"time": 2.0, "value": 10.0}]
        expr = kf_mod.scalar_expr(kfs, 0.0)
        # The eased factor must be one parenthesised atom, or the
        # "*<eased>" multiplication in scalar_expr misparses it.
        self.assertIn("*(1-pow(1-", expr)

    def test_ease_out_exact_value_render(self):
        """Scale 1->6 over 2 s, ease-out, frame at t=1.0 (u=0.5).

        Correct ease-out at u=0.5 is 0.75, so scale = 1+5*0.75 =
        4.75 and the 120 px blob is ~570 px wide. The pre-fix bug
        evaluated to 6-pow(1-u,2) = 5.75 at u=0.5 (~690 px) — the
        round-2 test only asserted ease-out > linear, which the
        buggy value also satisfied.
        """
        proj = {"version": 1, "name": "r7-ease-out", "width": 640,
                "height": 360, "fps": 30,
                "clips": [{"src": self.circle, "duration": 2,
                           "keyframes": {"scale": [
                               {"time": 0.0, "value": 1.0,
                                "easing": "ease-out"},
                               {"time": 2.0, "value": 6.0}]}}]}
        out = os.path.join(WORK, "ease-out.mp4")
        Renderer(proj, workdir=os.path.join(WORK, "render-ease-out")
                 ).render(out, quiet=True)
        png = _frame(out, 30, os.path.join(WORK, "ease-out.png"))
        a = _gray(png)
        cols = np.where((a > 128).any(axis=0))[0]
        width = int(cols[-1] - cols[0] + 1) if len(cols) else 0
        self.assertGreaterEqual(width, 520, f"blob {width}px, want ~570")
        self.assertLessEqual(width, 620, f"blob {width}px, want ~570")

    # ---- round-7 capability pins -----------------------------------
    def test_round7_capabilities(self):
        """vocal_separation + ai_inpainting wired in round 7.

        vocal_separation is done (UVR model on disk + onnxruntime
        in both interpreters). ai_inpainting / face_effect depend on
        cv2, which lives in the video venv only — done there,
        planned under the system python. (round 9) face_effect is
        "done" now: the round-7 "partial" was the colour-only
        false-positive caveat, closed by YOLO person gating.
        """
        from musecut import ai as ai_mod
        caps = ai_mod.capabilities()
        self.assertEqual(caps["vocal_separation"]["status"], "done")
        try:
            import cv2  # noqa: F401
            has_cv2 = True
        except ImportError:
            has_cv2 = False
        self.assertEqual(caps["ai_inpainting"]["status"],
                         "done" if has_cv2 else "planned")
        self.assertEqual(caps["face_effect"]["status"],
                         "done" if has_cv2 else "planned")
        self.assertEqual(caps["ai_outpainting"]["status"],
                         "partial" if has_cv2 else "planned")
        # (round 10) vocal_beautify is "done" now: the classical
        # voice-polish chain in musecut.audio needs no ML model.
        self.assertEqual(caps["vocal_beautify"]["status"], "done")

    # ---- whip-pan mastery demo --------------------------------------
    def test_whip_pan_demo(self):
        """Two-pass whip pan, engine only; measured on the render.

        Pass 1 plates: A whips out (still -> fast ease-in pan right,
        px 0.5 -> 0.35 in the last 0.3 s), B whips in (fast ease-out
        settle, px 0.65 -> 0.5 in the first 0.3 s), both scale 1.3,
        fit "none". Pass 2 applies motion blur (tmix) to the panned
        plates — tmix runs before the transform keyframes in the
        engine, so the blur must see already-panned frames. Cut at
        1.0 s. Assertions are within-shot (the two shots have
        different inherent sharpness): the whip frames must be much
        softer than the settled frames of the SAME shot.
        """
        if not os.path.isfile(self.van2s):
            self.skipTest("footage clip missing: " + VAN_CLIP)
        # Pass 1a: plate A.
        plate_a = os.path.join(MEDIA, "plateA.mp4")
        proj = new_project("whip-plateA", width=1280, height=720)
        proj["clips"] = [{
            "src": self.van2s, "duration": 1.0, "fit": "none",
            "keyframes": {
                "scale": [{"time": 0.0, "value": 1.3}],
                "position": [
                    {"time": 0.0, "value": [0.5, 0.5]},
                    {"time": 0.7, "value": [0.5, 0.5],
                     "easing": "ease-in"},
                    {"time": 1.0, "value": [0.35, 0.5]},
                ],
            },
        }]
        Renderer(proj, workdir=WORK).render(plate_a, quiet=True)
        # Pass 1b: plate B.
        plate_b = os.path.join(MEDIA, "plateB.mp4")
        proj = new_project("whip-plateB", width=1280, height=720)
        proj["clips"] = [{
            "src": self.seg_b, "duration": 1.0, "fit": "none",
            "keyframes": {
                "scale": [{"time": 0.0, "value": 1.3}],
                "position": [
                    {"time": 0.0, "value": [0.65, 0.5],
                     "easing": "ease-out"},
                    {"time": 0.3, "value": [0.5, 0.5]},
                    {"time": 1.0, "value": [0.5, 0.5]},
                ],
            },
        }]
        Renderer(proj, workdir=WORK).render(plate_b, quiet=True)
        # Pass 2: motion blur on the panned plates, cut at 1.0 s.
        out = os.path.join(WORK, "whip-pan.mp4")
        proj = new_project("r7-whip-pan", width=1280, height=720)
        proj["clips"] = [
            {"src": plate_a, "duration": 1.0,
             "motion_blur": {"frames": 6}},
            {"src": plate_b, "duration": 1.0,
             "motion_blur": {"frames": 6}},
        ]
        Renderer(proj, workdir=WORK).render(out, quiet=True)
        # Duration + size.
        res = _run(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-show_entries",
                    "stream=width,height,codec_type",
                    "-of", "json", out])
        self.assertEqual(res.returncode, 0, res.stderr)
        import json
        probe = json.loads(res.stdout)
        dur = float(probe["format"]["duration"])
        self.assertAlmostEqual(dur, 2.0, delta=0.05)
        vstream = [s for s in probe["streams"]
                   if s["codec_type"] == "video"][0]
        self.assertEqual((vstream["width"], vstream["height"]),
                         (1280, 720))
        # Frame measurements (static top band, per-shot comparison).
        stats = {}
        for n in (12, 28, 36, 51):
            png = _frame(out, n, os.path.join(WORK, f"whip-{n}.png"))
            stats[n] = _band_stats(png)
        # A-side: whip frame much softer than the settled frame.
        self.assertLess(stats[28]["lap"], stats[12]["lap"] * 0.5,
                        f"A whip LapVar {stats[28]['lap']:.1f} vs "
                        f"settled {stats[12]['lap']:.1f}")
        self.assertLess(stats[28]["xy"], stats[12]["xy"],
                        "A whip must drop the X/Y edge ratio")
        # B-side: whip frame softer than the settled frame.
        self.assertLess(stats[36]["lap"], stats[51]["lap"] * 0.8,
                        f"B whip LapVar {stats[36]['lap']:.1f} vs "
                        f"settled {stats[51]['lap']:.1f}")
        # No black bars anywhere (frame means far above black).
        for n, st in stats.items():
            self.assertGreater(st["mean"], 20.0, f"frame n={n} too dark")
        # The proof file is this verified render.
        shutil.copyfile(out, PROOF)


if __name__ == "__main__":
    unittest.main()
