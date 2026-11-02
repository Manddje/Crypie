# Crypie

Trading-app voor [Bitvavo](https://bitvavo.com) die indicatoren, (wereld)nieuws en Claude
combineert tot handelsbeslissingen. Draait in Docker en is bedoeld voor deployment via Coolify.

## Status

| Stap | Inhoud | Status |
|------|--------|--------|
| 1 | Docker-fundament, Bitvavo-koppeling, database, dashboard | klaar |
| 2 | Configureerbare indicatoren, samen met Claude in te stellen, met backtests en budgetbewaking | klaar |
| 3 | Nieuws inlezen (RSS, GDELT, cryptocurrency.cv, Google News) en door Claude laten beoordelen | klaar |
| 4 | Handelsmotor: paper trading met logboek, harde limieten en evaluatielus; live na expliciete bevestiging | klaar |
| 5 | Inloggen met wachtwoord en authenticator-app (MFA), herstelcodes, sessiecookie | klaar |
| 6 | Beveiliging na een audit: login en sessies, step-up, exit-lus, nieuwsfilters, container en afhankelijkheden | in deze PR |

## Lokaal draaien

```bash
cp .env.example .env        # kies een eigen POSTGRES_PASSWORD; een Bitvavo-key is optioneel
docker compose up --build
```

`DATABASE_URL` in `.env` neemt `POSTGRES_PASSWORD` over (`${POSTGRES_PASSWORD}`). Een bestaande lokale
`.env` met `crypie`/`crypie` blijft werken; een later gewijzigd wachtwoord past een bestaand
`pgdata`-volume niet meer aan.

Dashboard: <http://localhost:8000>, alleen op `127.0.0.1` (niet vanaf andere machines in je netwerk).
Bij de eerste start staat de setup-link in de log (`docker compose logs app`); zie
[Inloggen](#inloggen). De API-documentatie (`/docs`, `/redoc`, `/openapi.json`) staat uit; zet
`API_DOCS_ENABLED=true` om hem aan te zetten, ook dan alleen na inloggen.

Zonder Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"     # of precies de versies van CI: pip install --require-hashes -r requirements-dev.txt
DATABASE_URL=sqlite:///./data/crypie.sqlite3 uvicorn app.main:app --reload
pytest
```

### Afhankelijkheden

`pyproject.toml` bevat de toegestane versiebereiken. `requirements.txt` (voor het image) en
`requirements-dev.txt` (CI en ontwikkelen) zijn vastgezette lijsten met hashes; het image installeert
alleen die exacte versies, en alleen als binaire wheels. Pas je afhankelijkheden aan, maak dan beide
opnieuw met het `uv`-commando bovenin het bestand:

```bash
uv pip compile pyproject.toml --generate-hashes --python-version 3.15 \
  --python-platform x86_64-manylinux_2_28 -o requirements.txt
uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.15 \
  --python-platform x86_64-manylinux_2_28 -o requirements-dev.txt
```

Dependabot stelt wekelijks updates voor (pip, GitHub Actions en het basis-image). CI draait de tests op
Python 3.12 en 3.15, op SQLite en Postgres, plus een controle van de toeleveringsketen.
Lightweight Charts is meegeleverd (npm `lightweight-charts@5.2.1`) met zijn hash in
`app/static/vendor/SHA256SUMS`; controleer met `sha256sum --strict -c app/static/vendor/SHA256SUMS` en
werk versie en hash altijd samen bij.

## Deployen op Coolify

De app draait als eigen Coolify-project met twee resources: een **PostgreSQL**-database en de
app als **Docker Compose**-resource vanuit deze repository.

1. Maak een PostgreSQL-resource aan (gebruiker en database `crypie`). Noteer de interne URL.
2. Nieuwe resource: **Docker Compose** vanuit deze Git-repository (branch `main`), bestand
   `docker-compose.yaml`. Dit bestand bevat alleen de app; Postgres zit in
   `docker-compose.override.yaml` en wordt alleen lokaal gebruikt.
3. Zet in de app-omgeving `DATABASE_URL` op de interne URL van de database, met het schema
   `postgresql+psycopg://` in plaats van `postgres://`. Dit is verplicht: zonder `DATABASE_URL`
   faalt de deploy meteen ("required variable DATABASE_URL is missing a value"). Alle andere
   instellingen uit `.env.example` worden via `${VAR:-default}` doorgegeven; een lege waarde
   betekent "default".
4. Zet `SESSION_SECRET` op een willekeurige waarde van minstens 32 tekens (bijvoorbeeld een door
   Coolify gegenereerde `SERVICE_PASSWORD_64_...` of de uitvoer van `openssl rand -hex 32`).
5. Voeg `ANTHROPIC_API_KEY` toe en zet `TRADING_MODE=paper`. Een Bitvavo-key mag ook al, maar dan
   met alleen **view**-rechten; **trade**-rechten en `LIVE_TRADING_ACK` pas als het account beveiligd
   is en je live gaat. Zet in Bitvavo een IP-whitelist op het Coolify-serveradres.
6. Deploy en maak het account aan met de setup-link uit de log (`no account yet: open
   /setup?token=...`); zie [Inloggen](#inloggen). Koppel het publieke domein bij voorkeur pas daarna,
   of zet er tot dan basic auth van Coolify voor.
7. Koppel een eigen domein met https aan de `app`-service op poort 8000 (de poort wordt niet op de
   host gepubliceerd; alleen Coolify's proxy komt erbij). Gebruik geen gegenereerde `*.sslip.io`-naam.
   Zie [Beveiliging](#beveiliging) voor `FORWARDED_ALLOW_IPS` en `TRUSTED_ORIGINS`.

De container draait met een alleen-lezen root-bestandssysteem, een tmpfs van 64 MB op `/tmp`, zonder
capabilities en met `no-new-privileges`. Grenzen: `CRYPIE_MEM_LIMIT` (standaard `768m`, zonder extra
swap), `CRYPIE_CPUS` (standaard `1.0`) en maximaal 256 processen. Krijgt een backtest te weinig
geheugen, verhoog ze dan; zet de Resource Limits in Coolify op dezelfde waarden. Het volume
`crypie-data` op `/srv/data` is alleen nodig voor SQLite. Uvicorn draait met één worker, zonder
websockets en met maximaal 64 gelijktijdige verbindingen (daarboven een 503). Draai één instantie van
de app.

Geheimen kun je ook als bestand aanleveren (Docker/Coolify secrets): elke instelling wordt ook gelezen
uit `/run/secrets/<naam in kleine letters>`, bijvoorbeeld `/run/secrets/bitvavo_api_secret` of
`/run/secrets/anthropic_api_key`. Zo'n bestand gaat vóór de omgevingsvariabele met dezelfde naam.

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
`ANTHROPIC_API_KEY`, `CRYPTOPANIC_TOKEN`), `DATABASE_URL`, `LIVE_TRADING_ACK` en de plafonds van de
limieten. Live-modus kun je in het portaal pas kiezen als de omgeving `LIVE_TRADING_ACK=true`,
`AUTH_ENABLED=true` én een Bitvavo-sleutel heeft; anders weigert de app de wijziging en blijft hij in
oefenmodus. Dezelfde instellingen zijn er als API: `GET/PUT /api/settings`,
`DELETE /api/settings[/{key}]`.

**Huidig wachtwoord bij gevoelige wijzigingen.** Wie de modus, de markten, een harde limiet (het hele
blok Harde limieten), risk-off aan/uit, het gedrag zonder Claude, het Claude-maandbudget of de
RSS-feeds wijzigt of terugzet, moet in dat blok het veld **Huidig wachtwoord** invullen. Hetzelfde
geldt voor het resetten van de oefenportefeuille en voor handmatige orders zolang de bot live handelt.
Andere instellingen (intervallen, modellen, nieuwsbronnen aan/uit) gaan zonder wachtwoord. Bij een
ontbrekend of fout wachtwoord wordt niets opgeslagen en blijven je ingevulde waarden staan; foute
wachtwoorden tellen mee voor dezelfde wachttijd als het inloggen. Via de API stuur je het mee als
`"current_password"` in de JSON-body of in de header `X-Current-Password` (handig bij `DELETE`); zonder
het juiste wachtwoord antwoordt de API met 403. Met `AUTH_ENABLED=false` is er geen wachtwoord en
vervalt deze stap.

Grenzen bij het opslaan:

- **Plafonds uit de omgeving**: `MAX_TRADE_EUR_CEILING` (standaard 1000 euro),
  `MAX_EXPOSURE_PCT_CEILING` (100%) en `MAX_TRADES_PER_DAY_CEILING` (50). Het portaal kan ze niet
  zetten of overschrijden; een hogere waarde uit het portaal of de omgeving wordt bij het starten
  verlaagd tot het plafond (staat in de log). Wil je hoger, verhoog dan het plafond in Coolify of `.env`.
- Maximaal 20 markten, 30 feeds en 200 agendaregels. `NaN` of oneindig in een getalveld wordt geweigerd
  ("geen geldig getal").
- RSS-feeds moeten `http(s)`-URL's zijn met een publieke domeinnaam: geen IP-adressen, `localhost`,
  namen zonder punt of op `.local`, `.internal`, `.lan`, `.home.arpa` of `.intranet`, en geen
  `gebruiker:wachtwoord@` in de URL.
- Elke wijziging komt met oude en nieuwe waarde in de log (niveau INFO).

## Strategie en indicatoren

Een strategie is JSON (zie `app/strategy/model.py` voor het schema) met indicatoren (`sma`, `ema`,
`rsi`, `macd`, `bollinger`, `drawdown` = % onder de hoogste koers van N candles), koopcondities, verkoopcondities en optioneel stop-loss, trailing stop en take-profit.

De standaardstrategie koopt een RSI-dip die weer draait (zie `DEFAULT_STRATEGY`) en stapt uit op een
**trailing stop**: zodra de winst 2% is geweest schuift de stop mee tot 3% onder het hoogste punt sinds
instap en gaat nooit meer omlaag, zodat een stijging wordt uitgereden tot de koers echt terugvalt. Het
verkoopsignaal is pas "RSI boven 70 én dalend", niet zodra RSI 70 aantikt. Een backtest over twaalf
maanden (nov 2025 t/m okt 2026, een dalende markt) liet zien dat de trailing stop verliezen en drawdown
flink beperkt; de RSI-draai was in die periode ongeveer neutraal. "Standaardstrategie herstellen" op
/strategy zet deze regels terug als je eerder een eigen strategie opsloeg.
Op de pagina **/strategy** kun je:

- in gewone taal beschrijven wat je wilt; Claude vertaalt het naar een geldige strategie met toelichting
  en draait direct een backtest;
- de JSON zelf bewerken, backtesten op echte Bitvavo-candles en opslaan als actieve strategie.

De backtest is long-only, rekent Bitvavo-fees mee (`BACKTEST_FEE_PCT`), vult stop-loss, trailing stop en
take-profit op de drempelprijs en vergelijkt met kopen-en-houden. Hij toont ook **Koopsignaal live**: hoe
vaak de harde koopvoorwaarden alleen al zouden afgaan (`live_buy_signal_pct`). Boven ongeveer 20% waarschuwt
de pagina.

Controles bij opslaan of toepassen (op /strategy, via `PUT /api/strategy` en bij een evaluatievoorstel):

- een strategie moet een `stop_loss_pct` of een `trailing_stop_pct` hebben;
- harde koopvoorwaarden mogen niet altijd waar zijn (zoals `close > 0` of `rsi14 <= 100`); als zacht
  filter mag dat wel;
- indicatorperiodes zijn begrensd op 1000 candles (RSI 200, MACD-traag 500) en de totale opwarmtijd op
  1000. Een live cyclus haalt genoeg candles op voor die opwarmtijd (minstens 300, hoogstens 1440).

Een voorstel van Claude of van de evaluatie toont eerst een overzicht van wat er verandert ten opzichte van
de actieve strategie; een voorstel dat de controles niet haalt krijgt geen knop om toe te passen. Is de
opgeslagen strategie ongeldig geworden (bijvoorbeeld door strengere grenzen na een update), dan handelt de
bot met de standaardstrategie, logt dat als fout en tonen /strategy en het overzicht een waarschuwing. Sla
een geldige strategie op om dat op te heffen.

Met een open positie gaat een verkoopsignaal voor op een gelijktijdig koopsignaal. De RSI is 50 op een
volkomen vlakke koers en negeert bewegingen kleiner dan 0,01%.

## Nieuws

Een achtergrondtaak haalt elke `NEWS_FETCH_INTERVAL_MIN` minuten nieuws op en laat het beoordelen:

| Bron | Kosten | Instelling |
|------|--------|------------|
| RSS (CoinDesk, Cointelegraph, The Block, Decrypt, CryptoSlate, NewsBTC, BeInCrypto, DL News) | gratis | `NEWS_RSS_FEEDS` (komma-gescheiden) |
| cryptocurrency.cv (300+ bronnen, archief per coin, berichten komen met coin-tag) | gratis, geen sleutel; hobbyproject zonder garanties | `CRYPTOCV_ENABLED` |
| Google News zoekfeed per coin (laatste 24 uur) | gratis | `GOOGLE_NEWS_ENABLED` |
| Fear & Greed-index (alternative.me), marktbreed, als context voor Claude | gratis | `FEAR_GREED_ENABLED` |
| GDELT DOC API (wereldnieuws, elke 15 min bijgewerkt) | gratis, max. 1 verzoek per 5 s per IP | `GDELT_ENABLED`, `GDELT_QUERY` |
| CryptoPanic v2 | betaald (Growth-plan); zonder token overgeslagen | `CRYPTOPANIC_TOKEN`, `CRYPTOPANIC_PLAN` |

Nieuwe berichten (ontdubbeld op URL, maximaal `NEWS_MAX_AGE_HOURS` oud) gaan in batches van
`NEWS_BATCH_SIZE` naar Claude (`CLAUDE_MODEL_NEWS`, standaard Haiku 5.5). Per bericht komt terug:
relevant ja/nee, sentiment (-1 tot 1), impact (0 tot 3), horizon (uren/dagen/weken), geraakte coins en
een Nederlandse samenvatting, plus een `event_key` voor het nieuwsfeit (bijvoorbeeld `binance-hack`), zodat
berichten van verschillende bronnen over hetzelfde feit bij elkaar horen; Claude krijgt de sleutels van de laatste
48 uur mee om ze te hergebruiken. Per coin wordt daaruit een **marktsentiment** berekend **per nieuwsfeit** (niet
per bericht: tien bronnen over één hack wegen als één hack): gewogen op impact en met vervaltijd per horizon
(6 uur, 36 uur, 1 week halfwaardetijd). Binnen een nieuwsfeit telt **één stem per nieuwsbron** (domein), en
"n bronnen" telt verschillende bronnen, niet berichten. Een feit dat maar één bron meldt telt hoogstens als
impact 'middel'; marktbreed nieuws ('alle coins') telt pas mee als een tweede bron het meldt. Het getoonde
bericht per feit is de eerste melding. Dit sentiment is de input voor de handelsbeslissing in stap 4. Pagina
**/news** toont alles, met per nieuwsfeit of het nu **meetelt** in dat sentiment en zo niet waarom (nog niet
beoordeeld, niet koersrelevant, ouder dan 48 uur, over andere coins, impact 0, marktbreed nieuws uit één
bron); de knoppen *Telt mee* / *Telt niet mee* filteren daarop (`?telt=ja|nee`). API: `GET /api/news`
(velden `counts`, `counts_reason`; filter `?counts=true|false`), `GET /api/news/sentiment`,
`POST /api/news/refresh`.

Nieuws is onbetrouwbare invoer, dus:

- Feeds moeten publieke `http(s)`-URL's met een hostnaam zijn (geen IP-adres, geen `localhost` of interne
  namen, geen `gebruiker:wachtwoord@`). Feeds die doorsturen naar `http`, naar een privé-adres of vaker
  dan 3 keer, worden overgeslagen. Per verzoek maximaal 5 MB en 30 seconden, maximaal 200 berichten per
  antwoord. De container moet zelf DNS kunnen opvragen; met alleen een HTTP-proxy en geen lokale DNS
  weigert de controle alle feeds.
- Berichten zonder `http(s)`-link worden niet opgeslagen; links op /news openen met
  `rel="noopener noreferrer"`. Een publicatiedatum in de toekomst wordt de ophaaltijd.
- In de beslisprompt staan geen URL's of ruwe titels; nieuwsteksten staan in een apart afgebakend blok dat
  Claude als onbetrouwbaar behandelt.
- Berichten die Claude 3 keer niet kan beoordelen worden overgeslagen ("Beoordeling door Claude mislukt").
- Fear & Greed wordt genegeerd als de waarde ouder is dan 48 uur, het ophalen mislukt of de bron uit
  staat. Het label volgt uit de waarde (0-24 Extreme Fear, 25-46 Fear, 47-54 Neutral, 55-75 Greed,
  76-100 Extreme Greed).
- Bewaartermijn: nieuws 30 dagen, Claude-gebruik 400 dagen.

## Handelen

Elke `TRADE_INTERVAL_MIN` minuten draait per markt uit `MARKETS` één cyclus (`app/trading/engine.py`):

1. **Mechanisch (exit-lus)**: los van de cyclus controleert de bot elke `EXIT_CHECK_INTERVAL_MIN` minuten
   (standaard 2, instelbaar 1-60) de stop-loss, trailing stop en take-profit van **alle** open posities op
   de actuele Bitvavo-koers, en sluit een positie die zo'n drempel raakt. De trailing stop schuift mee met
   het hoogste punt sinds instap (`Position.high_price`). Daar komt Claude niet aan. De exit-lus loopt ook
   als `TRADING_ENABLED` uit staat, de kill switch aan staat of de markt uit `MARKETS` is gehaald; de kill
   switch stopt alleen nieuwe aankopen. Bitvavo kent geen stop-orders aan de beurskant: de bescherming
   werkt alleen zolang de app draait, en met een interval van 2 minuten kan een snelle crash ruim voorbij
   de stop gaan.
2. **Niets te beslissen?** Geen buy-signaal zonder positie, geen sell-signaal met positie en geen
   zwaar positief of negatief nieuws (impact-gewogen score ≥ 0,4 of ≤ -0,3 met minstens één bericht van
   hoge impact): dan wordt alleen een `hold` gelogd en wordt Claude niet aangeroepen (bespaart kosten).
3. **Claude beslist** (`CLAUDE_MODEL_DECISION`, Sonnet 5.5): krijgt strategie, indicatorwaarden, signaal,
   nieuwssentiment met topberichten, open positie, limieten en een samenvatting van zijn eigen recente
   beslissingen met uitkomst. Antwoord: buy/sell/hold, zekerheid, bedrag, toelichting. Kopen mag bij een
   buy-signaal, of zonder signaal op sterk positief nieuws van hoge impact (**news entry**, dan maximaal de
   helft van `MAX_TRADE_EUR`); verkopen bij een sell-signaal of als news exit. Een news entry of news exit
   zonder strategiesignaal mag alleen als minstens twee verschillende bronnen (andere uitgever én andere
   site) hetzelfde nieuwsfeit melden; anders blijft het `hold` ("niet bevestigd door een tweede bron").
   Slecht nieuws is voor de bot nooit een reden om "de dip te kopen".
   **Dipbewaking**: gelden de harde koopvoorwaarden behalve de `rising`/`falling`-bevestiging (de dip is er,
   de draai nog niet), dan controleert de bot die markt elke `WATCH_INTERVAL_MIN` minuten (standaard 5, 0 = uit)
   in plaats van eens per cyclus, zonder journaalregels. Zodra de bevestiging komt gaat de situatie meteen naar
   Claude; na `WATCH_MAX_MIN` minuten (standaard 180) zonder draai stopt de bewaking. Beide instelbaar in het portaal.
   Operators `rising`/`falling` vergelijken een serie met zichzelf `right` candles eerder
   (`{"left": "rsi14", "op": "rising", "right": 1}`: de RSI draait omhoog), zodat een dip pas gekocht wordt als
   de draai begonnen is. Claude krijgt daarnaast `recent_candles` (laatste 6 candles van koers en indicatoren,
   24-uurs verandering en afstand tot het dieptepunt) met de instructie niet in een nog dalende koers te kopen.
   Claude krijgt in `recent_candles.price_context` ook de korting t.o.v. het 7- en 30-daags hoog en de
   afstand tot het 30-daags gemiddelde, met de kanttekening dat lager niet de bodem is.
   **Lange-termijntrend** (`TREND_CONTEXT_ENABLED`, standaard aan): per markt haalt de bot eens per dag week- en
   maandcandles op en berekent het 200-weeks gemiddelde en de maand-RSI (PlanB-stijl). Boven het gemiddelde met
   RSI boven 50 is de lange trend "opwaarts", eronder "neerwaarts". Claude krijgt dit als `long_term_trend`
   met de instructie om in een neerwaartse lange trend terughoudender te zijn met dipaankopen; het is geen signaal.
   Markten met te weinig historie (jonge coins) krijgen "onbekend". /trading toont de tabel.
   **Risk-off** (`RISK_OFF_ENABLED`, standaard aan): rond de momenten in de agenda (`EVENT_CALENDAR`, standaard de
   FOMC-rentebesluiten en Amerikaanse CPI-cijfers t/m 2027, regels als `2026-12-09 19:00 Omschrijving` in UTC)
   koopt de bot niet van `RISK_OFF_BEFORE_MIN` (60) minuten ervoor tot `RISK_OFF_AFTER_MIN` (120) minuten erna.
   Na een nieuwsfeit met impact 3 en sentiment ≤ -0,5 (hack, beurs in problemen) pauzeert kopen
   `SHOCK_PAUSE_HOURS` (6) uur voor de geraakte coins, mits minstens twee bronnen het melden. Verkopen,
   stop-loss en take-profit blijven werken; de
   dipbewaking wordt niet gewapend. Claude ziet dit als `market_events` (actieve risk-off en de komende 72 uur);
   /trading toont de agenda en een actieve risk-off. Alles instelbaar in het portaal.
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
   (pauzeren op `/trading`, blokkeert alleen nieuwe aankopen). De daglimiet telt het gerealiseerde
   resultaat van vandaag plus het ongerealiseerde resultaat van open posities. Met `LIVE_CAPITAL_EUR`
   (standaard 0 = het hele account) rekenen blootstelling en daglimiet live met dat bedrag en alleen met
   de posities van de bot zelf. Na een order die Bitvavo of de oefenportefeuille weigert, vraagt de bot
   60 minuten geen advies aan Claude voor die markt. Zonder Claude (budget op, geen key):
   `CLAUDE_UNAVAILABLE_MODE` = `hold` (niet kopen) of `indicators` (het strategiesignaal volgen en kopen
   met de helft van `MAX_TRADE_EUR`, niet tegen sterk negatief nieuws in); een verkoopsignaal op een open
   positie wordt in beide gevallen gevolgd.
5. **Transactiekosten**: paper-orders betalen dezelfde taker-fee als Bitvavo (`TRADING_FEE_PCT`,
   standaard 0,25% per kant; met een live key wordt het echte tarief van je account gelezen). Winst en
   verlies zijn altijd netto na kosten, ook live (Bitvavo meldt de betaalde fee per order terug), en
   Claude krijgt de kosten mee in zijn afweging.
6. **Logboek**: elke beslissing komt in `decisions` met alle input en later de koers 1, 24 en 72 uur erna.
   Posities staan in `positions` en vormen het trade-journaal op `/journal`. Deze uitkomsten gaan terug naar Claude bij de volgende beslissing: zo
   "leert" de bot zonder dat het model verandert.
7. **Evaluatie**: elke `EVALUATION_INTERVAL_HOURS` (standaard wekelijks) schrijft `CLAUDE_MODEL_EVALUATION`
   (Fable 5.1) een rapport over de resultaten en doet eventueel een strategievoorstel, inclusief backtest.
   Een voorstel wordt pas actief als jij het op `/trading` toepast.

De actieve strategie bepaalt de regels (indicatoren, condities, stop-loss, trailing stop, take-profit) en wordt op elke
markt uit `MARKETS` toegepast; het `market`-veld van de strategie is alleen de standaard voor backtests.

**Handmatige orders** (overzicht en `POST /api/orders`) volgen dezelfde regels als de bot: de markt moet
in `MARKETS` staan en het bedrag ligt tussen 5 euro en het maximum per order. Een aankoop wordt
tegengehouden door de kill switch en de harde limieten, opent een beschermde positie (één per markt) en
kan niet als er al een positie of een onafgehandelde order in die markt is. Een verkoop verkleint of
sluit de positie van de bot in die markt. Zolang de bot live handelt vraagt een handmatige order om je
huidige wachtwoord.

**Orders en herstarts.** Live orders krijgen een `clientOrderId` en staan als `pending` in de database
voordat ze naar Bitvavo gaan. Na een time-out zoekt de bot de order op in plaats van hem opnieuw te sturen;
onafgehandelde orders worden bij het starten en vóór elke exit-controle afgestemd, en de markt wacht zolang.
Gedeeltelijke vullingen worden als zodanig geboekt; de rest blijft open met zijn stop. Live verkopen gebruiken
de hoeveelheid coins, afgerond naar beneden op de `quantityDecimals` van de markt en begrensd op het
beschikbare saldo. Een herstart start niet meer meteen een volledige cyclus: de taken gaan verder op hun
volgende tijdstip (bewaard in de `settings`-tabel). Draai één instantie (`--workers 1`); de database
voorkomt bovendien dat twee instanties dezelfde markt kopen.

**Database**: er is geen handmatige migratie nodig. Bij het starten worden nieuwe kolommen en indexen
toegevoegd, en op Postgres worden te korte `VARCHAR`-kolommen verbreed. Staan er in oude data twee open
posities in dezelfde markt, dan wordt de unieke index overgeslagen met een fout in de log; sluit er één
en herstart.

### Tradingplan en trade-journaal

- **`/plan` (Tradingplan)** toont op één pagina het plan dat de bot volgt: markten en modus, de harde
  limieten, de instap- en uitstapregels van de actieve strategie (hard en zacht), dipbewaking, de
  nieuwstriggers, de beslisvolgorde en de rol van Claude. Dat deel wordt uit de instellingen en de
  strategie gelezen, dus het klopt altijd met wat de bot echt doet. Daarboven staan vier vrije velden
  van jou: **doel en horizon**, **eigen regels**, **wat de bot niet doet** en **lessen**. Ze worden in de
  `settings`-tabel bewaard (`trading_plan`) en gaan als `trading_plan` mee in elke Claude-beslissing en
  elke evaluatie. Eigen regels kunnen Claude terughoudender maken, nooit de spelregels of de harde
  limieten omzeilen. API: `GET/PUT /api/plan`.
- **`/journal` (Journaal)** toont elke positie van de bot (open en gesloten) als journaalregel: instap
  en uitstap met koers, hoeveelheid, inleg, kosten en netto resultaat, de voorwaarden en de toelichting
  van Claude bij de instap en bij de uitstap, en een eigen **notitie met labels** per trade. Filters op
  markt, status, winst/verlies, sluitreden, periode, label en vrije tekst; totalen (netto resultaat,
  winstkans, winst/verlies-factor, kosten, gemiddelde looptijd, per markt). Notities staan op de positie
  zelf (`positions.note`, `positions.tags`) en gaan als `owner_notes` mee naar de evaluatie. Handmatige
  orders zonder positie staan apart onderaan. API: `GET /api/journal`, `PUT /api/journal/{id}/note`.

### Grafieken op het overzicht

- **Koers en acties**: candles van Bitvavo per markt (1 dag / 7 / 30 / 90 dagen) met daarop elke koop
  (groene pijl), elke verkoop met de reden (stop-loss, trailing stop, take-profit, signaal, nieuws),
  de Claude-adviezen die niet tot een order leidden (blauwe stip) en de kopen die een regel blokkeerde
  (oranje vierkant). Routinematige "afwachten"-cycli van de regels staan er niet op. Onder de grafiek
  dezelfde acties als lijst. API: `GET /api/chart/{markt}?period=7d`.
- **Portefeuillewaarde**: euro plus crypto tegen de slotkoers van elke candle. Er is geen
  geschiedenis-tabel: de waarde wordt vanuit het huidige saldo teruggerekend door de orders één voor
  één terug te draaien. Stortingen of opnames op een live-account zijn geen orders en zien er daarom
  uit als een sprong aan het begin van de periode. API: `GET /api/portfolio/history?period=7d`.
- Orders die nog onderweg zijn, onbekend of geweigerd zijn, of die niets gevuld hebben, tellen in beide
  grafieken niet mee.
- Tekenwerk door [Lightweight Charts](https://github.com/tradingview/lightweight-charts) (Apache 2.0),
  meegeleverd in `app/static/vendor/`; er wordt niets van een CDN geladen.

### Live gaan

1. Draai minimaal enkele weken in `TRADING_MODE=paper` en bekijk de evaluaties.
2. Zorg dat het account beveiligd is: login met authenticator, `SESSION_SECRET` gezet, eigen
   https-domein (zie [Beveiliging](#beveiliging)). Live kan alleen met `AUTH_ENABLED=true`.
3. Maak in Bitvavo een API-key met alleen **trade**-rechten (geen opname) en een IP-whitelist. Gebruik
   bij voorkeur een apart (sub)account, of zet `LIVE_CAPITAL_EUR` op het bedrag dat de bot mag beheren.
4. Zet in de omgeving de keys en `LIVE_TRADING_ACK=true`, en kies daarna op **Instellingen** de modus
   Live, met je huidige wachtwoord (of zet `TRADING_MODE=live` in de omgeving). Zonder die bevestiging
   weigert de app live-modus en draait hij in oefenmodus. Begin met een laag maximum per order.
5. Controleer met één kleine order dat Bitvavo de order met `clientOrderId` terugmeldt en dat een
   verkoop goed wordt afgerond op `quantityDecimals`; die velden zijn niet tegen de live API getest.

Terug naar oefenmodus? Open live posities blijven beschermd (via de live-broker) zolang
`BITVAVO_API_KEY`/`BITVAVO_API_SECRET` en `LIVE_TRADING_ACK=true` in de omgeving blijven staan. Haal ze
pas weg als de live posities gesloten zijn. Anders zijn die posities onbeschermd: dat staat elke controle
als fout in de log, in rood op het overzicht en op /trading, en in `/api/trading/status`
(`unprotected_positions`).

## Inloggen

Elke pagina en elk API-pad (behalve `/health` en `/static`) vraagt om een login in twee stappen:
gebruikersnaam en wachtwoord, daarna een 6-cijferige code uit een authenticator-app
(Google/Microsoft Authenticator, 1Password, Apple Wachtwoorden). Zonder login stuurt de app elke
bezoeker naar `/login`, ook als er nog geen account is; de API antwoordt dan 401.

**Eerste keer.** Het account kan alleen worden aangemaakt met de setup-link. Bij het starten zonder
account schrijft de server die in de log (niveau WARNING): `no account yet: open /setup?token=...`. Je
kunt ook zelf een vaste `SETUP_TOKEN` zetten; de log toont het token dan niet. Op `/setup?token=...` kies
je een gebruikersnaam en wachtwoord (minstens 10 tekens), scan je een QR-code met je authenticator-app en
bevestig je met een code. Je krijgt dan acht herstelcodes te zien die elk één keer werken als je telefoon
kwijt is. Zonder (geldig) token toont `/setup` alleen een verwijzing naar de serverlog; foute tokens tellen
als mislukte poging. In plaats van de link kan ook `python -m app.auth create-account <naam>` op de server:
dat vraagt het wachtwoord en toont een eenmalige code waarmee je bij de eerste login de authenticator
koppelt. Koppel het publieke domein bij voorkeur pas na de setup (of zet er tot dan basic auth van Coolify
voor), en zet `LIVE_TRADING_ACK` en een trade-key pas als het account beveiligd is.

**Account** (`/account`), steeds met je huidige wachtwoord ter bevestiging:

- **Wachtwoord wijzigen** en **Nieuwe herstelcodes**: andere browsers worden uitgelogd; deze browser
  blijft ingelogd met de oorspronkelijke verlooptijd.
- **Authenticator opnieuw instellen**: wachtwoord plus een huidige code of een herstelcode, daarna koppel
  je binnen 5 minuten de nieuwe app op `/account/mfa`. Andere browsers worden uitgelogd. *Annuleren* gaat
  terug naar /account.
- **Overal uitloggen**: beëindigt alle sessies, ook die van deze browser. *Uitloggen* in de menubalk logt
  alleen deze browser uit.

Buitengesloten? Gebruik de commando's in de container (Coolify: Terminal bij de app), zie
[Beveiliging](#beveiliging). Na `reset-mfa` of `create-account` kun je de authenticator alleen koppelen
met de eenmalige code die het commando toont (1 uur geldig). Een bestaand account zonder authenticator
(bijvoorbeeld een setup die langer dan 10 minuten geleden is afgebroken) krijgt zo'n code met
`python -m app.auth reset-mfa`.

Omgevingsvariabelen: `AUTH_SESSION_HOURS` (standaard 24), `SESSION_SECRET` (zie
[Beveiliging](#beveiliging)), `SETUP_TOKEN` (optioneel vast setup-token) en `AUTH_ENABLED=false` om de
login lokaal uit te zetten (dan kan live handelen niet).

Technisch: wachtwoord (PBKDF2 met 600.000 iteraties; een oudere hash wordt bij het inloggen bijgewerkt,
een hash onder 100.000 iteraties of een onversleuteld wachtwoord werkt niet meer), TOTP-geheim en gehashte herstelcodes staan in de tabel `auth_users`. Het TOTP-geheim staat
onversleuteld in de database. De sessie is een HMAC-gesigneerde cookie (`HttpOnly`, `SameSite=Lax`,
`Secure` en de naam `__Host-crypie_session` achter https) met een doel, account-id en versie; een
gewijzigd wachtwoord of *Overal uitloggen* maakt oude cookies ongeldig. Codes kunnen niet opnieuw
gebruikt worden binnen hun tijdvenster.

## Claude-kosten

Elke Claude-aanroep wordt met tokens en geschatte kosten opgeslagen in de tabel `claude_usage`.
Het maandbudget (standaard `CLAUDE_MONTHLY_BUDGET_USD` = 100) is een harde grens: bij 90% waarschuwt het
dashboard, bij 100% weigert de app nieuwe Claude-aanroepen tot de volgende maand. Het budget en het model
per taak (`CLAUDE_MODEL_STRATEGY`, `CLAUDE_MODEL_DECISION`, `CLAUDE_MODEL_NEWS`, `CLAUDE_MODEL_EVALUATION`)
pas je aan op **Instellingen** (het budget met je huidige wachtwoord).

- Per doel geldt een plafond: de nieuwsbeoordeling mag hoogstens 50% van het maandbudget gebruiken. Is dat
  deel op, dan blijft nieuws de rest van de maand onbeoordeeld; beslissingen werken gewoon door.
- Elke aanroep reserveert vooraf zijn maximale kosten (bijvoorbeeld ongeveer $0,08 voor een beslissing op
  Opus, $0,35 voor een evaluatie). Daardoor stopt het budget net vóór 100% in plaats van erover te gaan.
  Mislukte aanroepen die waarschijnlijk wel betaald zijn (ongeldig of afgebroken antwoord, time-out) tellen
  mee tegen de geschatte kosten.
- Een aanroep heeft een time-out van 60 seconden en 1 nieuwe poging (de strategie-assistent 180 seconden).
- `GET /api/claude/usage` toont het gebruik per doel (`by_purpose`), de plafonds (`shares`) en wat
  gereserveerd is voor lopende aanroepen (`reserved_usd`).

Zet daarnaast in de Anthropic Console een uitgavenlimiet op de API-key als tweede slot.

## Beveiliging

Crypie handelt met echt geld en draait op een publiek domein, dus het web-deel is dichtgezet.

### Wat de app doet

- **Oefenen is de standaard.** `TRADING_MODE=paper` plaatst nooit echte orders; fills worden gesimuleerd
  tegen de live ticker. Live handelen kan alleen met `LIVE_TRADING_ACK=true`, een Bitvavo-sleutel én
  `AUTH_ENABLED=true`; anders start de app in oefenmodus met een waarschuwing. Elke order, paper of live,
  komt in de tabel `orders` met de reden erbij.
- **Login en sessies**: wachtwoord plus authenticator-code (zie [Inloggen](#inloggen)). Sessiecookies zijn
  gebonden aan het account en kunnen worden ingetrokken (*Overal uitloggen*, nieuw wachtwoord, nieuwe
  herstelcodes, nieuwe authenticator, en de CLI-commando's hieronder). Na de update van deze stap moet
  iedereen één keer opnieuw inloggen.
- **Setup-token**: het account kan alleen worden aangemaakt via `/setup?token=...` (uit de log of
  `SETUP_TOKEN`) of met `create-account` op de server.
- **Step-up**: gevoelige instellingen, het resetten van de oefenportefeuille en handmatige live orders
  vragen opnieuw om je wachtwoord (zie [Instellingen](#instellingen)).
- **Wachttijd bij foute pogingen**: per clientadres (IPv6 per /64) voor het wachtwoord, en per account
  voor de tweede stap. Bewaard in de database (tabel `auth_throttle`, overleeft een herstart); mislukte
  pogingen worden na 24 uur vergeten. De wachtwoordstap wordt nooit per account geblokkeerd. Meer dan
  twee gelijktijdige wachtwoordcontroles krijgen meteen een 429 ("Het is even druk op de server").
- **Cross-site bescherming**: POST/PUT/PATCH/DELETE vanaf een andere site worden geweigerd (403). Dat
  leunt op de `Sec-Fetch-Site`-header van browsers; voor oudere browsers moet de `Origin` gelijk zijn aan
  het eigen `schema://host`. Achter een TLS-proxy moet de app de `X-Forwarded-Proto` van de proxy dus
  vertrouwen (`FORWARDED_ALLOW_IPS`), of zet je het publieke adres in `TRUSTED_ORIGINS`.
- **Headers**: een strikte Content-Security-Policy (`script-src 'self'`, geen inline scripts; alle
  JavaScript staat in `app/static/`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`,
  HSTS over https en `Cache-Control: no-store` op de inlog- en accountpagina's.
- **Grenzen**: verzoeken mogen hoogstens 16 KB zijn op de openbare inlogpagina's en 1 MB elders (anders
  413). `/health` geeft alleen `{"status": "ok"}`. De API-documentatie staat uit tenzij
  `API_DOCS_ENABLED=true`.
- **Exit-lus**: stop-loss, trailing stop en take-profit draaien altijd, los van de handelscyclus en de
  kill switch (zie [Handelen](#handelen)). Het overzicht toont in rood welke open posities niet beschermd
  kunnen worden, bijvoorbeeld live posities zonder sleutel of `LIVE_TRADING_ACK`.
- **Nieuws als onbetrouwbare invoer**: alleen publieke feeds, begrensde downloads, nieuws pas mee laten
  wegen bij een tweede bron, nieuws en vrije tekst afgebakend in de prompt (zie [Nieuws](#nieuws)). De
  CryptoPanic-token en de volledige verzoekregels van httpx komen niet meer in de log.
- **Waarschuwingen op het overzicht**: bij een ontbrekende of te korte `SESSION_SECRET`, bij onbeschermde
  posities en bij een ongeldige opgeslagen strategie.

### Checklist voor de eigenaar

1. Zet `SESSION_SECRET` in Coolify op een willekeurige waarde van minstens 32 tekens. Zonder staat de
   sleutel in de database en kan wie de database of een back-up kan lezen cookies vervalsen. Een nieuwe
   `SESSION_SECRET` logt iedereen uit en maakt openstaande herstelcodes en koppelcodes ongeldig (TOTP blijft
   werken; maak daarna nieuwe herstelcodes).
2. Draai Crypie op een eigen https-domein in plaats van een gegenereerde `*.sslip.io`-naam.
3. Zet zo nodig `FORWARDED_ALLOW_IPS` op het netwerk van Coolify's Traefik (standaard loopback en de
   privé-reeksen), nooit op `*`: anders kan een bezoeker zijn eigen IP-adres kiezen en de wachttijd
   omzeilen. Staat Cloudflare ervoor, voeg dan de IP-reeksen van Cloudflare toe, anders delen alle bezoekers
   één wachttijd. Het client-IP is het meest rechtse adres in `X-Forwarded-For` dat niet in de lijst staat.
4. Haal bij de eerste deploy de setup-link uit de log en maak het account meteen aan.
5. Vervang je CryptoPanic-token bij CryptoPanic: eerdere versies schreven hem leesbaar in de log.
6. Gebruik een Bitvavo-key met alleen de nodige rechten (view, en trade pas bij live; nooit opnemen) en
   een IP-whitelist op het serveradres.
7. Optioneel: zet `LIVE_CAPITAL_EUR`, en laat de bot live werken op een apart Bitvavo-(sub)account.
8. In GitHub: zet de workflow-rechten van Actions op 'Read' en bescherm de `main`-branch.

### Commando's op de server

In de container (Coolify: Terminal bij de app); `python -m app.auth ...` werkt vanuit elke map.

```bash
python -m app.auth create-account <naam>   # account aanmaken in plaats van /setup; toont een koppelcode
python -m app.auth reset-password          # nieuw wachtwoord (twee keer vragen, of één regel van stdin:
                                           #   echo 'wachtwoord' | python -m app.auth reset-password)
python -m app.auth reset-mfa               # authenticator en herstelcodes wissen; toont een koppelcode (1 uur)
python -m app.auth revoke-sessions         # iedereen uitloggen
python -m app.auth clear-throttle          # alle blokkades na foute pogingen opheffen
python -m app.auth delete-account [--yes]  # account wissen (vraagt om bevestiging zonder --yes)
```

`reset-password`, `reset-mfa` en `delete-account` loggen ook alle browsers uit. Het wachtwoord gaat nooit
als argument mee (het zou in de shell-geschiedenis en de proceslijst terechtkomen).

### Nieuwe instellingen

| Variabele | Standaard | Betekenis |
|-----------|-----------|-----------|
| `SESSION_SECRET` | leeg | Sleutel voor cookies en herstelcodes; zet minstens 32 tekens |
| `SETUP_TOKEN` | leeg | Vast setup-token; leeg = bij elke start een nieuw token in de log |
| `TRUSTED_ORIGINS` | leeg | Extra origins die mogen posten (komma-gescheiden), bijvoorbeeld `https://crypie.example.nl`; handig als de proxy het https-schema niet doorgeeft |
| `API_DOCS_ENABLED` | `false` | `/docs`, `/redoc` en `/openapi.json` aan (ook dan achter de login) |
| `FORWARDED_ALLOW_IPS` | loopback en privé-reeksen | Proxies die `X-Forwarded-For`/`-Proto` mogen zetten |
| `EXIT_CHECK_INTERVAL_MIN` | 2 | Minuten tussen controles van stop-loss en take-profit (1-60; ook in het portaal) |
| `LIVE_CAPITAL_EUR` | 0 | Bedrag dat de bot live beheert; 0 = het hele account (ook in het portaal) |
| `MAX_TRADE_EUR_CEILING` | 1000 | Plafond voor het maximum per order, alleen via de omgeving |
| `MAX_EXPOSURE_PCT_CEILING` | 100 | Plafond voor het percentage in crypto, alleen via de omgeving |
| `MAX_TRADES_PER_DAY_CEILING` | 50 | Plafond voor het aantal trades per dag, alleen via de omgeving |
| `CRYPIE_MEM_LIMIT` / `CRYPIE_CPUS` | `768m` / `1.0` | Grenzen van de container (docker-compose) |

## Structuur

```
app/
  main.py                   FastAPI: JSON-API (/api/...), dashboard (/), /trading, /journal, /plan, /strategy, /news, /settings
  config.py                 instellingen uit omgevingsvariabelen (en /run/secrets)
  settings_store.py         instellingen uit het portaal: validatie, plafonds, gevoelige sleutels
  security.py               middlewares: security-headers, cross-site bescherming, grenzen aan request bodies
  db.py                     SQLAlchemy-modellen: orders, settings, claude_usage, news_items, positions, decisions, evaluations, auth_users, auth_throttle
  auth/                     login: wachtwoord (passwords.py), TOTP (totp.py), cookie (session.py), account (service.py), routes + middleware (routes.py), CLI (__main__.py)
  bitvavo/client.py         Bitvavo REST-client (publiek + gesigneerd)
  bitvavo/broker.py         PaperBroker / LiveBroker
  indicators/core.py        SMA, EMA, RSI, MACD, Bollinger, drawdown (pure Python)
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
  static/                   CSS en JavaScript (geen inline scripts), meegeleverde Lightweight Charts in vendor/
  trading/plan.py           tradingplan: doel, eigen regels en lessen van de eigenaar (/plan), context voor Claude
  trading/journal.py        trade-journaal: posities + beslissingen + eigen notities (/journal)
  trading/history.py        data voor de grafieken op het overzicht: acties per markt, portefeuillewaarde terugrekenen
  news/sources.py           RSS-, GDELT-, cryptocurrency.cv-, Google News- en CryptoPanic-fetchers
  news/service.py           opslaan, ontdubbelen, beoordelen, marktsentiment per coin
tests/                      pytest (respx mockt Bitvavo, Claude wordt gemockt)
```
