# Third-Party Licenses

Media Buddy links against the following third-party Python and JavaScript
packages. Each retains its own license; nothing here overrides those terms.

## Python (backend)

| Package | License | Use |
|---|---|---|
| FastAPI | MIT | HTTP framework |
| Uvicorn | BSD-3-Clause | ASGI server |
| SQLAlchemy | MIT | ORM |
| Pydantic | MIT | Data validation |
| Requests | Apache-2.0 | HTTP client |
| PyYAML | MIT | YAML parsing |
| jsonschema | MIT | Schema validation |
| Pillow | MIT-CMU (HPND) | Image processing |
| python-dotenv | BSD-3-Clause | .env file loading |
| python-multipart | Apache-2.0 | multipart parser |
| httpx | BSD-3-Clause | Async HTTP client (provider calls) |
| keyring | MIT | OS keychain access (Settings → API Keys) |
| PyJWT | MIT | JWT helpers (kept for compatibility) |
| opencc-python-reimplemented | Apache-2.0 | Simplified/Traditional Chinese conversion |
| edge-tts | LGPL-3.0 | Microsoft Edge TTS (no key, free).<br>**LGPL note**: dynamically linked, not modified — compliant with LGPL §6. |

## JavaScript (frontend / Electron)

| Package | License | Use |
|---|---|---|
| React, React-DOM | MIT | UI framework |
| react-router-dom | MIT | Client routing |
| zustand | MIT | State management |
| axios | MIT | HTTP client |
| Electron | MIT | Desktop shell |
| Vite | MIT | Bundler |
| TypeScript | Apache-2.0 | Compiler |
| electron-builder | MIT | Distribution packaging |

## Excluded — never linked into Media Buddy

These were considered or removed for license reasons:

- **OpenMontage** (AGPL-3.0) — strong copyleft; we re-implemented any
  needed functionality from public API documentation in our own MIT-
  compatible code (`src/backend/lib/stock_sources/`, `video_compose.py`,
  etc.). No OpenMontage code is shipped.
- **piper-tts** (GPL-3.0-or-later) — strong copyleft; would force the
  whole product to GPL. Removed in Phase 2.11g; Edge TTS covers the
  same offline-friendly + free + CJK requirements.

## Compliance audit (2026-09-22)

Run `pip show <package> | grep License` to verify any specific package.
LGPL terms for `edge-tts` are satisfied because:
1. We do not modify the upstream package source.
2. We dynamically link via `import edge_tts` (not statically embedded).
3. Users may replace `edge-tts` with a fork by reinstalling without
   needing to rebuild Media Buddy.
