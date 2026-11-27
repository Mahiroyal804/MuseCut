# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.21.x   | ✅ |
| < 0.21  | ❌ |

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Report it
privately through GitHub's
[private vulnerability reporting](https://github.com/Mahiroyal804/MuseCut/security/advisories/new)
on this repository.

Include:

- what the vulnerability is and where it lives (file + line if known),
- steps to reproduce it,
- what an attacker could do with it.

We will confirm receipt, assess the report, and fix verified issues
in a patch release. Please allow reasonable time for a fix before
any public disclosure.

## Scope notes

MuseCut renders local media with ffmpeg and runs local ML models.
It never uploads media, credentials, or timelines anywhere, and it
has no network calls of its own — the only network use is the
optional Muse TTS backend, which the user configures explicitly.
