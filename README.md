# GEO Python Camoufox Sidecar

This submodule implements GEO RPA protocol v4 with FastAPI/Uvicorn on
`camofox-browser:9377`. It uses `camoufox==0.5.5` and `playwright==1.59.0` to
control the fixed Camoufox 152.0.4-beta.29 release.

Only the controlled HTTP routes used by `backend/app/rpa/camofox_browser.py`
are available. The service does not expose arbitrary JavaScript execution,
cookie export, arbitrary request headers, or a general browser debugging API.

## Protocol and isolation

`/health` always declares `geoRpaProtocolVersion=4`. Per-account login snapshot
is owned by GEO `RpaProviderAccount.storageState` and hydrated over `/rpa/accounts/session`. The snapshot contains cookies and local storage only. Complete Firefox profiles and Chromium/Sandbox cookies
are never imported or exported.

Closing a session first removes it from the public session map and installs one
awaitable closing barrier for that user. New sessions wait for the barrier. A
Context close timeout restarts the controlled browser and removes all stale
Context indexes, preventing late pages from becoming ghost tabs or reusing a stale Context after restart.

GEO hydrates the account session from MySQL, then checkpoints Cookie + LocalStorage
through `POST /rpa/accounts/session/checkpoint` before closing a manual window.
A failed checkpoint must not be reported as a successful login.

Default Compose starts headed Firefox on a private Xvfb display and publishes
only `127.0.0.1:9377`. Window-level VNC is deprecated: do not map RFB/noVNC
ports, and do not install `x11vnc` or noVNC into the image. GEO manual
authentication and task observation use tab screenshots over the authenticated
application WebSocket. `ENABLE_WINDOW_PUBLISHER` remains off unless a leftover
window route is explicitly being tested.

## Authorized interaction pacing

Provider automation owns the reviewed DOM interaction rules for all six
platforms. `InteractionPacingPolicy` uses fixed hover/focus settling, bounded
keyboard chunks, and a pre-submit wait so rich-text editors can process each
input phase reliably. For authorized automated tasks, the native browser mouse
moves in a fixed number of steps only to the center of an already-located,
visible input or submit control; the backend cannot supply coordinates.

The sidecar does not change browser fingerprints, network location, account
identity, or CAPTCHA behavior. Login, verification, restrictions, and blocking
dialogs return stable outcomes for human handling rather than being retried or
bypassed.

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

The Docker build context is the submodule root. `Dockerfile.base` builds a
versioned local base image containing system libraries, fixed Python
dependencies, and the verified archive. It uses Tsinghua Debian and PyPI
mirrors and never pulls `uv` from GHCR. `Dockerfile` derives from that base
and copies only `app/` and the entrypoint, so routine rule changes do not
repeat dependency installation or browser extraction. The base image uses the
fixed `python:3.12-slim-bookworm` image and fails when the archive is absent,
has an unexpected hash, or lacks `camoufox-bin`. Neither image contains Node
runtime or downloads a browser at build or runtime.

```bash
docker build -f Dockerfile.base -t geo-camofox-browser-base:py312-camoufox152.0.4b29 .
docker build --build-arg CAMOFOX_BROWSER_BASE_IMAGE=geo-camofox-browser-base:py312-camoufox152.0.4b29 -t geo-camofox-browser:local .
```

Independent Compose deploy from this directory. If the image already exists:

```bash
docker compose up -d
```

That binds `127.0.0.1:9377` by default. Do not pass the GEO repo `.env.deploy` as
this directory's compose env-file. Same-host GEO app must use
`CAMOFOX_BROWSER_URL=http://host.docker.internal:9377`, not `127.0.0.1` or the
Docker DNS name `camofox-browser`. Copy `.env.example` to `.env` only when the
bind address or host ports need to change. First-time or base-image changes:

```bash
docker compose --profile build build camofox-browser-base
docker compose build camofox-browser
docker compose up -d
```

## Validation

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

Do not switch the production Compose service until protocol, login-snapshot,
window-isolation, timeout-recovery, provider, and resource acceptance gates are
complete with authorized test accounts.
