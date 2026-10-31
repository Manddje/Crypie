# Crypie

Trading-app voor [Bitvavo](https://bitvavo.com) die indicatoren, (wereld)nieuws en Claude
combineert tot handelsbeslissingen. Draait in Docker en is bedoeld voor deployment via Coolify.

## Status

| Stap | Inhoud | Status |
|------|--------|--------|
| 1 | Docker-fundament, Bitvavo-koppeling, database, dashboard | klaar |
| 2 | Configureerbare indicatoren, samen met Claude in te stellen, met backtests en budgetbewaking | klaar |
| 3 | Nieuws inlezen (RSS, GDELT, CryptoPanic) en door Claude laten beoordelen | klaar |
| 4 | Handelsmotor: paper trading met logboek, harde limieten en evaluatielus; live na expliciete bevestiging | klaar |
| 5 | Inloggen met wachtwoord en authenticator-app (MFA), herstelcodes, sessiecookie | in deze PR |

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
   gepubliceerd; alleen Coolify's proxy komt erbij).
6. Open het domein: de eerste bezoeker komt op `/setup` en maakt daar het account aan. Doe dat
   dus meteen na de eerste deploy; zie [Inloggen](#inloggen).

## Instellingen

Alles wat met handelen te maken heeft stel je in het portaal in, op de pagina **Instellingen**
(`/settings`): modus (oefen/live), markten, interval, startbedrag, de harde limieten, het Claude-model per
taak en het maandbudget, en de nieuwsbronnen. Wat je daar opslaat:

- geldt direct, zonder herstart (de schedulers en de handelsmotor lezen de nieuwe waarden binnen een
  halve minuut);
- staat in de database (tabel `settings`, sleutels `config:*`) en overleeft dus een nieuwe deploy;
- gaat vóór de omgevingsvariabelen. De waarden uit Coolify of `.env` zijn alleen nog de standaard; per
  instelling zie je de standaardwaarde en kun je per blok terug naar standaard.

Niet in het portaal, met opzet: de sleutels (`BITVAVO_API_KEY`, `BITVAVO_API_SECRET`,
`ANTHROPIC_API_KEY`, `CRYPTOPANIC_TOKEN`), `DATABASE_URL` en `LIVE_TRADING_ACK`. Live-modus kun je in het
portaal pas kiezen als de omgeving `LIVE_TRADING_ACK=true` én een Bitvavo-sleutel heeft; anders weigert
de app de wijziging en blijft hij in oefenmodus. Dezelfde instellingen zijn er als API:
`GET/PUT /api/settings`, `DELETE /api/settings[/{key}]`.

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
   zwaar positief of negatief nieuws (impact-gewogen score ≥ 0,4 of ≤ -0,3 met minstens één bericht van
   hoge impact): dan wordt alleen een `hold` gelogd en wordt Claude niet aangeroepen (bespaart kosten).
3. **Claude beslist** (`CLAUDE_MODEL_DECISION`, Sonnet 5.5): krijgt strategie, indicatorwaarden, signaal,
   nieuwssentiment met topberichten, open positie, limieten en een samenvatting van zijn eigen recente
   beslissingen met uitkomst. Antwoord: buy/sell/hold, zekerheid, bedrag, toelichting. Kopen mag bij een
   buy-signaal, of zonder signaal op sterk positief nieuws van hoge impact (**news entry**, dan maximaal de
   helft van `MAX_TRADE_EUR`); verkopen bij een sell-signaal of als news exit. Slecht nieuws is voor de bot
   nooit een reden om "de dip te kopen".
   **Dipbewaking**: gelden de harde koopvoorwaarden behalve de `rising`/`falling`-bevestiging (de dip is er,
   de draai nog niet), dan controleert de bot die markt elke `WATCH_INTERVAL_MIN` minuten (standaard 5, 0 = uit)
   in plaats van eens per cyclus, zonder journaalregels. Zodra de bevestiging komt gaat de situatie meteen naar
   Claude; na `WATCH_MAX_MIN` minuten (standaard 180) zonder draai stopt de bewaking. Beide instelbaar in het portaal.
   Operators `rising`/`falling` vergelijken een serie met zichzelf `right` candles eerder
   (`{"left": "rsi14", "op": "rising", "right": 1}`: de RSI draait omhoog), zodat een dip pas gekocht wordt als
   de draai begonnen is. Claude krijgt daarnaast `recent_candles` (laatste 6 candles van koers en indicatoren,
   24-uurs verandering en afstand tot het dieptepunt) met de instructie niet in een nog dalende koers te kopen.
   Een voorwaarde met `"soft": true` is een **zacht filter**: het signaal ontstaat ook als het niet geldt,
   Claude ziet dat in `signal_checks` en `soft_filters_failed` en koopt dan hoogstens de helft van
   `MAX_TRADE_EUR`. De standaardstrategie gebruikt dat voor de EMA200-trendfilter, zodat een RSI-dip in een
   dalende markt wél aan Claude wordt voorgelegd. De backtest kent geen Claude en rekent zachte filters als hard.
   Elke cyclus komt per markt in het journaal: signaal, de uitkomst van iedere strategievoorwaarde met de
   echte waarden (bijvoorbeeld "rsi14 lager dan 35 ✗ (52,2 t.o.v. 35)"), nieuws, besluit en toelichting.
   Pagina `/trading` toont de laatste cyclus per markt en alle cycli van de laatste
   `DECISION_HISTORY_DAYS` dagen (standaard 7, instelbaar in het portaal).
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
3. Zet in de omgeving de keys en `LIVE_TRADING_ACK=true`, en kies daarna op **Instellingen** de modus
   Live (of zet `TRADING_MODE=live` in de omgeving). Zonder die bevestiging weigert de app live-modus en
   draait hij in oefenmodus. Begin met een laag maximum per order.

## Inloggen

Elke pagina en elk API-pad (behalve `/health` en `/static`) vraagt om een login in twee stappen:
gebruikersnaam en wachtwoord, daarna een 6-cijferige code uit een authenticator-app
(Google/Microsoft Authenticator, 1Password, Apple Wachtwoorden).

Zolang er geen account is, stuurt de app elke bezoeker naar **`/setup`**: daar kies je een
gebruikersnaam en wachtwoord (minstens 10 tekens), scan je een QR-code met je authenticator-app
en bevestig je met een code. Je krijgt dan acht herstelcodes te zien die elk één keer werken als
je telefoon kwijt is. Doe de setup dus direct na de eerste deploy, voordat iemand anders dat doet.

Op de pagina **Account** wijzig je je wachtwoord, maak je nieuwe herstelcodes of koppel je een
nieuwe authenticator (steeds met je huidige wachtwoord ter bevestiging). Buitengesloten? Draai in
de container (Coolify: Terminal bij de app):

```bash
python -m app.auth reset-password   # nieuw wachtwoord
python -m app.auth reset-mfa        # authenticator en herstelcodes wissen; opnieuw koppelen bij login
python -m app.auth delete-account   # alles wissen; /setup is dan weer beschikbaar
```

Omgevingsvariabelen zijn optioneel: `AUTH_SESSION_HOURS` (standaard 24), `SESSION_SECRET` (leeg =
eenmalig gegenereerd en in de database bewaard) en `AUTH_ENABLED=false` om de login lokaal uit te
zetten.

Technisch: wachtwoord (PBKDF2), TOTP-geheim en gehashte herstelcodes staan in de tabel
`auth_users`; de sessie is een HMAC-gesigneerde cookie (`HttpOnly`, `SameSite=Lax`, `Secure`
achter https). Na vijf mislukte pogingen per IP-adres volgt een oplopende wachttijd. Codes kunnen
niet opnieuw gebruikt worden binnen hun tijdvenster.

## Claude-kosten

Elke Claude-aanroep wordt met tokens en geschatte kosten opgeslagen in de tabel `claude_usage`.
Het maandbudget (standaard `CLAUDE_MONTHLY_BUDGET_USD` = 100) is een harde grens: bij 90% waarschuwt het
dashboard, bij 100% weigert de app nieuwe Claude-aanroepen tot de volgende maand. Het budget en het model
per taak (`CLAUDE_MODEL_STRATEGY`, `CLAUDE_MODEL_DECISION`, `CLAUDE_MODEL_NEWS`, `CLAUDE_MODEL_EVALUATION`)
pas je aan op **Instellingen**.
Zet daarnaast in de Anthropic Console een uitgavenlimiet op de API-key als tweede slot.

## Veiligheid

- `TRADING_MODE=paper` (standaard) plaatst nooit echte orders; fills worden gesimuleerd tegen de
  live ticker en de virtuele portefeuille staat in de database.
- `TRADING_MODE=live` weigert te starten zonder API-key en -secret.
- Elke order, paper of live, komt in de tabel `orders` met de reden erbij.
- Het dashboard en de API zitten achter een login met wachtwoord en authenticator-code; zie
  [Inloggen](#inloggen).

## Structuur

```
app/
  main.py                   FastAPI: JSON-API (/api/...), dashboard (/) en strategiepagina (/strategy)
  config.py                 instellingen uit omgevingsvariabelen
  db.py                     SQLAlchemy-modellen: orders, settings, claude_usage, news_items, positions, decisions, evaluations, auth_users
  auth/                     login: wachtwoord (passwords.py), TOTP (totp.py), cookie (session.py), account (service.py), routes + middleware (routes.py), CLI (__main__.py)
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
