"""AI translate — OPUS-MT MarianMT ONNX (Xenova mirrors), greedy decode.

ROUND 15 — REAL. The genuine Helsinki-NLP OPUS-MT models, converted
to ONNX by Xenova (transformers.js) and run here with raw
onnxruntime + the ``tokenizers`` library — no torch, no transformers
on this VM.

Pairs shipped (both verified 2026-11-21 on real sentences):
* en -> es: ``Xenova/opus-mt-en-es`` (base Helsinki-NLP/opus-mt-en-es),
  int8-quantized encoder + decoder, ``tokenizer.json``.
  Verified: "My name is Wolfgang and I live in Berlin" ->
  "Me llamo Wolfgang y vivo en Berlín."; "Never say yes to the store
  card" -> "Nunca digas que sí a la tarjeta de la tienda";
  "Slow is smooth, smooth is fast" -> "Lento es suave, suave es rápido".
* en -> ur: ``Xenova/opus-mt-en-mul`` (base Helsinki-NLP/opus-mt-en-mul),
  the multilingual model, with the target-language token ``>>urd<<``
  (vocab id 852) prepended to the source ids. Verified: the same
  Wolfgang sentence -> "میرا نام وولگنگ ہے اور میں برلین میں رہتا ہوں".

How the decode works (pinned by the probe in /tmp/r15probe/):
encoder once (input_ids + attention_mask -> last_hidden_state), then
a greedy loop on the non-merged decoder (full re-feed each step —
short caption lines, so the O(n^2) cost is negligible at ~0.1 s per
token). Decoder starts on the pad token (``decoder_start_token_id``
= pad for Marian), stops on eos (id 0), and the pad id is banned
from generation (the config's ``bad_words_ids``). The model config
asks for beam search (num_beams=4); this module decodes greedily —
simpler, deterministic, and measured correct on the verified
sentences. Beam search would buy a little quality on hard sentences;
it is not wired.

Two real gotchas, both measured in the probe:
1. Xenova's ``tokenizer.json`` carries
   ``"normalizer": {"type": "Precompiled", "precompiled_charsmap": null}``
   and the ``tokenizers`` Rust library panics on the null charsmap.
   The null means "no normalizer" (transformers.js ignores it), so
   this module writes a patched copy with ``normalizer: None``
   (``tokenizer_patched.json`` next to the original, which is kept
   for provenance) and loads that.
2. The multilingual tokenizer does NOT tokenize ">>urd<<" as one
   token from text — the Metaspace pre-tokenizer splits it into
   ``▁>``, ``>``, ``ur``, ``d``, ``<``, ``<`` (the vocab entry has
   no ``▁`` prefix, so the piece never matches). The language token
   must be prepended by ID (852) to the encoded ids. Passing the
   tag as text silently produces garbage ("Leady is glat") — the
   probe caught it, and the module never builds the tag from text.

Honest scope: greedy decode, not beam-4; only en->es and en->ur
are bundled (other Xenova opus-mt pairs follow the same pattern —
drop the model dir in and add a PAIRS row). The en->ur model is a
low-resource pair and its quality is uneven: the canonical sentence
translates perfectly, but "Never say yes to the store card" comes
out garbled ("ہرگز نہیں کہہ دو سپٹ کارڈ") and "Slow is smooth"
mistranslates "slow" as cold ("ٹھنڈی ہے"). The en->es model is
solid on caption-style lines. ``translate_words`` re-times the
translated words by even distribution across the original span —
an approximation, documented, not a re-alignment.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

MODELS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models", "translate")

# pair (src, tgt) -> model dir + decode constants.
# decoder_start/pad are Marian's: decoder starts on the pad token.
PAIRS: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("en", "es"): {
        "dir": "en-es",
        "repo": "Xenova/opus-mt-en-es",
        "base": "Helsinki-NLP/opus-mt-en-es",
        "decoder_start_token_id": 65000,
        "pad_token_id": 65000,
        "eos_token_id": 0,
        "lang_token_id": None,  # single-pair model, no tag needed
        "encoder_sha256": "03434edd1147e6c49e22066e9dc5888235e99cc35a6711d1c4ea5b06455d5da9",
        "decoder_sha256": "2b45074cc819f1d901e40510d7bc0781d99327d16c7eca04518c4ee0c3d6ba10",
    },
    ("en", "ur"): {
        "dir": "en-mul",
        "repo": "Xenova/opus-mt-en-mul",
        "base": "Helsinki-NLP/opus-mt-en-mul",
        "decoder_start_token_id": 64109,
        "pad_token_id": 64109,
        "eos_token_id": 0,
        "lang_token_id": 852,  # ">>urd<<" in the mul vocab
        "encoder_sha256": "f10f49c2691043ba7cfd2a85d86f84dddda428d4209cf9b495f6e114b313737a",
        "decoder_sha256": "12c04e64038937e2b51483e9b8cb378ee5be5bad0dbf206097ebe01e507ecc34",
    },
}

# Long inputs are split on sentence boundaries before encoding;
# Marian's max position is 512 tokens and caption lines are short.
_MAX_TOKENS_PER_CHUNK = 400
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_SESSIONS: Dict[Tuple[str, str], Dict[str, Any]] = {}


def available_pairs() -> List[Tuple[str, str]]:
    """Pairs whose model files are all on disk."""
    return [p for p in PAIRS if _pair_files_present(p)]


def _pair_dir(pair: Tuple[str, str]) -> str:
    return os.path.join(MODELS_DIR, PAIRS[pair]["dir"])


def _pair_files_present(pair: Tuple[str, str]) -> bool:
    d = _pair_dir(pair)
    return all(os.path.isfile(os.path.join(d, f)) for f in (
        "encoder_model_quantized.onnx",
        "decoder_model_quantized.onnx",
        "tokenizer.json",
    ))


def models_present() -> bool:
    """True when at least one pair is fully on disk."""
    return len(available_pairs()) > 0


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_models() -> Dict[str, Any]:
    """Check every bundled pair is the verified mirror (encoder +
    decoder size + sha256). Returns per-pair status; ``overall`` is
    ``verified`` only when both pairs match."""
    out: Dict[str, Any] = {"pairs": {}}
    for pair, cfg in PAIRS.items():
        d = _pair_dir(pair)
        entry: Dict[str, Any] = {"dir": d, "repo": cfg["repo"]}
        for role in ("encoder", "decoder"):
            path = os.path.join(d, f"{role}_model_quantized.onnx")
            if os.path.isfile(path):
                entry[f"{role}_size"] = os.path.getsize(path)
                entry[f"{role}_sha256"] = _sha256(path)
                entry[f"{role}_ok"] = (
                    entry[f"{role}_sha256"] == cfg[f"{role}_sha256"])
            else:
                entry[f"{role}_size"] = None
                entry[f"{role}_sha256"] = None
                entry[f"{role}_ok"] = False
        entry["status"] = ("verified" if entry["encoder_ok"]
                           and entry["decoder_ok"] else "mismatch")
        out["pairs"][f"{pair[0]}->{pair[1]}"] = entry
    out["overall"] = ("verified" if all(
        e["status"] == "verified" for e in out["pairs"].values())
        else "mismatch")
    return out


def _patched_tokenizer_path(pair_dir: str) -> str:
    """tokenizers-py cannot load Xenova's tokenizer.json as-is (the
    Precompiled normalizer has a null charsmap and the Rust loader
    panics). The null means 'no normalizer', so write a patched copy
    with normalizer=None next to the original and load that."""
    patched = os.path.join(pair_dir, "tokenizer_patched.json")
    if not os.path.isfile(patched):
        src = os.path.join(pair_dir, "tokenizer.json")
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["normalizer"] = None
        with open(patched, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return patched


def _load_pair(pair: Tuple[str, str]) -> Dict[str, Any]:
    if pair in _SESSIONS:
        return _SESSIONS[pair]
    if pair not in PAIRS:
        raise ValueError(
            f"unsupported translation pair {pair!r}; available: "
            f"{sorted(PAIRS)} (bundled on disk: {available_pairs()})")
    if not _pair_files_present(pair):
        raise FileNotFoundError(
            f"translation model for {pair!r} not on disk: "
            f"{_pair_dir(pair)} — fetch it from "
            f"https://huggingface.co/{PAIRS[pair]['repo']}")
    import onnxruntime as ort
    from tokenizers import Tokenizer

    d = _pair_dir(pair)
    sess = {
        "tokenizer": Tokenizer.from_file(_patched_tokenizer_path(d)),
        "encoder": ort.InferenceSession(
            os.path.join(d, "encoder_model_quantized.onnx"),
            providers=["CPUExecutionProvider"]),
        "decoder": ort.InferenceSession(
            os.path.join(d, "decoder_model_quantized.onnx"),
            providers=["CPUExecutionProvider"]),
        "cfg": PAIRS[pair],
    }
    _SESSIONS[pair] = sess
    return sess


def _encode(sess: Dict[str, Any], text: str) -> List[int]:
    """Encode one chunk: optional lang-token id + token ids + </s>
    (the post-processor appends </s>)."""
    ids = sess["tokenizer"].encode(text).ids
    lang = sess["cfg"]["lang_token_id"]
    if lang is not None:
        ids = [lang] + ids
    return ids


def _translate_ids(sess: Dict[str, Any], ids: List[int],
                   max_new_tokens: int = 64) -> str:
    cfg = sess["cfg"]
    enc_ids = np.array([ids], dtype=np.int64)
    mask = np.ones_like(enc_ids)
    hs = sess["encoder"].run(
        ["last_hidden_state"],
        {"input_ids": enc_ids, "attention_mask": mask})[0]
    dec = [cfg["decoder_start_token_id"]]
    for _ in range(max_new_tokens):
        d = np.array([dec], dtype=np.int64)
        logits = sess["decoder"].run(
            ["logits"],
            {"input_ids": d,
             "encoder_hidden_states": hs,
             "encoder_attention_mask": mask})[0]
        nxt = logits[0, -1].copy()
        nxt[cfg["pad_token_id"]] = -np.inf  # config bad_words_ids
        tok_id = int(np.argmax(nxt))
        if tok_id == cfg["eos_token_id"]:
            break
        dec.append(tok_id)
    return sess["tokenizer"].decode(dec[1:], skip_special_tokens=True)


def translate_text(text: str, src: str = "en", tgt: str = "es",
                   max_new_tokens: int = 64) -> str:
    """Translate one string. Empty/blank input returns "" unchanged.
    Long inputs are split on sentence boundaries and translated
    chunk by chunk (Marian max position 512)."""
    if not text or not text.strip():
        return ""
    sess = _load_pair((src, tgt))
    ids = _encode(sess, text)
    if len(ids) <= _MAX_TOKENS_PER_CHUNK:
        return _translate_ids(sess, ids, max_new_tokens)
    parts = [p for p in _SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    if len(parts) <= 1:  # one huge sentence: truncate to the cap
        return _translate_ids(sess, ids[:_MAX_TOKENS_PER_CHUNK],
                              max_new_tokens)
    return " ".join(
        _translate_ids(sess, _encode(sess, p), max_new_tokens)
        for p in parts)


def translate_words(words: Sequence[Dict[str, Any]],
                    src: str = "en", tgt: str = "es") -> List[Dict[str, Any]]:
    """Translate a caption word list ``[{"word", "start", "end"}]``.

    The words are joined to one line, translated as a line, and the
    translated words are re-timed by EVEN DISTRIBUTION across the
    original span (first translated word starts at the first original
    start, last ends at the last original end). That is an
    approximation — word counts differ across languages, so there is
    no true per-word alignment — and it is documented as such.
    """
    words = list(words)
    if not words:
        return []
    text = " ".join(str(w["word"]) for w in words)
    translated = translate_text(text, src=src, tgt=tgt)
    out_words = translated.split()
    if not out_words:
        return []
    t0 = float(words[0]["start"])
    t1 = float(words[-1]["end"])
    span = max(t1 - t0, 0.0)
    n = len(out_words)
    result = []
    for i, w in enumerate(out_words):
        s = t0 + span * i / n
        e = t0 + span * (i + 1) / n
        result.append({"word": w, "start": round(s, 3), "end": round(e, 3)})
    return result


# ---- SRT ----------------------------------------------------------

_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*"
    r"(\d+):(\d+):(\d+)[,.](\d+)")


def _srt_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _seconds_to_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return (f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:"
            f"{(ms // 1000) % 60:02d},{ms % 1000:03d}")


def parse_srt(path: str) -> List[Dict[str, Any]]:
    """Parse an .srt file into [{"start", "end", "text"}] cues."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    cues: List[Dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # lines[0] is the cue number, lines[1] the timing line.
        m = _SRT_TIME.search(lines[1])
        if m is None:
            continue
        start = _srt_to_seconds(*m.groups()[:4])
        end = _srt_to_seconds(*m.groups()[4:])
        cues.append({"start": start, "end": end,
                     "text": " ".join(lines[2:]).strip()})
    return cues


def write_srt(cues: Sequence[Dict[str, Any]], path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{_seconds_to_srt(float(c['start']))} --> "
                    f"{_seconds_to_srt(float(c['end']))}\n{c['text']}\n\n")
    return path


def translate_srt(src: str, dst: str, src_lang: str = "en",
                  tgt_lang: str = "es") -> str:
    """Translate an .srt file cue by cue, preserving timings and
    cue order exactly."""
    cues = parse_srt(src)
    for c in cues:
        c["text"] = translate_text(c["text"], src=src_lang, tgt=tgt_lang)
    return write_srt(cues, dst)


def translate_file(src: str, dst: str, src_lang: str = "en",
                   tgt_lang: str = "es") -> str:
    """Translate a caption/text file. Dispatches on extension:

    * ``.srt`` — cue-by-cue, timings preserved (see translate_srt);
    * ``.json`` — a word list ``[{"word","start","end"}]`` or
      ``{"words": [...]}`` (re-timed by translate_words) or
      ``{"text": "..."}`` (plain text);
    * ``.txt`` — line by line, blank lines preserved.
    """
    if not os.path.isfile(src):
        raise FileNotFoundError(f"source not found: {src!r}")
    ext = os.path.splitext(src)[1].lower()
    if ext == ".srt":
        return translate_srt(src, dst, src_lang, tgt_lang)
    if ext == ".json":
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "words" in data:
            out: Any = {"words": translate_words(
                data["words"], src=src_lang, tgt=tgt_lang)}
        elif isinstance(data, list):
            out = translate_words(data, src=src_lang, tgt=tgt_lang)
        elif isinstance(data, dict) and "text" in data:
            out = {"text": translate_text(
                data["text"], src=src_lang, tgt=tgt_lang)}
        else:
            raise ValueError(
                f"unsupported caption JSON shape in {src!r}: expected a "
                "word list, {'words': [...]} or {'text': ...}")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return dst
    if ext == ".txt":
        with open(src, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        out_lines = [translate_text(ln, src=src_lang, tgt=tgt_lang)
                     if ln.strip() else "" for ln in lines]
        with open(dst, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + ("\n" if lines else ""))
        return dst
    raise ValueError(f"unsupported input for translate_file: {ext!r} "
                     "(use .srt, .json or .txt)")
