# GEO Python Camoufox Sidecar

This submodule implements GEO RPA protocol v3 with FastAPI/Uvicorn on
`camofox-browser:9377`. It uses `camoufox==0.5.5` and `playwright==1.59.0` to
control the fixed Camoufox 152.0.4-beta.29 release.

Only the controlled HTTP routes used by `backend/app/rpa/camofox_browser.py`
are available. The service does not expose arbitrary JavaScript execution,
cookie export, arbitrary request headers, or a general browser debugging API.

## Protocol and isolation

`/health` always declares `geoRpaProtocolVersion=3`. Per-account state is
stored only as `sha256(userId)[:32]/storage-state.json`; complete Firefox
profiles, Chromium/Sandbox cookies, and IndexedDB are never imported or
exported.

Closing a session first removes it from the public session map and installs one
awaitable closing barrier for that user. New sessions wait for the barrier. A
Context close timeout restarts the controlled browser and removes all stale
Context indexes, preventing late pages from becoming ghost tabs or writing to
the previous profile.

Set `ENABLE_WINDOW_PUBLISHER=true` to enable account-scoped X11 window
publication. Without it, `/health` remains `browserReady=false`, so the
backend cannot accept an incomplete v3 deployment. A shared desktop is never
presented as an account-scoped observer.

## Local development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
```

## Browser archive and image

Place the reviewed Linux x86_64 archive at the ignored path below. Do not
rename or unpack it.

```text
bin/camoufox-152.0.4-beta.29-lin.x86_64.zip
```

The Dockerfile verifies this SHA-256 before extracting it:

```text
1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c
```

The Docker build context is the submodule root. It uses the fixed
`python:3.12-slim-bookworm` base image and fails when the archive is absent,
has an unexpected hash, or lacks `camoufox-bin`. The image contains no Node
runtime and does not download a browser at build or runtime.

```bash
docker build -t geo-camofox-browser:python-v3 .
```

## Validation

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

Do not switch the production Compose service until protocol, profile,
window-isolation, timeout-recovery, provider, and resource acceptance gates are
complete with authorized test accounts.
