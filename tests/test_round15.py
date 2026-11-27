"""Round-15 tests (v0.18.0): AI translate — OPUS-MT MarianMT ONNX.

Covers ``musecut.translate`` + ``musecut.ai.ai_translate`` (round 15,
2026-11-21):
* Both model pairs are the verified mirrors (Xenova opus-mt-en-es
  and opus-mt-en-mul, int8-quantized encoder + decoder, sha256
  pinned) and load in onnxruntime with the documented signatures —
  encoder [1, L] in, last_hidden_state [1, L, 512] out; decoder
  [1, D] + encoder states in, logits [1, D, vocab] out.
* en->es is correct on three real sentences, exact-matched
  (the model's own widget example + two caption-style lines).
* en->ur produces real Urdu script, and the canonical sentence
  matches exactly; the model's uneven quality on short colloquial
  lines is asserted honestly — the garbled "store card" output is
  recorded as a known model limit, not hidden: the test asserts
  only that the output is non-empty Urdu-script text for those
  lines, never that they are correct.
* The multilingual lang token must be prepended by id (852): the
  probe showed passing ">>urd<<" as text splits it into pieces
  and produces garbage, so a regression test asserts the id form
  is what the module uses (translate_text("...") with tgt="ur"
  returns the exact canonical Urdu).
* Empty/blank input returns "" without touching the model.
* translate_words re-times by even distribution: first translated
  word starts at the first original start, last ends at the last
  original end, times strictly increasing.
* SRT round-trip: timings and cue order preserved exactly, text
  translated.
* ai.ai_translate dispatches .txt/.json/.srt; missing source
  raises; unsupported pair raises; capabilities report
  ai_translate "done".

The translate calls cost ~0.1 s per output token on this CPU;
the test sentences are short (8-20 tokens).

Run:  ~/workspace/.venv-video/bin/python -m unittest tests.test_round15 -v
"""

import json
import os
import re
import subprocess
import unittest

import numpy as np

from musecut import __version__
from musecut import ai as ai_mod
from musecut import translate as tr_mod
from musecut.render import Renderer
from musecut.timeline import new_project

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.expanduser("~/workspace/musecut/.tmp/test-round15")
POOL = os.path.expanduser("~/workspace/tools/footage-library/pool")
TYRE_CLIP = os.path.join(POOL, "mixkit_11677_720.mp4")

try:
    import onnxruntime  # noqa: F401
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

try:
    import cv2  # noqa: F401
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

NEEDS_CV2 = unittest.skipUnless(
    HAS_CV2,
    "cv2 not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")
NEEDS_ORT = unittest.skipUnless(
    HAS_ORT,
    "onnxruntime not installed in this Python — run with "
    "~/workspace/.venv-video/bin/python")
NEEDS_MODELS = unittest.skipUnless(
    tr_mod.models_present(),
    "OPUS-MT translate models not on disk")

# Urdu-script range (Arabic block incl. Urdu letters).
URDU_RE = re.compile(r"[\u0600-\u06FF]")


class TestTranslateModels(unittest.TestCase):
    def test_models_verified(self):
        info = tr_mod.verify_models()
        self.assertEqual(info["overall"], "verified", info)
        for pair, entry in info["pairs"].items():
            self.assertEqual(entry["status"], "verified", (pair, entry))

    @NEEDS_ORT
    def test_encoder_decoder_signatures(self):
        for pair in (("en", "es"), ("en", "ur")):
            sess = tr_mod._load_pair(pair)
            enc_in = sess["encoder"].get_inputs()
            self.assertEqual([i.name for i in enc_in],
                             ["input_ids", "attention_mask"])
            enc_out = sess["encoder"].get_outputs()[0]
            self.assertEqual(enc_out.name, "last_hidden_state")
            self.assertEqual(len(enc_out.shape), 3)
            self.assertEqual(enc_out.shape[-1], 512)
            dec_in = {i.name for i in sess["decoder"].get_inputs()}
            self.assertEqual(dec_in, {"input_ids", "encoder_hidden_states",
                                      "encoder_attention_mask"})
            dec_out = sess["decoder"].get_outputs()[0]
            self.assertEqual(dec_out.name, "logits")

    def test_available_pairs(self):
        self.assertEqual(sorted(tr_mod.available_pairs()),
                         [("en", "es"), ("en", "ur")])


class TestTranslateText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_en_es_exact(self):
        cases = [
            ("My name is Wolfgang and I live in Berlin",
             "Me llamo Wolfgang y vivo en Berlín."),
            ("Never say yes to the store card",
             "Nunca digas que sí a la tarjeta de la tienda"),
            ("Slow is smooth, smooth is fast",
             "Lento es suave, suave es rápido"),
        ]
        for src, want in cases:
            got = tr_mod.translate_text(src, src="en", tgt="es")
            self.assertEqual(got, want, f"en->es {src!r}")

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_en_ur_canonical_exact(self):
        # The model's own widget example — deterministic greedy.
        got = tr_mod.translate_text(
            "My name is Wolfgang and I live in Berlin",
            src="en", tgt="ur")
        self.assertEqual(
            got, "میرا نام وولگنگ ہے اور میں برلین میں رہتا ہوں")

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_en_ur_short_lines_are_urdu_script(self):
        # Honest scope: opus-mt-en-ur is a low-resource pair and
        # garbles some short colloquial lines (the "store card"
        # line comes out "ہرگز نہیں کہہ دو سپٹ کارڈ"). Assert only
        # what is true — non-empty Urdu-script output — never that
        # these lines are correct.
        for src in ("Never say yes to the store card",
                    "Slow is smooth, smooth is fast"):
            got = tr_mod.translate_text(src, src="en", tgt="ur")
            self.assertTrue(got.strip(), src)
            self.assertRegex(got, URDU_RE, f"en->ur {src!r} -> {got!r}")

    def test_empty_input_is_noop(self):
        self.assertEqual(tr_mod.translate_text(""), "")
        self.assertEqual(tr_mod.translate_text("   "), "")

    def test_unsupported_pair_raises(self):
        with self.assertRaises(ValueError):
            tr_mod.translate_text("Hello", src="en", tgt="fr")

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_translate_words_retiming(self):
        words = [
            {"word": "Never", "start": 0.0, "end": 0.4},
            {"word": "say", "start": 0.4, "end": 0.7},
            {"word": "yes", "start": 0.7, "end": 1.0},
            {"word": "to", "start": 1.0, "end": 1.2},
            {"word": "the", "start": 1.2, "end": 1.5},
            {"word": "store", "start": 1.5, "end": 1.9},
            {"word": "card", "start": 1.9, "end": 2.3},
        ]
        out = tr_mod.translate_words(words, src="en", tgt="es")
        self.assertEqual(" ".join(w["word"] for w in out),
                         "Nunca digas que sí a la tarjeta de la tienda")
        self.assertAlmostEqual(out[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(out[-1]["end"], 2.3, places=2)
        for a, b in zip(out, out[1:]):
            self.assertLess(a["end"], b["end"])
            self.assertEqual(a["end"], b["start"])

    def test_translate_words_empty(self):
        self.assertEqual(tr_mod.translate_words([]), [])


class TestTranslateFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_srt_round_trip(self):
        src = os.path.join(WORK, "in.srt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:02,000\n"
                    "Never say yes to the store card\n\n"
                    "2\n00:00:02,500 --> 00:00:04,000\n"
                    "Slow is smooth, smooth is fast\n")
        dst = os.path.join(WORK, "out.srt")
        self.assertEqual(tr_mod.translate_srt(src, dst), dst)
        cues = tr_mod.parse_srt(dst)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 0.0)
        self.assertAlmostEqual(cues[0]["end"], 2.0)
        self.assertAlmostEqual(cues[1]["start"], 2.5)
        self.assertAlmostEqual(cues[1]["end"], 4.0)
        self.assertEqual(cues[0]["text"],
                         "Nunca digas que sí a la tarjeta de la tienda")
        self.assertEqual(cues[1]["text"],
                         "Lento es suave, suave es rápido")

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_txt_lines_and_blanks(self):
        src = os.path.join(WORK, "in.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Slow is smooth, smooth is fast\n\n"
                    "My name is Wolfgang and I live in Berlin\n")
        dst = os.path.join(WORK, "out.txt")
        self.assertEqual(
            tr_mod.translate_file(src, dst, tgt_lang="es"), dst)
        with open(dst, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "Lento es suave, suave es rápido")
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "Me llamo Wolfgang y vivo en Berlín.")

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_json_word_list(self):
        src = os.path.join(WORK, "words.json")
        words = [{"word": "Slow", "start": 0.0, "end": 0.5},
                 {"word": "is", "start": 0.5, "end": 0.8},
                 {"word": "smooth", "start": 0.8, "end": 1.3}]
        with open(src, "w", encoding="utf-8") as f:
            json.dump(words, f)
        dst = os.path.join(WORK, "words_es.json")
        self.assertEqual(
            tr_mod.translate_file(src, dst, tgt_lang="es"), dst)
        with open(dst, encoding="utf-8") as f:
            out = json.load(f)
        self.assertEqual(" ".join(w["word"] for w in out),
                         "Lento es suave")
        self.assertAlmostEqual(out[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(out[-1]["end"], 1.3, places=2)

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            tr_mod.translate_file("/nonexistent/nope.srt",
                                  os.path.join(WORK, "x.srt"))

    def test_unsupported_extension_raises(self):
        src = os.path.join(WORK, "in.csv")
        with open(src, "w") as f:
            f.write("a,b\n")
        with self.assertRaises(ValueError):
            tr_mod.translate_file(src, os.path.join(WORK, "out.csv"))


class TestAiTranslate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(WORK, exist_ok=True)

    @NEEDS_ORT
    @NEEDS_MODELS
    def test_ai_translate_txt(self):
        src = os.path.join(WORK, "ai_in.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("Slow is smooth, smooth is fast\n")
        dst = os.path.join(WORK, "ai_out.txt")
        self.assertEqual(ai_mod.ai_translate(src, dst), dst)
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(),
                             "Lento es suave, suave es rápido")

    def test_ai_translate_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            ai_mod.ai_translate("/nonexistent/nope.txt",
                                os.path.join(WORK, "x.txt"))

    def test_capabilities_done(self):
        caps = ai_mod.capabilities()
        self.assertEqual(caps["ai_translate"]["status"], "done")

    @NEEDS_CV2
    @NEEDS_ORT
    @NEEDS_MODELS
    def test_translate_renders_captions(self):
        # The feature proof, pinned in the suite: a real clip rendered
        # with OPUS-MT-translated Spanish captions, frame-checked
        # against a no-caption control of the same clip (the clip is
        # bright in the caption band, so the check diffs the two
        # renders — only the caption pixels may differ).
        if not os.path.isfile(TYRE_CLIP):
            self.skipTest("footage clip missing: " + TYRE_CLIP)
        words_en = [
            {"word": "Slow", "start": 0.0, "end": 0.4},
            {"word": "is", "start": 0.4, "end": 0.7},
            {"word": "smooth", "start": 0.7, "end": 1.1},
            {"word": "smooth", "start": 1.1, "end": 1.5},
            {"word": "is", "start": 1.5, "end": 1.7},
            {"word": "fast", "start": 1.7, "end": 2.0},
        ]
        words_es = tr_mod.translate_words(words_en, src="en", tgt="es")
        self.assertEqual(" ".join(w["word"] for w in words_es),
                         "Lento es suave suave es rápido")

        def _render(words, tag):
            proj = new_project(f"r15-tr-{tag}", width=1280, height=720)
            proj["clips"] = [{"src": TYRE_CLIP, "duration": 2.0}]
            if words is not None:
                proj["captions"] = {"words": words, "font_size": 64}
            out = os.path.join(WORK, f"clip_{tag}.mp4")
            Renderer(proj, workdir=WORK).render(out)
            return out

        def _frame(path, t):
            out = os.path.join(WORK, "r15_frame.png")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t),
                            "-i", path, "-frames:v", "1", out], check=True)
            img = cv2.imread(out)
            self.assertIsNotNone(img, f"no frame from {path}")
            return img

        es_path = _render(words_es, "es")
        ctl_path = _render(None, "ctl")
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", es_path], capture_output=True, text=True)
        self.assertAlmostEqual(float(dur.stdout.strip()), 2.0, places=1)
        es = _frame(es_path, 1.0).astype(np.int16)
        ctl = _frame(ctl_path, 1.0).astype(np.int16)
        diff = np.abs(es[int(720 * 0.6):] - ctl[int(720 * 0.6):]).max(axis=2)
        changed = int((diff > 30).sum())
        self.assertGreater(changed, 500,
                           "too few pixels differ — caption not burned in")
        self.assertLess(changed, 0.30 * diff.size,
                        "diff is not localised like a caption")

    def test_version_pin_0_17_0(self):
        # (round 31) version bumped to 0.18.0; the pin moves with it.
        self.assertEqual(__version__, "0.21.0")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            self.assertIn('version = "0.21.0"', f.read())
        with open(os.path.join(ROOT, "musecut", "__init__.py")) as f:
            self.assertIn("0.21.0", f.read())


if __name__ == "__main__":
    unittest.main()
