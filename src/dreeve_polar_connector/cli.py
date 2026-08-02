"""Command line entry points.

`run` is what the container does. The others exist for setup (`login`, `webhook register`) and for
operators (`sync-once`, `status`, `healthcheck`).
"""

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import signal
import sys
from argparse import Namespace
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import replace
from typing import Any

import aiohttp

from dreeve_polar_connector.accesslink import oauth, webhooks
from dreeve_polar_connector.accesslink.client import AccessLinkClient
from dreeve_polar_connector.accesslink.exceptions import AccessLinkError, WebhookAlreadyExists
from dreeve_polar_connector.config import Config, InvalidConfiguration
from dreeve_polar_connector.delivery import WatchFolder, WatchFolderUnusable
from dreeve_polar_connector.ledger import LEDGER_FILENAME, CorruptLedger, Ledger
from dreeve_polar_connector.logging_ import Secrets, configure_logging
from dreeve_polar_connector.loop import AuthorizationStamp, SyncLoop, heartbeat_path
from dreeve_polar_connector.polar import AccessLinkPolarClient, AuthorizationRequired, PolarError
from dreeve_polar_connector.server import HttpServer, build_local_app, build_webhook_app
from dreeve_polar_connector.status import UNHEALTHY_AFTER_CYCLES, Status
from dreeve_polar_connector.sync import Sync, SystemClock
from dreeve_polar_connector.tokens import Authorization, CorruptTokenStore, StoredWebhook, TokenStore

EXIT_OK = 0
EXIT_FAILED = 1
HEALTHCHECK_TIMEOUT_SECONDS = 5

logger = logging.getLogger(__name__)

Command = Callable[[Config, Secrets, TokenStore, Namespace], Coroutine[Any, Any, int]]


def no_authorization_instruction(config: Config) -> str:
    url = authorize_url_for(config)
    if url:
        return (
            f"Nobody has authorized this connector yet. Open {url} in a browser and approve access. "
            f"The next sync starts as soon as you do."
        )

    return (
        "Nobody has authorized this connector yet, and HTTP_ADDR is 'off' so there is no page to open. "
        "Run `docker compose run --rm polar-connector login` once instead."
    )


def authorization_stamp_for(tokens: TokenStore) -> AuthorizationStamp | None:
    """Which authorization is stored right now, as a value the loop can compare without holding a token.

    Reads the file on every call — `TokenStore.current()` does no caching — which is what lets the loop
    notice a token written by any route: `/callback`, `login` in a second container, or a restored backup.
    """
    authorization = tokens.current()
    if authorization is None:
        return None

    return (authorization.polar_user_id, authorization.authorized_at)


def authorize_url_for(config: Config) -> str | None:
    """Where a human should go to authorize. None when there is no server to serve it."""
    if config.http_addr is None:
        return None
    if config.public_url:
        return f"{config.public_url}/authorize"

    host, _, port = config.http_addr.rpartition(":")
    # An address to listen on (every interface), not one to connect to.
    reachable = "127.0.0.1" if _binds_every_interface(host) else host

    return f"http://{reachable}:{port}/authorize"


def _binds_every_interface(host: str) -> bool:
    """True for '0.0.0.0', '::' and '' — addresses that mean "every interface", not a real host to
    dial. Checked with `ipaddress` rather than a hardcoded string, so a literal "0.0.0.0" never has to
    appear in this module's source (which is also what keeps ruff's S104 from firing on it)."""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dreeve-polar-connector",
        description="Syncs Polar Flow training sessions into Dreeve's watch folder.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Authorize once from a terminal, by pasting the code from your browser.")
    commands.add_parser(
        "logout", help="Forget the stored authorization, so a different Polar account can be connected."
    )
    sync_command = commands.add_parser("sync-once", help="Run a single sync cycle and exit.")
    sync_command.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded, download nothing and leave the ledger untouched.",
    )
    commands.add_parser("run", help="Run cycles on POLL_INTERVAL until stopped. This is what the container does.")
    commands.add_parser("healthcheck", help="Exit 0 while the connector is healthy. Used by Docker's HEALTHCHECK.")
    commands.add_parser("status", help="Print what the running connector is doing.")
    webhook_command = commands.add_parser("webhook", help="Manage the Polar webhook for this client.")
    webhook_command.add_argument("action", choices=("register", "show", "delete"))

    arguments = parser.parse_args(argv)

    try:
        config = Config.from_env(os.environ)
    except InvalidConfiguration as exception:
        # Logging is configured from the very config that just failed, so this goes straight to stderr.
        print(exception, file=sys.stderr)
        return EXIT_FAILED

    if getattr(arguments, "dry_run", False):
        config = replace(config, dry_run=True)

    secrets = Secrets([config.client_secret])
    configure_logging(config.log_level, config.log_format, secrets)

    tokens = TokenStore(config.tokens_dir)
    try:
        for value in tokens.secrets():
            secrets.add(value)
    except CorruptTokenStore as exception:
        # `logout` is the documented way out of an unreadable store, so refusing to dispatch it here
        # would leave deleting the file by hand as the only route. Nothing else can run: every other
        # command needs a token it cannot read, and one it cannot read is one it cannot redact either.
        if arguments.command != "logout":
            logger.error("%s", exception)
            return EXIT_FAILED
        logger.warning("%s Continuing, because clearing it is what `logout` is for.", exception)

    dispatch: dict[str, Command] = {
        "login": login,
        "logout": logout,
        "sync-once": sync_once,
        "run": run,
        "healthcheck": healthcheck,
        "status": status_command,
        "webhook": webhook_action,
    }

    return asyncio.run(dispatch[arguments.command](config, secrets, tokens, arguments))


async def login(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    """The fallback for HTTP_ADDR=off: print the URL, read the code back from a human."""
    if not sys.stdin.isatty():
        logger.error(
            "This command needs a terminal to read the authorization code from. "
            "Run it with `docker compose run --rm polar-connector login`, which attaches one."
        )
        return EXIT_FAILED

    state = "cli"
    print(
        "1. Open this URL in a browser and approve access:\n\n"
        f"   {oauth.authorization_url(client_id=config.client_id, state=state, redirect_uri=config.redirect_uri)}\n\n"
        "2. You will be redirected to your registered redirect URL. Copy the value of the\n"
        "   `code=` parameter out of the address bar and paste it below.\n"
    )
    # Blocking, so it goes through a thread rather than the event loop directly — the same reason
    # every synchronous file operation in this project runs via `asyncio.to_thread`.
    code = (await asyncio.to_thread(input, "Authorization code: ")).strip()
    if not code:
        logger.error("No code was entered; nothing was sent to Polar.")
        return EXIT_FAILED

    async with aiohttp.ClientSession() as session:
        try:
            token = await oauth.exchange_code(
                session,
                client_id=config.client_id,
                client_secret=config.client_secret,
                code=code,
                redirect_uri=config.redirect_uri,
            )
            secrets.add(token.access_token)
            member_id = f"dreeve-{token.x_user_id}"
            await AccessLinkClient(session, token.access_token).register_user(member_id)
        except AccessLinkError as exception:
            logger.error("Authorization failed. %s", exception)
            return EXIT_FAILED

    await tokens.store(
        Authorization(
            polar_user_id=token.x_user_id,
            access_token=token.access_token,
            member_id=member_id,
            authorized_at=SystemClock().now(),
        )
    )
    logger.info(
        "Authorized Polar user %s. The token is stored in %s and does not expire; "
        "keep that volume and you will not have to do this again.",
        token.x_user_id,
        tokens.tokens_path,
    )

    return EXIT_OK


async def logout(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    """Clears the stored authorization, which is what re-opens `/authorize`.

    Leaves `webhook.json` alone. That file holds a signing key Polar discloses exactly once, and losing
    it costs a webhook re-registration that has nothing to do with changing accounts.

    This runs in its own container, so it can only change the file. A running connector holds the `Sync`
    it already built (`loop.SyncLoop._resolve_sync`) and goes on using the token it loaded, which is why
    the message below asks for a restart rather than assuming this was enough.
    """
    try:
        stored = tokens.current() is not None
    except CorruptTokenStore as exception:
        # Clearing an unreadable file is the whole point: it is what unblocks `/authorize`, which refuses
        # while it cannot tell who is connected.
        logger.warning("%s Clearing it anyway.", exception)
        stored = True

    if not stored:
        logger.info("Nothing to do: no authorization is stored in %s.", tokens.tokens_path)
        return EXIT_OK

    tokens.clear()
    logger.info(
        "Cleared the stored authorization in %s. Restart the connector so it stops using the token it "
        "already loaded, then authorize again. %s",
        tokens.tokens_path,
        no_authorization_instruction(config),
    )

    return EXIT_OK


async def sync_once(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    async with aiohttp.ClientSession() as session:
        try:
            sync = await build_sync(config, secrets, tokens, session)
            result = await sync.run_once()
        except (PolarError, CorruptLedger, CorruptTokenStore, WatchFolderUnusable) as exception:
            logger.error("Sync failed. %s", exception)
            return EXIT_FAILED

    logger.info("Cycle finished: %s", result)

    return EXIT_OK


async def run(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    clock = SystemClock()

    try:
        # Fail fast on the two things no amount of waiting will fix. A missing authorization is
        # deliberately not one of them: the container stays up, reports unhealthy and says what to open.
        await WatchFolder(config.watch_dir, config.on_conflict).prepare()
        Ledger.load(config.state_dir / LEDGER_FILENAME)
    except (WatchFolderUnusable, CorruptLedger) as exception:
        logger.error("Cannot start. %s", exception)
        return EXIT_FAILED

    status = Status(config.poll_interval, clock.now(), authorize_url=authorize_url_for(config))
    wake = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        servers: list[HttpServer] = []
        if config.http_addr is not None:
            servers.append(
                HttpServer(
                    config.http_addr,
                    build_local_app(
                        config=config,
                        status=status,
                        clock=clock,
                        tokens=tokens,
                        session=session,
                        secrets=secrets,
                        wake=wake,
                    ),
                )
            )
        if config.webhook_addr is not None:
            servers.append(
                HttpServer(
                    config.webhook_addr,
                    build_webhook_app(status=status, clock=clock, tokens=tokens, wake=wake),
                )
            )
        for server in servers:
            await server.start()

        try:
            # After the server is up: Polar pings the URL during registration and refuses to create
            # the webhook unless something answers 200. Inside this `try` (not before it) so that
            # nothing `ensure_webhook` does — it must never be fatal, but a listening server must never
            # outlive a crash either — can leave the server running on a dead process.
            await ensure_webhook(config, secrets, tokens, session, status)

            async def factory(should_stop: Callable[[], bool]) -> Sync:
                return await build_sync(config, secrets, tokens, session, should_stop)

            loop = SyncLoop(
                config,
                factory,
                status,
                clock,
                wake=wake,
                authorization_stamp=lambda: authorization_stamp_for(tokens),
            )

            event_loop = asyncio.get_running_loop()
            for received in (signal.SIGTERM, signal.SIGINT):
                try:
                    event_loop.add_signal_handler(received, loop.request_stop)
                except (NotImplementedError, RuntimeError) as exception:
                    # NotImplementedError on Windows; RuntimeError when this event loop is not running on
                    # the process's main thread (as in a test driven through `asyncio.to_thread`). Neither
                    # is fatal: MAX_CYCLES and the healthcheck still work, only a graceful `docker stop`
                    # does not.
                    logger.warning("Could not install a handler for %s: %s", received.name, exception)

            logger.info("Watching Polar every %d seconds; delivering into %s", config.poll_interval, config.watch_dir)
            if tokens.current() is None:
                logger.warning("%s", no_authorization_instruction(config))

            await loop.run()
        finally:
            for server in servers:
                await server.stop()

    return EXIT_OK


async def healthcheck(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    if config.http_addr is None:
        return _heartbeat_is_recent(config)

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                _local_url(config.http_addr, "/healthz"),
                timeout=aiohttp.ClientTimeout(total=HEALTHCHECK_TIMEOUT_SECONDS),
            ) as response,
        ):
            if response.status == 200:
                return EXIT_OK
            print(f"Not healthy: {response.status} {(await response.json()).get('healthy')}", file=sys.stderr)
    except (aiohttp.ClientError, TimeoutError) as exception:
        print(f"Not healthy: {exception}", file=sys.stderr)

    return EXIT_FAILED


async def status_command(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:  # noqa: ARG001
    if config.http_addr is None:
        print("HTTP_ADDR is off, so there is no status endpoint to ask.", file=sys.stderr)
        return EXIT_FAILED

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                _local_url(config.http_addr, "/status"),
                timeout=aiohttp.ClientTimeout(total=HEALTHCHECK_TIMEOUT_SECONDS),
            ) as response,
        ):
            print(json.dumps(await response.json(), indent=2))
    except (aiohttp.ClientError, TimeoutError) as exception:
        print(f"Could not reach the status endpoint: {exception}", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


async def webhook_action(config: Config, secrets: Secrets, tokens: TokenStore, arguments: Namespace) -> int:
    if arguments.action == "show":
        stored = tokens.webhook()
        if stored is None:
            print(json.dumps({"configured": False}))
            return EXIT_OK
        print(json.dumps({"configured": True, "id": stored.webhook_id, "url": stored.url}, indent=2))
        return EXIT_OK

    async with aiohttp.ClientSession() as session:
        credentials = {"client_id": config.client_id, "client_secret": config.client_secret}
        try:
            if arguments.action == "delete":
                stored = tokens.webhook()
                if stored is None:
                    logger.info("No webhook is registered locally; nothing to delete.")
                    return EXIT_OK
                await webhooks.delete(session, webhook_id=stored.webhook_id, **credentials)
                tokens.clear_webhook()
                logger.info("Deleted webhook %s", stored.webhook_id)
                return EXIT_OK

            if not config.webhook_url:
                logger.error("WEBHOOK_URL is not set, so there is no URL to register.")
                return EXIT_FAILED

            created = await webhooks.create(
                session, url=config.webhook_url, client_id=config.client_id, client_secret=config.client_secret
            )
        except WebhookAlreadyExists as exception:
            logger.error("%s", exception)
            return EXIT_FAILED
        except AccessLinkError as exception:
            verb = "delete" if arguments.action == "delete" else "register"
            logger.error("Could not %s the webhook. %s", verb, exception)
            return EXIT_FAILED

    secrets.add(created.signature_secret_key)
    await tokens.store_webhook(
        StoredWebhook(
            webhook_id=created.webhook_id,
            signature_secret_key=created.signature_secret_key,
            url=created.url,
        )
    )
    logger.info("Registered webhook %s for %s", created.webhook_id, created.url)

    return EXIT_OK


async def ensure_webhook(
    config: Config,
    secrets: Secrets,
    tokens: TokenStore,
    session: aiohttp.ClientSession,
    status: Status,
) -> None:
    """Registers the webhook once, and revives it if Polar has deactivated it.

    Never fatal: a connector that cannot register a webhook still polls, and polling is the safety net
    the whole design leans on.
    """
    if not config.webhook_url:
        return

    credentials = {"client_id": config.client_id, "client_secret": config.client_secret}
    stored = tokens.webhook()

    try:
        if stored is None:
            created = await webhooks.create(
                session, url=config.webhook_url, client_id=config.client_id, client_secret=config.client_secret
            )
            secrets.add(created.signature_secret_key)
            try:
                await tokens.store_webhook(
                    StoredWebhook(
                        webhook_id=created.webhook_id,
                        signature_secret_key=created.signature_secret_key,
                        url=created.url,
                    )
                )
            except OSError as exception:
                # Polar has already disclosed the one-time signing key, and it is lost the moment this
                # write fails — not self-healing, so a generic warning is not good enough. Polling still
                # continues; this connector just cannot verify a webhook delivery until a human acts.
                logger.error(
                    "Registered webhook %s with Polar, but could not save its signing key to %s: %s. "
                    "That key is gone for good, so deliveries can never be verified. Run `webhook delete` "
                    "(or remove it from the Polar admin page), then restart to register a fresh one. "
                    "Polling continues meanwhile.",
                    created.webhook_id,
                    tokens.webhook_path,
                    exception,
                )
                return

            status.webhook_registered(webhook_id=created.webhook_id, active=True)
            logger.info("Registered webhook %s for %s", created.webhook_id, created.url)
            return

        info = await webhooks.read(session, **credentials)
        if info is None:
            logger.warning(
                "A webhook is stored locally but Polar has none. Delete %s and restart to register again.",
                tokens.webhook_path,
            )
            return

        if info.url and info.url != config.webhook_url:
            # Nothing here can change a registered URL — Polar has no update call — so this is reported
            # rather than repaired. Worth reporting loudly: `active` stays true and /status looks healthy
            # while every delivery goes to the old address, until seven days of failures deactivate it.
            logger.error(
                "Polar delivers this webhook to %s, but WEBHOOK_URL is now %s, so no delivery can arrive. "
                "Run `webhook delete`, then restart to register the new URL. Polling continues meanwhile.",
                info.url,
                config.webhook_url,
            )

        if not info.active:
            logger.info("Polar had deactivated the webhook; activating it again")
            await webhooks.activate(session, **credentials)

        status.webhook_registered(webhook_id=info.webhook_id, active=True)
    except WebhookAlreadyExists:
        logger.error(
            "Polar already has a webhook for this client, but this connector has no signing key for it. "
            "Delete it with `webhook delete` or in the Polar admin page, then restart. Polling continues meanwhile."
        )
    except (AccessLinkError, OSError) as exception:
        logger.warning("Could not set up the webhook: %s. Polling continues; only latency is affected.", exception)


async def build_sync(
    config: Config,
    secrets: Secrets,
    tokens: TokenStore,
    session: aiohttp.ClientSession,
    should_stop: Callable[[], bool] = lambda: False,
) -> Sync:
    authorization = tokens.current()
    if authorization is None:
        # Raised before any client is constructed: no request, no failed call to count.
        raise AuthorizationRequired(no_authorization_instruction(config))

    secrets.add(authorization.access_token)

    watch_folder = WatchFolder(config.watch_dir, config.on_conflict)
    await watch_folder.prepare()

    return Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=AccessLinkPolarClient(AccessLinkClient(session, authorization.access_token)),
        watch_folder=watch_folder,
        polar_user_id=authorization.polar_user_id,
        clock=SystemClock(),
        should_stop=should_stop,
    )


def _local_url(http_addr: str, path: str) -> str:
    host, _, port = http_addr.rpartition(":")
    reachable = "127.0.0.1" if _binds_every_interface(host) else host

    return f"http://{reachable}:{port}{path}"


def _heartbeat_is_recent(config: Config) -> int:
    heartbeat = heartbeat_path(config.state_dir)
    try:
        touched_at = heartbeat.stat().st_mtime
    except OSError:
        print(f"No heartbeat at {heartbeat} yet.", file=sys.stderr)
        return EXIT_FAILED

    stale_after = config.poll_interval * UNHEALTHY_AFTER_CYCLES
    if SystemClock().now().timestamp() - touched_at > stale_after:
        print(f"The last successful cycle was more than {stale_after} seconds ago.", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK
