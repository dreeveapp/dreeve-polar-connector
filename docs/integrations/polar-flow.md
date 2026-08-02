# Polar Flow

Dreeve imports anything dropped into its `watch/` folder. Getting your Polar training sessions *into*
that folder is what the **Polar connector** does: it periodically lists new exercises, downloads the
original `.fit` files and drops them in. No manual exports.

```
Polar Flow → connector → watch/ → Dreeve imports it
```

The connector is a **separate container** from its own repository,
[dreeveapp/dreeve-polar-connector](https://github.com/dreeveapp/dreeve-polar-connector). It is not
part of Dreeve itself, it only writes files into the folder you
already mount.

> [!IMPORTANT]
> **Important** Polar's API only hands out exercises uploaded in the **last 30 days**, and only those
> uploaded *after* you connect. Older history cannot be fetched at all. Read
> [Only the last 30 days](/integrations/polar-flow.md#only-the-last-30-days)
> before you rely on it.

## Setup

### 1. Register a Polar client

Create a client at [admin.polaraccesslink.com](https://admin.polaraccesslink.com). You get a client id
and a client secret, and you register one **redirect URL** — where Polar sends your browser back after
you approve access. Use the address this connector will be reachable at, plus `/callback`:

```
http://<this-host>:8080/callback
```

If Polar later refuses that URL, see
[When Polar refuses the redirect](/integrations/polar-flow.md#when-polar-refuses-the-redirect).

### 2. Add the container

Add this alongside the `app` and `daemon` services in your
[docker-compose.yml](/getting-started/installation.md#docker-composeyml):

```yml
  # Pulls training sessions out of Polar Flow into the watch folder.
  polar-connector:
    image: ghcr.io/dreeveapp/dreeve-polar-connector:latest
    container_name: dreeve-polar-connector
    restart: unless-stopped
    volumes:
      # The same ./watch folder the app and daemon mount.
      - ./watch:/watch
      - ./polar/state:/state
      - ./polar/tokens:/tokens
    ports:
      # The local listener: /authorize, /status and the healthcheck. LAN only — never forward this.
      - '8080:8080'
      # Uncomment only with WEBHOOK_ADDR=0.0.0.0:8081 set. This is the one port that may face the
      # internet; it serves /webhook and nothing else.
      # - '8081:8081'
    env_file: ./.env
```

### 3. Configure it

Add to your `.env`:

```bash
POLAR_CLIENT_ID=your-client-id
POLAR_CLIENT_SECRET=your-client-secret
# Where this connector is reachable in a browser. Used to build the redirect URI,
# which must match what you registered for the client.
PUBLIC_URL=http://<this-host>:8080
```

### 4. Authorize it

Start the container, then open `http://<this-host>:8080/authorize` in a browser and approve access:

```bash
> docker compose up -d polar-connector
```

Until you do, the container reports **unhealthy** on purpose — an unauthorized connector is otherwise
silent, and a silent connector looks exactly like a working one with nothing to do. `GET /status` tells
you which URL to open.

The token Polar issues does not expire. Keep the `./polar/tokens` volume and you never authorize again.

Running headless, with `HTTP_ADDR=off` and no page to open? Use this instead, which prints the URL and
reads the code back from your terminal:

```bash
> docker compose run --rm polar-connector login
```

### 5. Start it

```bash
> docker compose up -d
> docker compose logs -f polar-connector
```

A cycle runs immediately, then one every 15 minutes. Files land in the watch folder as
`<exerciseId>.fit`, and the daemon container will import them, exactly as if you had
dropped them there yourself.

## Only the last 30 days

The AccessLink API returns exercises uploaded to Flow within the last 30 days, and only ones uploaded
**after** the moment you authorized this client. There is no endpoint for anything older, so this
connector does not pretend to backfill. Two consequences worth knowing before you start:

- **Authorize before your next ride.** Anything uploaded while nobody has authorized is never offered
  to the connector, even within the 30-day window.
- **Old history is a manual job.** Export it from Polar Flow and drop the files into the watch folder
  yourself. If you have already done that, set `SINCE=now` so the connector does not deliver the same
  sessions a second time.

## Why the first sync is spread out

A first run is up to a month of exercises, and asking for all of them at once is the most reliable way
to get rate-limited. So a cycle downloads at most `MAX_DOWNLOADS_PER_CYCLE` exercises (25 by default)
and picks up where it left off next time. At the default 15-minute interval that is around **2400 a
day**, which clears any 30-day window comfortably within one. The remainder shows up as `backlog`.

Fetch the status by running:

```bash
> docker compose exec polar-connector dreeve-polar-connector status
```

| Key | Meaning                                                                                                             |
|---|---------------------------------------------------------------------------------------------------------------------|
| `healthy` | `false` while nobody has authorized, and once three `POLL_INTERVAL`s have passed without a completed cycle.          |
| `startedAt` | When this container started.                                                                                        |
| `cycles` | Cycles attempted since that start, successful or not.                                                               |
| `lastSuccessfulSync` | End of the last cycle that completed. `null` until the first one does.                                               |
| `nextRunAt` | When the next cycle is due, jitter and backoff included.                                                            |
| `backoffSeconds` | How long the connector is currently backing off after a rate limit. `0` when all is well.                           |
| `authorization` | `ok`, `required` (nobody has authorized yet), `revoked` (Polar refused the stored token) or `consent-required`.     |
| `authorizeUrl` | The URL to open, while there is something to authorize. `null` once authorized.                                     |
| `authorizationError` | What Polar said, when the state is anything but `ok`.                                                               |
| `polarUser` | The Polar user id the stored token belongs to.                                                                      |
| `lastError` | The last failure message, cleared by the next successful cycle.                                                     |
| `lastCycle` | What the last cycle did. `null` before the first one.                                                               |
| `lastCycle.listed` | Exercises Polar returned for the window.                                                                            |
| `lastCycle.delivered` | Files written to the watch folder this cycle. Capped by `MAX_DOWNLOADS_PER_CYCLE`.                                  |
| `lastCycle.failed` | Downloads that went wrong and will be retried.                                                                      |
| `lastCycle.skipped` | Exercises older than `SINCE`, or filtered out by `SPORTS`, so deliberately not fetched.                             |
| `lastCycle.withoutFile` | Exercises Polar has no file for, in any format.                                                                     |
| `lastCycle.backlog` | Same as the top-level `backlog`.                                                                                     |
| `backlog` | Exercises still owed a download, whatever cycle they were listed in. This is the number to watch during a first run. |
| `exercises` | The whole ledger counted by state: `pending`, `delivered`, `skipped`, `failed`, `no-file`.                           |
| `webhook` | `configured`, `active`, its `id`, `lastEventAt` and `rejectedDeliveries` — deliveries refused because they could not be verified. All quiet unless `WEBHOOK_URL` is set. |
| `rateLimit` | What Polar last reported of your budget: `shortUsage`, `shortLimit`, `longUsage`, `longLimit`, `resetSeconds`.       |

## Configuration

| Variable | Default          | What it does |
|---|------------------|---|
| `POLAR_CLIENT_ID` | -                | **Required.** From [admin.polaraccesslink.com](https://admin.polaraccesslink.com). Also accepts `POLAR_CLIENT_ID_FILE` for Docker secrets. |
| `POLAR_CLIENT_SECRET` | -                | **Required.** Also accepts `POLAR_CLIENT_SECRET_FILE`. |
| `PUBLIC_URL` | -                | Where this connector is reachable in a browser. The redirect URI becomes `<PUBLIC_URL>/callback`. Leave empty when your client has exactly one registered URL — Polar then picks it. |
| `REDIRECT_URI` | -                | Sent verbatim, overriding the `PUBLIC_URL` default. See [When Polar refuses the redirect](/integrations/polar-flow.md#when-polar-refuses-the-redirect). |
| `WATCH_DIR` | `/watch`         | Dreeve's watch folder. |
| `STATE_DIR` | `/state`         | Where the ledger is stored. Mount it as a volume. |
| `POLAR_TOKENS` | `/tokens`        | Where the access token and webhook signing key are stored. Mount it as a volume. |
| `SINCE` | `-30d`           | How far back to reach on the first run. A date (`2026-01-01`), an ISO instant, a relative offset (`-30d`, `720h`) or `now`. Resolved once, then remembered. `-30d` is everything Polar has. |
| `SPORTS` | -                | Only these sports, comma separated (`running,cycling`). Matched against Polar's sport or its detailed sport name. Empty means everything. |
| `FALLBACK_FORMAT` | `tcx`            | `tcx`, `gpx` or `none`, for exercises that have no `.fit` file. |
| `ON_CONFLICT` | `skip`           | `skip` or `overwrite`, when the file is already in the watch folder. |
| `MAX_ATTEMPTS` | `5`              | How often an exercise may fail before it is left alone. |
| `POLL_INTERVAL` | `900`            | Seconds between cycles. Polar's budget is 520 requests per 15 minutes per user; a cycle at this pace uses about 2% of it. |
| `POLL_JITTER_PCT` | `10`             | Randomises the interval by ±this much, so every deployment of this image does not hit Polar on the same second. |
| `MAX_DOWNLOADS_PER_CYCLE` | `25`             | The per-cycle cap. See above. |
| `DOWNLOAD_DELAY_SECONDS` | `1`              | Pause between downloads. |
| `MAX_BACKOFF_SECONDS` | `21600`          | Cap on the backoff after a rate limit (6 hours). |
| `WEBHOOK_URL` | -                | A publicly reachable HTTPS URL ending in `/webhook`. Requires `WEBHOOK_ADDR` to be set alongside it. See [Instant delivery via webhook](/integrations/polar-flow.md#instant-delivery-via-webhook). |
| `HTTP_ADDR` | `0.0.0.0:8080`   | The **local** listener: `/healthz`, `/status`, `/authorize`, `/callback` and `/`. Keep it on your LAN — see [Which ports are safe to publish](/integrations/polar-flow.md#which-ports-are-safe-to-publish). `off` disables it, moving authorization to the `login` command and the healthcheck to a heartbeat file in `STATE_DIR`. |
| `WEBHOOK_ADDR` | `off`            | The **public** listener, serving `/webhook` and nothing else. Required when `WEBHOOK_URL` is set. `0.0.0.0:8081` is the conventional value. |
| `MAX_CYCLES` | `0`              | `0` runs forever; anything else runs that many cycles and exits. |
| `DRY_RUN` | `false`          | List what would be downloaded, download nothing, leave the ledger untouched. |
| `LOG_LEVEL` | `info`           | `debug`, `info`, `warning`, `error` or `critical`. |
| `LOG_FORMAT` | `text`           | `text` or `json`. |
| `PUID` / `PGID` | -                | Own the delivered files as this user. Set them to the same values Dreeve runs as. |
| `UMASK` / `TZ` | -                | As usual. |

> [!IMPORTANT]
> **Important** Just like Dreeve, the `.env` file is read when the container is **created**.
> After changing it, run `docker compose up -d --force-recreate`.

## Commands

```bash
# Authorize from a terminal, when there is no page to open. Once, interactively.
> docker compose run --rm polar-connector login

# Forget the stored authorization, so a different Polar account can connect.
# Restart afterwards: a running container keeps using the token it already loaded.
> docker compose run --rm polar-connector logout

# Run a single cycle and exit.
> docker compose run --rm polar-connector sync-once

# Show what it would fetch, without downloading anything.
> docker compose run --rm polar-connector sync-once --dry-run

# Ask a running connector what it is doing.
> docker compose exec polar-connector dreeve-polar-connector status

# Manage the webhook for this client.
> docker compose run --rm polar-connector webhook register
> docker compose run --rm polar-connector webhook show
> docker compose run --rm polar-connector webhook delete
```

## Instant delivery via webhook

Set `WEBHOOK_URL` to a publicly reachable HTTPS URL ending in `/webhook` and Polar notifies this
connector the moment a session is uploaded, cutting latency from `POLL_INTERVAL` to seconds. It is
registered for you on startup, and re-activated if Polar has deactivated it.

Set `WEBHOOK_ADDR` as well — `/webhook` has its own listener, and it is the only port you publish.
`HTTP_ADDR` plays no part in webhook delivery.

Polling stays on regardless, so a webhook that quietly stops working costs you latency and never data.
It is still worth fixing: Polar deactivates a webhook after seven days of failed deliveries.

If `/status` shows `webhook.rejectedDeliveries` climbing, deliveries are arriving but cannot be
verified: the stored signing key does not match the registered webhook. Run `webhook delete`, then
restart to register a fresh one.

One caveat before you act on that number: the port is public, so anyone who finds it can push it up by
posting nonsense. Treat a rising count as a reason to check `webhook show` against `WEBHOOK_URL`, not as
proof on its own. Genuine deliveries you never see is the symptom that matters, and `lastEventAt`
standing still while you finish a session is the one to watch.

## Which ports are safe to publish

The connector listens on two ports, and only one of them may face the internet.

| Port | Routes | Publish it? |
|---|---|---|
| `HTTP_ADDR` (default `8080`) | `/authorize`, `/callback`, `/status`, `/healthz`, `/` | **LAN only.** Never port-forward it, and never put it behind a public reverse proxy. |
| `WEBHOOK_ADDR` (off by default) | `/webhook` | Yes — this is the only route Polar needs to reach. |

Why it matters: anyone who can open `/authorize` can approve access with *their own* Polar account.
The connector stores one authorization, so theirs would replace yours — your sync stops, and their
training files are downloaded into your watch folder. The connector refuses that swap while it holds a
working authorization, but do not rely on it alone: keep the port off the internet.

`/webhook` is safe to publish because every delivery is verified with the HMAC key Polar issued at
registration, the body is size-capped, and the only thing a valid delivery does is wake the sync loop a
little earlier. Files are always fetched from Polar's API afterwards, never taken from a delivery.

## When something looks wrong

**`"authorization": "revoked"`** means Polar has refused the stored token, and the connector stops
asking rather than hammering an endpoint that is saying no. Open `/authorize` again; the next cycle
picks the new token up on its own, with no restart.

**`"authorization": "required"`** means nobody has authorized yet, or the token volume was lost. Same
fix, same URL — `authorizeUrl` in `/status` spells it out.

**`409 Already connected` when opening `/authorize`** is the takeover guard: this connector already
holds a working authorization, so it will not start a new flow. To connect a different account:

```bash
> docker compose run --rm polar-connector logout
> docker compose restart polar-connector
```

Then open `/authorize`. The restart is required, not tidiness: a running container keeps using the
token it already loaded, so `logout` alone changes what is on disk while the sync carries on with the
old account — and `/status` keeps reporting the old `polarUser`.

Two exceptions where `/authorize` stays open without a `logout`: `"authorization": "revoked"` and
`"authorization": "consent-required"`. Straight after a restart the state is `"required"` for a few
seconds even though a token is stored, because the connector has not yet tried to use it — the first
cycle settles it, and `/authorize` opens again if the token really was revoked.

**A rising `backlog` with `backoffSeconds` above `0`** is a rate limit, and it resolves itself. Check
`rateLimit` to see how much of your budget Polar thinks you have used.

## When Polar refuses the redirect

> Oops, something went wrong somewhere along the way.

That page means Polar refused the request before showing a consent screen, and the usual reason is a
`redirect_uri` it does not recognise for your client. Its admin page accepts a path, but the
authorization endpoint has been observed honouring only the bare origin for the same client.

Polar validates the redirect before authentication, so you can test candidates without signing in —
`303` means accepted, `200` means refused:

```bash
> CLIENT_ID=<your client id>
> REDIRECT=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' 'http://your-host:8080/callback')
> curl -s -o /dev/null -w '%{http_code}\n' \
    "https://flow.polar.com/oauth2/authorization?response_type=code&client_id=${CLIENT_ID}&redirect_uri=${REDIRECT}"
```

Set `REDIRECT_URI` to whichever string Polar accepts; it is sent verbatim, trailing slash included. A
bare origin works because this connector answers the callback at `/` as well as at `/callback`. Omitting
both `REDIRECT_URI` and `PUBLIC_URL` also works when the client has exactly one registered URL — Polar
then picks it, and the code arrives at whichever of the two paths that URL points to.
