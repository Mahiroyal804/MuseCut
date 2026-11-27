## What changed

<!-- One-line summary per change. -->

## Why

<!-- The workflow or bug this fixes. -->

## Verification

<!-- Exact command + result. New features must render real media
and verify the result (ffprobe, frame pixels, or decoded audio). -->

```bash
python3 -m unittest discover -s tests
# result:
```

## Docs updated

- [ ] `README.md` (if behaviour changed)
- [ ] `COMPARISON.md` (if the score changed)

## Checklist

- [ ] Clean-room only — no CapCut code or assets.
- [ ] No media, secrets, or credentials committed.
- [ ] Suite is green (298 tests, skips are explicit, never failures).
