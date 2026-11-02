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
