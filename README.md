# Crypie

Trading-app voor [Bitvavo](https://bitvavo.com) die indicatoren, (wereld)nieuws en Claude
combineert tot handelsbeslissingen. Draait in Docker en is bedoeld voor deployment via Coolify.

## Status

| Stap | Inhoud | Status |
|------|--------|--------|
| 1 | Docker-fundament, Bitvavo-koppeling, database, dashboard | klaar |
| 2 | Configureerbare indicatoren, samen met Claude in te stellen, met backtests en budgetbewaking | klaar |
| 3 | Nieuws inlezen (RSS, GDELT, CryptoPanic) en door Claude laten beoordelen | klaar |
| 4 | Handelsmotor: paper trading met logboek, harde limieten en evaluatielus; live na expliciete bevestiging | in deze PR |

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

De app draait als eigen Coolify-project met twee resources: een **PostgreSQL**-database en de
app als **Docker Compose**-resource vanuit deze repository.

1. Maak een PostgreSQL-resource aan (gebruiker en database `crypie`). Noteer de interne URL.
2. Nieuwe resource: **Docker Compose** vanuit deze Git-repository (branch `main`), bestand
   `docker-compose.yaml`. Dit bestand bevat alleen de app; Postgres zit in
   `docker-compose.override.yaml` en wordt alleen lokaal gebruikt.
3. Zet in de app-omgeving `DATABASE_URL` op de interne URL van de database, met het schema
   `postgresql+psycopg://` in plaats van `postgres://`. Alle andere instellingen uit
   `.env.example` worden via `${VAR:-default}` doorgegeven; een lege waarde betekent "default".
4. Voeg `BITVAVO_API_KEY`, `BITVAVO_API_SECRET` en `ANTHROPIC_API_KEY` toe en zet
   `TRADING_MODE=paper`. Begin met een Bitvavo-key met alleen **view**-rechten; **trade**-rechten
   pas bij live gaan. Zet in Bitvavo een IP-whitelist op het Coolify-serveradres.
5. Koppel een domein aan de `app`-service op poort 8000 (de poort wordt niet op de host
   gepubliceerd; alleen Coolify's proxy komt erbij). Zet het dashboard achter Coolify's
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

## Nieuws

Een achtergrondtaak haalt elke `NEWS_FETCH_INTERVAL_MIN` minuten nieuws op en laat het beoordelen:

| Bron | Kosten | Instelling |
|------|--------|------------|
| RSS (CoinDesk, Cointelegraph, The Block, Decrypt) | gratis | `NEWS_RSS_FEEDS` (komma-gescheiden) |
| GDELT DOC API (wereldnieuws, elke 15 min bijgewerkt) | gratis, max. 1 verzoek per 5 s per IP | `GDELT_ENABLED`, `GDELT_QUERY` |
| CryptoPanic v2 | betaald (Growth-plan); zonder token overgeslagen | `CRYPTOPANIC_TOKEN`, `CRYPTOPANIC_PLAN` |

Nieuwe berichten (ontdubbeld op URL, maximaal `NEWS_MAX_AGE_HOURS` oud) gaan in batches van
`NEWS_BATCH_SIZE` naar Claude (`CLAUDE_MODEL_NEWS`, standaard Haiku 5.5). Per bericht komt terug:
relevant ja/nee, sentiment (-1 tot 1), impact (0 tot 3), horizon (uren/dagen/weken), geraakte coins en
een Nederlandse samenvatting. Per coin wordt daaruit een **marktsentiment** berekend: gewogen op impact en
met vervaltijd per horizon (6 uur, 36 uur, 1 week halfwaardetijd). Dit sentiment is de input voor de
handelsbeslissing in stap 4. Pagina **/news** toont alles; API: `GET /api/news`, `GET /api/news/sentiment`,
`POST /api/news/refresh`.

## Handelen

Elke `TRADE_INTERVAL_MIN` minuten draait per markt uit `MARKETS` één cyclus (`app/trading/engine.py`):

1. **Mechanisch**: een open positie wordt gesloten als de koers de stop-loss of take-profit van de
   strategie raakt. Daar komt Claude niet aan.
2. **Niets te beslissen?** Geen buy-signaal zonder positie, geen sell-signaal met positie en geen
   zwaar negatief nieuws: dan wordt alleen een `hold` gelogd en wordt Claude niet aangeroepen (bespaart kosten).
3. **Claude beslist** (`CLAUDE_MODEL_DECISION`, Sonnet 5.5): krijgt strategie, indicatorwaarden, signaal,
   nieuwssentiment met topberichten, open positie, limieten en een samenvatting van zijn eigen recente
   beslissingen met uitkomst. Antwoord: buy/sell/hold, zekerheid, bedrag, toelichting. Kopen mag alleen bij
   een buy-signaal; verkopen bij een sell-signaal of als news exit.
4. **Harde limieten** (`app/trading/risk.py`), in code afgedwongen: `MAX_TRADE_EUR`, `MAX_EXPOSURE_PCT`,
   `DAILY_LOSS_LIMIT_PCT`, `MAX_TRADES_PER_DAY`, `COOLDOWN_MIN_AFTER_STOP`, plus een kill switch
   (pauzeren op `/trading`). Zonder Claude (budget op, geen key): `CLAUDE_UNAVAILABLE_MODE` = `hold` of `indicators`.
5. **Transactiekosten**: paper-orders betalen dezelfde taker-fee als Bitvavo (`TRADING_FEE_PCT`,
   standaard 0,25% per kant; met een live key wordt het echte tarief van je account gelezen). Winst en
   verlies zijn altijd netto na kosten, ook live (Bitvavo meldt de betaalde fee per order terug), en
   Claude krijgt de kosten mee in zijn afweging.
6. **Logboek**: elke beslissing komt in `decisions` met alle input en later de koers 1, 24 en 72 uur erna.
   Posities staan in `positions`. Deze uitkomsten gaan terug naar Claude bij de volgende beslissing: zo
   "leert" de bot zonder dat het model verandert.
7. **Evaluatie**: elke `EVALUATION_INTERVAL_HOURS` (standaard wekelijks) schrijft `CLAUDE_MODEL_EVALUATION`
   (Fable 5.1) een rapport over de resultaten en doet eventueel een strategievoorstel, inclusief backtest.
   Een voorstel wordt pas actief als jij het op `/trading` toepast.

De actieve strategie bepaalt de regels (indicatoren, condities, stop-loss, take-profit) en wordt op elke
markt uit `MARKETS` toegepast; het `market`-veld van de strategie is alleen de standaard voor backtests.

### Live gaan

1. Draai minimaal enkele weken in `TRADING_MODE=paper` en bekijk de evaluaties.
2. Maak in Bitvavo een API-key met **trade**-rechten (geen opname) en een IP-whitelist.
3. Zet `TRADING_MODE=live`, de keys en `LIVE_TRADING_ACK=true`. Zonder die bevestiging start de app niet
   in live-modus. Begin met een lage `MAX_TRADE_EUR`.

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
  db.py                     SQLAlchemy-modellen: orders, settings, claude_usage, news_items, positions, decisions, evaluations
  bitvavo/client.py         Bitvavo REST-client (publiek + gesigneerd)
  bitvavo/broker.py         PaperBroker / LiveBroker
  indicators/core.py        SMA, EMA, RSI, MACD, Bollinger (pure Python)
  strategy/model.py         strategie-schema (pydantic) en standaardstrategie
  strategy/engine.py        indicatorseries, signalen en backtester
  strategy/store.py         actieve strategie in de settings-tabel
  ai/strategy_assistant.py  Claude vertaalt een beschrijving naar een strategie (structured output)
  ai/news_analyst.py        Claude beoordeelt nieuwsberichten in batches (structured output)
  ai/decision_maker.py      Claude weegt signaal, nieuws en eigen resultaten tot een beslissing
  ai/evaluator.py           periodieke zelfevaluatie met strategievoorstel
  ai/budget.py              kosten per aanroep, maandbudget
  trading/engine.py         handelscyclus, posities, logboek, uitkomsten, evaluatie
  trading/risk.py           harde limieten en kill switch
  news/sources.py           RSS-, GDELT- en CryptoPanic-fetchers en parsers
  news/service.py           opslaan, ontdubbelen, beoordelen, marktsentiment per coin
tests/                      pytest (respx mockt Bitvavo, Claude wordt gemockt)
```
