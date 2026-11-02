"""The shared page frame and macros (UX audit, foundation): live banner, mode in the tab title and
navigation, kill switch, status, notices, checklist text alternatives, and safe time markup."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from starlette.datastructures import State

from app.main import app
from app.web import KILL_SWITCH_NL, STATIC_DIR, templates

BASE = "https://api.bitvavo.com/v2"
PAGES = ("/", "/trading", "/journal", "/plan", "/strategy", "/news", "/settings")


def _request(path="/trading", flashes=None, user=None):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(user=user, flashes=flashes or []),
        app=SimpleNamespace(state=State()),
    )


def _settings(mode="paper"):
    return SimpleNamespace(
        trading_mode=mode,
        markets=["BTC-EUR"],
        has_bitvavo_credentials=True,
        anthropic_api_key="",
    )


def _render(body: str, **ctx) -> str:
    page = (
        '{% extends "base.html" %}{% import "_macros.html" as ui %}'
        "{% block title %}Crypie · Proef{% endblock %}{% block content %}" + body + "{% endblock %}"
    )
    ctx.setdefault("request", _request())
    ctx.setdefault("settings", _settings())
    return templates.env.from_string(page).render(**ctx)


def test_live_mode_is_unmistakable_on_every_page():
    html = _render("", settings=_settings("live"), paused=False)
    assert '<p class="live-banner">LIVE · echt geld op Bitvavo</p>' in html
    assert '<body data-mode="live"' in html and '<html lang="nl" data-mode="live">' in html
    assert "<title>LIVE · Proef · Crypie</title>" in html
    assert "favicon-live.svg" in html
    paper = _render("", paused=False)
    assert "live-banner" not in paper and "<title>Proef · Crypie</title>" in paper
    assert 'class="pill mode-pill pill-paper"' in paper and 'href="/settings#trading"' in paper


def test_the_kill_switch_shows_in_the_navigation_and_the_tab():
    html = _render("", paused=True)
    assert "<title>Noodstop · Proef · Crypie</title>" in html
    assert 'Oefenmodus<span class="mode-extra">· noodstop</span>' in html
    assert 'data-paused="true"' in html and "favicon-paused.svg" in html
    live = _render("", settings=_settings("live"), paused=True)
    assert 'Live<span class="mode-extra">· noodstop</span>' in live
    assert "<title>LIVE · Noodstop · Proef · Crypie</title>" in live


def test_skip_link_and_main_target():
    html = _render("")
    assert html.index('class="skip-link" href="#main"') < html.index('class="nav"')
    assert '<main class="page" id="main" tabindex="-1">' in html


def test_flash_messages_render_on_top_and_are_announced():
    flashes = [{"kind": "ok", "text": "Opgeslagen."}, {"kind": "error", "text": "Mislukt <b>x</b>"}]
    html = _render("<h1>Pagina</h1>", request=_request(flashes=flashes))
    main = html[html.index('<main class="page"') :]
    assert main.index("Opgeslagen.") < main.index("<h1>Pagina</h1>")
    assert 'notice notice-ok" role="status"' in html
    assert 'notice notice-error" role="alert"' in html
    assert "Mislukt &lt;b&gt;x&lt;/b&gt;" in html  # flash text is escaped


def test_notice_roles_follow_the_kind_of_message():
    html = _render(
        '{{ ui.notice("warn", "blijvend") }}{{ ui.notice("error", "toestand", live=false) }}'
        '{{ ui.notice("warn", "na actie", live=true) }}'
    )
    assert 'notice-warn">' in html  # a lasting warning: no announcement
    assert 'notice-error">' in html and 'notice-warn" role="status"' in html


def test_kill_switch_macro_posts_without_password_and_explains_itself():
    html = _render('{{ ui.kill_switch(false, next="/") }}{{ ui.kill_switch(true, id="b") }}')
    assert html.count('action="/trading/action"') == 2
    assert 'name="next" value="/"' in html and 'name="next" value="/trading"' in html
    assert 'value="pause"' in html and KILL_SWITCH_NL["button"] in html
    assert 'value="resume"' in html and KILL_SWITCH_NL["resume"] in html
    assert KILL_SWITCH_NL["state"] in html and KILL_SWITCH_NL["help"] in html
    assert "current_password" not in html


def test_status_macro_by_level_and_without_health():
    now = datetime.now(UTC)
    health = {
        "level": "warn",
        "label": "Actief · koopt niet",
        "detail": "Zonder Claude-sleutel wacht de bot af.",
        "reasons": ["geen Claude-sleutel"],
        "last_cycle": now - timedelta(minutes=4),
        "next_cycle": now + timedelta(minutes=56),
        "exits_ok": False,
        "last_exit_ok": now - timedelta(minutes=17),
        "exit_interval_min": 1,
    }
    html = _render("{{ ui.status(health) }}{{ ui.status(none) }}", health=health)
    assert 'class="pill pill-orange pill-lg"' in html and "Actief · koopt niet" in html
    assert "geen Claude-sleutel" in html and "loopt achter" in html
    assert "Status onbekend" in html


def test_checklist_marks_have_a_text_alternative():
    class Decision:
        signal = "buy"

        def check_list(self):
            return {
                "buy": [
                    {
                        "text": "rsi14 lager dan 35",
                        "left": 41.1,
                        "right": 35,
                        "ok": False,
                        "soft": False,
                    },
                    {
                        "text": "koers boven ema200",
                        "left": 1,
                        "right": 2,
                        "ok": False,
                        "soft": True,
                    },
                    {
                        "text": "rsi14 stijgt",
                        "left": None,
                        "right": None,
                        "ok": True,
                        "soft": False,
                    },
                ],
                "sell": [],
            }

    html = _render("{{ ui.checks(d, legend=true) }}", d=Decision())
    # UX-89: the marks are SVG icons (hidden from screen readers), not text glyphs.
    assert "✗" not in html and "✓" not in html
    cross = '<path d="M7 7l10 10M17 7L7 17"/></svg></span>'
    assert cross + '<span class="visually-hidden">niet gehaald: </span>' in html
    assert "zacht filter, niet gehaald: </span>" in html and "gehaald: </span>rsi14 stijgt" in html
    assert 'class="checks-legend"' in html


def test_static_foundation_files_exist():
    for name in ("manifest.webmanifest", "icons/favicon.svg", "icons/favicon-live.svg"):
        assert (STATIC_DIR / name).is_file(), name
    for name in ("icons/apple-touch-icon.png", "icons/icon-192.png", "icons/icon-512.png"):
        assert (STATIC_DIR / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name


def test_css_has_one_section_per_page():
    css = Path(STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for page in (
        "Overzicht",
        "Handelen",
        "Journaal",
        "Tradingplan",
        "Strategie",
        "Nieuws",
        "Instellingen",
        "Inloggen en account",
    ):
        assert css.count(f"/* ==== Pagina: {page} ==== */") == 1, page
    # The column layout lives on .field, not on every bare <label>.
    assert not re.search(r"^label \{[^}]*flex-direction: column", css, re.MULTILINE)


# -- Rendered pages ------------------------------------------------------------------------------

TIME_IN_ATTRIBUTE = re.compile(r'="[^"]*<time', re.IGNORECASE)


@pytest.fixture
async def api():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@respx.mock
async def test_pages_use_the_frame_and_never_put_markup_in_attributes(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    for path in PAGES:
        r = await api.get(path)
        assert r.status_code == 200, path
        assert 'href="#main"' in r.text and 'id="main"' in r.text, path
        assert not TIME_IN_ATTRIBUTE.search(r.text), path
        assert "· Crypie</title>" in r.text, path


@respx.mock
async def test_navigation_shows_the_kill_switch_on_every_page(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    assert (await api.post("/trading/action", data={"action": "pause"})).status_code == 303
    try:
        for path in ("/news", "/journal", "/"):
            text = (await api.get(path)).text
            assert '<span class="mode-extra">· noodstop</span>' in text, path
            assert "<title>Noodstop · " in text, path
    finally:
        await api.post("/trading/action", data={"action": "resume"})
    assert "mode-extra" not in (await api.get("/news")).text


def test_templates_have_no_inline_styles():
    """Use the classes in app.css (utilities, .field, .metric, …) instead of style=. Every
    template is free of them now; keep it that way (UX-92)."""
    offenders = [
        path.name
        for path in (STATIC_DIR.parent / "templates").rglob("*.html")
        if re.search(r"\sstyle\s*=", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
