import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("MARKETS", "BTC-EUR,ETH-EUR")
os.environ["BITVAVO_API_KEY"] = ""
os.environ["BITVAVO_API_SECRET"] = ""
