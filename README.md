# share-it

A tiny self-hosted file drop. Drop files in, get shareable links out. Files clean themselves up on a schedule.

One container, one port, no database, no accounts.

## Why

Moving files between your own machines, your phone, a teammate, or pasting them into an LLM chat is more friction than it should be. AirDrop is Apple-only. Drive logs you in and indexes everything. `scp` is fine until it isn't.

Run `share-it` on any box on your network — your dev machine, a NAS, a VPS, a Tailscale node — and you get a drop zone at `http://<host>:3050`. Drag a file in, copy the link, paste it wherever you need it. Especially handy when you're working with LLMs and constantly need to hand them a screenshot, a log, or a dataset.

## Features

- Drag-and-drop, multi-file uploads, per-file links + Copy-all
- Random tokenized URLs (`/f/<token>`) — not guessable, not enumerable
- Auto-expiry — files older than `max_age_days` get swept on a schedule
- Size cap and allowed-extension whitelist via `config.yaml`
- Single FastAPI process, runs in Docker, ~150 lines of Python

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

Edit `config.yaml` (max age, max upload size, allowed extensions, cleanup interval), then `make restart`. Change the host port via the `ports:` line in `docker-compose.yml`.

## Security

There is no authentication. Anyone with a link can download the file until it is swept. Run it on a network you trust (LAN, Tailscale, Wireguard) or put it behind your own auth proxy.
