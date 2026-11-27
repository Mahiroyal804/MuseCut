# Contributing to MuseCut

MuseCut is an agent-native video editor: a JSON timeline + CLI + MCP
server on top of ffmpeg. Contributions are welcome — code, tests,
docs, and honest bug reports.

## Quick setup

```bash
git clone https://github.com/Mahiroyal804/MuseCut.git
cd MuseCut
pip install -e ".[dev]"
ffmpeg -version   # needs ffmpeg >= 6 with libass
```

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

298 tests, all green. Tests that need ML models or the Muse TTS
backend skip cleanly when those are absent — a skip is never a
failure. New features must come with a test that renders real media
and verifies the result (ffprobe, frame pixels, or decoded audio),
not just a unit mock.

## Rules

- **Clean-room only.** No CapCut code, assets, or binaries. Ever.
- **Honest scores.** If a feature is partial, say so in the code,
  the README, and `COMPARISON.md`. Never claim a capability the
  suite does not verify.
- **No media in the repo.** Renders, audio, and test videos are
  never committed — `.gitignore` blocks them. Tests generate their
  own media with ffmpeg lavfi sources.
- **No secrets.** No API keys, tokens, or credentials in code,
  tests, or docs.

## Pull requests

1. Fork, branch, commit with a clear message (`round N: what changed`).
2. Run the full suite before opening the PR.
3. Update `README.md` and `COMPARISON.md` when behaviour or the
   score changes.

## Licence

MIT — see [LICENSE](LICENSE).
