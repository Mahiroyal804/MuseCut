"""Round-6 tests (v0.9.0).

Covers round 6 (2026-11-21):
* AI matting — classical edge refinement of the RMBG matte
  (musecut.matting): trimap + guided filter (luma guide) + feather
  + colour decontamination + hardness. Measured on a real frame
  (van clip t=0.5s): alignment corr 0.378 -> 0.494, fringe
  7,096 -> 5,518 (semi widens slightly, +12% — documented
  tradeoff, the guided filter follows soft luminance ramps).
* version unified to 0.9.0 everywhere.
* 8K export row closes: the round-2 8K proof file is a real
  7680x4320 render (ffprobe-verified here).

Real footage: the same minivan clip as rounds 3-4
(pexels_1192116.mp4, 2 s slice from t=2 s). The matting tests
reuse ONE refined mask video + plates built in setUpClass
(sample_fps=2, ~17 s of inference) so the suite does not re-run
the model per test.
"""

import json
import os
import subprocess
import unittest

import numpy as np
from PIL import Image

from musecut import __version__
from musecut import ai as ai_mod
from musecut import bgremove as bg_mod
from musecut import captions as cap_mod
from musecut import matting as mat_mod
from musecut import providers as prov_mod
from musecut.render import Renderer, probe_media
from musecut.timeline import (CAPTION_STYLES, TimelineError, new_project,
                              validate_timeline)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _magenta_frac(png_path):
    """Fraction of pixels that are (near-)magenta in a PNG."""
    a = np.asarray(Image.open(png_path).convert("RGB")).astype(int)
    m = (a[:, :, 0] > 200) & (a[:, :, 1] < 80) & (a[:, :, 2] > 200)
    return float(m.mean())


WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round6")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")
EIGHT_K_PROOF = os.path.expanduser(
    "~/workspace/your_files/musecut-tests/8k-proof.mp4")


class Round6Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        cls.van2s = os.path.join(MEDIA, "van2s.mp4")
        if not os.path.isfile(cls.van2s) and os.path.isfile(VAN_CLIP):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "2", "-i", VAN_CLIP, "-t", "2", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", "-an", cls.van2s])
            assert res.returncode == 0, res.stderr
        # One refined mask video + clean RGB plate for the render
        # tests (sample_fps=2: 4 masks, ~17 s of inference + refine).
        cls.mask = os.path.join(MEDIA, "van_refined_mask.mp4")
        cls.plate = os.path.join(MEDIA, "van_clean_plate.mp4")
        if not os.path.isfile(cls.mask) or not os.path.isfile(cls.plate):
            mat_mod.make_refined_plates(
                cls.van2s, cls.mask, cls.plate, sample_fps=2.0,
                workdir=os.path.join(WORK, "matwork"))

    # ---- version --------------------------------------------------
    def test_version_unified_080(self):
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "musecut", "__init__.py")) as f:
            self.assertIn("0.21.0", f.read())

    # ---- trimap (synthetic) --------------------------------------
    def test_trimap_bands(self):
        xs = np.arange(100, dtype=np.float32)
        # soft step 0 -> 1 across x=40..50
        alpha = np.clip((xs[None, :] - 40) / 10.0, 0, 1)
        alpha = np.repeat(alpha, 100, axis=0)
        tm = mat_mod.trimap_from_alpha(alpha, radius=2)
        # middle row: definite bg / unknown band / definite fg
        self.assertEqual(int(tm[50, 10]), 0)    # definite bg
        self.assertEqual(int(tm[50, 90]), 255)  # definite fg
        self.assertEqual(int(tm[50, 45]), 128)  # unknown band
        # the unknown band straddles the edge (both sides eroded):
        # x=40 (start of the soft step) is unknown, x=50 (end) too
        self.assertEqual(int(tm[50, 40]), 128)
        self.assertEqual(int(tm[50, 50]), 128)
        with self.assertRaises(ValueError):
            mat_mod.trimap_from_alpha(alpha, radius=-1)

    # ---- guided filter aligns alpha with the image edge -----------
    def test_guided_filter_aligns_edge(self):
        # step image: black left, white right, edge at x=50
        img = np.zeros((100, 100), dtype=np.float32)
        img[:, 50:] = 1.0
        # raw alpha: soft step centred at x=45 (5 px off the edge)
        xs = np.arange(100, dtype=np.float32)
        raw = np.clip((xs[None, :] - 40) / 10.0, 0, 1)
        raw = np.repeat(raw, 100, axis=0)

        def cross(a):
            return float(np.argmax(a[50] >= 0.5))

        self.assertEqual(cross(raw), 45.0)
        q = mat_mod.guided_filter(img, raw, radius=8, eps=0.001)
        # the 0.5-crossing moves toward the true edge at x=50
        self.assertGreater(cross(q), 45.0)
        self.assertLessEqual(abs(cross(q) - 50.0), 3.0)
        with self.assertRaises(ValueError):
            mat_mod.guided_filter(img, raw, radius=0, eps=0.001)
        with self.assertRaises(ValueError):
            mat_mod.guided_filter(img, raw, radius=8, eps=0.0)

    # ---- feather only touches the unknown band --------------------
    def test_feather_band_only(self):
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[:, 50:] = 255
        alpha = np.where(np.arange(100)[None, :] < 50, 0, 255).astype(
            np.uint8)
        alpha = np.repeat(alpha, 100, axis=0)
        ref, clean = mat_mod.refine_alpha(
            Image.fromarray(img), Image.fromarray(alpha),
            feather_px=4, guided_radius=4, guided_eps=0.001,
            decontaminate=False)
        self.assertIsNone(clean)
        ra = np.asarray(ref, dtype=np.float32) / 255.0
        self.assertEqual(float(ra[50, 10]), 0.0)   # deep bg untouched
        self.assertEqual(float(ra[50, 90]), 1.0)   # deep fg untouched
        self.assertGreater(float(ra[50, 50]), 0.05)
        self.assertLess(float(ra[50, 50]), 0.95)  # softened in band
        with self.assertRaises(ValueError):
            mat_mod.refine_alpha(Image.fromarray(img),
                                 Image.fromarray(alpha), feather_px=-1)
        with self.assertRaises(ValueError):
            mat_mod.refine_alpha(Image.fromarray(img),
                                 Image.fromarray(alpha), hardness=-1.0)

    # ---- decontamination removes background spill -----------------
    def test_decontaminate_removes_spill(self):
        # red square on green; the 2 px ring is a 50/50 blend
        im2 = np.zeros((100, 100, 3), dtype=np.float32)
        im2[:, :, 1] = 1.0
        im2[30:70, 30:70] = [1.0, 0.0, 0.0]
        al2 = np.zeros((100, 100), dtype=np.float32)
        al2[30:70, 30:70] = 1.0
        for sl in ((slice(30, 32), slice(30, 70)),
                   (slice(68, 70), slice(30, 70)),
                   (slice(30, 70), slice(30, 32)),
                   (slice(30, 70), slice(68, 70))):
            al2[sl] = 0.5
            im2[sl] = [0.5, 0.5, 0.0]
        _, clean = mat_mod.refine_alpha(
            Image.fromarray((im2 * 255).astype(np.uint8)),
            Image.fromarray((al2 * 255).astype(np.uint8)),
            feather_px=0, guided_radius=4, guided_eps=0.001,
            decontaminate=True)
        self.assertIsNotNone(clean)
        c = np.asarray(clean, dtype=np.float32) / 255.0
        # the ring pixel is un-premultiplied back to (near-)red
        self.assertGreater(float(c[31, 50, 0]), 0.9)
        self.assertLess(float(c[31, 50, 1]), 0.1)

    # ---- hardness steepens the transition -------------------------
    def test_hardness_sharpens(self):
        xs = np.arange(100, dtype=np.float32)
        # wide soft step 0 -> 1 across x=40..60
        raw = np.clip((xs[None, :] - 40) / 20.0, 0, 1)
        raw = np.repeat(raw, 100, axis=0)
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[:, 50:] = 255

        def semi(hardness):
            ref, _ = mat_mod.refine_alpha(
                Image.fromarray(img),
                Image.fromarray((raw * 255).astype(np.uint8)),
                feather_px=0, guided_radius=8, guided_eps=0.001,
                decontaminate=False, hardness=hardness)
            ra = np.asarray(ref, dtype=np.float32) / 255.0
            return int(((ra > 0.05) & (ra < 0.95)).sum())

        raw_semi = int(((raw > 0.05) & (raw < 0.95)).sum())
        # the guided filter alone widens the step; hardness=8 pulls
        # the semi count back down to (at most) the raw level
        self.assertLess(semi(8.0), semi(0.0))
        self.assertLessEqual(semi(8.0), raw_semi)

    # ---- real-frame measurement: refinement improves the edge -----
    def test_real_frame_refinement_improves(self):
        if not os.path.isfile(self.van2s):
            self.skipTest("footage clip missing: " + self.van2s)
        frame = os.path.join(WORK, "meas_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", self.van2s, "-frames:v", "1",
                    frame])
        self.assertEqual(res.returncode, 0, res.stderr)
        img = Image.open(frame).convert("RGB")
        session = bg_mod._load_session()
        raw = bg_mod.segment_frame(session, img)
        ref, _ = mat_mod.refine_alpha(img, raw)  # defaults
        m_raw = mat_mod.measure_matte(img, raw)
        m_ref = mat_mod.measure_matte(img, ref)
        # alignment: alpha follows the real luminance edge better
        self.assertGreater(m_ref["align_corr"], m_raw["align_corr"])
        self.assertGreater(m_ref["align_corr"], 0.45)
        # halo: fewer fringe pixels after compositing on magenta
        self.assertLess(m_ref["fringe"], m_raw["fringe"])
        self.assertLess(m_ref["fringe"], 6000)

    # ---- refined plates exist and are frame-aligned ---------------
    def test_refined_plates_built(self):
        self.assertTrue(os.path.isfile(self.mask))
        self.assertTrue(os.path.isfile(self.plate))
        pm = probe_media(self.mask)
        pp = probe_media(self.plate)
        self.assertEqual((pm["width"], pm["height"]), (1280, 720))
        self.assertEqual((pp["width"], pp["height"]), (1280, 720))
        # both plates come from the same 2 s clip sampled at 2 fps:
        # 4 masks held 0.5 s each, the container reports 2.5 s
        # (the final held frame's own duration) — what matters is
        # that mask and plate agree with each other
        self.assertAlmostEqual(pp["duration"], pm["duration"], delta=0.1)
        self.assertGreater(pm["duration"], 2.0)
        self.assertLess(pm["duration"], 3.0)

    # ---- ai_matting renders through the engine -------------------
    def test_ai_matting_renders(self):
        out = os.path.join(WORK, "matting.mp4")
        ai_mod.ai_matting(self.van2s, out, background="#FF00FF",
                          mask_video=self.mask, rgb_plate=self.plate)
        probe = _probe(out, "codec_type,width,height,codec_name")
        self.assertEqual(_vstream(probe)["width"], 1280)
        self.assertEqual(_vstream(probe)["height"], 720)
        frame = os.path.join(WORK, "mat_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", out, "-frames:v", "1", frame])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreater(_magenta_frac(frame), 0.5)
        a = np.asarray(Image.open(frame).convert("RGB")).astype(int)
        car_px = tuple(a[335, 560])
        self.assertFalse(car_px[0] > 200 and car_px[1] < 80
                         and car_px[2] > 200,
                         f"car pixel is magenta: {car_px}")

    # ---- ai_matting transparent cutout keeps the alpha plane ------
    def test_ai_matting_cutout_alpha(self):
        out = os.path.join(WORK, "matting_cutout.mov")
        ai_mod.ai_matting(self.van2s, out, background="transparent",
                          mask_video=self.mask, rgb_plate=self.plate)
        probe = _probe(out, "codec_type,codec_name,width,height")
        v = _vstream(probe)
        self.assertEqual(v["codec_name"], "prores")
        frame = os.path.join(WORK, "cut_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", out, "-frames:v", "1", "-f",
                    "image2", frame])
        self.assertEqual(res.returncode, 0, res.stderr)
        # alpha plane: mostly clear background, some opaque subject
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", out, "-frames:v", "1",
                    "-vf", "alphaextract,format=gray",
                    os.path.join(WORK, "cut_alpha.png")])
        self.assertEqual(res.returncode, 0, res.stderr)
        al = np.asarray(Image.open(
            os.path.join(WORK, "cut_alpha.png")).convert("L"),
            dtype=np.float32) / 255.0
        self.assertGreater(float((al < 0.1).mean()), 0.5)
        self.assertGreater(float((al > 0.9).mean()), 0.02)

    # ---- ai_matting validation ------------------------------------
    def test_ai_matting_validation(self):
        with self.assertRaises(FileNotFoundError):
            ai_mod.ai_matting("nope.mp4", "out.mp4")
        with self.assertRaises(ValueError):
            ai_mod.ai_matting(self.van2s, "out.mp4",
                              mask_video=self.mask,
                              rgb_plate=self.plate, feather_px=-1)

    # ---- capabilities now list matting as done --------------------
    def test_capabilities_matting_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["ai_matting"]["status"], "done")
        self.assertIn("guided", caps["ai_matting"]["via"])

    # ---- mastery round 6: match-on-action -------------------------
    def test_match_on_action_demo(self):
        """Cut mid-motion wide -> tight (smart-reframe punch-in).

        The grey sedan drives left across the van clip. Clip A is the
        wide shot (0-0.75 s); clip B is the same action reframed at
        1.5x (0.75-1.25 s). The tracker on the RENDERED demo must
        show the car moving left at the same speed on both sides of
        the cut, with only a small position step at the cut itself.
        """
        if not os.path.isfile(self.van2s):
            self.skipTest("footage clip missing: " + self.van2s)
        seg_b = os.path.join(MEDIA, "moa-segB.mp4")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.75", "-i", self.van2s, "-t", "0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                    seg_b])
        self.assertEqual(res.returncode, 0, res.stderr)
        ai_mod.smart_reframe(
            seg_b, os.path.join(MEDIA, "moa-segB-reframe.json"),
            class_name="car", scale=1.5, width=1280, height=720,
            duration=0.5)
        from musecut.timeline import load_project
        proj_b = load_project(os.path.join(MEDIA, "moa-segB-reframe.json"))
        seg_b_ref = os.path.join(MEDIA, "moa-segB-reframed.mp4")
        Renderer(proj_b, workdir=WORK).render(seg_b_ref)
        proj = new_project("r6-match-on-action", width=1280, height=720)
        proj["clips"] = [
            {"src": self.van2s, "offset": 0.0, "duration": 0.75},
            {"src": seg_b_ref, "duration": 0.5},
        ]
        out = os.path.join(WORK, "match-on-action.mp4")
        Renderer(proj, workdir=WORK).render(out)
        from musecut import track as track_mod
        t = track_mod.track_object(
            out, class_name="car", sample_fps=8.0,
            workdir=os.path.join(WORK, "moatrack"))
        xs = [(f["time"], f["x"]) for f in t["frames"]]
        self.assertGreaterEqual(len(xs), 6)
        pre = [x for tm, x in xs if tm < 0.75]
        post = [x for tm, x in xs if tm >= 0.75]
        v_pre = (pre[-1] - pre[0]) / 0.75
        v_post = (post[-1] - post[0]) / 0.5
        # same direction (leftward) and same speed within 0.1 widths/s
        self.assertLess(v_pre, -0.1)
        self.assertLess(v_post, -0.1)
        self.assertLess(abs(v_pre - v_post), 0.1)
        # small position step at the cut (the reframe recentres)
        self.assertLess(abs(post[0] - pre[-1]), 0.1)

    # ---- 8K proof file is a real 7680x4320 render ----------------
    def test_8k_proof_file(self):
        self.assertTrue(os.path.isfile(EIGHT_K_PROOF),
                        "round-2 8K proof file missing")
        probe = _probe(EIGHT_K_PROOF,
                       "codec_type,codec_name,width,height")
        v = _vstream(probe)
        self.assertEqual((v["width"], v["height"]), (7680, 4320))

    # ---- caption styles: named registry ---------------------------
    def test_caption_styles_registry(self):
        self.assertEqual(set(CAPTION_STYLES),
                         {"viral-gold", "minimal", "news"})
        self.assertEqual(CAPTION_STYLES["viral-gold"]["font"], "Anton")
        self.assertEqual(CAPTION_STYLES["viral-gold"]["active_color"],
                         "#FFD60A")
        self.assertEqual(CAPTION_STYLES["minimal"]["font"], "Inter")
        self.assertEqual(CAPTION_STYLES["news"]["font"], "Oswald")
        self.assertTrue(CAPTION_STYLES["news"]["bubble"])
        self.assertFalse(CAPTION_STYLES["viral-gold"]["bubble"])
        self.assertFalse(CAPTION_STYLES["minimal"]["bubble"])

    def test_caption_style_default_is_viral_gold(self):
        words = [{"word": "SAVE", "start": 0.0, "end": 0.5},
                 {"word": "YOUR", "start": 0.5, "end": 1.0}]
        default = cap_mod.words_to_ass(words, width=1280, height=720)
        named = cap_mod.words_to_ass(words, width=1280, height=720,
                                     style="viral-gold")
        self.assertEqual(default, named)
        # the historical defaults, exactly: Anton, gold active word
        style_line = [l for l in default.splitlines()
                      if l.startswith("Style:")][0]
        self.assertIn("Anton", style_line)
        self.assertIn("&H000AD6FF", default)  # gold #FFD60A as ASS BGR

    def test_caption_style_minimal_and_news(self):
        words = [{"word": "SAVE", "start": 0.0, "end": 0.5},
                 {"word": "YOUR", "start": 0.5, "end": 1.0}]
        mini = cap_mod.words_to_ass(words, width=1280, height=720,
                                    style="minimal")
        mini_style = [l for l in mini.splitlines()
                      if l.startswith("Style:")][0]
        self.assertIn("Inter", mini_style)
        self.assertNotIn("&H000AD6FF", mini)  # no gold anywhere
        self.assertIn("&H009A9A9A", mini)     # grey inactive words
        news = cap_mod.words_to_ass(words, width=1280, height=720,
                                    style="news")
        news_style = [l for l in news.splitlines()
                      if l.startswith("Style:")][0]
        self.assertIn("Oswald", news_style)
        fields = news_style.split(",")
        self.assertEqual(fields[15], "3")  # BorderStyle 3 = bubble
        self.assertEqual(fields[7], "1")   # Bold
        self.assertIn("&H00111111", news)  # dark bubble colour

    def test_caption_style_explicit_wins(self):
        words = [{"word": "SAVE", "start": 0.0, "end": 0.5}]
        doc = cap_mod.words_to_ass(words, width=1280, height=720,
                                   style="news", font="Anton")
        style_line = [l for l in doc.splitlines()
                      if l.startswith("Style:")][0]
        self.assertIn("Anton", style_line)
        self.assertNotIn("Oswald", style_line)
        doc2 = cap_mod.words_to_ass(words, width=1280, height=720,
                                    style="minimal",
                                    active_color="#FF0000")
        self.assertIn("&H000000FF", doc2)  # red active (ASS BGR)

    def test_caption_style_unknown_rejected(self):
        words = [{"word": "SAVE", "start": 0.0, "end": 0.5}]
        with self.assertRaises(ValueError):
            cap_mod.words_to_ass(words, style="nope")
        proj = new_project(width=1280, height=720)
        proj["clips"] = [{"src": self.van2s, "duration": 2.0}]
        proj["captions"] = {"words": words, "style": "nope"}
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    # ---- caption styles: render proof (one clip, three styles) ---
    def test_caption_styles_render(self):
        if not os.path.isfile(self.van2s):
            self.skipTest("footage clip missing: " + self.van2s)
        words = [{"word": "SAVE", "start": 0.0, "end": 0.5},
                 {"word": "YOUR", "start": 0.5, "end": 1.0},
                 {"word": "MONEY", "start": 1.0, "end": 1.5},
                 {"word": "NOW", "start": 1.5, "end": 2.0}]
        frames = {}
        for style in ("viral-gold", "minimal", "news"):
            proj = new_project(width=1280, height=720)
            proj["clips"] = [{"src": self.van2s, "duration": 2.0}]
            proj["captions"] = {"words": words, "style": style}
            out = os.path.join(WORK, f"captions-{style}.mp4")
            Renderer(proj, workdir=WORK).render(out)
            png = os.path.join(WORK, f"captions-{style}.png")
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "0.75", "-i", out, "-frames:v", "1", png])
            self.assertEqual(res.returncode, 0, res.stderr)
            frames[style] = np.asarray(
                Image.open(png).convert("RGB")).astype(int)
        # caption band: lower third, y 590..680, x 200..1080
        band = {s: a[590:680, 200:1080] for s, a in frames.items()}

        def gold_px(a):
            return int(((a[:, :, 0] > 200) & (a[:, :, 1] > 140)
                        & (a[:, :, 2] < 120)).sum())

        def white_px(a):
            return int(((a[:, :, 0] > 200) & (a[:, :, 1] > 200)
                        & (a[:, :, 2] > 200)).sum())

        def dark_px(a):
            return int(((a[:, :, 0] < 40) & (a[:, :, 1] < 40)
                        & (a[:, :, 2] < 40)).sum())

        # viral-gold: the active word ("YOUR" at t=0.75) is gold;
        # minimal: no gold anywhere, white active word instead
        self.assertGreater(gold_px(band["viral-gold"]), 100)
        self.assertLess(gold_px(band["minimal"]), 10)
        self.assertGreater(white_px(band["minimal"]), 50)
        # news: a dark bubble box behind the text — far more dark
        # pixels in the band than the un-bubbled styles
        self.assertGreater(dark_px(band["news"]),
                           dark_px(band["viral-gold"]) + 500)
        self.assertGreater(dark_px(band["news"]),
                           dark_px(band["minimal"]) + 500)


if __name__ == "__main__":
    unittest.main()
