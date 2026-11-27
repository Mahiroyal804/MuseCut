"""MuseCut captions.

Two pieces:

1. :func:`words_to_ass` — turns word-level timings into an ASS subtitle
   file in the viral style: one phrase per line, lower third, the active
   word highlighted gold while the rest stays white. Rendered with
   ffmpeg's libass ``ass=`` filter. Optional CapCut-style upgrades:
   karaoke colours (``ktv_color``/``ktv_outline_color``/
   ``ktv_shadow_color``), text bloom/glow, speech-bubble boxes,
   underline, bold and italic (all native ASS — no faking).

2. :func:`transcribe` — auto-captions from an audio/video file using
   faster-whisper (word timestamps). faster-whisper is an optional
   dependency: it ships in the ``.venv-video`` environment on the dev VM.
   If it is not importable, a clear error tells the user how to get it
   instead of silently producing nothing.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .timeline import CAPTION_STYLES, default_font_path

GOLD = "#FFD60A"   # active word
WHITE = "#FFFFFF"  # inactive words


def _ass_font_name(font_path: str) -> str:
    """ASS font family name from a .ttf path.

    Strips the style suffix ("-VF", "-Regular", ...) and splits
    camelCase stems ("ArchivoBlack" -> "Archivo Black") so the name
    matches fontconfig's family exactly. libass resolves by family,
    and the old derivation ("Inter VF") does not match the "Inter"
    family — the new caption styles (Inter, Oswald) would silently
    fall back to a default font.
    """
    import re
    stem = os.path.splitext(os.path.basename(font_path))[0]
    for suf in ("-VF", "-Regular", "-Bold", "-Italic", "-Light",
                "-Medium", "-SemiBold", "-ExtraBold", "-ExtraLight"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    return stem.replace("-", " ")


def _hex_to_ass_bgr(hex_color: str) -> str:
    """#RRGGBB -> ASS colour string (ASS uses &HBBGGRR&)."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc."""
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _chunk_words(words: List[Dict[str, Any]], max_words: int = 5,
                 max_gap: float = 0.45) -> List[List[Dict[str, Any]]]:
    """Group words into display phrases.

    A new phrase starts after a long pause or when the phrase is full.
    """
    phrases: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    prev_end: Optional[float] = None
    for w in words:
        if cur and ((prev_end is not None and w["start"] - prev_end > max_gap)
                    or len(cur) >= max_words):
            phrases.append(cur)
            cur = []
        cur.append(w)
        prev_end = w["end"]
    if cur:
        phrases.append(cur)
    return phrases


def words_to_ass(words: List[Dict[str, Any]], width: int = 1280,
                 height: int = 720, font: Optional[str] = None,
                 font_size: Optional[int] = None,
                 active_color: Optional[str] = None,
                 inactive_color: Optional[str] = None,
                 margin_v: Optional[int] = None,
                 ktv_color: Optional[str] = None,
                 ktv_outline_color: Optional[str] = None,
                 ktv_shadow_color: Optional[str] = None,
                 underline: bool = False, bold: Optional[bool] = None,
                 italic: bool = False,
                 bloom_color: Optional[str] = None,
                 bloom_strength: float = 0.0,
                 bubble: Optional[bool] = None,
                 bubble_color: Optional[str] = None,
                 position: Optional[str] = None,
                 word_colors: Optional[Dict[str, str]] = None,
                 animation: str = "none",
                 word_bloom: Optional[Dict[str, str]] = None,
                 active_size_ratio: float = 1.0,
                 style: str = "viral-gold") -> str:
    """Build an ASS subtitle document from word timings.

    One Dialogue event per word; every event carries the full current
    phrase so the line never jumps, and only the active word is gold.
    This is the same technique used by viral caption builders.

    ``style`` (round 6) — a named preset from
    ``timeline.CAPTION_STYLES`` ("viral-gold" / "minimal" / "news").
    The style sets the defaults for ``font``, ``active_color``,
    ``inactive_color``, ``position``, ``bubble``, ``bubble_color``
    and ``bold``; any of those passed explicitly wins over the
    style. With no style and no explicit fields the output is
    exactly the historical default (Anton, gold active word, white
    rest, lower third).

    Upgrades (all native ASS, from CapCut's ``VETextInfo`` fields):

    * ``ktv_color`` / ``ktv_outline_color`` / ``ktv_shadow_color`` —
      the karaoke colours of the *active* word (colour, outline,
      shadow). Without them the active word uses ``active_color``.
    * ``bloom_color`` + ``bloom_strength`` (0..1) — text glow: the
      style outline/shadow take the bloom colour and grow with
      strength.
    * ``bubble`` — an opaque box behind the text (``bubble_color``).
    * ``underline`` / ``bold`` / ``italic`` — ASS style flags.
    * ``position`` — "lower" (ASS Alignment 2, the viral lower
      third), "mid" (Alignment 5, centre of frame) or "top"
      (Alignment 8). MarginV defaults: lower max(24, H*0.075),
      mid 0, top max(24, H*0.05); an explicit ``margin_v`` wins.
    * ``word_colors`` — {WORD: "#RRGGBB"}: when the active word
      matches a key (case-insensitive), its active colour is that
      colour instead of ``ktv_color`` / ``active_color``.
    * ``animation`` — "none" (default), "pop", "slide" or
      "typewriter", all native ASS:
        - pop: the active word scales 140% -> 100% over its first
          150 ms (``\\fscx``/``\\fscy`` + ``\\t``).
        - slide: the phrase's first event slides in from 80 px off
          over 200 ms (``\\move``); later word events are static,
          so the line never jumps.
        - typewriter: the active word's letters fade in one by one
          (``\\alpha`` + ``\\t`` per character, spread over the
          word's duration).
    * ``word_bloom`` — {WORD: "#RRGGBB"}: per-word glow outline for
      the active word, taking precedence over ``bloom_color``. The
      style outline itself is always black — the round-1 bug was
      that ``bloom_color`` tinted EVERY word's outline gold because
      it sat in the style; now only the active word glows.
    * ``active_size_ratio`` (1.0..2.0, round 5) — two-tier caption
      sizing: the active word renders at ``font_size * ratio``
      (ASS ``\\fs`` override) while the rest of the line stays at
      ``font_size``. This is the elite-reel small-setup + big-punch
      line (round-1 mastery rule 3) inside ONE caption event: the
      line's height grows to fit the big word, so the punch word
      literally stands taller than its neighbours. 1.0 = off.
    """
    if not words:
        raise ValueError("words_to_ass needs at least one word")
    if style not in CAPTION_STYLES:
        raise ValueError(
            f"unknown caption style: {style!r} "
            f"(want one of {sorted(CAPTION_STYLES)})")
    # Resolve the style preset; explicit fields win over the style.
    preset = CAPTION_STYLES[style]
    if font is None:
        font = str(preset["font"])
    if active_color is None:
        active_color = str(preset["active_color"])
    if inactive_color is None:
        inactive_color = str(preset["inactive_color"])
    if position is None:
        position = str(preset["position"])
    if bubble is None:
        bubble = bool(preset["bubble"])
    if bubble_color is None:
        bubble_color = str(preset["bubble_color"])
    if bold is None:
        bold = bool(preset["bold"])
    if position not in ("lower", "mid", "top"):
        raise ValueError(f"unknown caption position: {position!r}")
    if animation not in ("none", "pop", "slide", "typewriter"):
        raise ValueError(f"unknown caption animation: {animation!r}")
    if not (1.0 <= float(active_size_ratio) <= 2.0):
        raise ValueError(
            f"active_size_ratio must be 1.0..2.0, got {active_size_ratio!r}")
    fs = font_size or max(28, int(height * 0.088))
    active_fs = int(round(fs * float(active_size_ratio)))
    if margin_v is not None:
        mv = margin_v
    elif position == "mid":
        mv = 0
    elif position == "top":
        mv = max(24, int(height * 0.05))
    else:
        mv = max(24, int(height * 0.075))
    alignment = {"lower": 2, "mid": 5, "top": 8}[position]
    word_colors_bgr = {
        str(k).lower(): _hex_to_ass_bgr(v)
        for k, v in (word_colors or {}).items()
    }
    word_bloom_bgr = {
        str(k).lower(): _hex_to_ass_bgr(v)
        for k, v in (word_bloom or {}).items()
    }
    bloom_bgr = (_hex_to_ass_bgr(bloom_color) if bloom_color else None)
    font_path = default_font_path(font)
    font_name = _ass_font_name(font_path)

    active = _hex_to_ass_bgr(active_color)
    inactive = _hex_to_ass_bgr(inactive_color)
    ktv = _hex_to_ass_bgr(ktv_color) if ktv_color else active
    ktv_outline = (_hex_to_ass_bgr(ktv_outline_color)
                   if ktv_outline_color else None)
    ktv_shadow = (_hex_to_ass_bgr(ktv_shadow_color)
                  if ktv_shadow_color else None)
    # Style outline/shadow: plain black, always. Bloom/glow is a
    # PER-WORD effect — only the active word gets a coloured outline
    # (inline \3c in the event below). Round 1 put bloom_color in
    # the style, which tinted every word's outline gold.
    outline_col = "&H00000000"
    back_col = (_hex_to_ass_bgr(bubble_color) if bubble
                else "&H80000000")
    border_style = 3 if bubble else 1
    outline_w = 2 + int(3 * bloom_strength) if bloom_color else 2
    shadow_w = int(1 + 2 * bloom_strength) if bloom_color else 0

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{fs},{inactive},&H000000FF,{outline_col},"
        f"{back_col},{1 if bold else 0},{1 if italic else 0},"
        f"{1 if underline else 0},0,100,100,0.5,0,{border_style},"
        f"{outline_w},{shadow_w},{alignment},40,40,{mv},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    # Slide-in geometry: the \move end point is the line's natural
    # anchor for its alignment (centre for mid, bottom-centre for
    # lower, top-centre for top); the start point is 80 px off toward
    # the screen edge, so the line slides in over 200 ms.
    slide_move = ""
    if animation == "slide":
        cx = width // 2
        if position == "mid":
            cy = height // 2
            slide_move = f"\\move({cx},{cy + 80},{cx},{cy},0,200)"
        elif position == "top":
            slide_move = f"\\move({cx},{mv - 80},{cx},{mv},0,200)"
        else:  # lower
            cy = height - mv
            slide_move = f"\\move({cx},{cy + 80},{cx},{cy},0,200)"

    lines = [header]
    for phrase in _chunk_words(words):
        phrase_text = " ".join(w["word"] for w in phrase)
        for idx, w in enumerate(phrase):
            parts = []
            for j, pw in enumerate(phrase):
                if j == idx:
                    word_key = str(pw["word"]).lower()
                    word_ktv = word_colors_bgr.get(word_key, ktv)
                    # Active-word outline: per-word bloom > explicit
                    # ktv outline > bloom glow. Inactive words keep
                    # the style's black outline.
                    active_outline = (word_bloom_bgr.get(word_key)
                                      or ktv_outline or bloom_bgr)
                    on = [f"\\1c{word_ktv}"]
                    if active_outline:
                        on.append(f"\\3c{active_outline}")
                    if ktv_shadow:
                        on.append(f"\\4c{ktv_shadow}")
                    if active_fs != fs:
                        # Two-tier sizing: the active word stands
                        # taller; the \\fs in the "off" tags below
                        # resets the size for the rest of the line.
                        on.append(f"\\fs{active_fs}")
                    if animation == "pop":
                        on.append("\\fscx140\\fscy140"
                                  "\\t(0,150,\\fscx100\\fscy100)")
                    off = [f"\\1c{inactive}"]
                    if active_outline:
                        off.append(f"\\3c{outline_col}")
                    if ktv_shadow:
                        off.append(f"\\4c{back_col}")
                    if active_fs != fs:
                        off.append(f"\\fs{fs}")
                    if animation == "pop":
                        off.append("\\fscx100\\fscy100")
                    word_text = pw["word"]
                    if animation == "typewriter":
                        dur_ms = max(
                            1, int(round((w["end"] - w["start"]) * 1000)))
                        n_chars = len(word_text)
                        step = max(1, dur_ms // n_chars)
                        chars = []
                        for ci, ch in enumerate(word_text):
                            s = ci * step
                            e = min(dur_ms, (ci + 1) * step)
                            chars.append(
                                f"{{\\alpha&HFF&\\t({s},{e},"
                                f"\\alpha&H00&)}}{ch}")
                        word_text = "".join(chars)
                    parts.append(
                        f"{{{''.join(on)}}}{word_text}{{{''.join(off)}}}")
                else:
                    parts.append(pw["word"])
            text = " ".join(parts)
            if animation == "slide" and idx == 0:
                text = f"{{{slide_move}}}" + text
            lines.append(
                f"Dialogue: 0,{_ass_time(w['start'])},{_ass_time(w['end'])},"
                f"Default,,0,0,0,,{text}"
            )
    return "\n".join(lines) + "\n"


def write_ass(words: List[Dict[str, Any]], path: str, **kwargs: Any) -> str:
    """Write an ASS file; returns the path."""
    doc = words_to_ass(words, **kwargs)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def words_to_srt(words: List[Dict[str, Any]], max_words: int = 6) -> str:
    """Plain SRT export (no word highlight) for players/editors."""
    def srt_time(s: float) -> str:
        ms = int(round(s * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        sec, mmm = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{mmm:03d}"

    out = []
    for n, phrase in enumerate(_chunk_words(words, max_words=max_words,
                                            max_gap=0.6), 1):
        out.append(str(n))
        out.append(f"{srt_time(phrase[0]['start'])} --> "
                   f"{srt_time(phrase[-1]['end'])}")
        out.append(" ".join(w["word"] for w in phrase))
        out.append("")
    return "\n".join(out)


def transcribe(path: str, model: str = "base",
               language: Optional[str] = None,
               beam_size: int = 5) -> List[Dict[str, Any]]:
    """Transcribe media to word-level timings with faster-whisper.

    Returns ``[{"word": str, "start": float, "end": float}, ...]``.

    Raises RuntimeError with install instructions when faster-whisper is
    not available in the current interpreter.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed in this Python. "
            "Install it with: pip install faster-whisper "
            "(on this VM the working env is ~/workspace/.venv-video, "
            "or: pip install --user --break-system-packages faster-whisper)"
        ) from exc

    # faster-whisper's httpx client chokes on the bracketed IPv6 entries
    # in this VM's no_proxy/NO_PROXY; drop them for the download.
    # (Same recipe as the production STT gate on this machine.)
    os.environ.pop("no_proxy", None)
    os.environ.pop("NO_PROXY", None)

    model_obj = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _info = model_obj.transcribe(
        path, language=language, beam_size=beam_size,
        word_timestamps=True, vad_filter=True,
    )
    words: List[Dict[str, Any]] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({
                    "word": text,
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                })
    words.sort(key=lambda w: w["start"])
    # Drop any zero-length/overlapping artefacts.
    clean: List[Dict[str, Any]] = []
    for w in words:
        if w["end"] <= w["start"]:
            continue
        if clean and w["start"] < clean[-1]["end"]:
            w = dict(w, start=clean[-1]["end"])
            if w["end"] <= w["start"]:
                continue
        clean.append(w)
    return clean


def transcribe_to_file(path: str, out_path: str, model: str = "base",
                       language: Optional[str] = None) -> str:
    """Transcribe and save the word timings as JSON; returns the path."""
    words = transcribe(path, model=model, language=language)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(words, f, indent=2, ensure_ascii=False)
    return out_path
