import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("MARKETS", "BTC-EUR,ETH-EUR")
os.environ.setdefault("AUTH_ENABLED", "false")  # tests/test_auth.py switches it on itself
os.environ["BITVAVO_API_KEY"] = ""
os.environ["BITVAVO_API_SECRET"] = ""


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """News fetches resolve host names to check they are public (SSRF guard). Tests mock HTTP
    with respx and must not depend on real DNS: every name resolves to one fixed
    public address unless a test patches ``resolve_host`` itself."""
    from app.news import sources

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(sources, "resolve_host", resolve)


@pytest.fixture(autouse=True)
def _no_market_list(monkeypatch):
    """A settings save checks new markets against Bitvavo's market list (app.main). Tests do not
    reach Bitvavo for that: the list counts as unavailable (no check) unless a test sets one."""
    from app import main

    async def unavailable(client) -> None:
        return None

    monkeypatch.setattr(main, "fetch_market_names", unavailable)
    main.app.state.market_names = None
