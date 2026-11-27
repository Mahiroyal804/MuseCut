# MuseCut vs CapCut — Feature Parity Checklist

Target set by the project owner (2026-11-20): **everything in CapCut
must be in MuseCut**. This file is the honest scoreboard. Status
meanings:

- **Done** — implemented in v1/v2, covered by the smoke test or a unit
  test, verified on this VM.
- **Partial** — works, but narrower than CapCut's version (note says how).
- **Planned** — interface exists (or is specced here) but not
  implemented; **never claimed as working**.
- **Hookup point** — clean provider interface; bring your own backend.

v2 (2026-11-20) implements the ffmpeg-level RE findings and the pro
scope — see [PRO_PARITY.md](PRO_PARITY.md) for the Premiere/Resolve
scoreboard. Statuses below marked **(v2)** are verified by
`tests/test_v2.py` (20 tests) and `tests/test_pro.py` (16 tests);
full suite is 70 tests, all passing (v3: +12 tts, +17 v3).

CapCut's feature list is taken from its publicly documented feature
set (capcut.com, app store listings, help centre), **verified and
extended by reverse-engineering CapCut 20.3.0** (`com.lemon.lvoverseas`,
APK hash-verified, 2026-11-20 — full write-up in
`~/workspace/tools/reverse-engineering/capcut-findings.md` §4).
Findings from the decompiled output are marked **(RE)**. No CapCut
code was copied; findings are clean-room descriptions only.

---

## 1. Timeline

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| Trim clip | ✅ | **Done** — `start` + `duration` per clip | `timeline.py`, `render.py` |
| Split clip | ✅ | **Done** — split a clip into two timeline entries (or use `speed_curve` segmenting) | `timeline.py` |
| Merge / concat clips | ✅ | **Done** — sequential clips concat; `xfade`/`concat` join | `render.py` |
| Speed control (constant) | ✅ | **Done** — `speed` 0.05–16 (setpts + atempo chain) | `render.py` |
| Speed curves | ✅ | **Done** — `speed_curve: [{time, speed}]`, expanded to constant-speed segments. **(v0.10.0, round 8)** transform keyframes are now window-shifted into each segment's output window (before, the whole curve replayed per segment); clip durations use the segments' summed output durations. | `timeline.py`, `render.py` |
| Reverse | ✅ | **Done** — `reverse: true` (video `reverse` + audio `areverse`) | `render.py` |
| Freeze frame | ✅ | **Done** — `freeze_at` holds one frame for `duration` | `render.py` |
| Keyframes / animation | ✅ | **Done** — zoom + pan keyframes (`zoompan`); text fade/slide/pop; overlay fade/slide. **(v2) Full transform keyframes: position / scale / rotation / opacity + anchor point** (`keyframes.py`, per-frame ffmpeg expressions, verified in `tests/test_pro.py`). **(v0.10.0, round 8)** text animations: slide-down implemented, slide-up fixed (was wrong direction, only for y="lower"), slide-left/right + pulse + fade-out added; opacity `geq` chroma pinned (was pink). | `effects.py`, `render.py`, `keyframes.py` |
| Overlays / PiP | ✅ | **Done** — image/video overlays, position, size, animation | `render.py` |
| Masks | ✅ | **Done** — circle / rect / linear masks via `geq` + `maskedmerge`, blur or darken outside. **(v3)** diagonal seam (`angle`, −180..180; 90° = vertical like linear) + desaturate (B&W) mask effect; verified by render + pixel checks in `tests/test_v3.py` | `effects.py` |
| Blending modes | ✅ | **Done** — screen/multiply/overlay/darken/lighten/difference/addition/average/hardlight/softlight | `effects.py` |
| Multi-track timeline | ✅ | **Done** — main track + overlay track + text track + audio tracks; **(v2)** unlimited tracks + nested sequences / compound clips + markers + adjustment layers (see [PRO_PARITY.md](PRO_PARITY.md)) | `timeline.py` |

## 2. Transitions, effects, stickers, text

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| Transitions | ✅ | **Done** — crossfade, dip-to-black, whoosh (+synth SFX), wipe, hard cut. **(v0.7.0, round 5)** + slide (slideleft), zoom (zoomin), dissolve, circle (circleopen), pixelize, dip-to-white (fadewhite) — native ffmpeg xfade; zoomin magnifies the outgoing clip and fades the incoming in late, pixelize collapses the image into blocks late in the transition (both verified by frame measurement on a checkerboard in `tests/test_round5.py`). Zoom-blur as a single named type is NOT present — ffmpeg has no zoom+blur xfade; the two halves exist separately (zoom, hblur is not wired) | `render.py` |
| Video effects / filters | ✅ | **Done** — presets: cinematic, vivid, B&W, vintage, warm, cool, dramatic, soft, sharpen | `effects.py` |
| Stickers | ✅ | **Done** — image overlays with alpha (PNG/WebP), fade/slide animation | `render.py` |
| Text (fonts, styles) | ✅ | **Done** — 6 bundled fonts, size/colour/outline, lower-third/centre positioning | `timeline.py`, `render.py` |
| Text animations | ✅ | **Done** — fade, slide-up/down/left/right, pop, pulse (loop), fade-out (out), loop-bounce (loop, v0.11.0 round 9: text bobs ±8 px at 2 Hz, render-tested). In / loop / out families all covered | `render.py` |
| Auto-captions | ✅ | **Done** — faster-whisper word timestamps | `captions.py` |
| Karaoke word highlight | ✅ | **Done** — viral-gold ASS: active word gold, rest white, lower third. **(v3)** mid-frame + top placement (`position: lower|mid|top`, ASS Alignment 2/5/8) and per-word colours (`word_colors: {WORD: "#RRGGBB"}`, case-insensitive); verified by ASS output + render in `tests/test_v3.py` | `captions.py` |
| Caption styles | ✅ | **Done (v0.8.0, round 6)** — named registry `timeline.CAPTION_STYLES` (viral-gold / minimal / news), wired into `words_to_ass(style=...)` + render + timeline validation + MCP; explicit fields win over the style; viral-gold is the historical default (no-style output is byte-identical). Frame-verified: gold active word (1,269 gold px) vs minimal (0 gold, white active) vs news (dark bubble, +12k dark px) | `captions.py`, `timeline.py`, `render.py` |

## 3. Audio

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| Music / SFX tracks | ✅ | **Done** — multiple tracks, loop, volume, fade in/out | `render.py`, `audio.py` |
| Voiceover track | ✅ | **Done** — `role: "voice"` track | `render.py` |
| Voice changer | ✅ | **Done** — pitch/formant via `rubberband`; presets chipmunk / deep / robot / slow-mo | `audio.py` |
| Audio extraction from video | ✅ | **Done** — `musecut` uses clip audio directly; `audio.extract_audio` pulls a wav | `audio.py` |
| Noise reduction | ✅ | **Done** — `afftdn` (FFT denoise) per track | `audio.py` |
| Beat sync markers | ✅ | **Done** — onset detection on the energy envelope (stdlib, no aubio) | `audio.py` |
| Music ducking | ✅ | **Done** — `sidechaincompress` ducks music under voice | `audio.py`, `render.py` |
| Audio speed / pitch | ✅ | **Done** — atempo chains match video speed; rubberband for pitch | `render.py`, `audio.py` |
| Auto volume balancing | ✅ | **Done (v2)** — per-track `loudnorm` balance + CLI `musecut balance` | `audio.py`, `cli.py` |
| Audio mixer (EQ / compressor / keyframed volume / pan) | ✅ | **Done (v2)** — 3-band EQ, `acompressor`, keyframed volume (`eval=frame`), stereo pan (`stereotools`) | `audio.py`, `render.py` |

## 4. Video

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| Background remover (chroma key) | ✅ | **Done** — `chroma_key` (green screen, `chromakey` filter) | `effects.py` |
| Background remover (AI) | ✅ | **Done (v0.6.0, round 4)** — real RMBG-1.4 ONNX (CPU, verified Hugging Face mirror `briaai/RMBG-1.4`, 176,153,355 bytes) + `background_remove` clip field in the render engine (`alphamerge` + colour / image / transparent background); verified on real footage (street clip, magenta proof: background pixels (253,0,253), car pixel untouched; transparent path round-trips ProRes 4444 alpha, 83% clear / 7% opaque). Honest limits: masks sampled at 5 fps by default (~4.3 s/frame at 1024), no edge-refinement controls; model is BRIA non-commercial licensed | `bgremove.py`, `ai.py`, `render.py` |
| Auto-cutout | ✅ | **Done (v0.6.0, round 4)** — `ai.auto_cutout` = remove_background with a transparent background (ProRes 4444 + alpha, verified) | `ai.py` |
| Stabilisation | ✅ | **Done** — vidstab two-pass (`stabilize: true`) | `effects.py`, `render.py` |
| Colour adjustment | ✅ | **Done (v2)** — brightness/contrast/saturation/HSL (`eq`, `hue`), colour balance, presets, AND all of CapCut's verified adjust sliders: Exposure, Highlights, Shadows, Clarity, Dehaze, Sharpen, Fade (plus Grain) — each ffmpeg-backed, verified by render + frame luma in `tests/test_v2.py` | `effects.py` |
| 4K export | ✅ | **Done** — any width/height/fps; H.264/H.265. **(v2) Export codecs Done:** ProRes 422/LT/HQ/4444/422Proxy (`prores_ks`), AV1 (`libsvtav1`), GIF (palette), WAV/MP3 audio-only, DNxHD/DNxHR (`dnxhd`) — all verified by encode + ffprobe. HEVC with alpha stays **Planned** | `render.py` |
| Resolutions / fps / formats | ✅ | **Done** — 16:9, 9:16, any size; 1–120 fps; MP4/MOV/WebM. **(v2) Per-resolution bitrate ladder Done:** SD/HD/FullHD/2K/4K + GOP + profile in `export.bitrate_ladder` | `timeline.py`, `render.py` |
| Zoom / pan (Ken Burns) | ✅ | **Done** — keyframed zoom/pan | `effects.py` |

## 5. AI

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| Auto-captions | ✅ | **Done** — faster-whisper | `captions.py` |
| Text-to-speech | ✅ | **Done** — Muse TTS `avocado_v2:MAI_03` (speed 90) via `MuseTTSProvider`, registered by default; CLI `voice synth`, MCP `synthesize_voice`; real audio verified | `tts.py` |
| Auto-cutout | ✅ | **Done (v0.6.0, round 4)** — `ai.auto_cutout` (RMBG-1.4 + ProRes 4444 alpha, verified) — see §4 | `ai.py` |
| Templates | ✅ | **Done** — JSON timeline presets with `{{variable}}` substitution (`examples/`, `musecut template render`) | `cli.py`, `examples/` |
| AI effects / AI video gen | ✅ | **Planned** — out of scope for the ffmpeg engine; clips can be AI-generated elsewhere and used as `src` | — |

## 6. Export & access

| Feature | CapCut | MuseCut v2 | Module |
|---|---|---|---|
| No watermark | ❌ (free tier) | **Done** — never any watermark | `render.py` |
| No login / account | ❌ | **Done** — fully local, no account, no network | — |
| Scriptable / API | ❌ (no official API; community MCPs only write draft files and need desktop export) | **Done** — JSON timeline + CLI + stdio MCP server (15 tools in v2: + `set_export`, `set_canvas`, `add_marker`). **(RE)** CapCut's own draft is JSON too (`GetJsonFromDraft` / `GetMainDraftJsonStr` engine entry points, `mini_draft.json`, draft addressed by `draftPath`) — CapCut-draft import is **not needed**: owner decision 2026-11-20, MuseCut runs inside the agent, no human-style import/export | `cli.py`, `mcp_server.py` |
| Open source | ❌ | **Done** — MIT, this repo | — |
| Batch / programmatic render | ❌ | **Done** — render any number of timelines headless | `cli.py` |

## 7. Verified by reverse-engineering (CapCut 20.3.0) — status in MuseCut v2

Proved in the decompiled output (`DraftInfo` fields, `ExportConfig$CodecType`,
`com/draft/ve/data/*` serializers, strings.xml, native `.so` strings — see
`~/workspace/tools/reverse-engineering/capcut-findings.md` §4). Rows marked
**Done (v2)** are verified by `tests/test_v2.py` (20 tests) and
`tests/test_pro.py` (16 tests); the rest stay honest **Planned**.

| Feature (RE evidence) | Status in MuseCut v2 |
|---|---|
| Motion blur (`DraftInfo.motionBlurCnt`) | **Done (v2)** — `motion_blur` on clips, verified by render + frame luma change |
| Glitch / strobe effects | **Done (v3)** — clip `effects.glitch`: RGB-shift + slice displacement + noise (`rgbashift`/`geq`/`noise`, deterministic), optional strobe (every Nth frame black); verified by render + frame luma / pixel-diff checks in `tests/test_v3.py` |
| Camera / video tracking (`videoTracking`) | **Done (v0.6.0, round 4)** — real YOLOv5n ONNX (CPU) + nearest-to-prediction association + lag-free constant-velocity (g-h) filter (`musecut.track`); verified on real footage (city clip, car conf 0.86–0.91). Round-4 fix: the round-3 moving average LAGGED fast subjects by 0.086 of the frame (verified by cross-correlation — the engine was faithful, the track was late); g-h filter max lag 0.009 on the van clip. Handover: when the subject mostly exits the frame the track hands over to the largest in-frame subject of the same class (recorded in the track); ~0.4s/frame detection, ~20s for a 10s clip at 2fps; honest limits in `track.py` (no re-ID after leaving frame) |
| Smart reframe + auto reframe (`smartReframeCnt`) | **Done (v0.6.0, round 4)** — `ai.smart_reframe`: track → position keyframes, cover-scaled clip (`fit: "none"` + keyframe scale = cover scale); grid-diagnostic verified (subject centred ±2px); per-axis clamp (no vertical shift at cover scale, no black bars). Round-4 fix: tracking at 4 fps, g-h filter, handover support, clip trimmed to the tracked span — re-rendered proof measured centred at 0.508 / 0.512 / 0.497 (grey phase) and 0.496 / 0.503 (black sedan after handover); the round-3 proof measured 0.717 / 0.596 / 0.268 at the same times |
| Vocal separation (`vocalSeparation`) | **Done (v0.9.0, round 7)** — UVR MDX-Net (`UVR-MDX-NET-Inst_HQ_3.onnx`, 66,759,214 bytes, sha256 `317554b0…9adc`, GitHub release `TRvlvr/model_repo` — the HuggingFace mirror now 401s; same size + hash verified). The correct post-processing was pinned to the actual UVR source (`lib_v5/tfc_tdf_v3.py` + `separate.py`): the model output is the instrumental stem's complex spectrogram directly, NOT a mask; vocals = mix − instrumental; STFT n_fft=6144 / hop=1024, channels [L-real, L-imag, R-real, R-imag], lowest 3 bins zeroed, ×1.035 compensate. Verified on a real known mix (real VO + real bed, 12 s): instrumental vs bed corr 0.976, vocals vs VO 0.989, cross-corrs 0.026/0.027 — independently re-verified by the round-7 coordinator on a different mix (0.991/0.975). `musecut.vocalsep` + `ai.vocal_separation`; ~15 s CPU per 5.92 s chunk |
| Vocal beautify (`vocalBeautifyCnt`) | **Done (v0.12.0, round 10)** — classical vocal-polish chain (`musecut.audio.vocal_beautify_filter` + `ai.vocal_beautify`): highpass + afftdn denoise + deesser + presence EQ + acompressor, wired in-render as `audio[].vocal_beautify` and standalone with loudnorm to -16 LUFS. Measured on a synthetic voice-like probe: gap-vs-quiet noise -5.0 dB, sibilance/voice -2.7 dB, presence +0.5 dB, loud/quiet spread 10.4 -> 4.3 dB; verified on a real voiceover segment (duration preserved, peak < 0.99, -16 LUFS). Deliberately no `aexciter` — the probe showed it raises the gap floor +10.5 dB. Honest scope: a fixed classical chain, not an ML voice model — no per-voice adaptation, no voice clone/emotion control |
| Lip sync (`lipSyncCnt`) | **Done (v0.15.0, round 13)** — genuine Wav2Lip generator ONNX (verified HF mirror `vnalex/wav2lip-256-onnx`, 214,402,122 bytes, sha256-pinned); deterministic, mouth follows audio (speech-vs-silence mouth diff 3.86), identity preserved; `musecut.lipsync` / `ai.lip_sync`. Honest scope: ~1 s CPU per frame; regenerated lower half slightly softer, occasional faint smudge under the lip |
| Eye correction (`eyeCorrection`) | **Done (v0.16.0, round 14)** — Google MediaPipe Face Landmarker 478 ONNX (verified mirror `yakhyo/mediapipe-face-mesh-onnx`, 4,864,717 bytes, sha256-pinned); iris-centre offset measured per eye (y from the eyelid midpoint), geometric stadium warp moves the iris toward the eye centre — warp NCC-verified exact (+6.0/+7.0 px for a +0.25 eye-width warp, +13.0 px for +0.50, scores 0.997–0.999); 0.10 eye-width deadband, frontality gate (yaw ≤ 0.20), 0.35 eye-width shift clamp; `musecut.eyecorrect` / `ai.eye_correction`. Honest scope: the landmarker is shape-locked for iris-only changes and under-reads synthetic shifts ~3–4x; glasses glare degrades the measurement |
| Digital human (`digitalHumanCnt`) | **Planned** — ML avatar video; stub raises `NotImplementedError` |
| AI inpainting / outpainting / matting / translate / sticker (`aiInPaintingCnt`, `aiOutPaintingCnt`, `aiMatting`, `aiTranslateCnt`, `aiStickerCnt`) | **Done for matting (v0.8.0, round 6)** — the RMBG-1.4 matte is refined with classical edge controls: trimap (erode/dilate) + guided filter (luma guide) + feather + colour decontamination + hardness (`musecut.matting`, `ai.ai_matting`). Measured on a real frame: alignment corr 0.378 → 0.494, fringe 7,096 → 5,518; the semi-transparent transition widens slightly (+12%, documented tradeoff). **Done for inpainting (v0.9.0, round 7)** — classical OpenCV inpainting (Telea / Navier-Stokes, `musecut.inpaint`, `ai.ai_inpainting`): verified on a real frame — the wall-mounted AC unit is removed and replaced by wall texture (masked-region mean abs diff 24.08, unmasked diff 2.14 = encode noise; plate level unmasked pixels byte-identical). Honest scope: Telea fills from surrounding texture, it is not generative — a faint seam can show at the mask edge, and a moving object cannot be removed with a static rect. **Partial for outpainting (v0.14.0, round 12)** — classical mirror-seeded Telea extension (`musecut.outpaint`, `ai.ai_outpainting`): the canvas is extended by mirroring the edge content outward, with a 12px Telea seam band softening the mirror kink. Verified on a real 720p frame: interior byte-identical outside the band, outpainted-strip gradient energy 40.29 vs 5.06 Telea-only (8x texture kept); on a synthetic diagonal stripe the mirror V kink is visibly rounded but not straightened, and on a perfect linear ramp Telea ripples (band 2nd-diff 1.64 vs 0.64 mirror). Honest scope: extends texture/background plausibly, but structured objects at the edge mirror into symmetric doubles — it cannot invent new content, which is why it scores 1, never 2. Translate **Done (v0.17.0, round 15)** — genuine Helsinki-NLP OPUS-MT MarianMT int8 ONNX (verified Xenova mirrors opus-mt-en-es + opus-mt-en-mul, sha256-pinned), greedy decode with raw onnxruntime + `tokenizers` (`musecut.translate`, `ai.ai_translate`): en->es 3/3 exact on real sentences, en->ur canonical sentence exact; .srt/.json/.txt inputs with timings preserved; a real clip rendered with translated Spanish captions, OCR-verified on the frame. Honest scope: greedy, not beam-4; en->ur is a low-resource pair and garbles some short lines; translated words re-timed by even distribution. Sticker still **Planned** — ML model; stub raises `NotImplementedError` (chroma key is the working offline substitute) |
| Voice-clone tone (`cloneToneCnt`, `vcCloneToneCnt`) | **Planned** — TTS-voice cloning; hookup point only (`musecut.tts`) |
| Emotion tone (`emotionToneCnt`) | **Planned** — voice emotion control |
| Text shape (`textShapeId`) | **Done (v0.5.0, round 3)** — `texts[].shape`: arc / circle, one drawtext per character, radius 50–3000px, angle 30–360°; verified by render in `tests/test_round3.py` |
| 3D text (`movie3dTextTemplateId`) | **Done (v0.5.0, round 3)** — `texts[].3d`: extruded text (depth 1–20, depth colour), verified by render in `tests/test_round3.py` |
| Cover templates (`coverTemplateId`) | **Done (v0.5.0, round 3)** — top-level `cover`: bold / split / minimal templates, verified by render in `tests/test_round3.py` |
| Remove flicker (`removeFlickersCnt`) | **Done (round 2)** — `effects.deflicker` (ffmpeg `deflicker`); per-frame luma std 31.02 → 5.04 |
| Object lock (`objectLocked`) | **Done (round 2)** — `locked` + `lock_to` timeline metadata; follows authored keyframes, NOT motion tracking |
| Mix mode (`mixMode`) | **Done** — blend-mode per clip (§1 blending modes) |
| Face effect (`faceEffectId`) | **Done (v0.11.0, round 9)** — person-gated skin smoothing (`musecut.beautify` + `ai.beautify`): YOLOv5n person boxes (conf ≥ 0.4, expanded 8%, feathered 5 px) gate a YCbCr skin mask + bilateral filter; non-skin pixels byte-identical, non-person skin-coloured areas untouched. Measured: faceless van clip 19/20 plates byte-identical (one 0.407-conf driver sliver, 92 px); real person frame keeps its inside-box skin reduction (6.3%, same as ungated) with every pixel outside the box byte-identical; synthetic patch −33% at strength 0.7. Honest limits: the gate is a bounding box, not a silhouette — skin-coloured background INSIDE the box still gets smoothed, and a missed detection means no smoothing that frame; other face effects (slim face, eye enlarging, etc.) still Planned |
| Caption animation (`captionAnimationId`) | **Done (round 2)** — native ASS animations: pop / slide / typewriter (`captions.animation`) |
| Text templates (`textTemplateId`) | **Done (v0.7.0, round 5)** — `texts[].template`: title-bold / lower-third / quote / punch / caption-clean (font/size/colour/position/animation/glow/bubble baked in, `size_frac` scales with frame height, explicit fields win) + project-level `text_templates` (add or override); verified by render + frame checks in `tests/test_round5.py` |
| Two-tier caption sizing | **Done (v0.7.0, round 5)** — `captions.active_size_ratio` (1.0..2.0): the highlighted word renders at font_size × ratio (ASS `\fs` override, reset after the word) while the rest of the line stays at font_size — the elite-reel small-setup + big-punch line in one caption event; verified by ASS output + render (1.5× line measures ≥1.25× taller) in `tests/test_round5.py` |
| Canvas backgrounds (`VECanvasData`: type/blur/image/color) | **Done (v2)** — `canvas: {type: color\|image\|blur}` per project; verified by render + pixel checks. **(v3)** per-clip `fit`: `cover` (scale + centre-crop, no pad bars) and `blur` (blurred-canvas fill behind the clip); verified by render + corner-luma checks in `tests/test_v3.py` |
| Karaoke colours (`ktvColor/Outline/Shadow`) | **Done (v2)** — caption ASS `ktv_color` / `ktv_outline_color` / `ktv_shadow_color`, verified in the ASS output |
| Text bloom / glow (`bloom*` fields in `VETextInfo`) | **Done (v2)** — glow via ASS `bloom_color`/`bloom_strength` + drawtext glow; verified by render + ASS output |
| Text bubbles (`bubblePath`) | **Done (v2)** — bubble background on text (drawtext `box`); verified by render |
| Underline + offset, boldWidth, italicDegree | **Done (v2)** — ASS Underline/Bold/Italic fields + drawtext underline; verified by render + ASS field positions |
| AI transitions (Clipflow-generated) | **Planned** — server-side in CapCut; local equivalent is Planned AI work |
| AI sound-effect generation | **Planned** — server-side in CapCut |
| Auto volume balancing ("Balances other clips' volume based on the current clip") | **Done (v2)** — per-track `loudnorm` balance + CLI `musecut balance` |
| Super resolution on export | **Done (v0.13.0, round 11)** — the genuine Real-ESRGAN x4plus ONNX (Qualcomm AI Hub public asset — float model + w8a8 fast variant, `musecut.superres` / `ai.super_resolution`): tiled 128x128 inference with feathered seams, 4x upscale of stills and clips, rendered through the real engine. Verified on a real 720p frame (gradient energy 5.751 vs GT 5.763, lanczos 4.127; PSNR 29.01 vs lanczos 30.70 — the GAN PSNR trade-off, judged by the frame check). Honest scope: ~4.3 s/tile float on this CPU — stills + short clips practical, full-length clips are hours; w8a8 is ~2.7x faster with a slight quality cost (over-sharpening, +38% gradient energy vs GT) |
| HDR export ("Converts your clip to an HDR video") | **Done (v0.5.0, round 3)** — `export: {"codec": "h265", "hdr": true}`: zscale PQ/BT.2020 + libx265 mastering-display / MaxCLL, 10-bit; ffprobe-verified (smpte2084 / bt2020 / yuv420p10le / Main 10). Honest scope: SDR timeline converted into an HDR10 container — HDR highlights are NOT invented |
| HEVC with alpha export (`HEVC_WITH_ALPHA`) | **BLOCKED (verified 2026-11-20)** — libx265 lists no alpha pixel formats (no yuva*); VP9 alpha writes the tag but no alpha plane round-trips. Refused honestly instead of silently dropping alpha; the real alpha path is ProRes 4444 (`export.alpha`, yuva444p12le, verified: alpha plane mean 128 for 0x80) |
| Draft JSON import | **Not needed** — owner decision 2026-11-20: MuseCut runs inside the agent; no human-style import/export |

## 8. What CapCut has that MuseCut deliberately does NOT copy

| Feature | Why not |
|---|---|
| Cloud storage / cloud sync | Local-first by design; add your own storage behind the JSON timeline |
| Social feed / templates marketplace | Out of scope; templates are local JSON files |
| Mobile app / GUI | API-first by design; a GUI can be built on the MCP server later |
| Stock media library | Bring your own media; no licensing strings attached |

---

## How parity is verified

- `tests/test_smoke.py` (5 tests) renders a real video exercising:
  3 clips, zoom keyframes, cinematic grade, vignette + grain, speed
  change, whoosh + crossfade + dip-to-black transitions, word-highlight
  captions, text overlay, ducked music, end card — then verifies it
  with ffprobe + frame signal stats.
- `tests/test_v2.py` (20 tests, 2026-11-20) renders and verifies each
  CapCut RE item in §7 marked Done: ProRes/AV1/GIF/WAV/MP3 export,
  bitrate-ladder command construction, all seven adjust sliders,
  canvas colour/image/blur, text glow/bubble/underline/bold,
  karaoke-colour ASS, motion blur, loudnorm balance, freeze frame,
  reverse, masks, chroma key, vidstab, speed curves, and the 15-tool
  MCP server.
- `tests/test_pro.py` (16 tests, 2026-11-20) renders and verifies the
  pro features in [PRO_PARITY.md](PRO_PARITY.md): full transform
  keyframes, LUTs, audio mixer (EQ / compressor / keyframed volume /
  pan), nested sequences, proxies, DNxHD, markers, adjustment
  layers, scopes data, 4K.
- Full suite: `python3 -m unittest discover -s tests -v` — **210
  tests, all passing** (v0.12.0, round 10, 2026-11-21; run in
  `~/workspace/.venv-video/bin/python` — 9 inpaint/beautify tests
  skip cleanly under the system python, which has no cv2).
- Anything marked **Planned** raises `NotImplementedError` with a
  reason instead of pretending to work. If a feature is listed here
  as Done but no test covers it, that is a bug — file it.
