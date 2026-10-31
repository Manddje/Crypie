# Crypie

Trading-app voor [Bitvavo](https://bitvavo.com) die indicatoren, (wereld)nieuws en Claude
combineert tot handelsbeslissingen. Draait in Docker en is bedoeld voor deployment via Coolify.

## Status

| Stap | Inhoud | Status |
|------|--------|--------|
| 1 | Docker-fundament, Bitvavo-koppeling, database, dashboard | klaar |
| 2 | Configureerbare indicatoren, samen met Claude in te stellen, met backtests en budgetbewaking | in deze PR |
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

## Strategie en indicatoren

Een strategie is JSON (zie `app/strategy/model.py` voor het schema) met indicatoren (`sma`, `ema`,
`rsi`, `macd`, `bollinger`), koopcondities, verkoopcondities en optioneel stop-loss en take-profit.
Op de pagina **/strategy** kun je:

- in gewone taal beschrijven wat je wilt; Claude vertaalt het naar een geldige strategie met toelichting
  en draait direct een backtest;
- de JSON zelf bewerken, backtesten op echte Bitvavo-candles en opslaan als actieve strategie.

De backtest is long-only, rekent Bitvavo-fees mee (`BACKTEST_FEE_PCT`), vult stop-loss en
take-profit op de drempelprijs en vergelijkt met kopen-en-houden.

## Claude-kosten

Elke Claude-aanroep wordt met tokens en geschatte kosten opgeslagen in de tabel `claude_usage`.
`CLAUDE_MONTHLY_BUDGET_USD` (standaard 100) is een harde maandgrens: bij 90% waarschuwt het dashboard,
bij 100% weigert de app nieuwe Claude-aanroepen tot de volgende maand. Het model per taak is instelbaar:
`CLAUDE_MODEL_STRATEGY`, `CLAUDE_MODEL_DECISION`, `CLAUDE_MODEL_NEWS`, `CLAUDE_MODEL_EVALUATION`.
Zet daarnaast in de Anthropic Console een uitgavenlimiet op de API-key als tweede slot.

## Veiligheid

- `TRADING_MODE=paper` (standaard) plaatst nooit echte orders; fills worden gesimuleerd tegen de
  live ticker en de virtuele portefeuille staat in de database.
- `TRADING_MODE=live` weigert te starten zonder API-key en -secret.
- Elke order, paper of live, komt in de tabel `orders` met de reden erbij.

## Structuur

```
app/
  main.py                   FastAPI: JSON-API (/api/...), dashboard (/) en strategiepagina (/strategy)
  config.py                 instellingen uit omgevingsvariabelen
  db.py                     SQLAlchemy-modellen: orders, settings, claude_usage
  bitvavo/client.py         Bitvavo REST-client (publiek + gesigneerd)
  bitvavo/broker.py         PaperBroker / LiveBroker
  indicators/core.py        SMA, EMA, RSI, MACD, Bollinger (pure Python)
  strategy/model.py         strategie-schema (pydantic) en standaardstrategie
  strategy/engine.py        indicatorseries, signalen en backtester
  strategy/store.py         actieve strategie in de settings-tabel
  ai/strategy_assistant.py  Claude vertaalt een beschrijving naar een strategie (structured output)
  ai/budget.py              kosten per aanroep, maandbudget
tests/                      pytest (respx mockt Bitvavo, Claude wordt gemockt)
```
