# Crypie

Trading-app voor [Bitvavo](https://bitvavo.com) die indicatoren, (wereld)nieuws en Claude
combineert tot handelsbeslissingen. Draait in Docker en is bedoeld voor deployment via Coolify.

## Status

| Stap | Inhoud | Status |
|------|--------|--------|
| 1 | Docker-fundament, Bitvavo-koppeling, database, dashboard | in deze PR |
| 2 | Configureerbare indicatoren, samen met Claude in te stellen, met backtests | gepland |
| 3 | Nieuws inlezen (RSS, GDELT, CryptoPanic) en door Claude laten beoordelen | gepland |
| 4 | Paper trading met logboek en evaluatielus, daarna live met harde limieten | gepland |

## Lokaal draaien

```bash
cp .env.example .env        # vul eventueel je Bitvavo-key in; paper mode werkt ook zonder
docker compose up --build
```

Dashboard: <http://localhost:8000>. API-docs: <http://localhost:8000/docs>.

Zonder Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
DATABASE_URL=sqlite:///./data/crypie.sqlite3 uvicorn app.main:app --reload
pytest
```

## Deployen op Coolify

1. Nieuwe resource: **Docker Compose** vanuit deze Git-repository, bestand `docker-compose.yml`.
2. Zet in de Coolify-omgeving minimaal `POSTGRES_PASSWORD` (sterk wachtwoord) en `TRADING_MODE=paper`.
3. Voeg `BITVAVO_API_KEY` en `BITVAVO_API_SECRET` toe. Begin met een key met alleen **view**-rechten;
   **trade**-rechten pas bij live gaan. Zet in Bitvavo een IP-whitelist op het Coolify-serveradres.
4. Koppel een domein aan de `app`-service op poort 8000. Zet het dashboard achter Coolify's
   basic-auth of een eigen login voordat je naar live gaat.

## Veiligheid

- `TRADING_MODE=paper` (standaard) plaatst nooit echte orders; fills worden gesimuleerd tegen de
  live ticker en de virtuele portefeuille staat in de database.
- `TRADING_MODE=live` weigert te starten zonder API-key en -secret.
- Elke order, paper of live, komt in de tabel `orders` met de reden erbij.

## Structuur

```
app/
  main.py            FastAPI: JSON-API (/api/...) en dashboard (/)
  config.py          instellingen uit omgevingsvariabelen
  db.py              SQLAlchemy-modellen: orders, settings
  bitvavo/client.py  Bitvavo REST-client (publiek + gesigneerd)
  bitvavo/broker.py  PaperBroker / LiveBroker
tests/               pytest (respx mockt Bitvavo)
```
