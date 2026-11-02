"""Dutch labels that both the pages and the trading code need.

A neutral home (no web or database imports), so app.web and app.trading.journal share one list
instead of keeping their own copies.
"""

from __future__ import annotations

# Position.close_reason -> how a closed trade ended, as a phrase ('Reden: trailing stop geraakt').
# One list for every page; the short labels for table cells stay in app.web.LABELS_NL.
CLOSE_REASONS_NL: dict[str, str] = {
    "stop_loss": "stop-loss geraakt",
    "trailing_stop": "trailing stop geraakt",
    "take_profit": "take-profit geraakt",
    "signal": "verkoopsignaal van de strategie",
    "news_exit": "verkocht op negatief nieuws",
    "manual": "handmatig verkocht",
    "reset": "oefenportefeuille gereset",
}


def _pct(value: float) -> str:
    """A setting or limit as typed: '5%', '2,5%', '0,25%' (no rounding to whole numbers, so a
    page never states a different limit than the one enforced)."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text.replace(".", ",") + "%"


limit_pct = _pct


# The hard limits in one wording for every page (UX-105). Mirrors app/trading/risk.py:
# check_buy() counts today's orders and today's realized result plus open losses.
def daily_loss_rule_nl(pct: float) -> str:
    """'bij 5% verlies op één dag (gerealiseerd plus het verlies op open posities) koopt de bot
    tot 00:00 UTC niets meer'."""
    return (
        f"bij {_pct(pct)} verlies op één dag (gerealiseerd plus het verlies op open posities) "
        "koopt de bot tot 00:00 UTC niets meer"
    )


def max_orders_rule_nl(n: int) -> str:
    """'na 6 orders op een dag koopt de bot niets meer; kopen en verkopen tellen allebei, ...'.
    At 0 nothing is bought at all, so no promise that buying comes back at midnight."""
    if n <= 0:
        return (
            "op 0 gezet: er wordt niets gekocht, door de bot en ook niet handmatig, tot je het "
            "maximum verhoogt. Verkopen blijft altijd mogelijk"
        )
    return (
        f"na {n} {'order' if n == 1 else 'orders'} op een dag koopt de bot tot 00:00 UTC niets "
        "meer; kopen en verkopen tellen allebei, ook handmatige orders en verkopen door een "
        "stop. Verkopen blijft altijd mogelijk"
    )


def orders_limit_nl(today: int, maximum: int) -> str:
    """Why buying stopped at the daily maximum, without '7 van 6': sells stay allowed and
    count too, so the count can pass the maximum. 'het maximum aantal orders per dag is
    bereikt (7 orders vandaag, maximum 6); kopen kan weer vanaf 00:00 UTC'."""
    if maximum <= 0:
        return (
            "het maximum aantal orders per dag staat op 0, dus er wordt niets gekocht tot je "
            "het in Instellingen verhoogt"
        )
    return (
        f"het maximum aantal orders per dag is bereikt ({today} "
        f"{'order' if today == 1 else 'orders'} vandaag, maximum {maximum}); kopen kan weer "
        "vanaf 00:00 UTC"
    )


IMPACT_NL = {0: "geen", 1: "klein", 2: "middel", 3: "groot"}

# What the kill switch does, in one wording for every page, the status and /api/status
# (UX-09, UX-105). Mirrors the engine: risk.check_buy refuses every buy while paused (bot and
# manual); the exit loop and sells on a strategy sell signal keep running; a sell on negative
# news alone is held (TradingEngine, 'de noodstop staat aan: een verkoop alleen op ...').
KILL_SWITCH_HELP_NL = (
    "De noodstop blokkeert nieuwe aankopen, van de bot en handmatig. Stop-loss, trailing stop, "
    "take-profit en verkopen op een verkoopsignaal blijven werken; alleen een verkoop op "
    "negatief nieuws wacht tot je hervat."
)
