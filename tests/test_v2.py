"""MuseCut v2 tests — CapCut RE findings, ffmpeg-level items.

Covers findings §5 items 2-7, 11, 14:
* export codecs: ProRes, AV1, GIF, WAV/MP3 audio-only
* per-resolution bitrate ladder (command construction + render)
* adjust sliders: exposure / highlights / shadows / clarity /
  dehaze / fade / sharpen (render + visible effect)
* canvas backgrounds: colour / image / blur
* text upgrades: glow / bubble / underline / bold (render) +
  caption karaoke colours / bloom / bubble (ASS output)
* motion blur
* auto volume balancing (loudnorm per track)
* second smoke-test pass: freeze frame, reverse, masks, chroma key,
  vidstab, speed curves — each rendered and verified, not just
  implemented
* MCP: the 12 original tools still work + set_export / set_canvas /
  render-with-codec / caption-option passthrough
* ML stubs raise NotImplementedError (never faked)

Small canvases (320x180) and 1-3 s clips keep the suite fast.
Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMPDIR", os.path.expanduser("~/workspace/.tmp"))

from musecut import ai as ai_mod  # noqa: E402
from musecut import captions as cap_mod  # noqa: E402
from musecut.render import Renderer, probe_media  # noqa: E402
from musecut.timeline import TimelineError, validate_timeline  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/.tmp/musecut-test-v2")
MEDIA = os.path.join(WORK, "media")


def _run(cmd, **kw):
    env = dict(os.environ)
    env.pop("no_proxy", None)
    env.pop("NO_PROXY", None)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, **kw)


def _make_media():
    os.makedirs(MEDIA, exist_ok=True)
    jobs = [
        (["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", os.path.join(MEDIA, "clip1.mp4")]),
        (["-f", "lavfi", "-i", "smptebars=size=320x180:rate=30:duration=3",
          "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", os.path.join(MEDIA, "clip2.mp4")]),
        (["-f", "lavfi", "-i", "sine=frequency=880:duration=5",
          "-c:a", "libmp3lame", "-q:a", "4",
          os.path.join(MEDIA, "voice.mp3")]),
        (["-f", "lavfi", "-i", "sine=frequency=220:duration=8",
          "-c:a", "libmp3lame", "-q:a", "4",
          os.path.join(MEDIA, "music.mp3")]),
        # Deterministic brightness ramp: luma 0 -> 255 over 3 s.
        (["-f", "lavfi", "-i", "nullsrc=size=320x180:rate=30:duration=3",
          "-vf", "geq=lum_expr='255*N/90':cb_expr='128':cr_expr='128'",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "ramp.mp4")]),
        # Solid green clip (chroma-key target).
        (["-f", "lavfi", "-i", "color=c=0x00FF00:size=320x180:rate=30:duration=2",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          os.path.join(MEDIA, "green.mp4")]),
        # One still frame (background image for canvas tests).
        (["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.1",
          "-frames:v", "1", os.path.join(MEDIA, "bg.png")]),
    ]
    for argv in jobs:
        res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "error"] + argv)
        assert res.returncode == 0, res.stderr
    # A small .cube LUT: black -> 0.2 grey, white stays white
    # (brightens shadows). Size 2: r varies fastest, then g, then b.
    with open(os.path.join(MEDIA, "bright.cube"), "w") as f:
        f.write("LUT_3D_SIZE 2\n")
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    f.write(f"{1.0 if r else 0.2} {1.0 if g else 0.2} "
                            f"{1.0 if b else 0.2}\n")


def _ffprobe(path):
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-show_entries", "format=duration",
                "-of", "json", path])
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _yavg(path, t):
    """Mean luma of the frame at time t (signalstats + metadata print).

    NOTE: signalstats only *sets* frame metadata — the values only
    reach the log through the ``metadata`` filter's print mode.
    """
    res = _run(["ffmpeg", "-hide_banner", "-y", "-v", "info",
                "-ss", str(t), "-i", path,
                "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", res.stderr or "")
    assert m, f"no YAVG in signalstats output for {path}@{t}"
    return float(m.group(1))


def _base_proj(clips, **kw):
    proj = {"version": 1, "name": "v2", "width": 320, "height": 180,
            "fps": 30, "background": "#000000", "clips": clips,
            "overlays": [], "texts": [], "audio": [],
            "captions": None, "endcard": None, "export": None,
            "canvas": None, "sequences": {}, "markers": [],
            "adjustment_layers": []}
    proj.update(kw)
    return proj


class V2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK, exist_ok=True)
        _make_media()

    def _render(self, proj, out, **rkw):
        validate_timeline(proj)
        r = Renderer(proj, workdir=os.path.join(WORK, "render"))
        return r.render(os.path.join(WORK, out), quiet=True, **rkw)

    # ---- export codecs -------------------------------------------
    def test_export_prores(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}],
                          export={"codec": "prores422hq"})
        self._render(proj, "prores.mov")
        probe = _ffprobe(os.path.join(WORK, "prores.mov"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "prores")

    def test_export_av1(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}],
                          export={"codec": "av1"})
        self._render(proj, "av1.mp4")
        probe = _ffprobe(os.path.join(WORK, "av1.mp4"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "av1")

    def test_export_gif(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}],
                          export={"codec": "gif"})
        self._render(proj, "anim.gif")
        probe = _ffprobe(os.path.join(WORK, "anim.gif"))
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "gif")
        self.assertFalse(any(s["codec_type"] == "audio"
                             for s in probe["streams"]))

    def test_export_audio_only(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}],
                          audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                                  "role": "voice", "duration": 2}],
                          export={"codec": "wav"})
        self._render(proj, "out.wav")
        probe = _ffprobe(os.path.join(WORK, "out.wav"))
        a = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        self.assertEqual(a["codec_name"], "pcm_s16le")
        proj2 = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                             "duration": 2}],
                           audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                                   "role": "voice", "duration": 2}],
                           export={"codec": "mp3"})
        self._render(proj2, "out.mp3")
        probe2 = _ffprobe(os.path.join(WORK, "out.mp3"))
        a2 = next(s for s in probe2["streams"] if s["codec_type"] == "audio")
        self.assertEqual(a2["codec_name"], "mp3")

    # ---- bitrate ladder ------------------------------------------
    def test_bitrate_ladder(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}],
                          export={"codec": "h264",
                                  "bitrate_ladder": {"sd": "2M", "hd": "4M",
                                                     "fullhd": "8M",
                                                     "2k": "16M",
                                                     "4k": "32M",
                                                     "gop": 60,
                                                     "profile": "high"}})
        r = Renderer(proj, workdir=os.path.join(WORK, "render"))
        cmd, _, info = r.build_command(os.path.join(WORK, "ladder.mp4"))
        self.assertIn("-b:v", cmd)
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "2000000")  # sd: 180p
        self.assertIn("-g", cmd)
        self.assertTrue(info["bitrate_ladder"])
        # Ladder is ignored for ProRes (no -b:v in the command).
        proj2 = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                             "duration": 2}],
                           export={"codec": "prores422hq",
                                   "bitrate_ladder": {"sd": "2M"}})
        r2 = Renderer(proj2, workdir=os.path.join(WORK, "render"))
        cmd2, _, _ = r2.build_command(os.path.join(WORK, "ladder2.mov"))
        self.assertNotIn("-b:v", cmd2)

    # ---- adjust sliders ------------------------------------------
    def test_adjust_sliders_visible(self):
        base = _base_proj([{"src": "color:#404040", "duration": 2}])
        self._render(base, "adj_base.mp4")
        adj = _base_proj([{"src": "color:#404040", "duration": 2,
                           "effects": {"exposure": 1.0, "highlights": 0.5,
                                       "shadows": 0.5, "clarity": 0.5,
                                       "dehaze": 0.5, "fade": 0.3,
                                       "sharpen": 0.5}}])
        self._render(adj, "adj_sliders.mp4")
        y0 = _yavg(os.path.join(WORK, "adj_base.mp4"), 1.0)
        y1 = _yavg(os.path.join(WORK, "adj_sliders.mp4"), 1.0)
        # exposure +1 brightens the mid-grey frame clearly.
        self.assertGreater(y1 - y0, 10,
                           f"sliders had no visible effect: {y0} -> {y1}")

    # ---- canvas ---------------------------------------------------
    def test_canvas_variants(self):
        for ctype, extra in (("color", {"color": "#112233"}),
                             ("image", {"image": os.path.join(MEDIA, "bg.png")}),
                             ("blur", {"blur": 10})):
            canvas = {"type": ctype, **extra}
            proj = _base_proj(
                [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2}],
                canvas=canvas)
            out = f"canvas_{ctype}.mp4"
            self._render(proj, out)
            probe = _ffprobe(os.path.join(WORK, out))
            v = next(s for s in probe["streams"]
                     if s["codec_type"] == "video")
            self.assertEqual((v["width"], v["height"]), (320, 180))

    # ---- text upgrades + captions --------------------------------
    def test_text_upgrades_render(self):
        proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2}],
            texts=[{"text": "Glow test 100%", "start": 0, "duration": 2,
                    "font": "Anton", "size": 48,
                    "glow": {"color": "#FFD60A", "strength": 0.8},
                    "bubble": {"color": "#000000", "alpha": 0.6,
                               "padding": 10},
                    "underline": True, "bold_width": 2}])
        self._render(proj, "text_up.mp4")
        self.assertGreater(
            os.path.getsize(os.path.join(WORK, "text_up.mp4")), 5000)

    def test_caption_upgrades_ass(self):
        words = [{"word": "hello", "start": 0.0, "end": 0.4},
                 {"word": "world", "start": 0.4, "end": 0.9}]
        doc = cap_mod.words_to_ass(
            words, width=320, height=180, ktv_color="#00FF00",
            ktv_outline_color="#0000FF", ktv_shadow_color="#FF0000",
            bloom_color="#FFD60A", bloom_strength=0.6,
            bubble=True, bubble_color="#111111", underline=True,
            bold=True, italic=True)
        # karaoke colour override present (green = 00FF00 -> BGR 00FF00)
        self.assertIn("1c&H0000FF00", doc)
        # Style line is positional: Name, Fontname, Fontsize, colours,
        # Bold, Italic, Underline, ..., BorderStyle, Outline, Shadow.
        style = [ln for ln in doc.splitlines()
                 if ln.startswith("Style:")][0].split(",")
        self.assertEqual(style[7], "1")   # Bold
        self.assertEqual(style[8], "1")   # Italic
        self.assertEqual(style[9], "1")   # Underline
        self.assertEqual(style[15], "3")  # BorderStyle=3 (bubble box)
        # render the same captions end to end
        proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2}],
            captions={"style": "viral-gold", "words": words,
                      "ktv_color": "#00FF00", "bloom_color": "#FFD60A",
                      "bloom_strength": 0.6, "bubble": True})
        self._render(proj, "caps_up.mp4")
        self.assertTrue(os.path.isfile(os.path.join(WORK, "caps_up.mp4")))

    # ---- motion blur + balance -----------------------------------
    def test_motion_blur(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2, "motion_blur": {"frames": 8}}])
        self._render(proj, "mblur.mp4")
        probe = _ffprobe(os.path.join(WORK, "mblur.mp4"))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_balance(self):
        proj = _base_proj(
            [{"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2}],
            audio=[{"src": os.path.join(MEDIA, "voice.mp3"),
                    "role": "voice", "duration": 2, "balance": True}])
        self._render(proj, "balanced.mp4")
        probe = _ffprobe(os.path.join(WORK, "balanced.mp4"))
        self.assertTrue(any(s["codec_type"] == "audio"
                            for s in probe["streams"]))

    # ---- second smoke-test pass ----------------------------------
    def test_freeze_frame(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "ramp.mp4"),
                            "duration": 2, "freeze_at": 1.0}])
        self._render(proj, "frozen.mp4")
        y1 = _yavg(os.path.join(WORK, "frozen.mp4"), 0.2)
        y2 = _yavg(os.path.join(WORK, "frozen.mp4"), 1.8)
        self.assertAlmostEqual(y1, y2, delta=5,
                               msg=f"frozen frame changed: {y1} vs {y2}")

    def test_reverse(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "ramp.mp4"),
                            "duration": 3}])
        self._render(proj, "ramp_fwd.mp4")
        rev = _base_proj([{"src": os.path.join(MEDIA, "ramp.mp4"),
                           "duration": 3, "reverse": True}])
        self._render(rev, "ramp_rev.mp4")
        # Ramp goes dark->bright; reversed goes bright->dark.
        self.assertLess(_yavg(os.path.join(WORK, "ramp_fwd.mp4"), 0.1), 50)
        self.assertGreater(_yavg(os.path.join(WORK, "ramp_rev.mp4"), 0.1), 200)
        self.assertGreater(_yavg(os.path.join(WORK, "ramp_fwd.mp4"), 2.9), 200)
        self.assertLess(_yavg(os.path.join(WORK, "ramp_rev.mp4"), 2.9), 50)

    def test_mask(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2,
                            "mask": {"shape": "circle", "x": 0.5, "y": 0.5,
                                     "size": 0.3, "feather": 0.05,
                                     "effect": "blur"}}])
        self._render(proj, "masked.mp4")
        probe = _ffprobe(os.path.join(WORK, "masked.mp4"))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_chroma_key(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "green.mp4"),
                            "duration": 2,
                            "chroma_key": {"color": "0x00FF00",
                                           "similarity": 0.3,
                                           "blend": 0.1}}])
        self._render(proj, "keyed.mp4")
        # Solid green keyed out over black -> near-black frame.
        # (Limited-range black is Y=16, not 0.)
        self.assertLess(_yavg(os.path.join(WORK, "keyed.mp4"), 1.0), 20)

    def test_vidstab(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2, "stabilize": True}])
        self._render(proj, "stab.mp4")
        trfs = [f for f in os.listdir(os.path.join(WORK, "render"))
                if f.endswith(".trf")]
        self.assertTrue(trfs, "vidstab pass-1 .trf file was not created")
        probe = _ffprobe(os.path.join(WORK, "stab.mp4"))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 2.0,
                               delta=0.3)

    def test_speed_curve(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2,
                            "speed_curve": [{"time": 0, "speed": 1.0},
                                            {"time": 1, "speed": 2.0}]}])
        self._render(proj, "curve.mp4")
        probe = _ffprobe(os.path.join(WORK, "curve.mp4"))
        # 1s at 1x + 1s at 2x = 1.5s
        self.assertAlmostEqual(float(probe["format"]["duration"]), 1.5,
                               delta=0.3)

    # ---- MCP ------------------------------------------------------
    def test_mcp_new_tools(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "musecut.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, cwd=ROOT,
            env={**os.environ, "PYTHONPATH": ROOT})
        try:
            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            def notify(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}}})
            notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in tools["result"]["tools"]}
            for needed in ("create_project", "add_clip", "add_captions",
                           "render", "get_status", "set_export",
                           "set_canvas", "add_marker"):
                self.assertIn(needed, names)

            def call(mid, name, args):
                r = send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
                self.assertFalse(r["result"]["isError"],
                                 f"{name} failed: {r['result']}")
                return json.loads(r["result"]["content"][0]["text"])

            call(3, "create_project",
                 {"name": "mcpv2", "width": 320, "height": 180, "fps": 30,
                  "canvas": {"type": "color", "color": "#112233"},
                  "export": {"codec": "h264"}})
            call(4, "add_clip",
                 {"src": os.path.join(MEDIA, "clip1.mp4"), "duration": 2})
            call(5, "add_captions",
                 {"words": [{"word": "hi", "start": 0.0, "end": 0.5}],
                  "ktv_color": "#00FF00", "bloom_strength": 0.5})
            call(6, "set_export", {"codec": "prores422hq"})
            call(7, "set_canvas", {"type": "blur", "blur": 10})
            call(8, "add_marker", {"time": 1.0, "name": "hook"})
            st = call(9, "get_status", {})
            self.assertEqual(st["export"], "prores422hq")
            self.assertEqual(st["canvas"], "blur")
            self.assertEqual(st["markers"], 1)
            out = os.path.join(WORK, "mcp_render.mov")
            res = call(10, "render", {"output": out, "codec": "prores422hq",
                                      "workdir": os.path.join(WORK, "render")})
            self.assertTrue(os.path.isfile(out))
            probe = _ffprobe(out)
            v = next(s for s in probe["streams"]
                     if s["codec_type"] == "video")
            self.assertEqual(v["codec_name"], "prores")
        finally:
            proc.stdin.close()
            proc.wait(timeout=15)

    # ---- ML stubs -------------------------------------------------
    # (round 3) track_object / smart_reframe are NO LONGER stubs —
    # they run a real YOLOv5n ONNX tracker (musecut.track); see
    # test_round3.py. hdr_export is also real (HDR10 export in
    # render.py) — it is not an ai.py function any more, so it is
    # gone from this list too.
    # Round 4 wired remove_background + auto_cutout (real RMBG-1.4),
    # so they are gone from this list too.
    # Round 6 wired ai_matting (classical edge refinement of the
    # RMBG matte), so it is gone from this list too.
    # Round 7 wired vocal_separation (UVR MDX-Net, verified) and
    # ai_inpainting (classical cv2 Telea/NS), so they are gone too.
    # Round 10 wired vocal_beautify (classical voice-polish chain
    # in musecut.audio, no ML model), so it is gone too.
    # Round 11 wired super_resolution (Real-ESRGAN x4plus ONNX,
    # Qualcomm AI Hub public asset), so it is gone too.
    # Round 12 wired ai_outpainting (classical mirror-seeded Telea
    # in musecut.outpaint — a partial, never a model), so it is
    # gone too.
    # Round 14 wired eye_correction (MediaPipe Face Landmarker
    # 478 ONNX in musecut.eyecorrect), so it is gone too.
    # Round 15 wired ai_translate (OPUS-MT MarianMT int8 ONNX in
    # musecut.translate), so it is gone too.
    def test_ml_stubs_raise(self):
        for fn in ("digital_human",
                   "ai_sticker", "voice_clone",
                   "ai_transitions", "ai_sound_effects"):
            with self.assertRaises(NotImplementedError, msg=fn):
                getattr(ai_mod, fn)("in.mp4", "out.mp4")

    # ---- CLI ------------------------------------------------------
    def test_cli_render_codec(self):
        proj = _base_proj([{"src": os.path.join(MEDIA, "clip1.mp4"),
                            "duration": 2}])
        ppath = os.path.join(WORK, "cli_proj.json")
        with open(ppath, "w") as f:
            json.dump(proj, f)
        out = os.path.join(WORK, "cli_out.gif")
        res = _run([sys.executable, "-m", "musecut.cli", "render", ppath,
                    "-o", out, "--codec", "gif", "--quiet"], cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.isfile(out))


if __name__ == "__main__":
    unittest.main()
