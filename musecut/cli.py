"""MuseCut CLI.

Usage::

    musecut init my-video --width 1280 --height 720   # new timeline JSON
    musecut render project.json -o out.mp4             # render it
    musecut render project.json -o out.mov --codec prores422hq
    musecut render project.json -o out.gif             # codec from extension
    musecut render project.json -o out.wav             # audio-only export
    musecut render project.json -o out.mp4 --bitrate-ladder
    musecut render project.json -o out.mp4 --workdir /tmp/mc
    musecut captions voice.mp3 --model base -o words.json
    musecut voice list                             # TTS providers + voices
    musecut voice synth "Hello world" -o vo.wav    # Muse MAI_03 voiceover
    musecut voice synth "Hello world" -o vo.mp3 --voice avocado_v2:MAI_03 --speed 90
    musecut balance voice.mp3 -o voice-balanced.wav   # loudnorm to -16 LUFS
    musecut proxy make project.json -o proxy.json     # low-res proxies
    musecut proxy relink proxy.json -o project.json   # back to originals
    musecut scopes video.mp4 --time 1.0               # colour-scope data
    musecut validate project.json
    musecut info project.json                          # summary + capabilities
    musecut template list                              # bundled templates
    musecut template render reel --var clip1=a.mp4 -o out.mp4
    musecut beats music.mp3                            # beat markers
    musecut mcp                                        # start the MCP server
    musecut version

The CLI is a thin wrapper over the library: agents should prefer the
MCP server (``musecut mcp``) or the JSON timeline format directly.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__, ai as ai_mod
from . import audio as audio_mod
from . import captions as cap_mod
from . import proxy as proxy_mod
from . import scopes as scopes_mod
from . import tts as tts_mod
from .render import (DEFAULT_LADDER, EXPORT_CODECS, RenderError, Renderer,
                     probe_media)
from .timeline import (TimelineError, load_project, new_project,
                       save_project, timeline_duration)


def _fail(msg: str, code: int = 1) -> "SystemExit":
    print(f"musecut: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def cmd_init(args: argparse.Namespace) -> int:
    proj = new_project(args.name, args.width, args.height, args.fps)
    out = args.output or f"{args.name}.json"
    save_project(proj, out)
    print(f"created {out} ({args.width}x{args.height}@{args.fps})")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    try:
        proj = load_project(args.project)
    except (TimelineError, OSError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    if args.bitrate_ladder:
        proj.setdefault("export", {})["bitrate_ladder"] = dict(DEFAULT_LADDER)
    try:
        renderer = Renderer(proj, workdir=args.workdir,
                            preset=args.preset, crf=args.crf,
                            codec=args.codec,
                            filter_threads=args.filter_threads)
        result = renderer.render(args.output, quiet=args.quiet)
    except RenderError as exc:
        _fail(str(exc))
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    try:
        out = audio_mod.balance_loudness(
            args.input, args.output, target_i=args.target_i,
            target_tp=args.target_tp, target_lra=args.target_lra)
    except (subprocess.CalledProcessError, ValueError) as exc:
        _fail(str(exc))
    mean, maxv = audio_mod.mix_levels(out)
    print(json.dumps({"output": out, "mean_db": round(mean, 1),
                      "max_db": round(maxv, 1)}, indent=2))
    return 0


def cmd_captions(args: argparse.Namespace) -> int:
    try:
        words = cap_mod.transcribe(args.input, model=args.model,
                                   language=args.language)
    except RuntimeError as exc:
        _fail(str(exc))
    if args.format == "ass":
        path = args.output or "captions.ass"
        cap_mod.write_ass(words, path, width=args.width, height=args.height)
    elif args.format == "srt":
        path = args.output or "captions.srt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(cap_mod.words_to_srt(words))
    else:
        path = args.output or "words.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(words, f, indent=2, ensure_ascii=False)
    print(f"transcribed {len(words)} words -> {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        load_project(args.project)
    except (TimelineError, OSError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(f"{args.project}: valid")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    try:
        proj = load_project(args.project)
    except (TimelineError, OSError, json.JSONDecodeError) as exc:
        _fail(str(exc))
    print(json.dumps({
        "name": proj.get("name"),
        "size": f"{proj['width']}x{proj['height']}@{proj['fps']}",
        "clips": len(proj.get("clips") or []),
        "overlays": len(proj.get("overlays") or []),
        "texts": len(proj.get("texts") or []),
        "audio_tracks": len(proj.get("audio") or []),
        "has_captions": bool(proj.get("captions")),
        "has_endcard": bool(proj.get("endcard")),
        "estimated_duration": round(timeline_duration(proj), 2),
        "capabilities": ai_mod.capabilities(),
    }, indent=2))
    return 0


def _templates_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "examples")


def cmd_template(args: argparse.Namespace) -> int:
    if args.action == "list":
        names = sorted(f for f in os.listdir(_templates_dir())
                       if f.endswith(".json"))
        for n in names:
            print(n)
        return 0
    if args.action == "render":
        path = os.path.join(_templates_dir(), args.name)
        if not os.path.isfile(path):
            path = args.name
        try:
            proj = load_project(path)
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            _fail(str(exc))
        # --var key=value substitutions inside string fields
        subs = {}
        for item in args.var or []:
            if "=" in item:
                k, v = item.split("=", 1)
                subs["{{" + k + "}}"] = v
        if subs:
            blob = json.dumps(proj)
            for k, v in subs.items():
                blob = blob.replace(k, v)
            proj = json.loads(blob)
            from .timeline import validate_timeline
            validate_timeline(proj)
        try:
            renderer = Renderer(proj, workdir=args.workdir,
                                codec=args.codec)
            result = renderer.render(args.output, quiet=args.quiet)
        except RenderError as exc:
            _fail(str(exc))
        print(json.dumps(result.as_dict(), indent=2))
        return 0
    _fail(f"unknown template action: {args.action!r}")


def cmd_beats(args: argparse.Namespace) -> int:
    beats = audio_mod.detect_beats(args.input,
                                   sensitivity=args.sensitivity)
    print(json.dumps({"beats": beats, "count": len(beats)}, indent=2))
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    if args.action == "make":
        try:
            proj = load_project(args.project)
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            _fail(str(exc))
        proxy_dir = args.proxy_dir or os.path.join(
            os.path.dirname(os.path.abspath(args.output or args.project)),
            "proxies")
        try:
            proxied, mapping = proxy_mod.make_proxies_for_project(
                proj, proxy_dir, width=args.width, height=args.height,
                crf=args.crf)
        except RenderError as exc:
            _fail(str(exc))
        out = args.output or args.project.replace(".json", "-proxy.json")
        save_project(proxied, out)
        print(json.dumps({"proxy_project": out, "proxy_dir": proxy_dir,
                          "proxies": len(mapping),
                          "map": mapping}, indent=2))
        return 0
    if args.action == "relink":
        try:
            proj = load_project(args.project)
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            _fail(str(exc))
        try:
            orig = proxy_mod.relink_project(proj)
        except RenderError as exc:
            _fail(str(exc))
        out = args.output or args.project.replace(".json", "-orig.json")
        save_project(orig, out)
        print(json.dumps({"project": out, "relinked": True}, indent=2))
        return 0
    _fail(f"unknown proxy action: {args.action!r}")


def cmd_scopes(args: argparse.Namespace) -> int:
    try:
        if args.series:
            from .render import probe_media
            dur = probe_media(args.input)["duration"]
            data = scopes_mod.scopes_series(args.input, dur,
                                            samples=args.samples)
        else:
            data = scopes_mod.scopes_data(args.input, t=args.time)
    except RenderError as exc:
        _fail(str(exc))
    print(json.dumps(data, indent=2))
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    if args.action == "list":
        providers = {}
        for name in tts_mod.list_providers():
            if name == "muse":
                providers[name] = {
                    "voices": tts_mod.list_muse_voices(),
                    "default_voice": tts_mod.DEFAULT_MUSE_VOICE,
                    "default_speed": tts_mod.DEFAULT_MUSE_SPEED,
                }
            else:
                providers[name] = {}
        print(json.dumps({"providers": providers}, indent=2))
        return 0
    if args.action == "synth":
        if not args.text:
            _fail("voice synth needs text")
        if not args.output:
            _fail("voice synth needs -o/--output (.wav or .mp3)")
        try:
            provider = tts_mod.get_provider(args.provider)
            out = provider.synthesize(args.text, args.output,
                                      voice=args.voice, speed=args.speed)
            dur = probe_media(out)["duration"]
            mean, maxv = audio_mod.mix_levels(out)
        except (RuntimeError, ValueError) as exc:
            _fail(str(exc))
        print(json.dumps({
            "output": out, "provider": args.provider,
            "voice": args.voice or tts_mod.DEFAULT_MUSE_VOICE,
            "duration_s": round(dur, 2),
            "mean_db": round(mean, 1), "max_db": round(maxv, 1),
        }, indent=2))
        return 0
    _fail(f"unknown voice action: {args.action!r}")


def cmd_mcp(_args: argparse.Namespace) -> int:
    from .mcp_server import main
    main()
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"musecut {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="musecut",
        description="MuseCut — agent-native open-source video editor "
                    "(ffmpeg render engine + MCP server).")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="create a new timeline JSON")
    pi.add_argument("name")
    pi.add_argument("--width", type=int, default=1280)
    pi.add_argument("--height", type=int, default=720)
    pi.add_argument("--fps", type=int, default=30)
    pi.add_argument("-o", "--output")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser("render", help="render a timeline JSON to video")
    pr.add_argument("project")
    pr.add_argument("-o", "--output", required=True)
    pr.add_argument("--workdir")
    pr.add_argument("--preset", default="veryfast")
    pr.add_argument("--crf", type=int, default=20)
    pr.add_argument("--filter-threads", type=int, default=None,
                    help="filter-graph threads (default: auto, CPU "
                         "count capped at 8; 0 = single-threaded)")
    pr.add_argument("--codec", choices=sorted(EXPORT_CODECS),
                    help="export codec (default: project export.codec, "
                         "else inferred from the output extension, else h264)")
    pr.add_argument("--bitrate-ladder", action="store_true",
                    help="use the default per-resolution bitrate ladder")
    pr.add_argument("--quiet", action="store_true")
    pr.set_defaults(func=cmd_render)

    pc = sub.add_parser("captions", help="auto-transcribe to word timings")
    pc.add_argument("input")
    pc.add_argument("--model", default="base")
    pc.add_argument("--language")
    pc.add_argument("--format", choices=["json", "ass", "srt"], default="json")
    pc.add_argument("--width", type=int, default=1280)
    pc.add_argument("--height", type=int, default=720)
    pc.add_argument("-o", "--output")
    pc.set_defaults(func=cmd_captions)

    pv = sub.add_parser("validate", help="validate a timeline JSON")
    pv.add_argument("project")
    pv.set_defaults(func=cmd_validate)

    pin = sub.add_parser("info", help="summarise a timeline + capabilities")
    pin.add_argument("project")
    pin.set_defaults(func=cmd_info)

    pt = sub.add_parser("template", help="bundled timeline templates")
    pt.add_argument("action", choices=["list", "render"])
    pt.add_argument("name", nargs="?")
    pt.add_argument("-o", "--output")
    pt.add_argument("--workdir")
    pt.add_argument("--codec", choices=sorted(EXPORT_CODECS))
    pt.add_argument("--var", action="append",
                    help="template variable, e.g. --var clip1=video.mp4")
    pt.add_argument("--quiet", action="store_true")
    pt.set_defaults(func=cmd_template)

    pbal = sub.add_parser("balance", help="normalize an audio file's "
                          "loudness (EBU R128 loudnorm)")
    pbal.add_argument("input")
    pbal.add_argument("-o", "--output", required=True)
    pbal.add_argument("--target-i", type=float, default=-16.0)
    pbal.add_argument("--target-tp", type=float, default=-1.5)
    pbal.add_argument("--target-lra", type=float, default=11.0)
    pbal.set_defaults(func=cmd_balance)

    pb = sub.add_parser("beats", help="detect beat markers in audio")
    pb.add_argument("input")
    pb.add_argument("--sensitivity", type=float, default=1.5)
    pb.set_defaults(func=cmd_beats)

    pp = sub.add_parser("proxy", help="proxy workflow: make proxies "
                        "for a project, or relink back to originals")
    pp.add_argument("action", choices=["make", "relink"])
    pp.add_argument("project")
    pp.add_argument("-o", "--output")
    pp.add_argument("--proxy-dir")
    pp.add_argument("--width", type=int, default=640)
    pp.add_argument("--height", type=int, default=360)
    pp.add_argument("--crf", type=int, default=28)
    pp.set_defaults(func=cmd_proxy)

    ps = sub.add_parser("scopes", help="colour-scope data for a video "
                        "(luma/chroma stats as JSON)")
    ps.add_argument("input")
    ps.add_argument("--time", type=float, default=0.0)
    ps.add_argument("--series", action="store_true",
                    help="sample across the whole clip instead of one frame")
    ps.add_argument("--samples", type=int, default=5)
    ps.set_defaults(func=cmd_scopes)

    pvoice = sub.add_parser("voice", help="AI voices: list TTS providers "
                            "and voices, or synthesize a voiceover segment")
    pvoice.add_argument("action", choices=["list", "synth"])
    pvoice.add_argument("text", nargs="?",
                        help="text to synthesize (synth action)")
    pvoice.add_argument("-o", "--output",
                        help="output audio file (.wav or .mp3)")
    pvoice.add_argument("--provider", default="muse",
                        help="TTS provider (default: muse)")
    pvoice.add_argument("--voice",
                        help="voice id, e.g. avocado_v2:MAI_03 "
                             "(default: provider default)")
    pvoice.add_argument("--speed", type=float, default=None,
                        help="speed: multiplier (0.9) or Muse CLI scale "
                             "(90); default 90, never above 100")
    pvoice.set_defaults(func=cmd_voice)

    pm = sub.add_parser("mcp", help="start the MCP server (stdio)")
    pm.set_defaults(func=cmd_mcp)

    pver = sub.add_parser("version", help="print the version")
    pver.set_defaults(func=cmd_version)
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
