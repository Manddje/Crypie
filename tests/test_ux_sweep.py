"""The final accessibility, visual and wording sweep over every page (UX audit, sweep): shared
components, the page frame, and wording that every page shows the same way."""

from types import SimpleNamespace

from starlette.datastructures import State

from app.web import STATIC_DIR, templates


def _request(path="/trading", user=None):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(user=user, flashes=[]),
        app=SimpleNamespace(state=State()),
    )


def _settings(markets=("BTC-EUR", "ETH-EUR"), mode="paper"):
    return SimpleNamespace(
        trading_mode=mode,
        markets=list(markets),
        has_bitvavo_credentials=False,
        anthropic_api_key="",
    )


def _render(body: str = "", **ctx) -> str:
    page = (
        '{% extends "base.html" %}{% import "_macros.html" as ui %}'
        "{% block title %}Crypie · Proef{% endblock %}{% block content %}" + body + "{% endblock %}"
    )
    ctx.setdefault("request", _request())
    ctx.setdefault("settings", _settings())
    ctx.setdefault("paused", False)
    return templates.env.from_string(page).render(**ctx)


def test_the_footer_names_a_few_markets_and_counts_the_rest():
    """UX-95: with 20 markets the footer stays one line."""
    few = _render()
    assert "Markten: BTC-EUR, ETH-EUR · " in few
    markets = [f"C{i:02d}-EUR" for i in range(20)]
    many = _render(settings=_settings(markets))
    assert "Markten: C00-EUR, C01-EUR, C02-EUR en 17 andere · " in many
    assert "C03-EUR" not in many
    assert '<a href="/settings#trading">aanpassen</a>' in many


def test_the_account_icon_is_marked_on_the_account_page():
    """UX-65: the account icon is the current page on /account, like a navigation link."""
    on = _render(request=_request("/account", user="kris"))
    assert 'class="icon-btn active" href="/account"' in on and 'aria-current="page"' in on
    off = _render(request=_request("/settings", user="kris"))
    assert 'class="icon-btn" href="/account"' in off


def test_the_navigation_pill_carries_the_kill_switch_text_for_app_js():
    """app.js updates the pill when the kill switch changes in another tab."""
    html = _render()
    assert 'data-paused-text="Noodstop actief: kopen gepauzeerd"' in html
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "/api/status" in app_js and "crypie:status" in app_js
    assert "fetch(" not in (STATIC_DIR / "trading.js").read_text(encoding="utf-8")


def test_shared_components_live_in_one_place():
    """One pager, one counter in segmented filters, one mode colour in running text."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for gone in (".news-pager", ".count-badge", ".trading-mode", ".strategy-mode", ".pill-blue"):
        assert gone not in css, gone
    components = css[css.index("==== 5. Onderdelen") : css.index("==== 6. Hulpklassen")]
    for shared in (".pager", ".segmented .count", ".mode-text", ".page-help", ".filter-chips"):
        assert shared in components, shared
    footer = css[css.index("==== 7. Voettekst") : css.index("==== 8. Pagina")]
    assert ".footer-alert" in footer
    # The analysis pages need no script of their own for folding and counters any more.
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "data-collapse-mobile" in app_js and "data-char-count" in app_js
    assert not (STATIC_DIR / "analysis.js").exists()
