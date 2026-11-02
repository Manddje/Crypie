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
| 7 | Gebruiksvriendelijkheid na een UX-audit (`docs/ux-audit.md`): één status, noodstop, meldingen buiten de app, foutpagina's, toegankelijkheid | in deze PR |

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

## Het portaal

Het dashboard is Nederlandstalig en werkt ook zonder JavaScript; JavaScript maakt het alleen prettiger.

- **Modus.** De navigatie toont altijd *Oefenmodus* (indigo) of *Live* (rood). In live staat op elke
  pagina bovendien een rode balk "LIVE · echt geld op Bitvavo", begint de tabtitel met "LIVE · " en
  heeft het tabbladicoon een rode stip. De ondertitels van Overzicht, Handelen en Strategie noemen de
  modus, en bevestigingen in live beginnen met "LIVE · echt geld:". Zie [de noodstop](#handelen) voor
  wat de navigatie toont als kopen gepauzeerd is.
- **Tijden** staan in de HTML in UTC, met "UTC" erachter. De browser zet ze om naar je eigen tijdzone,
  met de naam van de zone (bijvoorbeeld "2 nov 15:05 CET"); de UTC-tijd staat dan in de tooltip. Zonder
  JavaScript blijft de UTC-tijd staan. Relatieve tijden ("5 min geleden", "over 43 min") lopen mee. De
  grafieken tonen lokale tijd, dagcandles zijn UTC-dagen. De agenda voer je in UTC in, en de
  dagtellers (dagverlies, orders per dag) beginnen om 00:00 UTC.
- **Na een actie** (opslaan, noodstop, cyclus, order, strategie, nieuws verversen) stuurt de app je
  terug naar de pagina (Post/Redirect/Get), met één melding bovenaan of in het blok of de kaart waar het
  om ging. De melding verschijnt één keer: vernieuwen herhaalt de actie niet en toont de melding niet
  opnieuw. Een formulier is na de eerste klik even vergrendeld, met een tekst op de knop zoals
  "Cyclus loopt…", zodat een dubbele klik niets twee keer doet. Een GET op een adres dat alleen
  formulieren ontvangt (bijvoorbeeld `/orders` of `/trading/action`) gaat naar de bijbehorende pagina.
  Een geweigerd formulier gaat terug naar zijn pagina met de reden; API-clients (zonder `text/html` in
  `Accept`) houden hun statuscode (400, 403, 409, 429, 502).
- **Foutpagina's** (404, 405, 400, 500 en 502 als Bitvavo niet reageert) zijn gewone pagina's met een
  weg terug. `/api/*` en andere clients krijgen JSON (`{"detail": ...}`). Wie niet is ingelogd, ziet de
  foutpagina in de opmaak van de inlogpagina, zonder gegevens van de instantie. Met
  `AUTH_ENABLED=false` sturen `/login`, `/setup`, `/account` en de MFA-pagina's door naar het overzicht
  met een melding. `GET /logout` toont een knop om uit te loggen.
- **Thema**: de knop rechtsboven wisselt tussen automatisch (volgt je systeem), licht en donker; de
  kleur van de browserbalk volgt mee. De app heeft een manifest en iconen, zodat je hem op het
  beginscherm van je telefoon kunt zetten.
- **Begrippen**: Handelen, Tradingplan en Strategie hebben onder de titel een uitklapblok *Begrippen
  uitgelegd* (order, positie, trade, candle, RSI, EMA, zacht filter, stop-loss, trailing stop,
  take-profit, dipbewaking, risk-off, netto en bruto, noodstop). Een *order* is één koop- of
  verkoopopdracht, een *positie* is wat de bot in een markt bezit, een *trade* is één positie van aankoop
  tot verkoop.
- **Adressen** die erbij kwamen: `GET /api/status` (zie [Status en meldingen](#status-en-meldingen)),
  `GET /api/dashboard/status` (het statusblok van het overzicht), `POST /dashboard/getting-started`
  (*Aan de slag* verbergen), `POST /settings/bitvavo-test` en `POST /settings/notify-test`,
  `GET /strategy?result=…`, `POST /strategy` met `action=restore_previous`, `base_version` en `market`,
  en `/trading/action` met `revert:<id>`. `POST /orders` neemt ook `sell_all` en `order_token`;
  `POST /api/backtest` geeft ook `first_time`, `last_time`, `wins` en `curve`.
- **Toegankelijkheid**: de pagina's zijn met axe-core gecontroleerd op de regels van WCAG 2.2 AA (licht
  en donker, op 1440 en 390 px), met een link *Naar de inhoud*, zichtbare focus, tekst bij ✓/✗/~ voor
  schermlezers en een tabel onder de koersgrafiek. Op 320 px breed scrolt geen pagina zijwaarts.

## Instellingen

Alles wat met handelen te maken heeft stel je in het portaal in, op de pagina **Instellingen**
(`/settings`). De pagina heeft een lijst met links naar de blokken (vanaf 1100 px breed een vaste kolom
links) en per blok een eigen opslaanknop (bijvoorbeeld *Handelen opslaan*); opslaan geldt alleen voor
dat blok:

- **Handelen**, in delen: *Modus en markten* (modus, automatisch handelen, markten, interval), *Kopen*
  (gedrag zonder Claude, lange-termijntrend, dipbewaking), *Bescherming* (exit-controle, risk-off,
  schok-pauze, agenda) en *Weergave* (hoeveel dagen beslissingen op Handelen staan);
- **Harde limieten**;
- **Kosten**: handelskosten per order en handelskosten in backtests;
- **Claude**: maandbudget, model per taak en evaluatie-interval;
- **Nieuws**: ophalen, beoordelen en bronnen;
- **Oefenportefeuille**: het startbedrag, dat samen met de reset wordt opgeslagen;
- **Uit de omgeving**: alleen ter informatie (zie hieronder).

Wat je opslaat:

- geldt direct, zonder herstart (de schedulers en de handelsmotor lezen de nieuwe waarden binnen een
  halve minuut);
- staat in de database (tabel `settings`, sleutels `config:*`) en overleeft dus een nieuwe deploy;
- gaat vóór de omgevingsvariabelen. De waarden uit Coolify of `.env` zijn alleen nog de standaard. Bij
  een aangepast veld staat "Aangepast. Standaard: …" met een knop *Terug naar standaard* (die vult de
  standaard in; daarna opslaan). Per blok zet *Standaard herstellen* alles in één keer terug; de
  bevestiging noemt elke instelling die verandert, en de knop staat uit als er niets is aangepast;
- wordt na het opslaan per instelling als oud → nieuw in het blok gemeld ("Dagverlieslimiet: 5% →
  2,5%"). Bewerkte maar niet opgeslagen velden zijn oranje gemarkeerd; een balk onderaan noemt ze, met
  *Wijzigingen terugdraaien*, en de browser waarschuwt als je de pagina verlaat.

Niet in het portaal, met opzet: de sleutels (`BITVAVO_API_KEY`, `BITVAVO_API_SECRET`,
`ANTHROPIC_API_KEY`, `CRYPTOPANIC_TOKEN`), `DATABASE_URL`, `LIVE_TRADING_ACK` en de plafonds van de
limieten. Live-modus kun je in het portaal pas kiezen als de omgeving `LIVE_TRADING_ACK=true`,
`AUTH_ENABLED=true` én een Bitvavo-sleutel heeft; anders weigert de app de wijziging en blijft hij in
oefenmodus. Dezelfde instellingen zijn er als API: `GET/PUT /api/settings`,
`DELETE /api/settings[/{key}]`.

**Huidig wachtwoord bij gevoelige wijzigingen.** Wie de modus, automatisch handelen aan/uit
(`TRADING_ENABLED`), de markten, een harde limiet (het hele blok Harde limieten), de handelskosten per
order (blok Kosten), de exit-controle (`EXIT_CHECK_INTERVAL_MIN`), risk-off (aan/uit, het venster ervoor
en erna, de agenda en de schok-pauze), het gedrag zonder Claude, het Claude-maandbudget of de RSS-feeds
wijzigt of terugzet, moet in dat blok het veld **Huidig wachtwoord** invullen. Hetzelfde geldt voor het
resetten van de oefenportefeuille en voor handmatige orders zolang de bot live handelt. Andere
instellingen (overige intervallen, modellen, nieuwsbronnen aan/uit) gaan zonder wachtwoord. Velden die
erom vragen dragen het label "vraagt wachtwoord", en de pagina zegt bij het bewerken of jouw
wijzigingen het wachtwoord nodig hebben. De **noodstop** (op Overzicht en Handelen) werkt bewust zonder
wachtwoord, zodat je kopen altijd snel kunt stilzetten; *Kopen hervatten* vraagt ook geen wachtwoord.
Bij een ontbrekend of fout wachtwoord wordt niets opgeslagen: het blok meldt "Niet opgeslagen: …",
het wachtwoordveld krijgt de focus en je bewerkte waarden blijven staan en gemarkeerd. Foute
wachtwoorden tellen mee voor dezelfde wachttijd als het inloggen; die wachttijd blokkeert ook het
inloggen, en bij nog 1 of 2 pogingen waarschuwt de pagina. Via de API stuur je het mee als
`"current_password"` in de JSON-body of in de header `X-Current-Password` (handig bij `DELETE`); zonder
het juiste wachtwoord antwoordt de API met 403. Met `AUTH_ENABLED=false` is er geen wachtwoord en
vervalt deze stap.

Grenzen bij het opslaan:

- **Plafonds uit de omgeving**: `MAX_TRADE_EUR_CEILING` (standaard 1000 euro),
  `MAX_EXPOSURE_PCT_CEILING` (100%) en `MAX_TRADES_PER_DAY_CEILING` (50). Het portaal kan ze niet
  zetten of overschrijden; een hogere waarde uit het portaal of de omgeving wordt bij het starten
  verlaagd tot het plafond (staat in de log), en `MIN_TRADE_EUR` zakt zo nodig mee. Wil je hoger,
  verhoog dan het plafond in Coolify of `.env`. Een plafond van 0 of lager valt met een waarschuwing
  terug op zijn standaard.
- Ook alleen via de omgeving begrensd: `AUTH_SESSION_HOURS` op 1-168 en `NEWS_MAX_AGE_HOURS` op hoogstens
  720 (de bewaartermijn van 30 dagen); een waarde daarbuiten wordt met een waarschuwing aangepast.
- Maximaal 20 markten, 30 feeds en 200 agendaregels. `NaN` of oneindig in een getalveld wordt geweigerd
  ("geen geldig getal").
- **Getallen** mogen met een decimale komma (`2,5`) of punt (`2.5`). Een getal dat ook als
  duizendtal te lezen is, zoals `1.000`, `2,500` of `1.000,50`, wordt geweigerd als onduidelijk (schrijf
  `1000` of `2,5`); dezelfde regel geldt voor het bedrag van een handmatige order. Het portaal toont
  getallen met een komma.
- **Ruimer in één keer**: wordt een harde limiet (maximum per order, kapitaal voor de bot of 0 = het hele
  account, maximaal in crypto, dagverlieslimiet, maximum aantal orders per dag) meer dan twee keer zo
  ruim, of de afkoelperiode na een stop-loss minder dan half zo lang, dan vraagt het portaal eerst om
  een extra bevestiging (zonder JavaScript met een vinkje op een aparte pagina). Alleen het portaal
  vraagt dat, de API niet.
- **Markten** die nieuw in de lijst komen worden gecontroleerd tegen de marktlijst van Bitvavo (als die
  binnen 3 seconden antwoordt; de lijst wordt een uur bewaard). Een onbekende markt wordt geweigerd met
  een suggestie ("BTCC-EUR bestaat niet op Bitvavo. Bedoel je BTC-EUR?"); is Bitvavo niet bereikbaar,
  dan wordt zonder controle opgeslagen.
- **Claude-model**: een naam die de app niet kent, wordt wel opgeslagen, maar met een waarschuwing bij
  het veld (waarschijnlijk een typfout). `GET /api/settings` geeft die waarschuwingen in `warnings`.
- **Standaard herstellen** zet nooit de modus terug. Een reset die de app naar live zou laten schakelen
  (omdat de omgeving `TRADING_MODE=live` heeft) wordt geweigerd, in het portaal en via
  `DELETE /api/settings[/{key}]` (409).
- **Naar live** vraagt in het portaal, naast het wachtwoord, om het woord `LIVE` te typen; het paneel
  noemt eerst de limieten die dan gelden. De API vraagt dat niet (wel het wachtwoord). Zie
  [Live gaan](#live-gaan).
- Velden die bij een schakelaar horen (bijvoorbeeld de risk-off-vensters bij *Risk-off rond
  agendamomenten*) zijn grijs en alleen-lezen zolang die schakelaar uit staat; hun waarde blijft
  bewaard.
- RSS-feeds moeten `https`-URL's zijn met een publieke domeinnaam: geen IP-adressen, `localhost`,
  namen zonder punt of op `.local`, `.internal`, `.lan`, `.home.arpa` of `.intranet`, en geen
  `gebruiker:wachtwoord@` in de URL. Een eerder opgeslagen `http`-feed (of een uit de omgeving) blijft
  werken met één waarschuwing in de log, maar het blok Nieuws kan pas weer worden opgeslagen als je die
  feed op `https://` zet.
- Elke wijziging komt met oude en nieuwe waarde in de log (niveau INFO).

**Uit de omgeving** toont wat alleen op de server staat: de Bitvavo-sleutel (niet ingesteld, ingesteld,
werkt, geweigerd door Bitvavo of mag niet handelen, met de reden en de code van Bitvavo), de
Claude-sleutel, de CryptoPanic-token, het meldingskanaal (zie [Meldingen](#meldingen)), de login en de
live-bevestiging. *Verbinding testen* doet één gesigneerd verzoek bij Bitvavo en toont je tarief;
Crypie kan de rechten van een sleutel niet zien. Weigert Bitvavo de sleutel, dan staat dat met de reden
ook in rood in de voettekst van elke pagina en in de saldomelding op het overzicht. *Testmelding sturen*
verstuurt een testbericht naar het meldingskanaal.

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

De pagina toont de regels eerst, in leesbare namen ("RSI 14 lager dan 35"), met de indicator-id's uit de
JSON ernaast; de vrije omschrijving staat klein als "Toelichting (vrije tekst)" en de pagina waarschuwt
als de regels veranderen maar de omschrijving niet. De strategie geldt voor al je markten; het veld
`market` bepaalt alleen op welke markt de backtest draait (bij *Testen* kun je een andere markt kiezen).

- **Resultaat op een eigen adres.** Een test en een voorstel van Claude staan daarna op
  `/strategy?result=…` (twee uur geldig, in het geheugen, weg na een herstart). Vernieuwen voert de test
  of de betaalde Claude-aanroep niet opnieuw uit.
- **Opslaan** staat onder de backtest en slaat precies de geteste versie op; is de JSON in de editor
  daarna gewijzigd, dan zegt de pagina dat. *Opslaan zonder test* in de editor vraagt eerst om
  bevestiging. In live, en als de backtest geen trades deed, het slechter deed dan kopen en vasthouden of
  bijna altijd een koopsignaal geeft, vraagt de pagina eerst om bevestiging.
- **Oud tabblad.** Is de strategie intussen ergens anders gewijzigd, dan wordt niet opgeslagen: je ziet
  de verschillen, met *Toch overschrijven* of *Laad de huidige*.
- **Vorige strategie.** Bij elke wijziging bewaart Crypie de strategie die er stond;
  *Vorige strategie terugzetten* wisselt die twee om (met bevestiging).
- **Fouten** in de JSON staan in gewoon Nederlands onder de editor, met de plek ("Koopregel 1 →
  vergelijking") en bij een syntaxfout de regels eromheen.
- **De backtest** noemt de periode, vergelijkt het netto resultaat met kopen en vasthouden (met een
  grafiek en een oordeel), waarschuwt bij minder dan 5 trades en toont het tradelog bruto.
  *Winnende trades* telt daar trades met bruto winst.

De backtest is long-only, rekent de kosten van Bitvavo mee (`BACKTEST_FEE_PCT`), vult stop-loss, trailing stop en
take-profit op de drempelprijs en vergelijkt met kopen-en-houden. Hij toont ook **Koopsignaal live**: hoe
vaak de harde koopvoorwaarden alleen al zouden afgaan (`live_buy_signal_pct`). Boven ongeveer 20% waarschuwt
de pagina.

Controles bij opslaan of toepassen (op /strategy, via `PUT /api/strategy` en bij een evaluatievoorstel):

- een strategie moet vanaf de instap een stop hebben: een `stop_loss_pct`, of een `trailing_stop_pct`
  zonder `trailing_start_pct` (een trailing stop die pas na een stijging ingaat telt niet). Een eerder
  opgeslagen strategie zonder zo'n stop blijft laden;
- harde koopvoorwaarden mogen niet altijd waar zijn (zoals `close > 0` of `rsi14 <= 100`); als zacht
  filter mag dat wel;
- indicatorperiodes zijn begrensd op 1000 candles (RSI 200, MACD-traag 500) en de totale opwarmtijd op
  1000. Een live cyclus haalt genoeg candles op voor die opwarmtijd (minstens 300, hoogstens 1440).

Een voorstel van Claude of van de evaluatie toont eerst een overzicht van wat er verandert ten opzichte van
de actieve strategie; een voorstel dat de controles niet haalt krijgt geen knop om toe te passen. Een
evaluatievoorstel kan bovendien niet met één klik (op /trading of via de API) worden toegepast als het de
stop vanaf de instap ruimer maakt of weghaalt, of als de backtest een live koopsignaal op meer dan 50% van
de candles laat zien; de kaart zegt dan waarom. Zo'n strategie kun je wel zelf opslaan op /strategy. Is de
opgeslagen strategie ongeldig geworden (bijvoorbeeld door strengere grenzen na een update), dan opent de
bot geen nieuwe posities (journaal: "opgeslagen strategie ongeldig"); verkopen, stop-loss, trailing stop
en take-profit blijven werken, met de regels van de standaardstrategie. Dat staat als fout in de log en
/strategy en het overzicht tonen een waarschuwing. Sla een geldige strategie op om dat op te heffen.

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
(6 uur, 36 uur, 1 week halfwaardetijd). Binnen een nieuwsfeit telt **één stem per nieuwsbron**, en
"n bronnen" telt verschillende bronnen, niet berichten. Bij RSS is de bron de site van de feed zelf (niet
de site waar een bericht naar linkt); elders de uitgever of het domein van de link, waarbij uitgevernaam en
site als één bron tellen. Een feit dat maar één bron meldt telt hoogstens als impact 'middel'; marktbreed
nieuws ('alle coins') telt pas mee als een tweede bron het meldt. Het getoonde bericht per feit is het
eerst opgeslagen bericht. Dit sentiment is de input voor de handelsbeslissing in stap 4. Pagina
**/news** toont alles, met per nieuwsfeit of het nu **meetelt** in dat sentiment en zo niet waarom (nog niet
beoordeeld, niet koersrelevant, ouder dan 48 uur, over andere coins, impact 0, marktbreed nieuws uit één
bron); de knoppen *Telt mee* / *Telt niet mee* filteren daarop (`?telt=ja|nee`) en tonen hun aantal.
Standaard toont /news de laatste 48 uur (het venster van het sentiment; kies 6, 24, 48 uur of 3 dagen).
Alle berichten van die periode worden geteld en gegroepeerd (tot 5000; staat er meer, dan zegt de pagina
dat), 30 nieuwsfeiten per pagina (`?page=`). De topberichten op de kaarten per coin linken naar het
feit in de lijst. Impact heeft één schaal: klein, middel, groot; "impact middel of groot" betekent 2 of
hoger. Zonder Claude-sleutel wordt nieuws wel opgehaald maar niet beoordeeld: dat is geen fout meer,
maar een rustige melding en "Nieuwsbeoordeling: uit" in de voettekst. Na *Nu verversen* zie je één
melding met het aantal nieuwe berichten, op dezelfde filter en periode. API: `GET /api/news`
(velden `counts`, `counts_reason`; filter `?counts=true|false`), `GET /api/news/sentiment`,
`POST /api/news/refresh` (geeft ook `assessing`: of de berichten beoordeeld worden).

Nieuws is onbetrouwbare invoer, dus:

- Feeds moeten publieke `https`-URL's met een hostnaam zijn (geen IP-adres, geen `localhost` of interne
  namen, geen `gebruiker:wachtwoord@`; zie [Instellingen](#instellingen)). Een feed op een IP-adres, een
  naam zonder punt of een interne of Docker-host (bijvoorbeeld een eigen RSSHub op
  `http://rsshub:1200`) wordt bij het ophalen overgeslagen, met alleen een waarschuwing in de log. Feeds
  die doorsturen naar `http`, naar een privé-adres of vaker dan 3 keer, worden ook overgeslagen. Per
  verzoek maximaal 5 MB en 30 seconden, maximaal 200 berichten per antwoord. De verbinding gaat naar het
  IP-adres dat gecontroleerd is. Daardoor negeert de nieuwsophaler `HTTP_PROXY`/`HTTPS_PROXY`: de
  container moet zelf DNS kunnen opvragen en rechtstreeks naar buiten kunnen, anders komt er geen nieuws
  binnen.
- Berichten zonder `http(s)`-link worden niet opgeslagen; links op /news openen met
  `rel="noopener noreferrer"`. Een publicatiedatum in de toekomst wordt de ophaaltijd; zulke berichten die
  al in de database stonden worden bij het starten rechtgezet en tellen tot dan nergens mee.
- In de beslisprompt staan geen URL's of ruwe titels; nieuwsteksten staan in een apart afgebakend blok dat
  Claude als onbetrouwbaar behandelt.
- Berichten die Claude 3 keer niet kan beoordelen worden overgeslagen ("Beoordeling door Claude mislukt");
  een time-out telt ook als mislukte poging.
- Fear & Greed wordt genegeerd als de waarde ouder is dan 48 uur, het ophalen mislukt of de bron uit
  staat. Het label volgt uit de waarde (0-24 Extreme Fear, 25-46 Fear, 47-54 Neutral, 55-75 Greed,
  76-100 Extreme Greed).
- Bewaartermijn: nieuws 30 dagen, Claude-gebruik 400 dagen (opgeruimd bij elke nieuwsronde).
- Na de update krijgen oudere RSS-berichten bij de volgende nieuwsronde de site van hun feed. Delen twee
  feeds dezelfde titel, dan blijven hun oude berichten onder die titel tellen tot ze uit het nieuwsvenster
  vallen.

## Handelen

Elke `TRADE_INTERVAL_MIN` minuten draait per markt uit `MARKETS` één cyclus (`app/trading/engine.py`):

1. **Mechanisch (exit-lus)**: los van de cyclus controleert de bot elke `EXIT_CHECK_INTERVAL_MIN` minuten
   (standaard 2, instelbaar 1-60) de stop-loss, trailing stop en take-profit van **alle** open posities op
   de actuele Bitvavo-koers, en sluit een positie die zo'n drempel raakt. De trailing stop schuift mee met
   het hoogste punt sinds instap (`Position.high_price`). Daar komt Claude niet aan. De exit-lus loopt ook
   als `TRADING_ENABLED` uit staat, de noodstop aan staat of de markt uit `MARKETS` is gehaald; de
   noodstop stopt alleen nieuwe aankopen. Bitvavo kent geen stop-orders aan de beurskant: de bescherming
   werkt alleen zolang de app draait, en met een interval van 2 minuten kan een snelle crash ruim voorbij
   de stop gaan. Lukt een verkoop niet (geen coins op het account, door Bitvavo geweigerd, of onuitgevoerd
   geannuleerd), dan blijft de positie open met zijn stop en probeert de volgende controle het opnieuw.
   Zo'n fout komt hoogstens eens per 60 minuten per positie in het journaal, en de positie staat dan met de
   reden in de rode melding over onbeschermde posities. Daar staan ook posities zonder koers (bijvoorbeeld
   een geschrapte markt) en posities die vastzitten op een order met onbekende uitkomst. Lukt het ophalen
   van de koersen niet, dan staat elke open positie als **niet bewaakt** in die melding, met sinds
   wanneer; de status wordt dan "Storing" en na `NOTIFY_EXIT_FAIL_MIN` minuten volgt een melding (zie
   [Status en meldingen](#status-en-meldingen)). Een mislukte afstemming van onafgehandelde orders slaat
   de controle van de stops niet meer over.
2. **Niets te beslissen?** Geen buy-signaal zonder positie, geen sell-signaal met positie en geen
   zwaar positief of negatief nieuws (impact-gewogen score ≥ 0,4 of ≤ -0,3 met minstens één nieuwsfeit
   met impact middel of groot): dan wordt alleen een `hold` gelogd en wordt Claude niet aangeroepen
   (bespaart kosten).
3. **Claude beslist** (`CLAUDE_MODEL_DECISION`, Sonnet 5.5): krijgt strategie, indicatorwaarden, signaal,
   nieuwssentiment met topberichten, open positie, limieten en een samenvatting van zijn eigen recente
   beslissingen met uitkomst. Antwoord: buy/sell/hold, zekerheid, bedrag, toelichting. Kopen mag bij een
   buy-signaal, of zonder signaal op sterk positief nieuws met impact middel of groot (**news entry**, dan maximaal de
   helft van `MAX_TRADE_EUR`); verkopen bij een sell-signaal of als news exit. Een news entry of news exit
   zonder strategiesignaal mag alleen als minstens twee verschillende bronnen (andere uitgever én andere
   site) hetzelfde nieuwsfeit melden; anders blijft het `hold` ("niet bevestigd door een tweede bron").
   Slecht nieuws is voor de bot nooit een reden om "de dip te kopen".
   **Dipbewaking**: gelden de harde koopvoorwaarden behalve de `rising`/`falling`-bevestiging (de dip is er,
   de draai nog niet), dan controleert de bot die markt elke `WATCH_INTERVAL_MIN` minuten (standaard 5, 0 = uit)
   in plaats van eens per cyclus, zonder journaalregels. Zodra de bevestiging komt draait de bot meteen een
   cyclus voor die markt: met Claude, of zonder Claude volgens `CLAUDE_UNAVAILABLE_MODE` (bij `hold` koopt hij
   dan niet). Na `WATCH_MAX_MIN` minuten (standaard 180) zonder draai stopt de bewaking. Beide instelbaar in
   het portaal.
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
   Na een **nieuwsschok** (een nieuwsfeit met impact groot (3) en sentiment ≤ -0,5, zoals een hack of een
   beurs in problemen) koopt de bot `SHOCK_PAUSE_HOURS` (6) uur niet in de geraakte coins, mits minstens twee bronnen het melden (één bron die
   een hack meldt pauzeert dus niet; zo kan één vijandige feed niet alle aankopen stilzetten). Verkopen,
   stop-loss en take-profit blijven werken; de
   dipbewaking wordt niet gewapend. Claude ziet dit als `market_events` (actieve risk-off en de komende 72 uur);
   /trading toont de agenda en een actieve risk-off. Alles instelbaar in het portaal.
   Een voorwaarde met `"soft": true` is een **zacht filter**: het signaal ontstaat ook als het niet geldt,
   Claude ziet dat in `signal_checks` en `soft_filters_failed` en koopt dan hoogstens de helft van
   `MAX_TRADE_EUR`. De standaardstrategie gebruikt dat voor de EMA200-trendfilter, zodat een RSI-dip in een
   dalende markt wél aan Claude wordt voorgelegd. De backtest kent geen Claude en rekent zachte filters als hard.
   Elke cyclus komt per markt in het journaal: signaal, de uitkomst van iedere strategievoorwaarde met de
   echte waarden, nieuws, besluit en toelichting. De pagina's tonen dat met leesbare namen en eerst een
   samenvatting ("Afwachten · 1 van 2 koopvoorwaarden gehaald · 1 zacht filter niet gehaald"), dan de
   lijst ("RSI 14 lager dan 35 (52,2 t.o.v. 35,0)", met ✓ gehaald, ✗ niet gehaald en ~ zacht filter niet
   gehaald; schermlezers horen die woorden). Pagina `/trading` toont de laatste cyclus per markt en de
   cycli van de laatste `DECISION_HISTORY_DAYS` dagen (standaard 7, instelbaar in het portaal); zie
   [De pagina Handelen](#de-pagina-handelen).
4. **Harde limieten** (`app/trading/risk.py`), in code afgedwongen: `MAX_TRADE_EUR`, `MAX_EXPOSURE_PCT`,
   `DAILY_LOSS_LIMIT_PCT`, `MAX_TRADES_PER_DAY` (in het portaal: *Maximum aantal orders per dag*),
   `COOLDOWN_MIN_AFTER_STOP`, plus de **noodstop** (zie hieronder). De dagverlieslimiet telt het
   gerealiseerde resultaat van vandaag plus alleen het verlies op open posities; winst op open posities
   verbergt dus geen gerealiseerd verlies. Bij de limiet koopt de bot tot 00:00 UTC niets meer; er
   wordt niets verkocht of gepauzeerd. `MAX_TRADES_PER_DAY` telt **orders**, geen trades: elke gevulde
   of nog onafgehandelde order van vandaag, kopen en verkopen apart, ook handmatige orders en verkopen
   door stop-loss of take-profit; bij de limiet koopt de bot tot 00:00 UTC niet meer, verkopen blijft
   altijd mogelijk. Handelen en Tradingplan tonen deze regels met dezelfde zinnen, met de namen uit
   Instellingen. Met `LIVE_CAPITAL_EUR`
   (standaard 0 = het hele account) rekenen blootstelling en daglimiet live met dat bedrag en alleen met
   de posities van de bot zelf. Na een order die Bitvavo of de oefenportefeuille weigert, vraagt de bot
   60 minuten geen advies aan Claude voor die markt. Zonder Claude (budget op, geen key):
   `CLAUDE_UNAVAILABLE_MODE` = `hold` (niet kopen) of `indicators` (het strategiesignaal volgen en kopen
   met de helft van `MAX_TRADE_EUR`, niet tegen sterk negatief nieuws in); een verkoopsignaal op een open
   positie wordt in beide gevallen gevolgd.
5. **Transactiekosten**: oefenorders betalen hetzelfde taker-tarief als Bitvavo (`TRADING_FEE_PCT`,
   standaard 0,25% per order, bij kopen én bij verkopen; met een live key wordt het echte tarief van je
   account gelezen). Winst en verlies zijn altijd netto na kosten, ook live (Bitvavo meldt de betaalde
   kosten per order terug), en Claude krijgt de kosten mee in zijn afweging. De pagina's zeggen "netto"
   waar de kosten zijn meegerekend en "bruto" waar niet.
6. **Logboek**: elke beslissing komt in `decisions` met alle input en later de koers 1, 24 en 72 uur erna.
   Posities staan in `positions` en vormen het trade-journaal op `/journal`. Deze uitkomsten gaan terug naar Claude bij de volgende beslissing: zo
   "leert" de bot zonder dat het model verandert.
7. **Evaluatie**: elke `EVALUATION_INTERVAL_HOURS` (standaard wekelijks) schrijft `CLAUDE_MODEL_EVALUATION`
   (Fable 5.1) een rapport over de resultaten en doet eventueel een strategievoorstel, inclusief backtest.
   Een voorstel wordt pas actief als jij het op `/trading` toepast (zie
   [De pagina Handelen](#de-pagina-handelen)). Zonder `ANTHROPIC_API_KEY` staan *Nu evalueren* en de
   automatische evaluaties uit.

De actieve strategie bepaalt de regels (indicatoren, condities, stop-loss, trailing stop, take-profit) en wordt op elke
markt uit `MARKETS` toegepast; het `market`-veld van de strategie is alleen de standaard voor backtests.

**De noodstop** ("Noodstop: kopen pauzeren" op Overzicht en Handelen, `POST /trading/action` met
`action=pause` of `resume`) blokkeert alleen nieuwe aankopen, van de bot en handmatig. Stop-loss,
trailing stop, take-profit en verkopen op een verkoopsignaal blijven werken; de cycli lopen door, dus
Claude kan nog worden gevraagd, en een verkoop alleen op negatief nieuws wacht tot je hervat. Aanzetten
en *Kopen hervatten* vragen elk één bevestiging en nooit een wachtwoord. Zolang de noodstop aan staat,
toont de navigatie op elke pagina "Oefenmodus · noodstop" of "Live · noodstop", begint de tabtitel met
"Noodstop · " en heeft het tabbladicoon een oranje stip; zet je hem in een ander tabblad om, dan volgen
die binnen een minuut. Na de knop kom je terug op de pagina waar je hem gebruikte. Overal staat dezelfde
uitleg in dezelfde woorden.

**Handmatige orders** (het orderformulier op het overzicht en `POST /api/orders`) volgen dezelfde regels
als de bot: de markt moet in `MARKETS` staan, een aankoop ligt tussen het minimum per order
(`MIN_TRADE_EUR`, minstens € 5) en het maximum per order, en handmatige orders tellen mee voor het
maximum aantal orders per dag en vallen onder alle harde limieten. Een aankoop wordt tegengehouden door
de noodstop (dan kun je alleen verkopen) en de harde limieten, opent een beschermde positie (één per
markt) en kan niet als er al een positie of een onafgehandelde order in die markt is. Een verkoop kan
vanaf € 5 en mag groter zijn dan het maximum per order; blijft er minder dan € 5 over, dan wordt de hele
positie verkocht, en *Hele positie verkopen* doet dat in één keer. Een verkoop verkleint of sluit de
positie van de bot in die markt. In het formulier:

- typ je het bedrag met een komma of punt (`25,50`); een onduidelijk bedrag zoals `1.000` wordt
  geweigerd. Onder het veld staat wat de app las ("= € 25,50");
- noemt de bevestiging de modus, de markt, de richting en het bedrag ("LIVE · echt geld: BTC-EUR kopen
  voor € 25,50? Dit is een echte order op Bitvavo.");
- staat de uitkomst daarna in de orderkaart zelf: wat er gekocht of verkocht is, tegen welke koers en
  met welke kosten, of waarom de order niet geplaatst is (eigen regels, Bitvavo onbereikbaar, sleutel
  geweigerd, of Bitvavo weigert de order). Is de uitkomst nog onbekend, dan zegt de kaart dat: plaats
  hem dan niet opnieuw. Na een weigering houdt het formulier je markt, richting en bedrag;
- plaatst hetzelfde formulier twee keer versturen (Terug en opnieuw versturen) de order niet nog een
  keer (een eenmalig `order_token`, 30 minuten in het geheugen).

Zolang de bot live handelt vraagt een handmatige order om je huidige wachtwoord.

**Orders en herstarts.** Live orders krijgen een `clientOrderId` en staan als `pending` in de database
voordat ze naar Bitvavo gaan. Na een time-out zoekt de bot de order op in plaats van hem opnieuw te sturen;
onafgehandelde orders worden bij het starten en vóór elke exit-controle afgestemd, en de markt wacht zolang.
Gedeeltelijke vullingen worden als zodanig geboekt; de rest blijft open met zijn stop. Live verkopen gebruiken
de hoeveelheid coins, afgerond naar beneden op de `quantityDecimals` van de markt en begrensd op het
beschikbare saldo. Een herstart start niet meer meteen een volledige cyclus: de taken gaan verder op hun
volgende tijdstip (bewaard in de `settings`-tabel). Aankopen in verschillende markten (cyclus,
dipbewaking en handmatig) worden één voor één gecontroleerd en geboekt, zodat ze samen de limieten niet
kunnen overschrijden. Draai één instantie (`--workers 1`); de database voorkomt bovendien dat twee
instanties dezelfde markt kopen.

**Database**: er is geen handmatige migratie nodig. Bij het starten worden nieuwe kolommen en indexen
toegevoegd, en op Postgres worden te korte `VARCHAR`-kolommen verbreed. Staan er in oude data twee open
posities in dezelfde markt, dan wordt de unieke index overgeslagen met een fout in de log; sluit er één
en herstart.

### Het overzicht

Het overzicht (`/`) begint met de status van de bot (zie [Status en meldingen](#status-en-meldingen)),
de noodstop en de portefeuille: "Portefeuille (oefengeld)" of "Portefeuille (echt geld)", met *Sinds
start* netto, inclusief open posities en kosten. Op een telefoon staan status en noodstop bovenaan.
Daaronder:

- **Aan de slag**: vijf stappen (markten en limieten, strategie, tradingplan, Claude-sleutel,
  voorwaarden voor live), elk met of ze klaar zijn en een link. De kaart staat er tot er een plan en een
  eerste order zijn, of tot je *Deze kaart verbergen* kiest.
- **Open posities** met het **netto** resultaat als je nu verkoopt: waarde × (1 − kosten) − inleg
  (de inleg bevat de kosten van de aankoop). In live staat er ook een regel als er nog open
  oefenposities zijn: die bewaakt de bot nog, maar verkopen of resetten kan alleen in oefenmodus.
- **Koersen** met het tijdstip van de koersen, en het **saldo**.
- De grafieken, de strategie, **Gesloten trades · 30 dagen** (alleen afgeronde trades, netto;
  *Winnende trades* is het deel met netto winst, bijvoorbeeld "0% · 0 van 1 met netto winst") en het
  Claude-budget ("$ 0,00 van $ 100").
- Het **orderformulier** (zie [Handmatige orders](#handelen)) en **Laatste orders**, alleen van de
  huidige modus.

Wacht er een strategievoorstel van Claude, dan staat bovenaan een melding met een link naar Handelen.
Live-advies (kapitaal voor de bot instellen, een beperkte sleutel) staat in één melding, met per punt
een link naar de plek waar je het oplost. De status ververst zichzelf elke minuut zolang het tabblad
zichtbaar is (`GET /api/dashboard/status`).

**Als Bitvavo hapert** wacht het overzicht hoogstens 3 seconden op koersen en saldo (parallel). Daarna
toont het de laatst bekende koersen met hun tijdstip en een melding waarom; een mislukte poging wordt
15 seconden onthouden, zodat volgende bezoeken niet opnieuw wachten. Het oefensaldo staat in de app zelf
en is er altijd; een munt zonder koers telt mee tegen de instapkoers van zijn positie ("geschat").

### De pagina Handelen

`/trading` laat zien wat de bot doet en waarom, in deze volgorde:

1. **Status van de bot** met de noodstop en *Nu een cyclus draaien* (in live met een bevestiging).
   Zolang het tabblad zichtbaar is, kijkt de pagina elke minuut via `/api/status` of de status, de
   laatste cyclus of de bewaking veranderde, en biedt dan aan de pagina te vernieuwen.
2. **Voorstel van Claude**: het nieuwste voorstel uit een evaluatie dat toegepast kan worden, met de
   backtest, wat er verandert en *Voorstel toepassen als actieve strategie* (met een bevestiging die
   de oude en de nieuwe strategie noemt). Open posities houden hun stop-loss en take-profit; een
   trailing stop volgt daarna de nieuwe strategie. Crypie bewaart de strategie die er stond: zolang het
   laatst toegepaste voorstel nog de actieve strategie is, kun je één keer terug met *Vorige strategie
   terugzetten* (`action=revert:<id>`). Een oud tabblad kan zo nooit een latere wijziging terugdraaien.
3. **Laatste cyclus per markt**: per markt de samenvatting, de voorwaarden en het nieuws. Een markt
   waarvan de cyclus mislukte, staat er als "Mislukt" met de reden en een link naar de markten.
4. **Open posities**, met de huidige koers, het netto resultaat en de afstand tot stop-loss en
   take-profit.
5. **Context en limieten**: drie meters (in crypto t.o.v. het maximum, het resultaat van vandaag
   t.o.v. de dagverlieslimiet, en orders vandaag t.o.v. het maximum), oranje vlak bij de grens en rood
   als die bereikt is. Daaronder, uitklapbaar, de harde limieten, de agenda en risk-off, de
   lange-termijntrend en de cycli en dipbewaking.
6. **Gesloten posities** van de laatste 30 dagen, alleen van de huidige modus; heeft de andere modus
   trades, dan staat er een link naar het journaal.
7. **Evaluaties door Claude**. *Nu evalueren* staat uit zonder Claude-sleutel, met de reden erbij.
8. **Alle cycli**: standaard alleen de belangrijke beslissingen (aankopen, verkopen, koopsignalen,
   blokkades, adviezen van Claude en mechanische exits); *Alles* toont ook elke keer "afwachten". Filter
   per markt, 25 per pagina met *Nieuwer* / *Ouder*: `/trading?view=all&market=BTC-EUR&page=2#cycli`.

Na elke actie (noodstop, hervatten, cyclus, evalueren, voorstel toepassen of terugzetten) zie je een
melding, bijvoorbeeld "Cyclus gedraaid: BTC-EUR afwachten · ETH-EUR gekocht.". Een weigering staat in
dezelfde woorden als bij een handmatige order (noodstop, orders per dag). Een onbekende actie geeft 400.

### Tradingplan en trade-journaal

- **`/plan` (Tradingplan)** toont op één pagina het plan dat de bot volgt: markten en modus, de harde
  limieten, de instap- en uitstapregels van de actieve strategie (hard en zacht), dipbewaking, de
  nieuwstriggers, de beslisvolgorde en de rol van Claude. Dat deel wordt uit de instellingen en de
  strategie gelezen, dus het klopt altijd met wat de bot echt doet. Daarboven staan vier vrije velden
  van jou (*Jouw plan, bewerkbaar*): **doel en horizon**, **eigen regels**, **wanneer de bot nooit mag
  handelen** en **lessen**, elk met een tekenteller. Ze worden in de `settings`-tabel bewaard
  (`trading_plan`) en gaan als `trading_plan` mee in elke Claude-beslissing en elke evaluatie. Eigen
  regels zijn instructies voor Claude: ze kunnen Claude terughoudender maken, nooit de spelregels of de
  harde limieten omzeilen. De pagina zegt of Claude het plan nu leest. Zonder Claude (geen sleutel of
  budget op) worden je eigen regels niet toegepast: bij `hold` koopt de bot niets, bij `indicators`
  volgt hij alleen het strategiesignaal. API: `GET/PUT /api/plan`.
- **`/journal` (Journaal)** toont elke trade (een positie van aankoop tot verkoop, open en gesloten) als
  journaalregel: instap en uitstap met koers, hoeveelheid, inleg, kosten en netto resultaat, wie hem
  opende (Claude, de regels zonder Claude of jij, handmatig), de voorwaarden en de toelichting bij de
  instap en de uitstap, en een eigen **notitie met labels** per trade. Bij een handmatige trade staat
  "Jouw reden" in plaats van "Claude destijds". Het journaal toont de modus waarin de bot nu handelt;
  heeft de andere modus trades, dan wissel je met *Oefenmodus N | Live N* (`?mode=live`). Filters op
  markt, status, winst/verlies, hoe gesloten (ook *handmatig verkocht* en *trailing stop geraakt*),
  geopend door (de bot of jij), periode (leeg of *alles* = geen filter), label en vrije tekst; actieve
  filters staan als labels onder *Resultaat van de selectie* en zijn één voor één weg te klikken, en
  een onbekende filterwaarde wordt gemeld. Totalen: netto resultaat, *Winnende trades* (het deel met
  netto winst), winst/verlies-factor, kosten, gemiddelde looptijd, per markt en per bron. Adressen in
  notities zijn klikbaar. Notities staan op de positie zelf (`positions.note`, `positions.tags`) en gaan
  als `owner_notes` mee naar de evaluatie. Handmatige orders zonder positie staan apart in *Orders zonder
  trade*. API: `GET /api/journal` (ook `?mode=` en `?by=`; geeft `ignored_filters`),
  `PUT /api/journal/{id}/note`.

### Grafieken op het overzicht

- **Koers en acties**: candles van Bitvavo per markt (1 dag / 7 / 30 / 90 dagen) met daarop elke koop
  (groene pijl), elke verkoop met de reden (stop-loss, trailing stop, take-profit, signaal, nieuws),
  de Claude-adviezen die niet tot een order leidden (blauwe stip) en de kopen die een regel blokkeerde
  (oranje vierkant), met een legenda. Meerdere orders in één candle worden één markering ("3×").
  *Claude-adviezen tonen* verbergt alleen de adviezen, niet de blokkades. Routinematige
  "afwachten"-cycli van de regels staan er niet op. Onder de grafiek staan de koersen per dag en de
  acties als tabel. API: `GET /api/chart/{markt}?period=7d`.
- **Portefeuillewaarde**: euro plus crypto tegen de slotkoers van elke candle. Er is geen
  geschiedenis-tabel: de waarde wordt vanuit het huidige saldo teruggerekend door de orders één voor
  één terug te draaien. Stortingen of opnames op een live-account zijn geen orders en zien er daarom
  uit als een sprong aan het begin van de periode. De as zoomt nooit verder in dan ±1% rond het
  startbedrag, zodat kleine schommelingen niet als een crash ogen. Een markt zonder koersen bij Bitvavo
  wordt overgeslagen en onder de grafiek genoemd. API: `GET /api/portfolio/history?period=7d` (met
  `skipped`: overgeslagen markten met de reden; 502 alleen als alle markten falen).
- Tijden staan in je eigen tijdzone met de naam van de zone, koersen met vier significante cijfers
  (ook bij goedkope coins), bedragen in dezelfde notatie als de rest van de app ("-€ 0,52"). Laadt een
  grafiek niet, dan zegt hij waarom, met *Opnieuw proberen* en de technische details uitklapbaar; de
  knoppen springen dan terug naar wat er te zien is.
- Orders die nog onderweg zijn, onbekend of geweigerd zijn, of die niets gevuld hebben, tellen in beide
  grafieken niet mee.
- Tekenwerk door [Lightweight Charts](https://github.com/tradingview/lightweight-charts) (Apache 2.0),
  meegeleverd in `app/static/vendor/`; er wordt niets van een CDN geladen.

### Live gaan

1. Draai minimaal enkele weken in `TRADING_MODE=paper` en bekijk de evaluaties.
2. Zorg dat het account beveiligd is: login met authenticator, `SESSION_SECRET` gezet, eigen
   https-domein (zie [Beveiliging](#beveiliging)). Live kan alleen met `AUTH_ENABLED=true`.
3. Maak in Bitvavo een API-key met alleen **view**- en **trade**-rechten (nooit opnemen) en een
   IP-whitelist, en zet daarna `BITVAVO_KEY_RESTRICTED=true`. Gebruik bij voorkeur een apart
   (sub)account, of zet `LIVE_CAPITAL_EUR` op het bedrag dat de bot mag beheren. In live-modus toont het
   overzicht één melding *Advies voor live* zolang `LIVE_CAPITAL_EUR` 0 is of `BITVAVO_KEY_RESTRICTED`
   niet op `true` staat, met per punt een link naar de oplossing; de app controleert de rechten van de
   key zelf niet. Zet ook een meldingskanaal (`NOTIFY_URL`, zie [Meldingen](#meldingen)).
4. Zet in de omgeving de keys en `LIVE_TRADING_ACK=true`. **Instellingen › Handelen › Modus** toont
   alle voorwaarden voor live tegelijk (login, sleutel, bevestiging op de server), elk met wat je moet
   doen, plus het advies hierboven. Kies daarna de modus Live: een paneel noemt de limieten die dan
   gelden en vraagt je `LIVE` te typen, plus je huidige wachtwoord (of zet `TRADING_MODE=live` in de
   omgeving). In dat paneel kun je de oefenportefeuille meteen laten resetten; open oefenposities worden
   dan gesloten zonder resultaat. Anders blijven ze bewaakt, en resetten kan pas weer in oefenmodus.
   Zonder de bevestiging op de server weigert de app live-modus en draait hij in oefenmodus. Begin met
   een laag maximum per order.
5. Controleer met één kleine order dat Bitvavo de order met `clientOrderId` terugmeldt en dat een
   verkoop goed wordt afgerond op `quantityDecimals`; die velden zijn niet tegen de live API getest.

In live is elke handeling met echt geld te herkennen: een rode balk op elke pagina, "LIVE · " in de
tabtitel, en bevestigingen die beginnen met "LIVE · echt geld:" (handmatige order, *Nu een cyclus
draaien*, een strategie opslaan, terugzetten of overschrijven, een voorstel toepassen of terugzetten).
Overzicht, journaal en Handelen tonen alleen de orders en trades van de huidige modus.

Terug naar oefenmodus? Open live posities blijven beschermd (via de live-broker) zolang
`BITVAVO_API_KEY`/`BITVAVO_API_SECRET` en `LIVE_TRADING_ACK=true` in de omgeving blijven staan. Haal ze
pas weg als de live posities gesloten zijn. Anders zijn die posities onbeschermd: dat staat elke controle
als fout in de log, in rood op het overzicht en op /trading, en in `/api/trading/status`
(`unprotected_positions`).

## Status en meldingen

### De status van de bot

Overzicht en Handelen tonen één status, die zegt of de bot kan doen wat hij moet doen. Bij meerdere
problemen wint de bovenste:

| Status | Wanneer |
|--------|---------|
| **Storing** (rood) | De bewaking van stop-loss en take-profit lukt niet of loopt achter, open posities worden niet bewaakt, of de cyclus mislukt (bijvoorbeeld omdat Bitvavo niet reageert). |
| **Noodstop actief** (oranje) | De noodstop staat aan: kopen is gepauzeerd. |
| **Uitgeschakeld** (grijs) | Automatisch handelen staat uit (`TRADING_ENABLED`); je kunt zelf een cyclus starten en de bewaking loopt door. |
| **Actief · koopt niet** (oranje) | De bot draait, maar kan nu niet kopen: geen Claude-sleutel of het budget is op terwijl `CLAUDE_UNAVAILABLE_MODE=hold`, het maximum aantal orders per dag of de dagverlieslimiet is bereikt (tot 00:00 UTC), de limiet voor crypto is bereikt, de opgeslagen strategie is ongeldig, of risk-off rond een agendamoment. |
| **Actief** (oranje) | De planner lijkt stil te staan: er is twee intervallen lang geen geplande cyclus gestart. |
| **Actief** (groen) | Alles in orde. |

Onder de status staan de redenen, "Laatste cyclus … · volgende …" (oranje als de planner achterloopt) en
"Bewaking stop-loss/take-profit: laatst gelukt … (elke N min)", rood met "loopt achter" als die
bewaking niet op tijd lukt. De laatste cyclus en de laatste geslaagde bewaking overleven een herstart.

`GET /api/status` geeft hetzelfde als JSON: `mode`, `health` (met `level`, `label`, `detail`,
`reasons`, `last_cycle`, `next_cycle`, `exits_ok`, `last_exit_ok`, `stale` en meer), `risk_now`
(blootstelling, resultaat van vandaag en orders vandaag tegenover hun limieten, en `resets_at`),
`unprotected_positions` en `now`. Het doet geen verzoeken bij Bitvavo, wordt niet gecachet en staat
achter de login; de pagina's vragen het elke minuut op zolang ze zichtbaar zijn. `/health` blijft de
kale controle voor Docker en de proxy (`{"status": "ok"}`).

### Meldingen

Crypie kan een bericht sturen naar je telefoon of een webhook als je moet ingrijpen. Het kanaal staat
alleen in de omgeving; leeg betekent uit.

| Variabele | Standaard | Betekenis |
|-----------|-----------|-----------|
| `NOTIFY_URL` | leeg | Een [ntfy](https://ntfy.sh)-topic (`https://ntfy.sh/<lange-geheime-naam>`) of een webhook. Alleen `https`, een publieke hostnaam en geen `gebruiker:wachtwoord@`, gecontroleerd zoals de nieuwsfeeds. Een ongeldig adres wordt met een waarschuwing genegeerd. Het adres is geheim (wie een ntfy-topic kent, kan meelezen) en komt nooit in de log. |
| `NOTIFY_TOKEN` | leeg | Optioneel: gaat mee als `Authorization: Bearer …` (ntfy-toegangsbeheer of een webhookgeheim). |
| `NOTIFY_FORMAT` | `auto` | `ntfy` (platte tekst met titel en prioriteit in headers), `json` (`{source, event, title, message, priority, at}`) of `auto` (ntfy voor een ntfy-host, anders JSON). |
| `NOTIFY_EXIT_FAIL_MIN` | 10 | Na zoveel minuten (1-1440) een falende bewaking van stop-loss en take-profit melden. |

Een melding komt bij: een positie die niet bewaakt wordt, een mislukte verkoop bij een stop, een
bewaking die langer dan `NOTIFY_EXIT_FAIL_MIN` minuten faalt, een order waarvan de uitkomst na 5 minuten
nog onbekend is, het bereiken van de dagverlieslimiet, een uitgevoerde live order, een leeg
Claude-budget en een (her)start. Berichten bevatten geen sleutels, bedragen of links; de details staan
in Crypie. Elk onderwerp komt hoogstens eens per 60 minuten (een live order één keer, de
dagverlieslimiet en het budget eens per dag), alles samen hoogstens 30 per uur, en de startmelding
hoogstens eens per 10 minuten (ook over herstarts heen). Versturen gebeurt op de achtergrond, met een
time-out van 10 seconden, en laat de bot nooit haperen. Op **Instellingen › Uit de omgeving** zie je of
het kanaal is ingesteld en stuur je met *Testmelding sturen* een testbericht.

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

- **Wachtwoord wijzigen**: andere browsers worden uitgelogd; deze browser blijft ingelogd met de
  oorspronkelijke verlooptijd.
- **Nieuwe herstelcodes**: wachtwoord plus een huidige code uit je authenticator of een herstelcode.
  Andere browsers worden uitgelogd; deze browser blijft ingelogd.
- **Authenticator opnieuw instellen**: wachtwoord plus een huidige code of een herstelcode, daarna koppel
  je binnen 5 minuten de nieuwe app op `/account/mfa`. Andere browsers worden uitgelogd. *Annuleren* gaat
  terug naar /account.
- **Overal uitloggen**: beëindigt alle sessies, ook die van deze browser (met een bevestiging).
  *Uitloggen* in de menubalk logt alleen deze browser uit.

/account is ingedeeld in *Status* (met wanneer je sessie afloopt), *Beveiliging wijzigen* (de drie
acties hierboven, uitklapbaar; de twee die codes of de koppeling vervangen zijn rood gemarkeerd en vragen
een bevestiging), *Overal uitloggen* en *Wachtwoord of authenticator kwijt?* met de commando's op de
server. Een fout staat in de kaart van het formulier dat je verstuurde, bij het veld.

**Inloggen in stappen.** De inlogpagina nummert de stappen (wachtwoord, dan code). Bij de code staat een
apart formulier *Herstelcode gebruiken* met een gewoon toetsenbord (streepjes en spaties mogen); na een
login met een herstelcode zie je hoeveel codes er over zijn ("nog 7 van 8"), als waarschuwing bij 2 of
minder. Bij nog 1 of 2 pogingen waarschuwt de pagina, en een blokkade noemt de wachttijd in minuten met
het tijdstip ("over 15 minuten (om 14:32 UTC)") en de uitweg (`clear-throttle`). /login zegt waarom je
er bent: sessie verlopen of elders beëindigd, je wijziging is niet opgeslagen, je bent uitgelogd, of de
wachtwoordstap duurde te lang. Daarna ga je terug naar de pagina waar je was. Niet-opgeslagen
instellingen bewaart het tabblad zolang het formulier onderweg is (nooit wachtwoorden of de
LIVE-bevestiging); terug op Instellingen kun je ze terugzetten of weggooien.

**Eerste authenticator.** De QR-pagina noemt de tijdslimiet (10 minuten na het wachtwoord) met het
tijdstip. *Later koppelen* logt je (na een bevestiging) uit. Was er nog nooit een authenticator gekoppeld,
heeft het account nog nooit ingelogd en is er geen eenmalige code van de server, dan begin je opnieuw met
de setup-link uit de log plus je wachtwoord; de server zet die link bij het starten weer in de log. Na
`reset-mfa` of `create-account` werkt alleen de eenmalige code. De herstelcodes kun je downloaden (.txt),
afdrukken en kopiëren (via http valt kopiëren terug op selecteren); *Verder* kan pas als je aanvinkt dat
je ze bewaard hebt. Na een herladen zijn ze weg.

/account toont ook hoeveel foute codes er vóór je laatste login zijn ingevuld. Staat daar een getal dat
je niet herkent, dan kent iemand anders waarschijnlijk je wachtwoord: wijzig het.

Buitengesloten? Gebruik de commando's in de container (Coolify: Terminal bij de app), zie
[Beveiliging](#beveiliging); /login en /account noemen ze ook. Na `reset-mfa` of `create-account` kun je
de authenticator alleen koppelen met de eenmalige code die het commando toont (1 uur geldig). Een
bestaand account zonder authenticator dat al eens heeft ingelogd, krijgt zo'n code met
`python -m app.auth reset-mfa`.

Omgevingsvariabelen: `AUTH_SESSION_HOURS` (standaard 24, 1-168), `SESSION_SECRET` (zie
[Beveiliging](#beveiliging)), `SETUP_TOKEN` (optioneel vast setup-token) en `AUTH_ENABLED=false` om de
login lokaal uit te zetten. Dan toont elke pagina een waarschuwing: iedereen die het adres kan bereiken
kan instellingen wijzigen en in oefenmodus handelen. Live handelen kan dan niet; open live posities
blijven wel beschermd zolang de sleutel en `LIVE_TRADING_ACK` er staan. De inlog- en accountpagina's
sturen dan door naar het overzicht.

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
- Een aanroep heeft een time-out van 60 seconden en 1 nieuwe poging (de nieuwsbeoordeling en de
  strategie-assistent 180 seconden).
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
  HSTS over https en `Cache-Control: no-store` op de inlog- en accountpagina's. Ook foutpagina's en
  gecomprimeerde antwoorden hebben deze headers.
- **Meldingen na een actie** reizen in een kort cookie (`crypie_flash`, achter https
  `__Host-crypie_flash`): HMAC-gesigneerd met de sessiesleutel, 120 seconden geldig, `HttpOnly`,
  `SameSite=Lax`. Het bevat alleen de tekst van de melding, nooit geheimen, en wordt gewist zodra een
  pagina hem heeft getoond.
- **Statische bestanden** met een versie in het adres (`?v=`) mogen een jaar in de cache
  (`immutable`); zonder versie worden ze telkens gecontroleerd. De app comprimeert zelf met gzip
  (compressie in Traefik is dus optioneel), behalve op `/login`, `/setup`, `/account` en `/logout`,
  waar geheimen staan (BREACH).
- **Grenzen**: verzoeken mogen hoogstens 16 KB zijn op de openbare inlogpagina's en 1 MB elders (anders
  413). `/health` geeft alleen `{"status": "ok"}`. De API-documentatie staat uit tenzij
  `API_DOCS_ENABLED=true`.
- **Exit-lus**: stop-loss, trailing stop en take-profit draaien altijd, los van de handelscyclus en de
  noodstop (zie [Handelen](#handelen)). Het overzicht toont in rood welke open posities niet beschermd
  kunnen worden, bijvoorbeeld live posities zonder sleutel of `LIVE_TRADING_ACK`, of alle posities als
  de koersen niet op te halen zijn; met `NOTIFY_URL` komt daar ook een melding van
  (zie [Meldingen](#meldingen)).
- **Nieuws als onbetrouwbare invoer**: alleen publieke feeds, begrensde downloads, nieuws pas mee laten
  wegen bij een tweede bron, nieuws en vrije tekst afgebakend in de prompt (zie [Nieuws](#nieuws)). De
  CryptoPanic-token en de volledige verzoekregels van httpx komen niet meer in de log.
- **Waarschuwingen op het overzicht**: bij een ontbrekende of te korte `SESSION_SECRET`, bij onbeschermde
  posities, bij een ongeldige opgeslagen strategie, met `AUTH_ENABLED=false` (op elke pagina), bij een
  Bitvavo-sleutel die Bitvavo weigert (ook in de voettekst van elke pagina) en in live-modus zolang
  `LIVE_CAPITAL_EUR` 0 is of `BITVAVO_KEY_RESTRICTED` niet op `true` staat.

### Checklist voor de eigenaar

1. Zet `SESSION_SECRET` in Coolify op een willekeurige waarde van minstens 32 tekens. Zonder staat de
   sleutel in de database en kan wie de database of een back-up kan lezen cookies vervalsen. Een nieuwe
   `SESSION_SECRET` logt iedereen uit en maakt openstaande herstelcodes en koppelcodes ongeldig (TOTP blijft
   werken; maak daarna nieuwe herstelcodes).
2. Draai Crypie op een eigen https-domein in plaats van een gegenereerde `*.sslip.io`-naam.
3. Zet zo nodig `FORWARDED_ALLOW_IPS` op het netwerk van Coolify's Traefik (standaard loopback, de
   privé-reeksen en IPv6 `fc00::/7`), nooit op `*`: anders kan een bezoeker zijn eigen IP-adres kiezen en
   de wachttijd omzeilen. Staat Cloudflare ervoor, voeg dan de IP-reeksen van Cloudflare toe, anders delen
   alle bezoekers één wachttijd. Het client-IP is het meest rechtse adres in `X-Forwarded-For` dat niet in
   de lijst staat. Stuurt een proxy die niet in de lijst staat `X-Forwarded-Proto: https`, dan staat er
   één waarschuwing in de log; controleer dan de lijst.
4. Houd de `readTimeout` van Traefik's entrypoint kort, of zet buffering aan met `maxRequestBodyBytes`:
   uvicorn heeft geen eigen time-out op trage uploads, en 64 trage verbindingen laten `/health` en het
   overzicht een 503 geven.
5. Haal bij de eerste deploy de setup-link uit de log en maak het account meteen aan.
6. Vervang je CryptoPanic-token bij CryptoPanic: eerdere versies schreven hem leesbaar in de log. Wis ook
   de oude logs in Coolify.
7. Gebruik een Bitvavo-key met alleen de nodige rechten (view, en trade pas bij live; nooit opnemen) en
   een IP-whitelist op het serveradres, en zet dan `BITVAVO_KEY_RESTRICTED=true`.
8. Optioneel: zet `LIVE_CAPITAL_EUR`, en laat de bot live werken op een apart Bitvavo-(sub)account.
   Zet voor live in elk geval een meldingskanaal (`NOTIFY_URL`) en stuur een testmelding.
9. Controleer na de update je opgeslagen waarden: wat boven een nieuw plafond lag is verlaagd, en een
   `http`-feed moet `https` worden voordat je het blok Nieuws weer kunt opslaan.
10. In GitHub: zet de workflow-rechten van Actions op 'Read' en bescherm de `main`-branch.

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
| `API_DOCS_ENABLED` | `false` | `/docs`, `/redoc` en `/openapi.json` aan (ook dan achter de login); Swagger UI en ReDoc komen dan in een vaste versie met SRI-hash van jsDelivr |
| `FORWARDED_ALLOW_IPS` | loopback, privé-reeksen en `fc00::/7` | Proxies die `X-Forwarded-For`/`-Proto` mogen zetten |
| `BITVAVO_KEY_RESTRICTED` | `false` | Zet op `true` als de Bitvavo-key alleen view/trade-rechten en een IP-whitelist heeft; haalt de herinnering in live-modus weg (wordt niet gecontroleerd) |
| `EXIT_CHECK_INTERVAL_MIN` | 2 | Minuten tussen controles van stop-loss en take-profit (1-60; ook in het portaal) |
| `LIVE_CAPITAL_EUR` | 0 | Bedrag dat de bot live beheert; 0 = het hele account (ook in het portaal) |
| `MAX_TRADE_EUR_CEILING` | 1000 | Plafond voor het maximum per order, alleen via de omgeving |
| `MAX_EXPOSURE_PCT_CEILING` | 100 | Plafond voor het percentage in crypto, alleen via de omgeving |
| `MAX_TRADES_PER_DAY_CEILING` | 50 | Plafond voor het maximum aantal orders per dag, alleen via de omgeving |
| `NOTIFY_URL` | leeg | Meldingskanaal (ntfy-topic of webhook, alleen https); leeg = uit. Zie [Meldingen](#meldingen) |
| `NOTIFY_TOKEN` | leeg | Optioneel bearer-token voor het meldingskanaal |
| `NOTIFY_FORMAT` | `auto` | `auto`, `ntfy` of `json` |
| `NOTIFY_EXIT_FAIL_MIN` | 10 | Minuten (1-1440) voordat een falende bewaking van stop-loss en take-profit gemeld wordt |
| `CRYPIE_MEM_LIMIT` / `CRYPIE_CPUS` | `768m` / `1.0` | Grenzen van de container (docker-compose) |

## Structuur

```
app/
  main.py                   FastAPI: JSON-API (/api/...), dashboard (/), /trading, /journal, /plan, /strategy, /news, /settings
  config.py                 instellingen uit omgevingsvariabelen (en /run/secrets)
  settings_store.py         instellingen uit het portaal: validatie, plafonds, gevoelige sleutels
  security.py               middlewares: security-headers, cross-site bescherming, grenzen aan request bodies
  web.py                    Jinja-filters (bedragen, procenten, tijden, markdown), vaste teksten zoals de noodstop
  labels.py                 Nederlandse labels en regelzinnen die pagina's en handelscode delen
  numbers.py                één regel voor getallen met komma of punt (instellingen en orders)
  flash.py                  eenmalige meldingen na een actie (gesigneerd cookie)
  errors.py                 foutpagina's (HTML voor browsers, JSON voor de API) en Post/Redirect/Get
  notify.py                 meldingen buiten de app (ntfy of webhook)
  db.py                     SQLAlchemy-modellen: orders, settings, claude_usage, news_items, positions, decisions, evaluations, auth_users, auth_throttle
  auth/                     login: wachtwoord (passwords.py), TOTP (totp.py), cookie (session.py), account (service.py), routes + middleware (routes.py), CLI (__main__.py)
  bitvavo/client.py         Bitvavo REST-client (publiek + gesigneerd)
  bitvavo/broker.py         PaperBroker / LiveBroker
  indicators/core.py        SMA, EMA, RSI, MACD, Bollinger, drawdown (pure Python)
  strategy/model.py         strategie-schema (pydantic) en standaardstrategie
  strategy/engine.py        indicatorseries, signalen en backtester
  strategy/store.py         actieve en vorige strategie in de settings-tabel
  strategy/page.py          strategiepagina: leesbare validatiefouten, backtest-oordeel, resultaten per adres
  ai/strategy_assistant.py  Claude vertaalt een beschrijving naar een strategie (structured output)
  ai/news_analyst.py        Claude beoordeelt nieuwsberichten in batches (structured output)
  ai/decision_maker.py      Claude weegt signaal, nieuws en eigen resultaten tot een beslissing
  ai/evaluator.py           periodieke zelfevaluatie met strategievoorstel
  ai/budget.py              kosten per aanroep, maandbudget
  trading/engine.py         handelscyclus, posities, logboek, uitkomsten, evaluatie
  trading/risk.py           harde limieten en noodstop
  trading/health.py         de status van de bot (/api/status) en de limieten zoals ze nu staan
  trading/display.py        weergave van beslissingen en posities voor de pagina's (leesbare voorwaarden, netto resultaat)
  static/                   CSS en JavaScript (geen inline scripts of stijlen), iconen, manifest, meegeleverde Lightweight Charts in vendor/
  trading/plan.py           tradingplan: doel, eigen regels en lessen van de eigenaar (/plan), context voor Claude
  trading/journal.py        trade-journaal: posities + beslissingen + eigen notities (/journal)
  trading/history.py        data voor de grafieken op het overzicht: acties per markt, portefeuillewaarde terugrekenen
  news/sources.py           RSS-, GDELT-, cryptocurrency.cv-, Google News- en CryptoPanic-fetchers
  news/service.py           opslaan, ontdubbelen, beoordelen, marktsentiment per coin
tests/                      pytest (respx mockt Bitvavo, Claude wordt gemockt)
```
