# share-it

Your own WeTransfer — self-hosted on your private network, no accounts, no cloud.

Drag a file in. Copy the link. Paste it anywhere. Files expire automatically.

---

## What it is

share-it is a personal file-drop server. Run it on a box inside your Tailscale network and you get a browser drop zone at `https://<your-node>:3050`. Anyone on your tailnet can reach it. No one outside can.

One container. One port. No database. No accounts.

## How it works

Upload a file, get a link — in one drag:

- Drop a file (or paste a screenshot with Cmd/Ctrl+V).
- The link copies to your clipboard the moment the upload finishes.
- A QR code appears alongside it so you can beam the file to a phone in one tap.
- Files delete themselves after 2 days (configurable).

Not just files — there's a **Text tab** to paste a code snippet or log blob and share that as a link too.

## Is it for you?

- You move files between your own machines, a teammate, or an LLM chat daily.
- You're already on Tailscale (or another private network).
- You want a link on your clipboard in under 3 seconds, not a cloud upload wizard.
- You paste screenshots into LLM chats and hate the friction.

If you need public sharing, fine-grained permissions, or long-term storage, use Cloudflare R2 + a presigned URL. share-it is for your private network.

## What you get

**Auto-copy on upload** — the link lands in your clipboard before you look up. No extra click.

**Text snippets** — paste any text, markdown, or log and get a shareable link. No local file needed.

**Paste-to-upload** — Cmd/Ctrl+V uploads whatever is on your clipboard. Screenshot, file, done.

**QR code on every link** — point your phone camera at it. Useful when you're on a laptop and need a file on a device that has no shared clipboard.

**Shell integration** — upload from the terminal, or pipe command output straight to a link:

```bash
# Upload a file
share report.pdf

# Upload piped output (e.g. a log, a JSON dump)
kubectl logs my-pod | share -
```

**Copy as Markdown** — one click to get `![](url)` for images or `[name](url)` otherwise. Paste into docs, issues, LLM context windows.

**Browser history** — your recent uploads stay in the sidebar with thumbnails, so you can re-copy a link from earlier without uploading again.

**Auto-expiry** — files clean themselves up on a schedule. No manual housekeeping.

## Quick start

Requirements: Docker with the Compose plugin.

```bash
git clone <repo-url> share-it
cd share-it
make up
```

Open http://localhost:3050

That's it. By default the server binds to `127.0.0.1:3050` (loopback only). To make it reachable from other machines, choose one approach:

**Tailscale** (recommended — HTTPS for free, no firewall rules):

```bash
cp .env.example .env     # leave BIND_ADDR=127.0.0.1
tailscale serve https:443 / http://127.0.0.1:3050
```

Your drop zone is then at `https://<your-node>.<tailnet>.ts.net`.

**LAN / trusted network** — set `BIND_ADDR` to a specific LAN IP (e.g. `192.168.1.10`) or `0.0.0.0` in `.env`, then `make restart`. Only do this on a network you control.

## Stop / restart

```bash
make down        # stop and remove the container
make restart     # restart after a config change
```

## Configure

Edit `config.yaml` for app behaviour, then `make restart`:

| Key | Default | What it does |
|-----|---------|--------------|
| `max_age_days` | `2` | Files older than this are swept on the next cleanup run |
| `max_upload_mb` | `1024` | Size cap for binary files |
| `max_upload_mb_text` | `2` | Smaller cap for `.txt` / `.md` uploads |
| `cleanup_interval_sec` | `3600` | How often the sweeper runs |
| `blocked_extensions` | (long list) | Extensions rejected at upload (executables, installers) |

The host-port binding lives in a gitignored `.env`. Copy the template to change `BIND_ADDR` or `HOST_PORT`:

```bash
cp .env.example .env
# edit BIND_ADDR and HOST_PORT, then:
make restart
```

## Shell helper

A ready-made `share` function lives in `scripts/share.sh`. Source it from your shell rc to make `share` available everywhere:

```bash
echo 'source /path/to/share-it/scripts/share.sh' >> ~/.zshrc
export SHARE_IT_HOST=https://your-node.ts.net   # or http://localhost:3050
```

Usage:

```bash
share screenshot.png          # prints + copies the link
some_command | share -        # upload piped output as stdout.txt
```

The script auto-detects `pbcopy` (macOS), `wl-copy` (Wayland), or `xclip` (X11) and puts the link in your clipboard.

## Raw API

`POST /upload` accepts a multipart `file` field. Send `Accept: text/plain` to get just the URL back — no JSON parsing needed in shell scripts:

```bash
curl -sf -H "Accept: text/plain" -F "file=@report.pdf" http://localhost:3050/upload
```

Other endpoints:

| Endpoint | What it does |
|----------|--------------|
| `GET /f/<token>` | Serve the file inline |
| `GET /f/<token>?dl=1` | Force a save dialog |
| `GET /healthz` | Liveness check (also returns version) |
| `GET /stats` | File count, total bytes, next expiry |
| `GET /qr?data=<url>` | SVG QR code for any URL |
| `GET /version` | Current version string |

## Security

There is no authentication. Anyone who can reach the port can download files by link. Run it on a network you trust (Tailscale, WireGuard, a LAN) or put it behind your own auth proxy. Do not bind to `0.0.0.0` on a host with a public IP.

## Versioning

The current version is in `VERSION` (SemVer). It appears in the page footer and in `/healthz`.

```bash
make bump-patch   # 0.2.3 -> 0.2.4  (bug fixes)
make bump-minor   # 0.2.3 -> 0.3.0  (new features)
make bump-major   # 0.2.3 -> 1.0.0  (breaking changes)
make up           # rebuild + redeploy
make version      # print current version
```
