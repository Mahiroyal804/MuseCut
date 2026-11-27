# MuseCut vs pro offline editors — Feature Parity Checklist

Target raised by the project owner (2026-11-20): **PC offline
editors (Premiere Pro / DaVinci Resolve level) are better than
CapCut — MuseCut is built like THAT**. CapCut parity
([CAPCUT_PARITY.md](CAPCUT_PARITY.md)) stays the floor; pro
capabilities are the new ceiling.

**GUI is NOT in scope** (owner decision 2026-11-20): MuseCut stays
agent-native — CLI + JSON timeline + MCP server. It is the
*capabilities* that go pro-level, not a human interface. MuseCut
runs inside the agent for agent editing; there is no human-style
import/export.

Status meanings (same as CAPCUT_PARITY.md):

- **Done** — implemented in v2, covered by a test that renders real
  media and verifies the result, run on this VM.
- **Partial** — works, but narrower than the pro version (note says how).
- **Planned** — interface exists (or is specced here) but not
  implemented; **never claimed as working**.

Verification: `tests/test_smoke.py` (5 tests, v1), `tests/test_v2.py`
(20 tests, CapCut RE items), `tests/test_pro.py` (16 tests, this
file's pro items), `tests/test_tts.py` (12 tests), `tests/test_v3.py`
(17 tests, round-1 gaps) — **70 tests, all passing** (2026-11-20).

---

## 1. Timeline

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| Unlimited multi-track timeline | ✅ | **Done** — clips / overlays / texts / audio are unbounded lists in the JSON timeline; the suite renders multi-track projects throughout | `timeline.py` |
| Nested sequences / compound clips | ✅ | **Done** — a clip's `nested` is a whole project: inline dict, `sequence:<name>` (top-level `sequences`), or a .json path; rendered recursively (depth cap 3), tested inline + named | `timeline.py`, `render.py` |
| Markers | ✅ | **Done** — timeline-level `markers: [{time, name}]`, validated, exposed via MCP `add_marker` / `get_status`; editorial metadata (no render effect — ffmpeg has no marker concept) | `timeline.py`, `mcp_server.py` |
| Adjustment layers | ✅ | **Done** — top-level `adjustment_layers: [{start, duration, effects}]`; each filter in the layer gets an `enable='between(t, start, end)'` window on the composited picture; brightness window verified on frames | `timeline.py`, `render.py` |
| Trim / split / merge | ✅ | **Done** (v1) | `timeline.py`, `render.py` |
| Speed ramping | ✅ | **Done** — `speed_curve: [{time, speed}]`, expanded to constant-speed segments; 1s@1x + 1s@2x = 1.5s verified | `timeline.py`, `render.py` |
| Reverse / freeze frame | ✅ | **Done** — verified on a brightness-ramp clip (reversed = bright→dark) and a frozen frame (YAVG identical at 0.2s and 1.8s) | `render.py` |

## 2. Transform keyframes

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| Position keyframes | ✅ | **Done** — `keyframes.position: [{time, value: [x, y]}]`, normalised 0..1, evaluated per frame via `overlay` x/y expressions | `keyframes.py`, `render.py` |
| Scale keyframes | ✅ | **Done** — `keyframes.scale: [{time, value}]`, per-frame `scale` with `eval=frame`, composited back onto the canvas. **(v3)** value range raised 0..4 → 0..8; verified in `tests/test_v3.py` (accepts 6.0, rejects 9.0, 1→6 render verified) | `keyframes.py`, `render.py` |
| Rotation keyframes | ✅ | **Done** — `keyframes.rotation: [{time, value}]` in degrees, per-frame `rotate` about the frame centre | `effects.py`, `render.py` |
| Opacity keyframes | ✅ | **Done** — `keyframes.opacity: [{time, value}]` 0..1, via `geq` alpha plane composited over black (verified: white frame 235 → ~38 luma over the keyframed fade). `colorchannelmixer` cannot take time expressions in this ffmpeg build — verified, hence `geq`. **(v0.10.0, round 8)** `geq` fills unspecified planes with the luma value, so the chroma expressions are now pinned explicitly (`cb_expr='cb(X,Y)':cr_expr='cr(X,Y)'`) — before this fix every opacity-keyframed clip came out pink (Cb/Cr = 235 on a white frame). | `keyframes.py`, `effects.py`, `render.py` |
| Anchor point | ✅ | **Done** — `keyframes.anchor: [x, y]` is the position reference point (the anchor sits at the keyframed position) | `render.py` |
| Keyframe interpolation | ✅ (bezier/ease) | **Done (round 2)** — per-keyframe ``"easing"``: "linear" / "ease-in" / "ease-out" / "ease-in-out", native per-frame ffmpeg expressions (`keyframes._eased`); verified in `tests/test_round2.py` (ease-in blob 270 px < linear 420 px < ease-out 570 px at u=0.5) | `keyframes.py` |
| Legacy zoom/pan keyframes | — | **Done** (v1, still supported) | `effects.py` |

## 3. Colour

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| LUT support (.cube) | ✅ (Lumetri) | **Done** — clip `effects.lut` (a `.cube` path) → `lut3d`; verified: a shadow-lifting LUT raises a dark frame's mean luma; a missing file is a clean `RenderError` | `effects.py`, `render.py` |
| Colour scopes (waveform / vectorscope / histogram) | ✅ | **Partial** — no GUI scope display (out of scope by design); **Done as data**: `musecut scopes video.mp4 [--time T] [--series]` returns luma/chroma/saturation stats as JSON (`y_avg`, `y_min/max`, `u/v_avg`, `sat_avg`, `clipped_highlights`, `crushed_blacks`) from ffmpeg `signalstats` | `scopes.py`, `cli.py` |
| Adjust sliders (exposure / highlights / shadows / clarity / dehaze / fade / sharpen) | ✅ | **Done** — all seven as ffmpeg-backed sliders (`exposure` filter, two-point `curves`, `unsharp`, `eq`+`curves` dehaze approximation, lifted-blacks fade); exposure +1 visibly brightens a mid-grey frame | `effects.py` |
| HDR export | ✅ | **Done (v0.5.0, round 3)** — `export: {"codec": "h265", "hdr": true}`: zscale PQ/BT.2020 + libx265 mastering-display / MaxCLL, 10-bit; ffprobe-verified (smpte2084 / bt2020 / yuv420p10le / Main 10). Honest scope: SDR timeline converted into an HDR10 container — HDR highlights are NOT invented. (This row said "Planned" here until round 10; CAPCUT_PARITY.md has said Done since round 3 — doc fix, no score change.) | `render.py` |

## 4. Audio mixer

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| Per-track EQ (3-band) | ✅ | **Done** — `eq: {low, mid, high}` dB (-24..24): lowshelf 200 Hz / parametric 1 kHz / highshelf 6 kHz | `audio.py`, `render.py` |
| Per-track compressor | ✅ | **Done** — `compressor: {threshold dB, ratio, attack ms, release ms, makeup dB}` → `acompressor` (makeup converted to ffmpeg's linear 1..64 factor) | `audio.py`, `render.py` |
| Keyframed volume | ✅ | **Done** — `volume_keyframes: [{time, value}]` → `volume` with `eval=frame`; verified by direct RMS: first half-second ~-7.6 dB vs full volume later (note: `volumedetect` mis-reports keyframed segments by ~2 dB — the test measures decoded RMS itself) | `audio.py`, `render.py` |
| Pan (stereo balance) | ✅ | **Done** — `pan: -1..1` → `stereotools=balance_out`; verified: pan -1 leaves the left channel untouched and silences the right | `audio.py`, `render.py` |
| Auto volume balancing (loudnorm) | ✅ | **Done** — per-track `balance` (EBU R128 single-pass loudnorm) + CLI `musecut balance` | `audio.py`, `cli.py` |
| Music ducking | ✅ | **Done** (v1, sidechaincompress) | `audio.py`, `render.py` |
| Noise reduction | ✅ | **Done** (v1, `afftdn`) | `audio.py` |
| Vocal separation | ✅ | **Done (v0.9.0, round 7)** — UVR MDX-Net (`UVR-MDX-NET-Inst_HQ_3.onnx`); verified on a real known mix (instrumental vs bed corr 0.976, vocals vs VO 0.989); `ai.vocal_separation` | `vocalsep.py`, `ai.py` |
| Vocal beautify | ✅ | **Done (v0.12.0, round 10)** — classical vocal-polish chain (highpass + afftdn + deesser + presence EQ + acompressor; `musecut.audio` / `ai.vocal_beautify`); measured on a synthetic voice-like probe (gap-vs-quiet -5.0 dB, sibilance -2.7 dB, presence +0.5 dB, spread 10.4 -> 4.3 dB) and on a real voiceover segment. Honest scope: fixed chain, not an ML voice model | `audio.py`, `ai.py` |

## 5. Workflow

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| Proxy workflow | ✅ | **Done** — `musecut proxy make project.json -o proxy.json` renders low-res H.264 picture proxies (MP3 proxies for audio-only inputs) and swaps every `src` to its proxy, storing `proxy_map` on the project; `musecut proxy relink` restores the originals exactly; proxy project renders | `proxy.py`, `cli.py` |
| Markers | ✅ | **Done** — see §1 | `timeline.py` |
| Blend modes | ✅ | **Done** (v1) | `effects.py` |
| Stabilisation | ✅ | **Done** — vidstab two-pass, verified (`.trf` created, output duration correct) | `effects.py`, `render.py` |
| Noise reduction | ✅ | **Done** (v1) | `audio.py` |

## 6. Export

| Feature | Premiere / Resolve | MuseCut v2 | Module |
|---|---|---|---|
| DNxHD / DNxHR | ✅ | **Done** — `export.codec: "dnxhd"` (ffmpeg `dnxhd` encoder); valid frame sizes only (1280x720 @145M, 1920x1080 @175M, 3840x2160 DNxHR HQ @880M — all three verified by encode + ffprobe); other sizes are a clean `RenderError` | `render.py` |
| ProRes (422 / LT / HQ / 4444 / Proxy) | ✅ | **Done** — `prores_ks`, profiles 0–4; HQ verified by encode + ffprobe | `render.py` |
| AV1 | ✅ | **Done** — `libsvtav1` preset 8; verified by encode + ffprobe | `render.py` |
| GIF / audio-only (WAV/MP3) | ✅ | **Done** — single-pass palette GIF; WAV/MP3 via a video intermediate + audio extraction | `render.py` |
| Per-resolution bitrate ladder | ✅ | **Done** — `export.bitrate_ladder` (SD/HD/FullHD/2K/4K + GOP + profile); verified in the built command; ignored for ProRes/DNxHD (their bitrates are fixed by the codec) | `render.py` |
| 4K export | ✅ | **Done** — 3840x2160 1 s render verified by ffprobe | `render.py` |
| 8K export | ✅ | **Done (round 2)** — real 5 s 7680x4320 render, ffprobe-verified (`~/workspace/your_files/musecut-tests/8k-proof.mp4`, 23.6 MB); source was 1080p, so an engine upscale, not native 8K footage | `timeline.py`, `render.py` |
| HEVC with alpha | ✅ | **Blocked (round 3, honestly unclosable on this build)** — libx265 has no alpha pixel formats, so HEVC-with-alpha cannot be encoded here; VP9 alpha doesn't round-trip (re-verified 2026-11-20). Refused at validation instead of silently dropping alpha | `render.py` |
| Super resolution on export | ✅ | **Done (v0.13.0, round 11)** — Real-ESRGAN x4plus ONNX (Qualcomm AI Hub public asset — float + w8a8 fast variant); tiled 128x128 inference with feathered seams, 4x upscale of stills and clips through the real engine (`musecut.superres` / `ai.super_resolution`). Verified on a real 720p frame (gradient energy matches GT, frame-checked sharper than lanczos). Honest scope: ~4.3 s/tile float on this CPU — stills + short clips practical, full-length clips are hours | `superres.py`, `ai.py` |

## 7. AI / ML (Planned unless marked Done — never faked)

| Feature | Status | Module |
|---|---|---|
| AI background removal / auto-cutout / AI matting | **Done (v0.6.0, round 4; matting v0.8.0, round 6)** — RMBG-1.4 ONNX (verified HF mirror, 176,153,355 bytes) + `background_remove` clip field; auto-cutout = transparent ProRes 4444 (alpha round-trips). Matting done: classical edge refinement (trimap + guided filter + feather + decontamination + hardness). Model is BRIA non-commercial licensed | `bgremove.py`, `matting.py`, `ai.py`, `render.py` |
| Camera / video tracking, smart reframe | **Done (v0.6.0, round 4)** — YOLOv5n ONNX + nearest-to-prediction association + lag-free g-h filter + handover to the largest in-frame subject; smart reframe at 4 fps, trimmed to the tracked span; re-rendered proof centred 0.508/0.512/0.497/0.496/0.503 | `track.py`, `ai.py` |
| Vocal separation | **Done (v0.9.0, round 7)** — UVR MDX-Net; verified on a real known mix (0.976 / 0.989) | `vocalsep.py`, `ai.py` |
| Vocal beautify | **Done (v0.12.0, round 10)** — classical vocal-polish chain (highpass + afftdn + deesser + presence EQ + acompressor; `musecut.audio` / `ai.vocal_beautify`); measured on a synthetic voice-like probe and a real voiceover segment. Honest scope: fixed chain, not an ML voice model (this §7 row said "Planned" until round 11 — the §4 row had it Done since round 10; doc fix, no score change) | `audio.py`, `ai.py` |
| Face effect | **Done (v0.11.0, round 9)** — person-gated classical skin smoothing (YOLOv5n person boxes + YCbCr skin mask + bilateral filter); measured: faceless van clip 19/20 plates byte-identical, real person frame keeps inside-box reduction with outside-box pixels byte-identical. Honest limits: the gate is a bounding box, not a silhouette; other face effects (slim face, eye enlarging) still Planned | `beautify.py`, `ai.py` |
| Lip sync | **Done (v0.15.0, round 13)** — genuine Wav2Lip generator ONNX (verified HF mirror `vnalex/wav2lip-256-onnx`, 214,402,122 bytes, sha256-pinned); deterministic, mouth follows audio (speech-vs-silence 3.86), identity preserved; `musecut.lipsync` / `ai.lip_sync`. Honest scope: ~1 s CPU/frame; regenerated lower half slightly softer | `lipsync.py`, `ai.py` |
| Eye correction | **Done (v0.16.0, round 14)** — Google MediaPipe Face Landmarker 478 ONNX (verified mirror `yakhyo/mediapipe-face-mesh-onnx`, 4,864,717 bytes, sha256-pinned); iris-centre offset measured per eye, geometric stadium warp moves the iris toward the eye centre (NCC-verified exact: +6.0/+7.0 px for +0.25 eye-width warp); 0.10 eye-width deadband, frontality gate, shift clamp; `musecut.eyecorrect` / `ai.eye_correction`. Honest scope: the landmarker under-reads synthetic iris-only shifts ~3–4x, so a synthetic +0.25 warp comes back only ~1 px of ~6 px; glasses glare degrades the measurement | `eyecorrect.py`, `ai.py` |
| Digital human | **Planned** — ML model, not bundled | `ai.py` |
| AI inpainting | **Done (v0.9.0, round 7)** — classical OpenCV inpainting (Telea / Navier-Stokes); verified on a real frame (AC unit removed, surroundings intact) | `inpaint.py`, `ai.py` |
| AI outpainting | **Partial (v0.14.0, round 12)** — classical mirror-seeded Telea extension; extends texture/background plausibly, interior byte-identical, but structured objects mirror into symmetric doubles — cannot invent new content (scores 1, never 2) | `outpaint.py`, `ai.py` |
| AI translate | **Done (v0.17.0, round 15)** — genuine Helsinki-NLP OPUS-MT MarianMT int8 ONNX (verified Xenova mirrors: opus-mt-en-es + opus-mt-en-mul, sha256-pinned); greedy decode with raw onnxruntime + `tokenizers`; en->es 3/3 exact on real sentences, en->ur canonical sentence exact (real Urdu script); .srt/.json/.txt inputs, timings preserved. Honest scope: greedy, not beam-4; en->ur is a low-resource pair and garbles some short lines; translated caption words are re-timed by even distribution | `translate.py`, `ai.py` |
| AI sticker | **Planned** — ML model, not bundled | `ai.py` |
| Voice clone | **Planned** — hookup point only (`musecut.tts`); needs a cloning TTS backend | `ai.py` |
| AI transitions / AI sound effects | **Planned** — server-side generation in CapCut; local equivalent is planned AI work | `ai.py` |
| Text-to-speech | **Done** — Muse TTS `avocado_v2:MAI_03` (speed 90) via `MuseTTSProvider`; CLI `musecut voice synth`, MCP `synthesize_voice` / `list_voices`; real audio verified (17 words → 6.89 s, mean −28.2 dB) | `tts.py` |

## 8. What MuseCut deliberately does NOT copy from pro editors

| Feature | Why not |
|---|---|
| GUI / NLE interface | Agent-native by design (owner decision 2026-11-20); the MCP server *is* the interface |
| Cloud storage / cloud sync / collaboration | Local-first by design |
| Stock media library | Bring your own media; no licensing strings attached |
| Human-style import/export (incl. CapCut-draft import) | Owner decision 2026-11-20: MuseCut runs inside the agent; no draft-folder import is needed |

---

## How pro parity is verified

- `tests/test_pro.py` (16 tests): every **Done** row in §1–§6 has a
  test that renders real media and checks the result with ffprobe,
  frame pixels (PNG md5 / signalstats luma), decoded audio RMS, or
  channel levels — not just "the command ran".
- Anything marked **Planned** raises `NotImplementedError` with a
  reason instead of pretending to work. If a feature is listed here
  as Done but no test covers it, that is a bug — file it.
- Full suite: `python3 -m unittest discover -s tests -v` — **41
  tests, all passing** (5 smoke + 20 v2 + 16 pro).
