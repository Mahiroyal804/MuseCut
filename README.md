# MuseCut 🎬

[![Tests](https://github.com/Mahiroyal804/MuseCut/actions/workflows/tests.yml/badge.svg)](https://github.com/Mahiroyal804/MuseCut/actions/workflows/tests.yml)
[![Version](https://img.shields.io/badge/version-0.21.0-blue)](https://github.com/Mahiroyal804/MuseCut/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

**The agent-native video editor. Better than CapCut — for AI agents.**

CapCut is built for humans tapping on a phone: login required, no
official API, no MCP, exports only from its desktop app, watermark on
the free tier. MuseCut flips that around: the editor is a **JSON
timeline + a command line + an MCP server**. An agent writes a
timeline, calls `render`, and gets a finished video — no login, no
watermark, no GUI, fully local, fully scriptable, open source (MIT).

Built on **ffmpeg**. Runs anywhere ffmpeg runs — including a headless
Linux VM with no GPU.

> **Why "better than CapCut"?** For a human with a phone, CapCut is
> great. For an *agent* producing videos programmatically, CapCut has
> no API at all — community MCPs only write CapCut draft files and
> still need the desktop app to export. MuseCut is API-first: the
> whole editor is a tool call away. See
> [CAPCUT_PARITY.md](CAPCUT_PARITY.md) for the full feature-by-feature
> comparison, and [PRO_PARITY.md](PRO_PARITY.md) for the pro offline
> editor (Premiere / Resolve level) scoreboard — **v2 targets pro
> capabilities**, not just CapCut parity.

---

## Features (v2)

| Area | What works |
|---|---|
| **Timeline** | JSON timelines: unlimited tracks, trim, split, merge, speed control (incl. speed curves), reverse, freeze frame, full transform keyframes (position / scale / rotation / opacity + anchor point), overlays/PiP, masks, blending modes, nested sequences / compound clips, markers, adjustment layers |
| **Transitions** | crossfade, dip-to-black, whoosh (with synthesised whoosh SFX), wipe, hard cut, motion blur, **slide, zoom, dissolve, circle, pixelize, dip-to-white (v0.7.0)** |
| **Text** | drawtext overlays, 6 bundled fonts, fade / slide / pop animations, glow/bloom, bubbles, underline, bold, **3D text, arc/circle text shape, text templates (title-bold / lower-third / quote / punch / caption-clean + project-level `text_templates`, v0.7.0)** |
| **Captions** | auto-captions via faster-whisper → viral word-highlight ASS (gold active word, lower third), karaoke colours (ktv color/outline/shadow), bloom, bubbles, underline/bold/italic, SRT export, **two-tier sizing — the highlighted word renders bigger (`active_size_ratio`, v0.7.0)** |
| **Audio** | voice/music/SFX tracks, music ducking under voice (sidechain compressor), voice changer (rubberband pitch/formant), noise reduction (afftdn), audio extraction, beat-sync markers, **audio mixer: per-track 3-band EQ, compressor, keyframed volume, stereo pan, loudnorm auto-balance** |
| **Video** | colour presets (cinematic, vivid, B&W, vintage, warm, cool…), all adjust sliders (brightness/contrast/saturation/HSL/exposure/highlights/shadows/clarity/dehaze/sharpen/fade), **LUT support (.cube)**, vignette + film grain, chroma-key (green screen), vidstab stabilisation, canvas backgrounds (color/image/blur), 4K export, 16:9 + 9:16 |
| **Workflow** | proxy workflow (`proxy make` / `proxy relink`), colour scopes as JSON data (`scopes`), per-resolution bitrate ladder, markers |
| **Templates** | JSON timeline presets with `{{variable}}` substitution |
| **Export** | MP4/MOV/WebM/GIF, H.264/H.265, **ProRes (422/LT/HQ/4444/Proxy), AV1, DNxHD/DNxHR, WAV/MP3 audio-only**, no watermark, no login |
| **AI voices** | Muse's own TTS wired in: `musecut voice synth "text" -o vo.wav` renders real speech with `avocado_v2:MAI_03` (speed 90, the production channel voice) — 24 kHz mono wav or mp3. Other backends plug in through the `tts.TTSProvider` protocol. |
| **MCP** | stdio MCP server, 17 tools: `create_project`, `add_clip`, `add_text`, `add_captions`, `add_transition`, `set_export`, `set_canvas`, `add_marker`, `list_voices`, `synthesize_voice`, `render`, `get_status`, … |

Honest limits (see [CAPCUT_PARITY.md](CAPCUT_PARITY.md) and
[PRO_PARITY.md](PRO_PARITY.md) — **score 92.46/100, 298/298 tests
green**): background removal (RMBG-1.4), object tracking, vocal
separation, lip sync, eye correction and super resolution are all
shipped (v0.6–v0.17) — see the version history below. Still missing:
digital human and voice cloning (needs a cloning TTS backend; plain
TTS is done). HEVC-with-alpha export is refused, not planned —
libx265 has no alpha pixel formats on this build. HDR10 export works.

---

## Quickstart

```bash
pip install -e .                 # or: pip install musecut
ffmpeg -version                  # needs ffmpeg >= 6 with libass

# 1. Make a timeline
musecut init my-video --width 1280 --height 720

# 2. Edit my-video.json (see below), then render
musecut render my-video.json -o my-video.mp4

# 3. Or render a bundled example (no media files needed)
musecut render examples/sample_timeline.json -o demo.mp4
```

### AI voices (Muse TTS)

```bash
musecut voice list
# providers: muse (voices: avocado_v2:MAI_03, default speed 90), none
musecut voice synth "Your script line here." -o vo.wav
# -> vo.wav, 24 kHz mono, real MAI_03 speech (no quota; retries on flaky backend)
musecut voice synth "Line two." -o vo.mp3 --speed 90
```

Speed is the production cap: default 90, never above 100 (the backend
clips word endings above 90). Add the wav as a voice track
(`{"src": "vo.wav", "role": "voice"}`) and it ducks the music.

### Auto-captions

```bash
pip install "musecut[captions]"   # faster-whisper
musecut captions voice.mp3 --model base -o words.json   # word timings
musecut captions voice.mp3 --format ass -o captions.ass # viral ASS
```

### MCP server (for agents)

```json
{
  "mcpServers": {
    "musecut": {
      "command": "musecut",
      "args": ["mcp"]
    }
  }
}
```

Then an agent can do, in plain tool calls:

```
create_project(name="reel", width=1080, height=1920)
synthesize_voice(text="Your script line here.", output="vo.wav")
add_clip(src="clip1.mp4", duration=5, keyframes={zoom:{from:1.0,to:1.2}})
add_transition(clip_index=1, type="whoosh", duration=0.5)
add_captions(transcribe_from="voice.mp3")
render(output="reel.mp4")
```

---

## JSON timeline format

```json
{
  "version": 1,
  "name": "demo",
  "width": 1280, "height": 720, "fps": 30,
  "clips": [
    {
      "src": "clip1.mp4", "duration": 5,
      "effects": {"filter": "cinematic", "vignette": 0.5, "grain": 0.4},
      "keyframes": {"zoom": {"from": 1.0, "to": 1.2}},
      "transition_in": {"type": "crossfade", "duration": 0.5}
    },
    {
      "src": "clip2.mp4", "duration": 5, "speed": 1.5,
      "transition_in": {"type": "whoosh", "duration": 0.5}
    }
  ],
  "texts": [
    {"text": "Watch this", "start": 0.5, "duration": 2.5,
     "font": "Anton", "size": 96, "animation": "slide-up"}
  ],
  "audio": [
    {"src": "voice.mp3", "role": "voice"},
    {"src": "music.mp3", "role": "music", "loop": true, "volume": 0.5,
     "ducking": {"sidechain": "voice", "threshold": 0.02, "ratio": 8}}
  ],
  "captions": {"style": "viral-gold", "words": [
    {"word": "Hello", "start": 0.1, "end": 0.4}
  ]},
  "endcard": {"duration": 5, "title": "Watch next",
              "subtitle": "Subscribe", "background": "#0B0B0B"}
}
```

Clip fields: `src` (video/image, or `color:#RRGGBB`, or `testsrc`),
`nested` (an inline project dict, `sequence:<name>`, or a `.json`
path — a compound clip), `start`, `duration`, `speed`,
`speed_curve` (piecewise `[{time, speed}]`), `reverse`, `freeze_at`,
`transition_in`, `keyframes` (zoom/pan, plus `position` / `scale` /
`rotation` / `opacity` lists of `{time, value}` and `anchor: [x, y]`),
`effects` (filter preset + brightness / contrast / saturation / hue /
exposure / highlights / shadows / clarity / dehaze / sharpen / fade /
grain / lut `.cube` / motion_blur / vignette), `chroma_key`,
`stabilize` (vidstab), `mask` (circle/rect/linear), `volume`,
`muted`. Audio fields: `eq: {low, mid, high}`, `compressor:
{threshold, ratio, attack, release, makeup}`, `volume_keyframes:
[{time, value}]`, `pan: -1..1`, `balance`. Project fields:
`export: {codec, bitrate_ladder, ...}`, `canvas: {type:
color|image|blur, ...}`, `markers: [{time, name}]`,
`adjustment_layers: [{start, duration, effects}]`,
`sequences: {name: {...}}`. Full schema: `musecut/timeline.py`.

---

## CLI reference

```
musecut init <name> [--width 1280] [--height 720] [--fps 30]
musecut render <project.json> -o out.mp4 [--preset veryfast] [--crf 20]
         [--codec h264|h265|prores|av1|gif|dnxhd|wav|mp3] [--bitrate-ladder]
musecut captions <media> [--model base] [--format json|ass|srt]
musecut validate <project.json>
musecut info <project.json>
musecut template list | render <name> [--var k=v ...] -o out.mp4
musecut beats <audio> [--sensitivity 1.5]
musecut balance <audio> [-o out.wav] [--target-i -16]
musecut proxy make <project.json> -o proxy.json [--scale 480]
musecut proxy relink <proxy-project.json> -o project.json
musecut scopes <video> [--time 1.0] [--series] [--samples 10]
musecut mcp
musecut version
```

## Development

```bash
python3 -m unittest discover -s tests -v   # 298 tests: 5 smoke + 20 v2 + 16 pro + 12 tts + 17 v3 + 13 round2 + 21 round3 + 11 round4 + 14 round5 + 20 round6 + 4 round7 + 10 round7-inpaint + 8 round7-vocalsep + 19 round8 + 11 round9 + 9 round10 + 9 round11 + 11 round12 + 16 round13 + 14 round14 + 20 round15 + 4 round31 + 5 round47 + 7 round52 + 2 round58 (9 inpaint/beautify/superres tests skip cleanly under the system python — no cv2/onnxruntime there — and run for real under ~/workspace/.venv-video/bin/python)
```

**v0.21.0 (round 58)** — 8K export closed: a 1 s render at
7680x4320 runs end to end (ffprobe 7680x4320 h264, frame luma
~31 for the #112233 colour clip), proven on real footage first
(a 1 s 8K render of the day-street clip, 57 s wall). No engine
change — the row was unrendered, not unimplemented. The round-58
mastery demo is the one-shot / long take (one continuous 6.13 s
shot, no cuts, scale 1.60→1.30 + x 0.35→0.58 + y 0.27→0.63 in one
linear glide).

**v0.20.0 (round 52)** — native mirror/flip effect: clip
`effects` `{"flip": true | "horizontal" | "vertical" | "both"}`
maps to ffmpeg `hflip` / `vflip`. A geometric mirror only — no
colour or timing change — so a flipped clip is frame-identical to
its source under the mirror map. Built for the round-52 mastery
demo (the 180-degree rule): the flipped control is the mirror a
camera on the other side of the axis of action would see.

**v0.19.0 (round 47)** — blend overlays now blend in RGB, not YUV:
`overlays[].blend` converts both inputs to planar RGB (`gbrp`),
blends there, and converts back to `yuv420p`. ffmpeg's blend on YUV
screens the chroma planes toward max (U and V both → 255), so the
first real blend render came out pink (YUV screen vs RGB screen
89 MAE apart on the same two frames); the RGB screen matches the
numpy formula to 0.42 MAE. First real render of the path: the
round-47 double exposure (day street screen-blended with the night
highway's light trails).

**v0.18.0 (round 31)** — radial (clock wipe) transition: `transition_in`
`{"type": "radial"}` maps to ffmpeg xfade `radial` (measured in
round 31: a soft ~30–40° angular ramp sweeping clockwise from 12
o'clock, 50% angle θ_50(p) ≈ 455·p − 76°, not 360·p).

**v0.17.0 (round 15)** — AI translate 0 → 2: the genuine Helsinki-NLP
OPUS-MT MarianMT models as Xenova int8 ONNX mirrors (`Xenova/opus-mt-en-es`
+ `Xenova/opus-mt-en-mul`, sha256-pinned), greedy decode with raw
onnxruntime + `tokenizers` (`musecut.translate` / `ai.ai_translate`).
en->es is exact on three real sentences, en->ur exact on the canonical
sentence; .srt/.json/.txt inputs, timings preserved. Honest scope:
greedy, not beam-4; en->ur is a low-resource pair and garbles some short
lines; translated caption words are re-timed by even distribution.

**v0.16.0 (round 14)** — eye correction 0 → 2: Google's MediaPipe Face
Landmarker (478 points, 256x256) via the verified ONNX mirror
`yakhyo/mediapipe-face-mesh-onnx` (GitHub releases, tag `weights`,
4,864,717 bytes, sha256 pinned) measures each iris-centre offset from
its eye centre (corners midpoint in x, eyelid midpoint in y); a
geometric warp (uniform shift across the iris disk's "stadium"
footprint, smoothstep falloff past it) moves the iris toward the eye
centre (`musecut.eyecorrect` / `ai.eye_correction`). Verified: the warp
moves iris texture exactly (NCC +6.0/+7.0 px for a +0.25 eye-width warp,
+13.0 px for +0.50, scores 0.997–0.999) and the correction applies
exactly the shift its measurement computed; a real 2 s talking-head
clip renders corrected (only 2–4 of 20 frames warp — this clip's
gaze error is ~0.09–0.10 eye-widths, right on the 0.10 deadband
edge, so the rest are untouched by design) with the faceless
B-roll frames untouched, frame-checked — the eyes look more
at-camera, no artefacts.
Honest scope: a 0.10 eye-width deadband (the landmarker noise floor is
~±0.06 — it is shape-locked for iris-only changes and under-reads
synthetic shifts ~3–4x), a frontality gate (yaw ≤ 0.20), a 0.35
eye-width shift clamp, and a sanity bound; glasses glare degrades the
measurement.

**v0.15.0 (round 13)** — lip sync 0 → 2: the genuine Wav2Lip
generator ONNX at 256x256 (public HF mirror
`vnalex/wav2lip-256-onnx`, 214,402,122 bytes, sha256 pinned)
via `musecut.lipsync` / `ai.lip_sync`; Wav2Lip mel preprocessing
(numpy STFT + Slaney melbank, cross-checked vs torch STFT
max|d| 0.060 and librosa corr 0.987), Haar face detection,
feathered lower-half paste. Verified on a real photo + real
voiceover: deterministic (mouth diff 0.0), mouth follows the audio
(speech-vs-silence 3.86), identity preserved. Honest scope: ~1 s
of CPU per frame — short clips practical, long clips are hours;
regenerated lower half slightly softer, occasional faint smudge
under the lip.

**v0.14.0 (round 12)** — classical outpainting 0 → 1: the canvas
is extended by mirroring the edge content outward, with a 12px
Telea seam band softening the mirror kink (`musecut.outpaint` /
`ai.ai_outpainting`); verified on a real 720p frame (interior
byte-identical, strip gradient energy 40.29 vs 5.06 Telea-only).
Honest scope: extends texture/background plausibly, but structured
objects at the edge mirror into symmetric doubles — it cannot
invent new content, so it scores 1, never 2.

**v0.13.0 (round 11)** — super resolution 0 → 2: the genuine
Real-ESRGAN x4plus ONNX (Qualcomm AI Hub public asset — float
model + w8a8 fast variant) via `musecut.superres` /
`ai.super_resolution`; tiled 128x128 inference with feathered
seams, verified on a real 720p frame (gradient energy matches
GT, frame-checked sharper than lanczos). Honest scope: ~4.3 s
per tile float on this CPU — stills + short clips practical,
full-length clips are hours.

**v0.12.0 (round 10)** — vocal beautify 0 → 1: a classical
voice-polish chain (highpass + afftdn + deesser + presence EQ +
acompressor, no ML model) for voice tracks, in-render via
`audio[].vocal_beautify` or standalone via `ai.vocal_beautify`.
Measured on a synthetic voice-like probe: gap noise -6.5 dB,
sibilance/voice -2.7 dB, presence +0.5 dB, loud/quiet spread
10.4 → 4.3 dB. Deliberately no `aexciter` — the probe showed it
raises the gap floor +10.5 dB. Crop-first scale rewrite stays
unshipped: measured ~3x less scale work but changes edge pixels
microscopically — needs an owner decision.

**v0.11.0 (round 9)** — beautify 1 → 2 via YOLO person-region
gating: smoothing is restricted to yolov5n-detected person boxes
(conf ≥ 0.4, expanded 8%, feathered 5 px), so the round-7
colour-only false positives are gone — on the faceless van clip
19/20 plates are byte-identical to the source frames (the one
exception is a 0.407-confidence driver sliver, 92 px) while a
real person frame keeps its inside-box skin reduction (6.3%) with
every pixel outside the box byte-identical. New text animation
`loop-bounce` (second loop-family member next to pulse) — the
text-animations row is now 2.
bug-hunt. Audio-only export builds the audio graph directly
(104 s → 1.2 s for an 8 s wav, mix RMS ratio 1.005); clip inputs
seek with `-ss` before `-i` (10x decode on a 50 s trim,
pixel-identical, MAE 0.0). Four real keyframe bugs fixed and
pinned by exact-value render tests: speed-curve + transform
keyframes replaying per segment, wrong clip durations for
speed-curve clips, slide-down unimplemented, slide-up moving the
wrong way — plus a `geq` chroma bug (opacity keyframes tinted
clips pink, Cb/Cr = 235; now pinned to identity). New text
animations: slide-left/right, pulse, fade-out.
`--filter-threads` / `Renderer(filter_threads=...)` added;
measured NO speedup on the bench project (medians 51.8 s vs
51.9 s) — the early 1.16x claim was suite contention, retracted.
True clean baseline: ~51.8 s for 8 s = 6.5x realtime (the old
"~15x" was measured while the suite ran concurrently).

**v0.9.0 (round 7)** — vocal separation, real: UVR MDX-Net
(`musecut.vocalsep`, `ai.vocal_separation` — the output is the
instrumental stem's complex spectrogram directly, NOT a mask;
vocals = mix − instrumental). Verified on a real known mix
(instrumental vs bed corr 0.976, vocals vs voiceover 0.989;
independently re-verified on a different mix: 0.991 / 0.975).
Classical inpainting, real: OpenCV Telea / Navier-Stokes
(`musecut.inpaint`, `ai.ai_inpainting` — the wall-mounted AC
unit removed, surroundings intact). Face effect, partial:
classical skin smoothing (`musecut.beautify`, `ai.beautify` —
YCbCr skin mask + bilateral filter; ML face effects still
planned). Also fixed: an ease-out easing precedence bug live
since round 2 (the bare `1-pow(1-u,2)` parsed as
`v0+(v1-v0)*1-pow(...)`). Mastery round 7: whip pan (see
EDITING_MASTERY.md). See COMPARISON.md for the round log and
the honest score.

**v0.8.0 (round 6)** — AI matting, real: classical edge
refinement of the RMBG matte (`musecut.matting` — trimap +
guided filter on the luminance guide + feather + colour
decontamination + hardness control), exposed as `ai.ai_matting`
(colour / image / transparent ProRes 4444 cutout). Measured on
a real street frame: edge alignment corr 0.378 → 0.494 (+31%),
halo/fringe pixels 7,096 → 5,518 (−22%); the semi-transparent
transition widens slightly (+12%, documented tradeoff — the
guided filter follows soft luminance ramps), and `hardness=8`
trades the other way (semi −24%, alignment +21%, fringe −7%).
Mastery round 6: match-on-action (see EDITING_MASTERY.md).
See COMPARISON.md for the round log and the honest score.

**v0.7.0 (round 5)** — the no-model remainders: 6 new transitions
(slide, zoom, dissolve, circle, pixelize, dip-to-white — native
ffmpeg xfade; zoomin magnifies the outgoing clip and fades the
incoming in late, pixelize collapses the image into blocks late in
the transition, both verified by frame measurement on a
checkerboard); text templates (5 built-ins + project-level
`text_templates`, explicit fields win, `size_frac` scales with
frame height); two-tier caption sizing (`active_size_ratio` 1.0–2.0
— the highlighted word renders bigger, the elite-reel small-setup
+ big-punch line in one caption event). Mastery round 5: the L-cut
(A's audio lingers 0.4 s under B's picture, FFT-verified). See
COMPARISON.md for the round log and the honest score.

**v0.6.0 (round 4)** — AI background removal (RMBG-1.4 ONNX,
verified Hugging Face mirror, `musecut.bgremove` + the
`background_remove` clip field: colour / image / transparent
ProRes 4444 cutout); reframe handover fix — lag-free g-h
filter tracking (the round-3 moving average lagged fast
subjects by 0.086 of the frame) + handover to the largest
in-frame subject when the tracked one exits, smart reframe at
4 fps and trimmed to the tracked span. RMBG-1.4 is
non-commercial licensed (BRIA) — see `providers.KNOWN_MODELS`.
See COMPARISON.md for the round log and the honest score.

**v0.5.0 (round 3)** — hybrid AI backend (`musecut.providers`: agent
models for voice/captions/stills + local ONNX for per-frame work, each
backend honestly reporting wired / registered / stub); real object
tracking (YOLOv5n ONNX + centroid association, `musecut.track`); smart
reframe (16:9→9:16 following the tracked subject); 3D text, arc/circle
text shape, cover templates; HDR10 export (PQ/BT.2020, h265) and
ProRes 4444 alpha export. HEVC-with-alpha stays refused: libx265 has
no alpha pixel formats on this build (verified). See COMPARISON.md for
the round log and the honest score.

The smoke test generates its own test media with ffmpeg lavfi
sources, renders a ~10 s video (transitions, captions, ducked music,
end card) and verifies it with ffprobe + frame signal stats. The v2
and pro suites render each feature with real media and verify the
result (ffprobe, frame pixels, decoded audio RMS, channel levels).
No sample media is committed to the repo.

## Licence

MIT — see [LICENSE](LICENSE).
