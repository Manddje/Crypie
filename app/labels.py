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
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"


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
    """'na 6 orders op een dag koopt de bot niets meer; kopen en verkopen tellen allebei, ...'."""
    return (
        f"na {n} {'order' if n == 1 else 'orders'} op een dag koopt de bot tot 00:00 UTC niets "
        "meer; kopen en verkopen tellen allebei, ook handmatige orders en verkopen door een "
        "stop. Verkopen blijft altijd mogelijk"
    )


IMPACT_NL = {0: "geen", 1: "klein", 2: "middel", 3: "groot"}
