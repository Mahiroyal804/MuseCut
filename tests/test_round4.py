"""Round-4 tests (v0.6.0).

Covers round 4 (2026-11-21):
* reframe handover fix — lag-free g-h filter tracking + handover
  to the largest in-frame subject when the tracked one exits;
  smart reframe at 4 fps, trimmed to the tracked span.
* AI background removal — RMBG-1.4 ONNX (verified Hugging Face
  mirror) + the ``background_remove`` clip field in the render
  engine (colour / image / transparent ProRes 4444 cutout).
* version unified to 0.6.0 everywhere.

Real footage: the same minivan clip as round 3
(pexels_1192116.mp4, 2 s slice from t=2 s). The background
tests reuse ONE mask video built in setUpClass (sample_fps=2,
~17 s of inference) so the suite does not re-run the model
per test.
"""

import json
import math
import os
import subprocess
import unittest

from musecut import __version__
from musecut import ai as ai_mod
from musecut import bgremove as bg_mod
from musecut import providers as prov_mod
from musecut import track as track_mod
from musecut.render import Renderer, probe_media
from musecut.timeline import TimelineError, load_project, validate_timeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-round4")
MEDIA = os.path.join(WORK, "media")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
VAN_CLIP = os.path.join(POOL, "pexels_1192116.mp4")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


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
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png_path).convert("RGB")).astype(int)
    m = (a[:, :, 0] > 200) & (a[:, :, 1] < 80) & (a[:, :, 2] > 200)
    return float(m.mean())


def _car_centre(png_path):
    """Centre-x (fraction) of the highest-confidence car in a frame."""
    from PIL import Image
    session = track_mod._load_session()
    img = Image.open(png_path).convert("RGB")
    dets = [d for d in track_mod.detect_frame(session, img, 0.25, 0.45)
            if d["class"] == "car"]
    assert dets, f"no car detected in {png_path}"
    best = max(dets, key=lambda d: d["conf"])
    x1, _, x2, _ = best["box"]
    return round(((x1 + x2) / 2) / img.size[0], 3), best["conf"]


NEEDS_BG = unittest.skipUnless(
    bg_mod.model_ready(),
    "RMBG-1.4 model not on disk — run with the model downloaded")
NEEDS_BG_TRACK = unittest.skipUnless(
    bg_mod.model_ready() and track_mod.model_ready(),
    "RMBG-1.4 / YOLOv5n models not on disk — run with the models "
    "downloaded")


class Round4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(MEDIA, exist_ok=True)
        # 2 s slice of the minivan clip, starting where the car is
        # tracked (t=2s in the source) — same slice as round 3.
        cls.van2s = os.path.join(MEDIA, "van2s.mp4")
        if not os.path.isfile(cls.van2s) and os.path.isfile(VAN_CLIP):
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", "2", "-i", VAN_CLIP, "-t", "2", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", "-an", cls.van2s])
            assert res.returncode == 0, res.stderr
        # One mask video for the background tests (sample_fps=2:
        # 4 masks, ~17 s of inference — the pipeline, not the
        # quality, is what these tests assert).
        cls.mask = os.path.join(MEDIA, "van_mask.mp4")
        if not os.path.isfile(cls.mask) and os.path.isfile(cls.van2s):
            bg_mod.make_mask_video(cls.van2s, cls.mask, sample_fps=2.0,
                                   workdir=os.path.join(WORK, "bgwork"))

    # ---- version + providers ------------------------------------
    def test_version_unified_060(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "README.md")) as f:
            self.assertIn("0.21.0", f.read())

    @NEEDS_BG
    def test_providers_rmbg_registered(self):
        info = prov_mod.KNOWN_MODELS["rmbg-1-4"]
        self.assertEqual(info["size_bytes"], 176153355)
        self.assertIn("huggingface.co/briaai/RMBG-1.4", info["url"])
        self.assertIn("NON-COMMERCIAL", info["license"])
        st = prov_mod.model_status("rmbg-1-4")
        self.assertEqual(st["status"], "present")
        backs = {b["name"]: b for b in prov_mod.backend_status()["backends"]}
        self.assertEqual(backs["onnx-model:rmbg-1-4"]["status"], "wired")

    # ---- g-h filter: no lag on linear motion --------------------
    def test_alpha_beta_no_lag(self):
        filt = track_mod._AlphaBeta()
        # Linear motion, 0.5 s steps, no noise: the filter must
        # track with ~zero lag (the moving average it replaces
        # lagged by half a step on this shape).
        true = [0.10 + 0.10 * i for i in range(6)]
        got = []
        for i, x in enumerate(true):
            t = i * 0.5
            if i == 0:
                filt.init([x, 0.5, 0.2, 0.1], t)
                got.append(x)
            else:
                got.append(filt.update([x, 0.5, 0.2, 0.1], t)[0])
        self.assertLess(max(abs(g - t) for g, t in zip(got, true)), 0.02)
        # Static + jitter: the filter must shrink the noise.
        filt2 = track_mod._AlphaBeta()
        noisy = [0.5 + s for s in (0.03, -0.02, 0.025, -0.015, 0.02)]
        out = []
        for i, x in enumerate(noisy):
            t = i * 0.5
            if i == 0:
                filt2.init([x, 0.5, 0.2, 0.1], t)
                out.append(x)
            else:
                out.append(filt2.update([x, 0.5, 0.2, 0.1], t)[0])
        import statistics
        self.assertLess(statistics.pstdev(out), statistics.pstdev(noisy))

    # ---- tracker: handover on the van clip ----------------------
    def test_tracker_handover_van(self):
        if not os.path.isfile(self.van2s) or not track_mod.model_ready():
            self.skipTest(
                "tracking needs the van clip + YOLOv5n model")
        tr = track_mod.track_object(self.van2s, class_name="car",
                                    sample_fps=4.0)
        frames = tr["frames"]
        # Grey car first (no lag at t=0 — the round-3 defect).
        self.assertAlmostEqual(frames[0]["x"], 0.48, delta=0.03)
        # Exactly one handover, at the exit, to the black sedan.
        self.assertEqual(len(tr["handovers"]), 1)
        hov = tr["handovers"][0]
        self.assertAlmostEqual(hov["time"], 1.5, delta=0.01)
        self.assertLess(hov["from"][0], 0.12)   # grey almost out
        self.assertAlmostEqual(hov["to"][0], 0.50, delta=0.05)
        # Track continues past the handover (same span).
        self.assertGreater(frames[-1]["time"], hov["time"])
        self.assertAlmostEqual(frames[-1]["x"], 0.335, delta=0.05)
        # G-h lag on the grey phase: raw centres at 4 fps were
        # 0.48/0.393/0.318/0.215/0.153/0.107 — filtered must stay
        # within 0.03 (moving average was 0.086 off).
        raw = [0.48, 0.393, 0.318, 0.215, 0.153, 0.107]
        got = [f["x"] for f in frames[:6]]
        self.assertLess(max(abs(g - r) for g, r in zip(got, raw)), 0.03)

    # ---- smart reframe: subject centred through the handover ----
    def test_reframe_fix_renders(self):
        if not os.path.isfile(self.van2s) or not track_mod.model_ready():
            self.skipTest(
                "smart reframe needs the van clip + YOLOv5n model")
        tpath = os.path.join(WORK, "reframe4.json")
        ai_mod.smart_reframe(self.van2s, tpath, class_name="car",
                             width=720, height=1280)
        proj = load_project(tpath)
        # Trimmed to the tracked span (1.75 s, not the 2.0 s clip).
        self.assertAlmostEqual(proj["clips"][0]["duration"], 1.75,
                               delta=0.05)
        out = os.path.join(WORK, "reframe4.mp4")
        Renderer(proj).render(out, quiet=True)
        probe = _probe(out, "codec_type,width,height,codec_name")
        self.assertEqual(_vstream(probe)["width"], 720)
        self.assertEqual(_vstream(probe)["height"], 1280)
        # Subject centred at every sampled time (grey phase and
        # the black sedan after the handover). Round 3 measured
        # 0.717 / 0.596 / 0.268 here — the defect this round fixes.
        for t, want in ((0.0, 0.51), (0.5, 0.51), (1.0, 0.50),
                        (1.5, 0.50)):
            frame = os.path.join(WORK, f"r4_{t}.png")
            res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                        "-ss", str(t), "-i", out, "-frames:v", "1", frame])
            self.assertEqual(res.returncode, 0, res.stderr)
            cx, conf = _car_centre(frame)
            self.assertAlmostEqual(cx, want, delta=0.08,
                                   msg=f"t={t}s centre {cx} (conf {conf})")

    # ---- RMBG model ----------------------------------------------
    @NEEDS_BG
    def test_rmbg_session(self):
        self.assertTrue(bg_mod.model_ready())
        session = bg_mod._load_session()
        ins = [(i.name, list(i.shape)) for i in session.get_inputs()]
        self.assertEqual(ins, [("input", ["batch_size", 3, 1024, 1024])])
        outs = [o.name for o in session.get_outputs()]
        self.assertEqual(outs, ["output"])

    def test_segment_frame_mask(self):
        if not os.path.isfile(self.van2s) or not bg_mod.model_ready():
            self.skipTest(
                "mask segmentation needs the van clip + RMBG-1.4 model")
        from PIL import Image
        session = bg_mod._load_session()
        frame = os.path.join(WORK, "seg_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", self.van2s, "-frames:v", "1",
                    frame])
        self.assertEqual(res.returncode, 0, res.stderr)
        mask = bg_mod.segment_frame(
            session, Image.open(frame).convert("RGB"))
        self.assertEqual(mask.size, (1280, 720))
        # Stats computed directly (no temp file needed).
        import numpy as np
        arr = np.asarray(mask, dtype=np.float32) / 255.0
        self.assertGreater(float((arr > 0.9).mean()), 0.02)
        self.assertGreater(float((arr < 0.1).mean()), 0.3)
        self.assertLess(float(arr.mean()), 0.5)

    # ---- background removal through the engine -------------------
    def test_remove_background_renders(self):
        if not os.path.isfile(self.van2s) or not os.path.isfile(self.mask):
            self.skipTest(
                "background removal needs the van clip + mask video")
        out = os.path.join(WORK, "bgremove.mp4")
        ai_mod.remove_background(
            self.van2s, out, background="#FF00FF",
            mask_video=self.mask)
        probe = _probe(out, "codec_type,width,height,codec_name")
        self.assertEqual(_vstream(probe)["width"], 1280)
        self.assertEqual(_vstream(probe)["height"], 720)
        frame = os.path.join(WORK, "bg_frame.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-ss", "0.5", "-i", out, "-frames:v", "1", frame])
        self.assertEqual(res.returncode, 0, res.stderr)
        # Background is magenta; the car is not.
        self.assertGreater(_magenta_frac(frame), 0.5)
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(frame).convert("RGB")).astype(int)
        car_px = tuple(a[335, 560])
        self.assertFalse(car_px[0] > 200 and car_px[1] < 80
                         and car_px[2] > 200,
                         f"car pixel is magenta: {car_px}")

    def test_auto_cutout_alpha(self):
        if not os.path.isfile(self.van2s) or not os.path.isfile(self.mask):
            self.skipTest(
                "auto cutout needs the van clip + mask video")
        out = os.path.join(WORK, "cutout.mov")
        ai_mod.auto_cutout(self.van2s, out, mask_video=self.mask)
        probe = _probe(out, "codec_type,pix_fmt,codec_name")
        self.assertEqual(_vstream(probe)["pix_fmt"], "yuva444p12le")
        alpha = os.path.join(WORK, "cutout_alpha.png")
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error",
                    "-i", out, "-vf", "alphaextract,format=gray",
                    "-frames:v", "1", alpha])
        self.assertEqual(res.returncode, 0, res.stderr)
        from PIL import Image
        import numpy as np
        arr = np.asarray(Image.open(alpha).convert("L")).astype(float)
        self.assertGreater(float((arr < 5).mean()), 0.5)    # bg clear
        self.assertGreater(float((arr > 250).mean()), 0.02)  # fg opaque

    def test_background_remove_validation(self):
        base = {"version": 1, "name": "t", "width": 640, "height": 360,
                "fps": 30, "clips": [], "overlays": [], "texts": [],
                "audio": [], "captions": None, "endcard": None,
                "export": None, "canvas": None, "sequences": {},
                "markers": [], "adjustment_layers": []}

        def _proj(clip):
            p = dict(base)
            p["clips"] = [clip]
            return p

        # Missing mask file.
        with self.assertRaises(TimelineError):
            validate_timeline(_proj({
                "src": "x.mp4", "fit": "cover",
                "background_remove": {"mask": "/no/such/mask.mp4",
                                      "background": "#FF00FF"}}))
        # Transparent + transform keyframes.
        with self.assertRaises(TimelineError):
            validate_timeline(_proj({
                "src": "x.mp4", "fit": "cover",
                "keyframes": {"position": [
                    {"time": 0.0, "value": [0.5, 0.5]}]},
                "background_remove": {"mask": self.mask,
                                      "background": "transparent"}}))
        # Transparent needs fit cover.
        with self.assertRaises(TimelineError):
            validate_timeline(_proj({
                "src": "x.mp4", "fit": "contain",
                "background_remove": {"mask": self.mask,
                                      "background": "transparent"}}))
        # Speed change breaks mask alignment.
        with self.assertRaises(TimelineError):
            validate_timeline(_proj({
                "src": "x.mp4", "fit": "cover", "speed": 2.0,
                "background_remove": {"mask": self.mask,
                                      "background": "#FF00FF"}}))

    # ---- capabilities report the new feature honestly ------------
    @NEEDS_BG_TRACK
    def test_capabilities_bg_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["ai_background_removal"]["status"], "done")
        self.assertEqual(caps["auto_cutout"]["status"], "done")
        # (round 6) ai_matting is now done — classical edge
        # refinement (trimap + guided filter + feather +
        # decontamination + hardness); the pin moves with it.
        self.assertEqual(caps["ai_matting"]["status"], "done")
        self.assertEqual(caps["track_object"]["status"], "done")
        self.assertEqual(caps["smart_reframe"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
