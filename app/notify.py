"""Optional notifications outside the app: an ntfy topic or a webhook, set in the environment.

``NOTIFY_URL`` (https only, a public host name, no user:password in it; checked like the news
feed addresses) receives a POST for the situations where the owner has to act: a position that
is not protected, a stop sell that fails, an exit check that stays down, an order with an
unknown outcome, the daily loss limit, an executed live order, an empty Claude budget and a
(re)start. ``NOTIFY_TOKEN`` is sent as a bearer token when set. ``NOTIFY_FORMAT`` picks the body:
"ntfy" (plain text plus Title/Priority/Tags headers), "json" (a small JSON object) or "auto"
(ntfy for an ntfy host, JSON otherwise).

Every message is short and holds no secrets, no keys, no amounts and no links; the details are in
Crypie itself. Sending never blocks the caller: it runs as a background task with a deadline,
and each kind of message is rate-limited per subject (key), plus a cap per hour overall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.news.sources import check_public_url, describe_error, redact, vetted_transport

log = logging.getLogger("crypie.notify")

SEND_TIMEOUT_S = 10.0
MAX_PER_HOUR = 30  # overall cap, whatever the events
MAX_MESSAGE = 900
DEFAULT_REPEAT = timedelta(minutes=60)
RESTART_KEY = "notify:last:restart"
RESTART_REPEAT = timedelta(minutes=10)
TEST_REPEAT_S = 10.0

# event -> (title, ntfy priority, ntfy tags). Titles are ASCII: they travel in a header.
EVENTS: dict[str, tuple[str, str, str]] = {
    "unprotected": ("Crypie: positie niet bewaakt", "urgent", "rotating_light"),
    "exit_sell_failed": ("Crypie: verkoop bij stop mislukt", "urgent", "rotating_light"),
    "exit_check_failing": ("Crypie: bewaking stop-loss ligt stil", "urgent", "rotating_light"),
    "order_unknown": ("Crypie: order met onbekende uitkomst", "high", "warning"),
    "daily_loss_limit": ("Crypie: dagverlieslimiet bereikt", "high", "warning"),
    "live_order": ("Crypie: live order uitgevoerd", "default", "moneybag"),
    "claude_budget": ("Crypie: Claude-budget op", "default", "warning"),
    "restart": ("Crypie gestart", "low", "arrows_counterclockwise"),
    "test": ("Crypie: testmelding", "default", "white_check_mark"),
}


def validate_notify_url(url: str) -> str:
    """The stripped URL, or ValueError (Dutch) when it is not https, has credentials, an IP
    address or a local name: the same rules as the news feeds, plus https only."""
    from app.news.sources import validate_feed_url

    checked = validate_feed_url(url)
    if urlsplit(checked).scheme.lower() != "https":
        raise ValueError("alleen https:// is toegestaan")
    return checked


def notify_configured(settings: Any) -> bool:
    """True when NOTIFY_URL is set to a usable address (an invalid one is ignored at start)."""
    url = getattr(settings, "notify_url", "") or ""
    if not url:
        return False
    try:
        validate_notify_url(url)
    except ValueError:
        return False
    return True


def _format(settings: Any) -> str:
    fmt = getattr(settings, "notify_format", "auto") or "auto"
    if fmt in ("ntfy", "json"):
        return fmt
    host = (urlsplit(settings.notify_url).hostname or "").lower()
    return "ntfy" if host == "ntfy.sh" or host.startswith("ntfy.") else "json"


def _clean_text(text: str) -> str:
    """No tokens, no control characters, bounded length."""
    text = redact(" ".join(str(text).split()))
    return text[:MAX_MESSAGE]


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


@dataclass
class _Message:
    event: str
    text: str


async def _post(settings: Any, message: _Message) -> None:
    """Send one message; raises on any failure (the caller logs it)."""
    url = validate_notify_url(settings.notify_url)
    await check_public_url(url)  # the host must resolve to public addresses only
    title, priority, tags = EVENTS.get(message.event, ("Crypie", "default", ""))
    headers = {"User-Agent": "Crypie"}
    token = getattr(settings, "notify_token", "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if _format(settings) == "ntfy":
        headers.update({"Title": _ascii(title), "Priority": priority, "Tags": tags})
        kwargs: dict[str, Any] = {"content": message.text.encode("utf-8")}
        headers["Content-Type"] = "text/plain; charset=utf-8"
    else:
        body = {
            "source": "crypie",
            "event": message.event,
            "title": title,
            "message": message.text,
            "priority": priority,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        kwargs = {"content": json.dumps(body, ensure_ascii=False).encode("utf-8")}
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(
        timeout=SEND_TIMEOUT_S, follow_redirects=False, transport=vetted_transport()
    ) as client:
        resp = await client.post(url, headers=headers, **kwargs)
        resp.raise_for_status()


class Notifier:
    """Rate-limited, fire-and-forget notifications; a no-op without NOTIFY_URL."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.enabled = notify_configured(settings)
        self.exit_fail_min = max(int(getattr(settings, "notify_exit_fail_min", 10) or 10), 1)
        self._last: dict[str, datetime] = {}
        self._sent: list[float] = []  # monotonic send times, for the hourly cap
        self._tasks: set[asyncio.Task] = set()
        self.sent_count = 0
        self.failed_count = 0

    def _allowed(self, key: str, every: timedelta | None, now: datetime) -> bool:
        last = self._last.get(key)
        if every is not None and last is not None and now - last < every:
            return False
        if every is None and last is not None:
            return False  # once per key (e.g. one message per live order)
        clock = time.monotonic()
        self._sent = [t for t in self._sent if clock - t < 3600]
        if len(self._sent) >= MAX_PER_HOUR:
            log.warning("notification cap of %d per hour reached; %s not sent", MAX_PER_HOUR, key)
            return False
        self._sent.append(clock)
        self._last[key] = now
        if len(self._last) > 1000:  # forget the oldest subjects
            for old in sorted(self._last, key=self._last.get)[:200]:
                self._last.pop(old, None)
        return True

    def send(
        self,
        event: str,
        text: str,
        *,
        key: str | None = None,
        every: timedelta | None = DEFAULT_REPEAT,
    ) -> bool:
        """Queue a message; returns False when it is not sent (off, rate-limited, no loop).

        ``key`` is the subject the rate limit applies to (default: the event); ``every`` is how
        often that subject may be repeated (None: only once per key).
        """
        if not self.enabled:
            return False
        if not self._allowed(key or event, every, datetime.now(UTC)):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        message = _Message(event, _clean_text(text))
        task = loop.create_task(self._deliver(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _deliver(self, message: _Message) -> None:
        try:
            await asyncio.wait_for(_post(self.settings, message), SEND_TIMEOUT_S + 5)
            self.sent_count += 1
            log.info("notification sent: %s", message.event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never the URL in the log: it is a secret
            self.failed_count += 1
            log.warning("notification %s failed: %s", message.event, describe_error(exc))

    async def drain(self, timeout: float = SEND_TIMEOUT_S + 5) -> None:
        """Wait for queued messages (tests, shutdown); cancels what is still running after."""
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()

    def restart(self, session: Any, text: str) -> bool:
        """The start-up message, at most once per RESTART_REPEAT across restarts (a crash loop
        must not flood the phone); the last one is kept in the database."""
        if not self.enabled:
            return False
        from app.db import Setting

        now = datetime.now(UTC)
        try:
            row = session.get(Setting, RESTART_KEY)
            last = datetime.fromisoformat(row.value) if row is not None else None
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if last is not None and timedelta(0) <= now - last < RESTART_REPEAT:
                return False
            if row is None:
                session.add(Setting(key=RESTART_KEY, value=now.isoformat()))
            else:
                row.value = now.isoformat()
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            log.warning("could not read or store the last restart notification")
        return self.send("restart", text, key="restart", every=RESTART_REPEAT)


_last_test = 0.0


def _failure_nl(exc: BaseException) -> str:
    """Why a message did not arrive, in plain words; the technical detail follows separately."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"de dienst weigerde het bericht (fout {exc.response.status_code})"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "de dienst antwoordde niet op tijd"
    return "het adres is niet te bereiken"


async def send_test(settings: Any) -> tuple[bool, str]:
    """Send a test message now (for a "Testmelding sturen" button). Returns (ok, Dutch text)."""
    global _last_test
    if not getattr(settings, "notify_url", ""):
        return False, "Er is geen meldingskanaal ingesteld (NOTIFY_URL)."
    try:
        validate_notify_url(settings.notify_url)
    except ValueError as exc:
        return False, f"Het meldingsadres is ongeldig: {exc}."
    clock = time.monotonic()
    if _last_test and clock - _last_test < TEST_REPEAT_S:
        return False, "Er is net een testmelding verstuurd. Wacht een paar seconden."
    _last_test = clock
    message = _Message(
        "test",
        "Dit is een testmelding van Crypie. Zie je dit bericht, dan komen meldingen over "
        "onbewaakte posities, mislukte verkopen en storingen ook aan.",
    )
    try:
        await asyncio.wait_for(_post(settings, message), SEND_TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001
        log.warning("test notification failed: %s", describe_error(exc))
        return False, (
            f"De testmelding is niet aangekomen: {_failure_nl(exc)}. Controleer NOTIFY_URL in "
            f"de serveromgeving. (Technisch: {describe_error(exc)})"
        )
    return True, "Testmelding verstuurd. Controleer je telefoon of de webhook."
