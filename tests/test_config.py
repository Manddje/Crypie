from app.config import Settings


def test_comma_lists_are_split(monkeypatch):
    monkeypatch.setenv("MARKETS", "btc-eur, sol-eur")
    monkeypatch.setenv("NEWS_RSS_FEEDS", "https://a.example/rss, https://b.example/feed")
    s = Settings(_env_file=None)
    assert s.markets == ["BTC-EUR", "SOL-EUR"]
    assert s.coins == ["BTC", "SOL"]
    assert s.news_rss_feeds == ["https://a.example/rss", "https://b.example/feed"]


def test_empty_env_values_fall_back_to_defaults(monkeypatch):
    # Coolify passes every compose variable, so an unset value arrives as "".
    monkeypatch.setenv("MARKETS", "")
    monkeypatch.setenv("NEWS_RSS_FEEDS", " ")
    s = Settings(_env_file=None)
    assert s.markets == ["BTC-EUR", "ETH-EUR"]
    assert len(s.news_rss_feeds) == 4
    assert all(f.startswith("https://") for f in s.news_rss_feeds)
