# share-it

A tiny self-hosted file drop. Drop files in, get shareable links out. Files clean themselves up on a schedule.

One container, one port, no database, no accounts.

## Why

Moving files between your own machines, your phone, a teammate, or pasting them into an LLM chat is more friction than it should be. AirDrop is Apple-only. Drive logs you in and indexes everything. `scp` is fine until it isn't.

Run `share-it` on any box on your network — your dev machine, a NAS, a VPS, a Tailscale node — and you get a drop zone at `http://<host>:3050`. Drag a file in, copy the link, paste it wherever you need it. Especially handy when you're working with LLMs and constantly need to hand them a screenshot, a log, or a dataset.

## Features

- Drag-and-drop, multi-file uploads, per-file links + Copy-all
- **Paste-to-upload** — paste a screenshot (or any clipboard file) with Cmd/Ctrl+V
- **Share a text snippet** — paste text/markdown, get a link, no local file needed
- **QR code** for every link — point a phone camera at it to grab the file
- **Copy as Markdown** — `![](url)` for images, `[name](url)` otherwise
- **Download** button (`?dl=1`) alongside inline **Open**/preview
- Random tokenized URLs (`/f/<token>`) — not guessable, not enumerable
- Auto-expiry — files older than `max_age_days` get swept on a schedule
- Browser-side history with thumbnails, image/markdown preview, and a live
  server-storage footer (file count + bytes + next expiry)
- Size cap and blocked-extension list (executables/installers) via `config.yaml`
- `GET /healthz` for uptime checks (wired into the Docker `HEALTHCHECK`)
- Single FastAPI process, runs in Docker, ~200 lines of Python

## Requirements

Docker with the `compose` plugin.

## Run

```bash
git clone <repo-url> share-it
cd share-it
make up
```

Open http://localhost:3050

## Stop

```bash
make down
```

## Configure

Edit `config.yaml` for app behaviour (max age, max upload size, blocked extensions, cleanup interval), then `make restart`.

The host-port binding lives in a gitignored `.env`. Copy the template once:

```bash
cp .env.example .env
```

Then edit `.env` to change `BIND_ADDR` (default `127.0.0.1`) or `HOST_PORT` (default `3050`) — e.g. set `BIND_ADDR=0.0.0.0` to expose on all interfaces, or to a specific Tailscale/LAN IP. Run `make restart` after editing.

## Command line

`POST /upload` takes a multipart `file` field and returns JSON
`{"path": "/f/<token>", "filename": ..., "size": ...}`. Send `Accept: text/plain`
to get back just the full URL instead — handy for shell scripts:

```bash
curl -sf -H "Accept: text/plain" -F "file=@report.pdf" http://localhost:3050/upload
```

A ready-made helper lives in `scripts/share.sh`:

```bash
source scripts/share.sh          # add to ~/.zshrc to keep it
export SHARE_IT_HOST=http://localhost:3050   # or your Tailscale URL

share screenshot.png             # prints + copies the link
some_command | share -           # upload piped output as stdout.txt
```

Other endpoints: `GET /healthz` (liveness), `GET /stats` (file count + bytes),
`GET /qr?data=<url>` (SVG QR code), `GET /f/<token>?dl=1` (force download).

## Versioning

The current version lives in a single `VERSION` file (SemVer). It's shown in the
page footer and returned by `GET /version` and `GET /healthz`.

Bump it with one command, then redeploy:

```bash
make bump-patch   # 0.2.0 -> 0.2.1  (bug fixes)
make bump-minor   # 0.2.0 -> 0.3.0  (new features, backwards compatible)
make bump-major   # 0.2.0 -> 1.0.0  (breaking changes)
make up           # rebuild + restart so the new version ships
make version      # print the current version
```

## Security

There is no authentication. Anyone with a link can download the file until it is swept. Run it on a network you trust (LAN, Tailscale, Wireguard) or put it behind your own auth proxy.
