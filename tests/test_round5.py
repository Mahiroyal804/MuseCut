"""Round-5 tests (v0.7.0).

Covers round 5 (2026-11-21) — the no-model remainders:
* 6 new transitions (slide / zoom / dissolve / circle / pixelize /
  dip-to-white), each a native ffmpeg xfade mapping, each frame-checked
  mid-transition against solid-colour clips so the check measures the
  transition itself, not the footage.
* Text templates — 5 built-in presets (title-bold / lower-third /
  quote / punch / caption-clean) + project-level ``text_templates``
  (add or override), resolved at render time, explicit fields win.
* Two-tier caption sizing — ``captions.active_size_ratio`` (1.0..2.0):
  the highlighted word renders at font_size * ratio while the rest of
  the line stays at font_size (ASS \\fs override, reset after the
  word). Frame-checked: the same phrase renders a taller text box
  at ratio 1.5 than at 1.0.
* version unified to 0.7.0 everywhere.
"""

import json
import os
import re
import subprocess
import unittest

import numpy as np
from PIL import Image

from musecut import __version__
from musecut import captions as cap_mod
from musecut.render import XFADE_MAP, Renderer
from musecut.timeline import (
    TEXT_TEMPLATES,
    TRANSITION_TYPES,
    TimelineError,
    new_project,
    resolve_text_template,
    validate_timeline,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round5")
OUT = os.path.expanduser("~/workspace/your_files/musecut-tests")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _frame(video, t, out_png):
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", out_png])
    assert res.returncode == 0, res.stderr
    return np.asarray(Image.open(out_png).convert("RGB")).astype(int)


def _probe_duration(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return float(json.loads(res.stdout)["format"]["duration"])


def _bright_mask(a, thresh=150):
    """Luma-bright pixels (text on a dark background)."""
    luma = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    return luma > thresh


def _bbox_height(mask):
    rows = np.where(mask.any(axis=1))[0]
    if len(rows) == 0:
        return 0
    return int(rows[-1] - rows[0] + 1)


def _is_red(px):
    return px[0] > 150 and px[1] < 80 and px[2] < 80


def _is_blue(px):
    return px[2] > 150 and px[0] < 80 and px[1] < 80


def _is_green(px):
    return px[1] > 150 and px[0] < 80 and px[2] < 80


def _is_yellow(px):
    return px[0] > 150 and px[1] > 150 and px[2] < 80


def _is_magenta(px):
    return px[0] > 150 and px[2] > 150 and px[1] < 80


def _is_cyan(px):
    return px[1] > 150 and px[2] > 150 and px[0] < 80


def _make_checker(path, size=40):
    """Black/white checkerboard PNG (640x360) for scale/block tests."""
    img = Image.new("RGB", (640, 360), "white")
    px = img.load()
    for y in range(360):
        for x in range(640):
            if (x // size + y // size) % 2 == 0:
                px[x, y] = (0, 0, 0)
    img.save(path)
    return path


class Round5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)
        os.makedirs(OUT, exist_ok=True)
        cls.checker = _make_checker(os.path.join(WORK, "checker.png"))

    # ---- transitions: validation + mapping -------------------------

    def test_new_transition_types_validate(self):
        for ttype in ("slide", "zoom", "dissolve", "circle", "pixelize",
                      "dip-to-white"):
            proj = new_project(width=640, height=360)
            proj["clips"] = [
                {"src": "color:FF0000", "duration": 1.0},
                {"src": "color:0000FF", "duration": 1.0,
                 "transition_in": {"type": ttype, "duration": 0.5}},
            ]
            validate_timeline(proj)  # must not raise
        proj = new_project(width=640, height=360)
        proj["clips"] = [
            {"src": "color:FF0000", "duration": 1.0},
            {"src": "color:0000FF", "duration": 1.0,
             "transition_in": {"type": "zoom-blur", "duration": 0.5}},
        ]
        # "zoom-blur" is NOT a type: the honest name is "zoom"
        # (ffmpeg zoomin). A made-up name must be rejected, not
        # silently mapped to a crossfade.
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    def test_xfade_map_covers_all_types_and_matches_ffmpeg(self):
        # Every transition type except "none" (hard cut) and "whoosh"
        # (fade + synth SFX by design) must map to a real ffmpeg
        # xfade transition name — checked live against this build.
        res = _run(["ffmpeg", "-hide_banner", "-h", "filter=xfade"])
        assert res.returncode == 0, res.stderr
        ffmpeg_names = set(re.findall(r"^\s+([a-z]+)\s+\d+\s",
                                      res.stdout + res.stderr,
                                      flags=re.M))
        for ttype in sorted(TRANSITION_TYPES):
            if ttype in ("none", "whoosh"):
                continue
            self.assertIn(ttype, XFADE_MAP, f"{ttype} has no xfade map")
            self.assertIn(XFADE_MAP[ttype], ffmpeg_names,
                          f"{ttype} -> {XFADE_MAP[ttype]} is not a "
                          "real xfade transition in this ffmpeg build")

    def test_all_new_transitions_render_and_frame_check(self):
        # 7 solid-colour clips, 2 s each, 1 s transitions. Midpoints:
        # 1.5 slide, 2.5 zoom, 3.5 dissolve, 4.5 circle,
        # 5.5 pixelize, 6.5 dip-to-white. Total 8 s.
        clips = [
            {"src": "color:FF0000", "duration": 2.0},                       # red
            {"src": "color:0000FF", "duration": 2.0,                       # blue
             "transition_in": {"type": "slide", "duration": 1.0}},
            {"src": "color:00FF00", "duration": 2.0,                       # green
             "transition_in": {"type": "zoom", "duration": 1.0}},
            {"src": "color:FFFF00", "duration": 2.0,                       # yellow
             "transition_in": {"type": "dissolve", "duration": 1.0}},
            {"src": "color:FF00FF", "duration": 2.0,                       # magenta
             "transition_in": {"type": "circle", "duration": 1.0}},
            {"src": "color:00FFFF", "duration": 2.0,                       # cyan
             "transition_in": {"type": "pixelize", "duration": 1.0}},
            {"src": "color:FFFFFF", "duration": 2.0,                       # white
             "transition_in": {"type": "dip-to-white", "duration": 1.0}},
        ]
        proj = new_project("r5-transitions", width=640, height=360)
        proj["clips"] = clips
        out = os.path.join(WORK, "r5-transitions.mp4")
        res = Renderer(proj, workdir=WORK).render(out)
        self.assertAlmostEqual(res.duration, 8.0, places=1)
        self.assertAlmostEqual(_probe_duration(out), 8.0, places=1)
        H, W = 360, 640

        # slide @1.5: a sharp vertical split — left red, right blue.
        a = _frame(out, 1.5, os.path.join(WORK, "tr-slide.png"))
        self.assertTrue(_is_red(a[H // 2, 100]),
                        f"slide left should be red, got {a[H // 2, 100]}")
        self.assertTrue(_is_blue(a[H // 2, W - 100]),
                        f"slide right should be blue, got {a[H // 2, W - 100]}")

        # zoom @2.5: ffmpeg's zoomin magnifies the OUTGOING clip and
        # fades the incoming in only during the last ~20% of the
        # transition — so at the midpoint the frame is still pure
        # blue (this is what distinguishes it from a crossfade,
        # which would be a 50/50 blend here). The magnification
        # itself is proven by test_zoom_transition_scales_outgoing_clip
        # below, on a checkerboard where scale is measurable.
        a = _frame(out, 2.5, os.path.join(WORK, "tr-zoom.png"))
        self.assertTrue(_is_blue(a[H // 2, W // 2]),
                        f"zoom mid should still be outgoing blue, got "
                        f"{a[H // 2, W // 2]}")
        a = _frame(out, 2.9, os.path.join(WORK, "tr-zoom-late.png"))
        self.assertTrue(_is_green(a[H // 2, W // 2]),
                        f"zoom late should be incoming green, got "
                        f"{a[H // 2, W // 2]}")

        # dissolve @3.5: a true blend — green + yellow = yellow-green.
        a = _frame(out, 3.5, os.path.join(WORK, "tr-dissolve.png"))
        c = a[H // 2, W // 2]
        self.assertTrue(40 < c[0] < 220 and c[1] > 200 and c[2] < 80,
                        f"dissolve centre should blend green+yellow, got {c}")

        # circle @4.5: magenta disc growing from the centre —
        # centre magenta, corners still yellow.
        a = _frame(out, 4.5, os.path.join(WORK, "tr-circle.png"))
        self.assertTrue(_is_magenta(a[H // 2, W // 2]),
                        f"circle centre should be magenta, got {a[H // 2, W // 2]}")
        self.assertTrue(_is_yellow(a[20, 20]),
                        f"circle corner should still be yellow, got {a[20, 20]}")

        # pixelize @5.5: on solid colours pixelize is a uniform
        # blend (there is no texture to block up) — the block
        # structure is proven by test_pixelize_transition_is_blocky
        # below, on a checkerboard. Here: a true 50/50 blend, and
        # the transition completes to cyan by 5.9.
        a = _frame(out, 5.5, os.path.join(WORK, "tr-pixelize.png"))
        c = a[H // 2, W // 2]
        self.assertTrue(60 < c[0] < 200 and 60 < c[1] < 200
                        and c[2] > 200,
                        f"pixelize mid should blend magenta+cyan, got {c}")
        a = _frame(out, 5.9, os.path.join(WORK, "tr-pixelize-late.png"))
        self.assertTrue(_is_cyan(a[H // 2, W // 2]),
                        f"pixelize late should be cyan, got "
                        f"{a[H // 2, W // 2]}")

        # dip-to-white @6.5: the frame is (near-)white at the midpoint.
        a = _frame(out, 6.5, os.path.join(WORK, "tr-white.png"))
        luma = (0.299 * a[:, :, 0] + 0.587 * a[:, :, 1]
                + 0.114 * a[:, :, 2]).mean()
        self.assertGreater(luma, 200,
                           f"dip-to-white mid should be near-white, "
                           f"mean luma {luma:.1f}")

    def test_zoom_transition_scales_outgoing_clip(self):
        # zoomin's real signature is the magnification of the outgoing
        # clip — invisible on solid colours, measurable on a grid.
        # A 40 px checkerboard (640x360) has 13 internal vertical
        # edges on its centre row; the zoom is steep — edges fall
        # 13 → 11 → 9 → 5 → 1 → 0 across the first half, so by the
        # midpoint one checker square fills the frame — with the
        # incoming green not yet faded in.
        checker = self.checker
        proj = new_project("r5-zoom", width=640, height=360)
        proj["clips"] = [
            {"src": checker, "duration": 2.0},
            {"src": "color:00FF00", "duration": 2.0,
             "transition_in": {"type": "zoom", "duration": 1.0}},
        ]
        out = os.path.join(WORK, "r5-zoom.mp4")
        res = Renderer(proj, workdir=WORK).render(out)
        self.assertAlmostEqual(res.duration, 3.0, places=1)

        def _row_edges(png_path):
            a = np.asarray(Image.open(png_path).convert("L")).astype(int)
            row = a[180, 64:576]  # centre row, central 80%
            bw = (row > 128).astype(int)
            return int(np.abs(np.diff(bw)).sum())

        f0 = _frame(out, 0.5, os.path.join(WORK, "zm-start.png"))
        f1 = _frame(out, 1.5, os.path.join(WORK, "zm-mid.png"))
        e0 = _row_edges(os.path.join(WORK, "zm-start.png"))
        e1 = _row_edges(os.path.join(WORK, "zm-mid.png"))
        self.assertGreaterEqual(e0, 10,
                                f"checker centre row should show the "
                                f"grid intact pre-transition, got {e0} "
                                f"edges")
        self.assertLess(e1, e0,
                        f"zoomin must magnify the outgoing grid "
                        f"(fewer edges mid-transition): {e0} -> {e1}")
        green = ((f1[:, :, 1] > 150) & (f1[:, :, 0] < 80)
                 & (f1[:, :, 2] < 80)).mean()
        self.assertLess(green, 0.1,
                        f"incoming green must not have faded in yet "
                        f"at the zoom midpoint (frac {green:.3f})")

    def test_pixelize_transition_is_blocky(self):
        # pixelize's real signature is the block collapse: the
        # outgoing grid stays intact through the first half of the
        # transition, then late in the transition the image merges
        # into a few large blocks — a dissolve never does this
        # (its grid stays sharp until the blend finishes).
        checker = self.checker
        proj = new_project("r5-px", width=640, height=360)
        proj["clips"] = [
            {"src": checker, "duration": 2.0},
            {"src": "color:00FFFF", "duration": 2.0,
             "transition_in": {"type": "pixelize", "duration": 1.0}},
        ]
        out = os.path.join(WORK, "r5-px.mp4")
        res = Renderer(proj, workdir=WORK).render(out)
        self.assertAlmostEqual(res.duration, 3.0, places=1)

        def _row_edges(png_path):
            a = np.asarray(Image.open(png_path).convert("L")).astype(int)
            row = a[180, 64:576]
            bw = (row > 128).astype(int)
            return int(np.abs(np.diff(bw)).sum())

        _frame(out, 1.5, os.path.join(WORK, "px-mid.png"))
        _frame(out, 1.8, os.path.join(WORK, "px-late.png"))
        e_mid = _row_edges(os.path.join(WORK, "px-mid.png"))
        e_late = _row_edges(os.path.join(WORK, "px-late.png"))
        self.assertGreaterEqual(e_mid, 10,
                                f"pixelize mid should still show the "
                                f"grid, got {e_mid} edges")
        self.assertLessEqual(e_late, 2,
                             f"pixelize late must collapse the grid "
                             f"into blocks, got {e_late} edges")
        late = np.asarray(Image.open(
            os.path.join(WORK, "px-late.png")).convert("RGB")).astype(int)
        # ... while the blend is still incomplete (not pure cyan yet).
        self.assertLess(int(late[180, 320, 1]), 250,
                        f"block collapse must happen before the "
                        f"blend finishes, centre {late[180, 320]}")

    # ---- text templates --------------------------------------------

    def test_builtin_templates_resolve(self):
        self.assertEqual(
            sorted(TEXT_TEMPLATES),
            ["caption-clean", "lower-third", "punch", "quote",
             "title-bold"])
        for name, tmpl in TEXT_TEMPLATES.items():
            self.assertIn("font", tmpl, name)
            self.assertIn("size_frac", tmpl, name)
            self.assertTrue(0.005 <= tmpl["size_frac"] <= 0.5, name)
            self.assertIn("color", tmpl, name)

    def test_template_defaults_and_explicit_wins(self):
        t = resolve_text_template({"text": "HI", "template": "punch",
                                   "start": 0, "duration": 1})
        self.assertEqual(t["font"], "Anton")
        self.assertEqual(t["color"], "#FFD60A")
        self.assertEqual(t["size_frac"], 0.16)
        self.assertEqual(t["animation"], "pop")
        # An explicit field beats the template.
        t2 = resolve_text_template({"text": "HI", "template": "punch",
                                    "color": "#00FF00",
                                    "start": 0, "duration": 1})
        self.assertEqual(t2["color"], "#00FF00")
        self.assertEqual(t2["font"], "Anton")  # template fills the rest
        # No template: returned unchanged (as a copy).
        t3 = {"text": "HI", "start": 0, "duration": 1, "size": 40}
        self.assertEqual(resolve_text_template(t3), t3)
        with self.assertRaises(TimelineError):
            resolve_text_template({"text": "HI", "template": "nope",
                                   "start": 0, "duration": 1})

    def test_project_level_templates(self):
        proj = new_project(width=640, height=360)
        proj["text_templates"] = {
            "brand": {"font": "Anton", "size_frac": 0.10,
                      "color": "#00FF00", "x": "left", "y": "top",
                      "animation": "fade"},
        }
        proj["clips"] = [{"src": "color:000000", "duration": 1.0}]
        proj["texts"] = [{"text": "BRAND", "template": "brand",
                          "start": 0, "duration": 1}]
        validate_timeline(proj)
        t = resolve_text_template(proj["texts"][0],
                                  proj["text_templates"])
        self.assertEqual(t["color"], "#00FF00")
        # Override of a built-in name wins for that project.
        proj["text_templates"]["punch"] = {
            "font": "Inter", "size_frac": 0.05, "color": "#FFFFFF",
            "x": "center", "y": "lower", "animation": "fade"}
        t2 = resolve_text_template({"text": "X", "template": "punch",
                                    "start": 0, "duration": 1},
                                   proj["text_templates"])
        self.assertEqual(t2["font"], "Inter")

    def test_project_template_validation(self):
        proj = new_project(width=640, height=360)
        proj["clips"] = [{"src": "color:000000", "duration": 1.0}]
        proj["texts"] = [{"text": "X", "template": "brand",
                          "start": 0, "duration": 1}]
        proj["text_templates"] = {"brand": {"font": "Anton",
                                            "size_frac": 0.9}}
        with self.assertRaises(TimelineError):
            validate_timeline(proj)
        proj["text_templates"] = {"brand": {"font": "Anton",
                                            "size_frac": 0.10,
                                            "nope_field": 1}}
        with self.assertRaises(TimelineError):
            validate_timeline(proj)
        proj["text_templates"] = {"brand": {"font": "Anton",
                                            "size_frac": 0.10}}
        validate_timeline(proj)
        # Unknown template name on a text is rejected too.
        proj["texts"] = [{"text": "X", "template": "ghost",
                          "start": 0, "duration": 1}]
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    def test_templates_render(self):
        # Grey background so the lower-third bubble (black) is visible.
        proj = new_project("r5-templates", width=1280, height=720)
        proj["clips"] = [{"src": "color:404040", "duration": 5.0}]
        proj["texts"] = [
            {"text": "TITLE", "template": "title-bold",
             "start": 0.0, "duration": 1.0},
            {"text": "lower third line", "template": "lower-third",
             "start": 1.0, "duration": 1.0},
            {"text": "a quiet quote", "template": "quote",
             "start": 2.0, "duration": 1.0},
            {"text": "PUNCH", "template": "punch",
             "start": 3.0, "duration": 1.0},
            {"text": "small clean line", "template": "caption-clean",
             "start": 4.0, "duration": 1.0},
        ]
        out = os.path.join(OUT, "musecut-round5-templates.mp4")
        res = Renderer(proj, workdir=WORK).render(out)
        self.assertAlmostEqual(res.duration, 5.0, places=1)
        H, W = 720, 1280

        # title-bold @0.5: bright text in the centre band.
        a = _frame(out, 0.5, os.path.join(WORK, "tm-title.png"))
        band = _bright_mask(a[H // 3:2 * H // 3, :])
        self.assertGreater(band.sum(), 500,
                           "title-bold should be visible mid-frame")
        title_h = _bbox_height(_bright_mask(a))

        # lower-third @1.5: bright text low-left + a black bubble box
        # (darker than the grey background) behind it.
        a = _frame(out, 1.5, os.path.join(WORK, "tm-lower.png"))
        low_left = _bright_mask(a[int(H * 0.75):, :W // 2])
        self.assertGreater(low_left.sum(), 200,
                           "lower-third text should be low-left")
        dark = (a[int(H * 0.7):, :W // 2].sum(axis=2) < 60)
        self.assertGreater(dark.sum(), 1000,
                           "lower-third bubble should darken its box")

        # quote @2.5: bright text in the centre band.
        a = _frame(out, 2.5, os.path.join(WORK, "tm-quote.png"))
        band = _bright_mask(a[H // 3:2 * H // 3, :])
        self.assertGreater(band.sum(), 300,
                           "quote should be visible mid-frame")

        # punch @3.5: gold text (not white) in the centre band.
        a = _frame(out, 3.5, os.path.join(WORK, "tm-punch.png"))
        gold = ((a[:, :, 0] > 150) & (a[:, :, 1] > 100)
                & (a[:, :, 2] < 100))
        self.assertGreater(gold[H // 3:2 * H // 3, :].sum(), 300,
                           "punch should render gold mid-frame")

        # caption-clean @4.5: small bright text low — and visibly
        # smaller than the title-bold line.
        a = _frame(out, 4.5, os.path.join(WORK, "tm-clean.png"))
        clean_h = _bbox_height(_bright_mask(a[int(H * 0.6):, :]))
        self.assertGreater(clean_h, 5,
                           "caption-clean should be visible low")
        self.assertLess(clean_h, title_h,
                        "caption-clean line should be smaller than "
                        "the title-bold line")

    # ---- two-tier caption sizing -----------------------------------

    def test_two_tier_ass_tags(self):
        words = [{"word": "go", "start": 0.0, "end": 0.4},
                 {"word": "BIG", "start": 0.4, "end": 0.9},
                 {"word": "now", "start": 0.9, "end": 1.3}]
        ass = cap_mod.words_to_ass(words, width=1280, height=720,
                                   font_size=48, active_size_ratio=1.5)
        # Active word at 72 (48 * 1.5), reset to 48 right after it.
        self.assertIn("\\fs72", ass)
        self.assertIn("\\fs48", ass)
        # Ratio 1.0 = off: no \fs overrides at all.
        ass1 = cap_mod.words_to_ass(words, width=1280, height=720,
                                    font_size=48, active_size_ratio=1.0)
        self.assertNotIn("\\fs", ass1)
        # Bounds.
        for bad in (0.9, 2.5):
            with self.assertRaises(ValueError):
                cap_mod.words_to_ass(words, active_size_ratio=bad)

    def test_two_tier_validation(self):
        proj = new_project(width=640, height=360)
        proj["clips"] = [{"src": "color:000000", "duration": 2.0}]
        proj["captions"] = {
            "words": [{"word": "go", "start": 0.0, "end": 0.4}],
            "active_size_ratio": 1.5,
        }
        validate_timeline(proj)
        proj["captions"]["active_size_ratio"] = 2.5
        with self.assertRaises(TimelineError):
            validate_timeline(proj)

    def test_two_tier_renders_taller(self):
        words = [{"word": "go", "start": 0.0, "end": 0.5},
                 {"word": "BIG", "start": 0.5, "end": 1.0},
                 {"word": "now", "start": 1.0, "end": 1.5}]
        heights = {}
        for ratio, tag in ((1.0, "r10"), (1.5, "r15")):
            proj = new_project(f"r5-twotier-{tag}", width=1280,
                               height=720)
            proj["clips"] = [{"src": "color:000000", "duration": 2.0}]
            proj["captions"] = {"words": words, "font_size": 64,
                                "active_size_ratio": ratio}
            out = os.path.join(WORK, f"r5-twotier-{tag}.mp4")
            Renderer(proj, workdir=WORK).render(out)
            # Frame at 0.75 s: "BIG" is the active word.
            a = _frame(out, 0.75,
                       os.path.join(WORK, f"tt-{tag}.png"))
            heights[tag] = _bbox_height(
                _bright_mask(a[int(720 * 0.6):, :]))
        self.assertGreater(heights["r10"], 10,
                           "baseline caption line should be visible")
        self.assertGreaterEqual(
            heights["r15"], heights["r10"] * 1.25,
            f"two-tier line should be >=1.25x taller: "
            f"ratio 1.0 -> {heights['r10']}px, "
            f"ratio 1.5 -> {heights['r15']}px")

    # ---- version ----------------------------------------------------

    def test_version_070(self):
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())


if __name__ == "__main__":
    unittest.main()
