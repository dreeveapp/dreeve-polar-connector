<h1 align="center">Dreeve Polar Connector</h1>

<p align="center">
<a href="https://github.com/cgtobi/dreeve-polar-connector/blob/main/LICENSE"><img src="https://img.shields.io/github/license/cgtobi/dreeve-polar-connector?color=428f7e&logo=open%20source%20initiative&logoColor=white" alt="License"></a>
<a href="https://docs.dreeve.app"><img src="https://img.shields.io/badge/docs-docs.dreeve.app-428f7e?logo=readthedocs&logoColor=white" alt="Documentation"></a>
</p>

---

<h4 align="center">Pulls your training sessions out of Polar Flow and drops them into <a href="https://dreeve.app">Dreeve</a>'s watch folder.</h4>

## 📖 Documentation

Head over to the [documentation](https://docs.dreeve.app) to get started with the installation and
configuration of this connector.

## ⚠️ Polar only keeps 30 days

The Polar AccessLink API returns exercises uploaded to Flow in the **last 30 days**, and only those
uploaded **after** you connect this client. There is no way to backfill older history through the API,
so this connector does not try to. To bring in what you already have, export it from Polar Flow and
drop the files into Dreeve's watch folder by hand.

## 🚀 First run

1. Create a client at [admin.polaraccesslink.com](https://admin.polaraccesslink.com) and register
   `http://<this-host>:8080/callback` as its redirect URL.
2. Copy `.env.example` to `.env`, and fill in `POLAR_CLIENT_ID`, `POLAR_CLIENT_SECRET` and
   `PUBLIC_URL=http://<this-host>:8080`.
3. `docker compose up -d`
4. Open `http://<this-host>:8080/authorize` and approve access. Syncing starts immediately.

Until step 4 the container reports unhealthy on purpose, and `GET /status` tells you which URL to open.

Running headless with `HTTP_ADDR=off`? Use `docker compose run --rm polar-connector login` instead,
which prints the URL and reads the code back from your terminal.

### 😖 "Oops, something went wrong somewhere along the way."

That page means Polar refused the request before showing consent, and the usual reason is a
`redirect_uri` it does not recognise for your client. Its admin page accepts a path, but the
authorization endpoint has been observed honouring only the bare origin for the same client.

Polar validates the redirect before authentication, so you can test candidates without signing in —
`303` means accepted, `200` means refused:

```bash
CLIENT_ID=<your client id>
REDIRECT=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' 'http://your-host:8080/callback')
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://flow.polar.com/oauth2/authorization?response_type=code&client_id=${CLIENT_ID}&redirect_uri=${REDIRECT}"
```

Set `REDIRECT_URI` to whichever string Polar accepts; it is sent verbatim, trailing slash included.
A bare origin works because the callback is served at `/` as well as at `/callback`. Omitting both
`REDIRECT_URI` and `PUBLIC_URL` also works when the client has exactly one registered URL — Polar then
picks it, and the code arrives at whichever of the two paths that URL points to.

## ⚡ Optional: instant delivery via webhook

Set `WEBHOOK_URL` to a publicly reachable HTTPS URL ending in `/webhook` and Polar will notify this
connector the moment a session is uploaded, cutting latency from `POLL_INTERVAL` to seconds. Polling
stays on regardless, so a webhook that stops working costs latency and never data.

## 🔍 Operations

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Docker healthcheck. 503 while unauthorized or stalled |
| `GET /status` | Cycle counts, backlog, rate-limit budget, webhook state |
| `GET /authorize` | Start the one-time authorization |
| `POST /webhook` | Polar's deliveries |

`docker compose exec polar-connector dreeve-polar-connector status` prints the same report from a shell.

If `/status` reports `"authorization": "revoked"`, Polar has refused the stored token and the connector
stops asking rather than hammering an endpoint that is saying no. Open `/authorize` again — the next cycle
picks the new token up on its own, with no restart.

## 🛠️ Development

Everything runs in Docker; nothing but Docker is needed on the host.

```bash
make build
make check     # ruff + format + mypy + pytest
make test
```

`src/dreeve_polar_connector/accesslink/` is a standalone AccessLink client that deliberately knows
nothing about Dreeve — a test enforces that it never imports from the rest of the package — so it can be
contributed upstream to [polar-flow-api](https://github.com/StuMason/polar-flow) later.

## 📄 License

GPL-3.0-or-later. Derived from
[dreeveapp/dreeve-garmin-connector](https://github.com/dreeveapp/dreeve-garmin-connector), whose
copyright notice is retained in `LICENSE`.
