# UI/UX-audit Crypie

**2 november 2026** · stand van de code: commit `ca42c79`

## Scope en methode

- **Pagina's:** Overzicht, Handelen, Journaal, Tradingplan, Strategie, Nieuws, Instellingen en Account, plus de inlogflow (setup, MFA, herstelcodes) en de foutpagina's.
- **Weergave:** viewports van 390, 820 en 1440 px, aangevuld met 320, 360 en 720 px voor reflow en zoom, in het lichte en het donkere thema.
- **Tests:** axe-core tegen WCAG 2.2 AA en een volledige toetsenbordronde. Daarnaast Playwright-interactietests op lokale instanties met testdata, een lege database, een onbereikbare Bitvavo, live-modus met een ongeldige sleutel, en 20 markten.
- **Uitgangssituatie:** de bot stond in oefenmodus en er was geen Claude-sleutel (`ANTHROPIC_API_KEY`). Waar Claude-uitvoer nodig was, is beoordeeld hoe de UI met de ontbrekende sleutel omgaat.
- **Verwerking:** de 8 auditstromen leverden samen 172 bevindingen op. Elke bevinding is in een tweede ronde nagespeeld. Na het ontdubbelen blijven er 107 over. Als twee stromen dezelfde bevinding een andere ernst gaven, is de schaal hieronder gevolgd.
- **Ernst:**
  - **kritiek:** kan leiden tot een verkeerde handelsactie, financieel verlies, verwarring tussen live en oefenmodus of buitensluiten, of blokkeert een kerntaak.
  - **hoog:** hindert een kerntaak ernstig, misleidt actief, of is een WCAG 2.2 AA-fout op kerninhoud of kernbediening.
  - **middel:** duidelijke frictie of inconsistentie bij een secundaire taak.
  - **laag:** afwerking.
- **Inspanning:** S = minder dan een uur tot een paar uur (template of CSS), M = een halve tot een hele dag, L = meerdere dagen of een herontwerp.
- **Bewijs:** de verwijzingen naar bestanden en regelnummers (stand van commit `ca42c79`) zijn het reproduceerbare bewijs. Screenshots en testscripts zijn tijdens de audit gemaakt en zitten niet in de repository; hun bestandsnamen staan er alleen ter herkenning bij. De oorspronkelijke ids uit de auditronde staan als "bron" in het bewijs.

## Samenvatting

- **Het fundament is sterk.** De harde limieten worden in code afgedwongen, live vraagt om step-up en de live-blokkade bestaat uit meerdere lagen. De uitleg van beslissingen is transparant en alles werkt zonder JavaScript. De problemen zitten in wat de UI laat zien na een klik, na een fout of tijdens een storing.
- **De handmatige order is het zwakste punt.** Na een weigering springt het formulier terug naar 'BTC-EUR / Kopen'. De bevestigingsvraag noemt de order niet. Een decimale komma kan van '25,50' 2550 maken. En elke weigering op grond van een eigen regel heet 'Bitvavo niet bereikbaar' (UX-02, UX-03, UX-19, UX-20).
- **Oefen- en live-modus lopen op drie plekken door elkaar.** In live staan oefenorders als 'Uitgevoerd' (UX-01). 'Standaard herstellen' kan zonder live-bevestiging naar LIVE schakelen (UX-04). En buiten Overzicht herken je live alleen aan een klein pilletje (UX-05).
- **De status vertelt niet altijd de waarheid.** Er staat 'Bot actief', terwijl de bot zonder Claude-sleutel nooit koopt (UX-06). Als de stop-loss-bewaking stilvalt, verschijnt er geen enkele melding (UX-07). Buiten de app hoor je niets (UX-08).
- **Feedback valt buiten beeld.** In Instellingen ziet een mislukte wachtwoordcheck er precies zo uit als een geslaagde opslag (UX-48). Bevestigingen ontbreken of staan duizenden pixels boven het scherm (UX-99). De sticky kop bedekt de plek waar een anker naartoe springt (UX-14).
- **De app groeit niet mee met normaal gebruik.** Na een week is Handelen 80.000 px hoog (UX-21). Nieuws kapt stil af op 150 berichten (UX-37). Het journaalfilter faalt al vanuit de standaardstand (UX-36).
- **De toegankelijkheid faalt op de belangrijkste elementen.** Kleurtokens en focusringen halen AA niet, juist bij de modus-pill, de noodstop en de orderknop. Eén CSS-ronde op de tokens lost het grootste deel van de 160+ axe-meldingen op (UX-69, UX-70).
- **Veel is snel op te lossen.** 56 van de 107 punten zijn klein werk (S), nog eens 7 zijn S–M. De top 10 lost alle 5 kritieke punten op en 13 van de 20 hoge.

| Ernst | Aantal |
|---|---|
| Kritiek | 5 |
| Hoog | 20 |
| Middel | 57 |
| Laag | 25 |
| **Totaal** | **107** (uit 172 ruwe bevindingen) |

## Wat goed is (behouden)

1. **Oefen en live zijn bewust gescheiden.** Je ziet 'Portefeuille (oefengeld)' en 'met oefengeld' tegenover een vetgedrukt 'Dit plaatst een echte order op Bitvavo'. De overstap naar live heeft een eigen bevestiging, en live orders en gevoelige instellingen vragen om je wachtwoord. Live kan pas als login, Bitvavo-sleutel en `LIVE_TRADING_ACK` alle drie in orde zijn. Tot die tijd is de optie Live uitgeschakeld, met uitleg waarom (`settings.html:56-65`).
2. **De noodstop is snel en eerlijk.** Pauzeren werkt bewust zonder wachtwoord. Tijdens een pauze staat er een oranje melding met een hervatknop en de uitleg dat stop-loss en take-profit gewoon doorlopen. Houd dit snelle noodpad bij elke aanpassing aan de step-up intact.
3. **De melding over onbewaakte posities is voorbeeldig.** Ze is rood en zegt 'NIET bewaakt'. Per positie staan de modus, hoeveelheid, stop, actuele koers en reden, plus een concrete handeling ('verkoop zelf op Bitvavo'). Ze verschijnt op Overzicht én Handelen (`_macros.html:79-90`). Laat haar ook afgaan bij een mislukte exit-check (UX-07).
4. **Beslissingen zijn controleerbaar.** Elke voorwaarde toont de echte waarden ('41,1 t.o.v. 35,0'). Daarnaast staan 'Besloten door', de zekerheid, 'Niet uitgevoerd: <reden>' en de koersuitkomst na 1, 24 en 72 uur.
5. **Claude krijgt nooit ongemerkt de macht.** Een voorstel komt met een verschiltabel en een backtest met eerlijke kanttekeningen (kosten wel meegerekend, slippage niet). Een server-side controle blokkeert strategieën zonder stop of met een koopvoorwaarde die altijd waar is, en toepassen vraagt om bevestiging. De kaart 'Harde limieten' zegt dat Claude die grenzen niet kan omzeilen. De bewaking van stop-loss en take-profit staat los van de kill switch en van Claude, en het interval daarvan is geklemd op 1–60 min.
6. **Instellingen tonen waar een waarde vandaan komt.** Per blok staat 'standaard' of 'N aangepast', per veld 'Standaard uit omgeving: X'. De plafonds uit de omgeving gelden ook server-side. De helpteksten zijn concreet en de validatie is specifiek en Nederlands ('BTC-USD is geen euromarkt…'). Na een fout blijven je ingevulde waarden staan.
7. **Het journaal is eerlijk over geld en goed om van te leren.** Instap en uitstap staan naast elkaar, met 'incl. kosten' en het netto resultaat naast het bruto. Je ziet ook Claude's redenering van dat moment. Notities bewerk je inline, en na opslaan kom je terug in dezelfde filterselectie (`back_url` met `:target`). De totalen zijn klikbaar.
8. **Nieuws is transparant.** Per bericht zie je of het meetelt en zo niet, waarom. Dubbele berichten worden gegroepeerd ('n bronnen', 'Ook gemeld door'). Externe links lopen via `safe_url` met `rel="noopener noreferrer"`.
9. **De koersgrafiek is doordacht.** Markers verschillen in vorm én kleur en hebben een legenda. Er is een OHLC-tooltip en een lijst 'Acties in deze periode als lijst'. De kleuren komen uit design-tokens en wisselen mee met het thema. Verouderde antwoorden worden genegeerd en je voorkeuren worden onthouden. Verticaal vegen over de grafiek scrolt gewoon de pagina.
10. **De technische basis is robuust.** Alles werkt zonder JavaScript met een strikte CSP. Een geslaagde actie stuurt door met een 303 (Post/Redirect/Get). Server-side vangnetten voorkomen een dubbele koop (één positie per markt, koop-locks). De pagina's zijn snel: 3–12 ms tot de eerste byte buiten Overzicht, en Overzicht laadt met 20 markten in ongeveer 1,9 s.
11. **Lege staten sturen door.** Voorbeelden: 'Geen open posities – De bot wacht op een koopsignaal… Laatste cyclus: …' met 'Bekijk beslissingen', 'Nog geen trades' → 'Naar Handelen' en 'Geen trades in deze selectie' → 'Filters wissen'.
12. **De basis voor toegankelijkheid en opmaak is goed.**
    - Elke pagina heeft `lang="nl"`, een unieke titel en landmarks, en het actieve navigatie-item heeft `aria-current="page"`. De grafiekknoppen hebben `aria-pressed`.
    - Winst en verlies hangen nooit alleen op kleur ('-' en '+'). `prefers-reduced-motion` wordt gerespecteerd en het thema laadt zonder flits.
    - Cijfers staan in `tabular-nums` en er zijn centrale Nederlandse opmaakfilters. Op 390 px heeft geen enkele pagina horizontale overflow.
    - De inlogflow gebruikt de juiste `autocomplete`-waarden en biedt een handmatige sleutel naast de QR-code. Herstelcodes bevatten geen verwarrende tekens, en een foute login verraadt niet of een gebruikersnaam bestaat.

## Top 10 aanbevelingen

1. **Scheid oefen en live in 'Laatste orders'** (UX-01). Voeg in `recent_orders` één `where(Order.mode == mode)` toe, zoals posities en grafiek dat al doen. Toon daarna in live een regel voor nog open oefenposities. Zo zie je bij de overstap naar live geen fictieve orders meer aan voor echte. *Kritiek · S*
2. **Haal de modus uit 'Standaard herstellen'** (UX-04). Neem `trading_mode` niet meer mee in de reset van een blok. Dan kan een reset nooit meer zonder live-bevestiging naar LIVE schakelen. *Kritiek · S*
3. **Maak van het orderformulier een echte controle** (UX-02, UX-19, UX-03). Geef je invoer terug na een fout. Toon een weigering in de orderkaart als 'Order niet geplaatst: …' in plaats van 'Bitvavo niet bereikbaar'. Bouw de bevestiging op uit de waarden zoals de browser ze heeft gelezen ('LIVE · echt geld: BTC-EUR kopen voor € 25,00?'). Dan valt een kommafout direct op. *Kritiek · M*
4. **Toon fouten en bevestigingen waar je kijkt** (UX-48, UX-99, UX-14). Zet een mislukte wachtwoordcheck in het betreffende blok en geef het foute veld focus. Gebruik één flash-mechanisme voor orders, cycli, opslaan en notities. Voeg `scroll-padding-top` toe (72 px, op mobiel 110 px), zodat de plek waar een anker naartoe springt niet onder de navigatie verdwijnt. *Kritiek en hoog · S–M*
5. **Laat de status de waarheid vertellen** (UX-06, UX-07). Leid de status af in één functie `bot_health()`, met teksten als 'Actief · koopt niet (geen Claude-sleutel)' en 'kopen geblokkeerd tot 00:00 UTC'. Meld bij een mislukte exit-check alle posities als onbewaakt. Voeg een regel toe die laat zien dat de bewaking draait: 'Bewaking: laatst gelukt 1 min geleden'. *Hoog · M*
6. **Maak live onmiskenbaar** (UX-05). Zet op elke pagina een volle rode balk 'LIVE · echt geld op Bitvavo' onder de navigatie en geef oefenmodus een eigen kleur. Laat 'Nu een cyclus draaien' en 'Opslaan als actieve strategie' in live om bevestiging vragen. *Hoog · S*
7. **Pas de kleurtokens en focusringen in één CSS-ronde aan** (UX-69, UX-70). Geef groen, accent, rood, oranje en de twee grijzen nieuwe waarden, voor licht en donker. Geef schakelaars en de modusknoppen een focusring van 2 px. Dat lost het grootste deel van de 160+ axe-meldingen op, juist bij de modus, de noodstop en de orderknop. *Hoog · S*
8. **Repareer drie kleine dingen die nu stuk of onjuist zijn** (UX-20, UX-36, UX-50). De helptekst zegt dat handmatige orders niet meetellen voor de daglimiet, maar ze tellen wel mee. Het journaalfilter faalt door `value=""`; maak daar `value="0"` van. Door `inputmode="numeric"` kun je herstelcodes op de telefoon niet intypen. Samen kost dit minder dan een uur. *Hoog · S*
9. **Laat zien dat een actie bezig is en voorkom dubbel versturen** (UX-100). Regel dit centraal in app.js: zet `aria-busy`, toon een knoptekst uit `data-busy` en blokkeer een tweede klik. Schakel de knop pas uit na het versturen, anders verdwijnt `name=action` uit het verzoek. Dat voorkomt dubbele cycli en dubbele Claude-kosten. *Hoog · S*
10. **Maak Handelen leesbaar, ook na een week** (UX-21, UX-23). Toon standaard alleen beslissingen met een actie, blokkade of advies, 25 per pagina, met een inklapbare checklist. Zet een wachtend strategievoorstel bovenaan in plaats van op 80.000 px. *Hoog · M*

## Bevindingen per thema

---

## 1. Vertrouwen & veiligheid

### [UX-01] In live-modus staan oefenorders als echte orders, en open oefenposities zijn onzichtbaar
**Ernst** kritiek · **Inspanning** S–M · **Waar** Overzicht > Laatste orders; Open posities, Handelen en Journaal na de overstap naar live

- **Probleem** `recent_orders` filtert niet op modus. Op een live-instantie met de testdata staan de drie oefenorders als 'Uitgevoerd' onder 'Laatste orders', direct naast 'Dit plaatst een echte order op Bitvavo.'. Tegelijk tonen Open posities, het Journaal en `/api/positions` niets. De bewaking van stop-loss en take-profit blijft de open oefenpositie in BTC wel volgen, en kan die ook verkopen. De koersgrafiek filtert wél op modus ('0 orders'), dus tabel en grafiek spreken elkaar op dezelfde pagina tegen.
- **Waarom het ertoe doet** Juist bij de overstap naar live houd je fictief geld voor echte orders, en lijkt een stop-loss-verkoop van een oefenpositie een echte verkoop. De kaart om de oefenportefeuille te resetten is in live verborgen, dus die positie kun je niet eens sluiten.
- **Aanbeveling** Filter `recent_orders` op `engine.mode`, zoals `open_positions` en de grafiek al doen. Toon in live een regel 'Nog 1 open oefenpositie (wordt nog bewaakt): bekijken of sluiten'. Bied bij de overstap naar live aan de oefenportefeuille te resetten, en geef Journaal en Handelen een filter op modus.
- **Bewijs** `app/main.py:2032`, `app/bitvavo/broker.py:416-417`, `dashboard.html:211, 234-252`, `engine.py:1490-1500`, `settings.html:128`; `live-orders.png`, `live-dashboard.png` (bron: H-01)

### [UX-02] Na een geweigerde order springt het formulier terug naar 'BTC-EUR / Kopen / € 25', en de bevestiging noemt de order niet
**Ernst** kritiek · **Inspanning** M · **Waar** Overzicht > Handmatige order; in live ook Handelen > Nu een cyclus draaien

- **Probleem** Weigert de server een order, dan rendert het dashboard opnieuw zonder je invoer. 'ETH-EUR · Verkopen · 25' wordt dan 'BTC-EUR · Kopen · 25'. Dat gebeurde in alle vier geteste gevallen: boven het maximum, al een open positie, verkopen zonder positie, en kopen tijdens een pauze.
  - De enige controle vooraf is de vaste vraag 'Weet je zeker dat je deze order wilt plaatsen?'. Die is in oefen- en live-modus gelijk en noemt geen markt, richting, bedrag of modus.
  - De orderknop is in beide modi dezelfde blauwe knop.
  - In live vraagt 'Nu een cyclus draaien' niet om bevestiging, terwijl een handmatige cyclus ook draait als automatisch handelen uit staat.
- **Waarom het ertoe doet** Wie na een fout alleen het bedrag aanpast, koopt BTC in plaats van ETH te verkopen. De bevestigingsvraag is het vangnet hiervoor, maar geeft niets om te controleren. In live is dat een onomkeerbare marktorder met echt geld.
- **Aanbeveling**
  - Geef `market`, `side` en `amount_quote` terug aan de template en zet `selected`/`value` op basis daarvan. Geef het wachtwoordveld nooit terug.
  - Bouw de bevestigingsvraag in app.js op uit de formulierwaarden, via een attribuut `data-confirm-order`. Bijvoorbeeld 'LIVE · echt geld: BTC-EUR kopen voor € 25,00?' of 'ETH-EUR verkopen voor € 25,00 met oefengeld?'.
  - Maak van de richting een keuzeknop 'Kopen | Verkopen' en laat de knoptekst meelopen ('€ 25 ETH verkopen').
  - Geef 'Nu een cyclus draaien' in live een `data-confirm`.
  - Pauzeren blijft werken zonder wachtwoord.
- **Bewijs** `app/main.py:2084-2093`, `dashboard.html:212, 215-221, 228`, `app.js:27-49`, `trading.html:37`; `sb-order-sell-no-position.png`, `live-order-form.png` (bron: A-01, G-01, D-21, F-02 deels, E-01 deels)

### [UX-03] Een decimale komma wordt in bedrag- en limietvelden stil genegeerd: '25,50' wordt 2550
**Ernst** kritiek · **Inspanning** M (S voor alleen de controle in de bevestiging) · **Waar** Overzicht > Handmatige order; Instellingen > alle getalvelden

- **Probleem** De velden zijn `type="number"`. Hoe een browser de komma in een getalveld leest, hangt af van browser en taalinstelling. In Chromium (getest met een Engelse en een Nederlandse locale) wordt '25,50' 2550 en '2,5' 25, en het veld blijft geldig. Je kunt er dus niet op rekenen dat de komma als decimaalteken wordt gelezen. In Instellingen hebben de velden `step="any"`:
  - 'Dagverlieslimiet 2,5' werd opgeslagen als 25%.
  - 'Maximum per order 50,5' wordt € 505.
  - Een verkoop van '25,50' wordt € 2.550. Voor verkopen geldt geen maximum per order en het bedrag wordt begrensd op de grootte van de positie, dus dan wordt de hele positie verkocht.
- **Waarom het ertoe doet** Een Nederlander typt vanzelf een komma. Een harde risicolimiet wordt dan ongemerkt tien keer ruimer, en de bevestiging toont het bedrag niet (UX-02).
- **Aanbeveling**
  - Snel: toon naast het veld '= € 25,50' op basis van `valueAsNumber`, en zet de waarde zoals de browser die heeft gelezen in de bevestigingsvraag.
  - Structureel: gebruik `type="text" inputmode="decimal"` met een pattern. Zet de komma server-side om naar een punt (`cfg.parse_values`, en `amount_quote` als tekst ontvangen) en weiger waarden als '1.000'.
  - Toon na het opslaan van limieten de oude en de nieuwe waarde. Vraag extra bevestiging als een harde limiet in één keer meer dan twee keer zo ruim wordt.
- **Bewijs** `dashboard.html:212, 221`, `settings.html:77-79`, `engine.py:1752-1755, 1798-1801`; `comma.js`, `comma-settings.js` (bron: F-01)

### [UX-04] 'Standaard herstellen' in Handelen kan zonder live-bevestiging naar LIVE schakelen
**Ernst** kritiek · **Inspanning** S · **Waar** Instellingen > Handelen > Standaard herstellen

- **Probleem** De reset van het blok wist ook `trading_mode`. Stel dat de omgeving op `TRADING_MODE=live` staat (een gedocumenteerde route) en je in het portaal naar Oefenmodus bent overgeschakeld. Dan zet de reset de bot terug op LIVE.
  - Je krijgt alleen de algemene vraag 'Alle instellingen in dit blok terugzetten…' en het verzoek om je wachtwoord.
  - De speciale live-bevestiging verschijnt niet, omdat app.js alleen kijkt welk keuzerondje is aangevinkt.
  - Het enige signaal is de grijze regel 'Standaard uit omgeving: Live · TRADING_MODE'.
- **Waarom het ertoe doet** Wie alleen een interval wil terugzetten, laat de bot zo onbedoeld echte orders plaatsen. De bewuste live-bevestiging wordt omzeild.
- **Aanbeveling** Neem `trading_mode` niet mee in de reset van een blok, zodat de modus alleen via het keuzerondje kan veranderen. Zou een reset toch de modus wijzigen, weiger dat dan server-side met de melding 'De omgeving staat op LIVE; kies de modus expliciet'.
- **Bewijs** `app/main.py:1762-1779, 1946-1951`, `app.js:42-48`, `settings.html:41, 107`, `README.md:412-413` (bron: C-02)

### [UX-05] Live-modus is alleen te herkennen aan een klein pilletje
**Ernst** hoog · **Inspanning** S–M · **Waar** Navigatie op alle pagina's; Handelen, Strategie en Journaal in live

- **Probleem** De modus staat als pilletje van 12 px in de navigatie.
  - 'Oefenmodus' is info-blauw met een contrast van 3,88:1, onder AA. Dezelfde stijl wordt gebruikt voor 'Open', 'uitgevoerd', '2 bronnen', links en info-meldingen. 'Live' is rood, maar even klein.
  - Op Overzicht verandert in live wel de tekst (ondertitel, orderkaart, wachtwoordveld). Handelen en Strategie noemen 'live' of 'echt geld' echter nergens.
  - De hero heet in oefenmodus 'Portefeuille (oefengeld)', maar in live alleen 'Portefeuille'.
  - 'Opslaan als actieve strategie' vraagt geen bevestiging en geen wachtwoord, terwijl je voor het wijzigen van een limiet wel je wachtwoord moet invullen.
- **Waarom het ertoe doet** Wie wisselt tussen een oefen- en een live-instantie, of na een tijd terugkomt, kan een cyclus draaien of een strategie activeren in de veronderstelling dat het om fictief geld gaat.
- **Aanbeveling**
  - Zet `data-mode` op `<body>` en geef live een vaste balk over de volle breedte onder de navigatie: 'LIVE · echt geld op Bitvavo'. Maak die massief rood met witte tekst; #d7261a haalt 5,02:1, #ff453a met wit maar 3,41:1.
  - Geef oefenmodus een eigen kleur in plaats van info-blauw, bijvoorbeeld de indigo van het merk (#5e5ce6).
  - Noem de modus in de ondertitels van Handelen en Strategie, en zet '(echt geld)' achter 'Portefeuille'.
  - Laat 'Nu een cyclus draaien' en 'Opslaan als actieve strategie' in live om bevestiging vragen (`data-confirm`).
- **Bewijs** `base.html:31-35`, `dashboard.html:8, 30, 228`, `trading.html:7, 37`, `strategy.html:79, 141`; axe `.nav-right > .pill-blue` 3,88/4,15; `nav--light.png`, `live-trading-top.png`, `live-dash-top.png` (bron: E-01, G-04, D-01 deels)

### [UX-06] 'Bot actief' staat groen terwijl de bot niet kan kopen
**Ernst** hoog · **Inspanning** M · **Waar** Overzicht en Handelen > hero; meldingen over de Claude-sleutel

- **Probleem** De status kent alleen Actief, Gepauzeerd en Uitgeschakeld.
  - Zonder Claude-sleutel, met de standaardinstelling `claude_unavailable_mode='hold'`, wordt elk koopsignaal 'afwachten'. Toch meldt Overzicht 'beslissingen volgen alleen de regels', en de lege staat 'De bot wacht op een koopsignaal'.
  - Na het bereiken van het dagmaximum ('max. 6 (vandaag 7)') of de dagverlieslimiet blijft het pilletje groen.
  - Is Bitvavo onbereikbaar, dan toont Handelen 'Actief' zonder enige melding. `run_cycle` faalt dan al voordat `last_error` wordt gezet. Bovendien staat `last_error` alleen op Handelen.
- **Waarom het ertoe doet** 'Draait de bot?' is de eerste vraag op deze pagina's. Nu wacht de eigenaar dagen op een aankoop die met deze instellingen niet kan komen.
- **Aanbeveling**
  - Leid de status op één plek af, in een functie `bot_health()` voor beide pagina's. Mogelijke teksten: 'Actief', 'Actief · koopt niet (geen Claude-sleutel, modus Afwachten)', 'Actief · kopen geblokkeerd tot 00:00 UTC (6/6 orders)', 'Storing · Bitvavo niet bereikbaar sinds 11:09' en 'Gepauzeerd'. Gebruik oranje of rood waar dat past.
  - Vervang de melding over de Claude-sleutel door één tekst die de ingestelde modus kent, met een link naar Instellingen › Handelen.
  - Toon `last_error` ook op Overzicht.
- **Bewijs** `dashboard.html:25, 47-57, 166`, `trading.html:14, 21-28`, `engine.py:323, 1112-1146`, `risk.py:153-182`; `sb-hero-limit-reached.png`, `fr-dashboard.png` (bron: G-06, A-06)

### [UX-07] Als de stop-loss-bewaking stilvalt, zie je niets; risico en bewaking staan nergens als actuele stand
**Ernst** hoog · **Inspanning** M · **Waar** Handelen en Overzicht > meldingen, limietkaart, Open posities; bewaking van stop-loss en take-profit

- **Probleem**
  - Mislukt het ophalen van de koersen voor de bewaking, dan stopt `run_exits` voordat de posities aan de beurt komen. `self.unprotected` wordt dan niet bijgewerkt en de fout belandt alleen in de log ('exit check failed', elke minuut).
  - De rode melding over onbewaakte posities blijft leeg, Handelen zegt 'Actief', en Open posities toont de stop alsof die bewaakt wordt. Dat is in strijd met wat `README.md:268-272` belooft.
  - `last_exit_run` en `exit_interval_min` gaan wel naar de template, maar worden nergens getoond.
  - De limietkaart toont alleen de grenzen. Je ziet niet hoeveel procent nu in crypto zit, of hoe het dagresultaat zich verhoudt tot de dagverlieslimiet.
- **Waarom het ertoe doet** De eigenaar denkt dat zijn posities beschermd zijn, terwijl er al uren geen controle draait. Als Bitvavo terugkomt, kan de koers al ver voorbij de stop liggen.
- **Aanbeveling**
  - Vang in `run_exits` een mislukte koers- of afstemmingsaanvraag af. Zet dan alle open posities in `unprotected`, met de reden 'koers niet op te halen sinds 12:36; stop-loss en take-profit worden niet gecontroleerd'.
  - Houd bij wanneer de bewaking voor het laatst lukte (`last_exit_ok`). Toon op Handelen 'Bewaking stop-loss/take-profit: laatst gelukt 1 min geleden (elke 2 min)', in rood zodra dat langer dan twee keer het interval geleden is.
  - Toon de limieten als meters: 'In crypto 6% van max. 50%', 'Dagresultaat −0,04% (grens −5%)', '3 van 6 orders'.
  - Laat `/health` alleen controleren of de app draait. De Docker-HEALTHCHECK en Traefik gebruiken dat endpoint; een 503 maakt het dashboard onbereikbaar op het moment dat je het juist nodig hebt.
- **Bewijs** `engine.py:1501, 1508, 1512, 1577`, `main.py:446-452, 1175-1201`, `trading.html:11-13, 56-75`, `Dockerfile:30-31`; `down-trading.png`, `down-openpos.png`, `server-8091.log` (bron: H-02, G-21)

### [UX-08] Geen enkele melding buiten de app
**Ernst** hoog · **Inspanning** M · **Waar** Hele app; Instellingen > Uit de omgeving

- **Probleem** Er is geen meldingskanaal; zoeken op ntfy, webhook, telegram, smtp en pushover levert niets op. De volgende situaties bestaan alleen als logregel of als melding op een pagina:
  - een onbewaakte positie;
  - een mislukte verkoop bij de stop;
  - een order met onbekende uitkomst;
  - het bereiken van de dagverlieslimiet;
  - een leeg Claude-budget;
  - een stilgevallen bewaking (UX-07).

  Een gecrashte container is extern wel te zien via `/health`, de inhoudelijke problemen niet.
- **Waarom het ertoe doet** De bot is gemaakt om zonder toezicht te draaien. Juist op de momenten waarop je moet ingrijpen, hoor je niets.
- **Aanbeveling**
  - Voeg via de omgeving één optioneel kanaal toe, bijvoorbeeld `NOTIFY_URL` voor een ntfy-topic of een webhook. Dat past bij hoe sleutels nu ook in de omgeving worden beheerd.
  - Toon het kanaal onder 'Uit de omgeving', met een knop 'Testmelding sturen'.
  - Stuur een melding bij: een onbewaakte positie, een mislukte verkoop bij de stop, een bewaking die langer dan X minuten niet lukt, een order met onbekende uitkomst, het bereiken van de dagverlieslimiet, een uitgevoerde live-order, een leeg Claude-budget en een herstart.
  - Begrens het aantal meldingen per gebeurtenis, en zet geen bedragen of sleutels in de URL.
- **Bewijs** `engine.py:1569-1576`, `main.py:446-452, 621-623`, `README.md:262-272, 527` (bron: H-03)

### [UX-09] Noodstop: alleen op Handelen, drie namen, en de uitleg verschilt per pagina
**Ernst** middel · **Inspanning** M · **Waar** Overzicht > hero; Handelen > Pauzeren; Tradingplan

- **Probleem**
  - Op Overzicht staat bij 'Bot actief' alleen de kleine link 'Naar handelen ›' (98×20 px). Om te pauzeren moet je dus eerst naar een andere pagina.
  - Hervatten kan op Overzicht juist met één klik, zonder bevestiging, en stuurt je daarna naar /trading.
  - De noodrem heet 'Pauzeren', 'Gepauzeerd' en 'kill switch'. Ook de uitleg wisselt: 'stop-loss of take-profit' (Overzicht), 'stop-loss, take-profit of een verkoopsignaal' (Handelen), 'open posities wel sluiten' (Plan).
  - Nergens staat dat cycli tijdens een pauze doorlopen, dat Claude nog wordt geraadpleegd (en betaald), en dat een verkoop op slecht nieuws geblokkeerd wordt.
  - Staat automatisch handelen uit, dan toont Handelen 'Uitgeschakeld' naast een rode knop 'Pauzeren', zonder uitleg.
  - Een pauze is niet te zien in de navigatie.
- **Waarom het ertoe doet** De noodrem is het belangrijkste vangnet. Twijfel over wat hij doet leidt tot te laat ingrijpen, of tot niet ingrijpen uit angst dat de bot alles verkoopt.
- **Aanbeveling**
  - Zet in de hero van Overzicht een massieve rode knop 'Pauzeren' (de macro `icon('pause')` bestaat al), met dezelfde bevestigingsvraag. Laat de actie via een gecontroleerde interne `next` terugkeren naar Overzicht.
  - Vraag bevestiging bij hervatten: 'De bot mag dan weer kopen.'
  - Gebruik één naam, bijvoorbeeld 'Kopen pauzeren (noodstop)', en één macro met de volledige uitleg.
  - Laat het pilletje in de navigatie 'Oefenmodus · gepauzeerd' tonen.
  - Houd Pauzeren altijd bereikbaar, ook als automatisch handelen uit staat, met één zin over het verschil.
- **Bewijs** `dashboard.html:14-18, 47-57`, `trading.html:26, 31-37`, `plan.html:60, 103`, `engine.py:993-994`, `main.py:1463`; `sb-dash-paused.png`, `paused-trading-top.png` (bron: A-07, G-05, A-23 deels)

### [UX-10] Strategie opslaan: geen bevestiging, ongeteste JSON mogelijk, en een oud tabblad draait een voorstel ongemerkt terug
**Ernst** middel · **Inspanning** M · **Waar** Strategie > Geteste strategie en JSON-editor; Handelen > Evaluaties

- **Probleem**
  - 'Opslaan als actieve strategie' maakt de strategie direct actief, zonder bevestiging. 'Standaardstrategie herstellen' vraagt wel om bevestiging.
  - In het blok 'Geteste strategie' staat de knop boven de backtest, en dus boven cijfers als '-15,7% tegenover +3,6% voor kopen en vasthouden' en 'Koopsignaal live 78,1%'.
  - De knop gebruikt `form="json-form"`. Pas je na het testen de JSON aan, dan sla je ongeteste JSON op.
  - Bij opslaan wordt niet gecontroleerd of de strategie intussen is gewijzigd. In een verouderd tabblad op 'Opslaan' klikken zette de stop-loss terug van 4% naar 5%. Handelen bleef daarna 'voorstel toegepast' tonen, en de knop om het voorstel opnieuw toe te passen kwam niet meer terug.
- **Waarom het ertoe doet** De strategie bepaalt elke aankoop; in live gaat het om echt geld.
- **Aanbeveling**
  - Geef de knop in het testblok een eigen formulier, met de geteste JSON in een verborgen veld. Of laat app.js de knop uitschakelen zodra de JSON verandert ('JSON gewijzigd: test opnieuw'). Zet de knoppen onder de backtest.
  - Vraag bevestiging in live, en als `live_buy_signal_pct > 20` of het rendement onder kopen en vasthouden ligt.
  - Stuur een `loaded_hash` mee. Wijkt de strategie intussen af, toon dan de `strategy_diff` met de keuze 'Toch overschrijven' of 'Laad de huidige'.
  - Bewaar de vorige strategie en bied 'Vorige strategie terugzetten' aan.
- **Bewijs** `strategy.html:66-83, 79, 132, 141`, `app/strategy/store.py:53-60`, `main.py:1009-1014`, `trading.html:261-269`; `strat-backtest-loose.png`, `tabs.js` (bron: B-04, H-04)

### [UX-11] Eigen regels in het tradingplan lijken bindend
**Ernst** middel · **Inspanning** S · **Waar** Tradingplan > Jouw plan

- **Probleem** De ondertitel zegt 'Het plan waar de bot zich aan houdt', maar de eigen regels zijn instructies voor Claude.
  - Na opslaan staat er 'Claude krijgt het vanaf de volgende beslissing mee', ook als er geen sleutel is.
  - Bij het plan staat niet dat Claude nu niet beschikbaar is.
  - Met `CLAUDE_UNAVAILABLE_MODE=indicators` koopt de bot gewoon op het strategiesignaal, en worden regels als 'niet kopen in het weekend' stil genegeerd.
- **Waarom het ertoe doet** De eigenaar kan verwachten dat zo'n weekendpauze hard wordt afgedwongen.
- **Aanbeveling**
  - Zet in de kop van 'Jouw plan' een regel die de werkelijke situatie toont. In de stand 'hold': 'Claude is niet beschikbaar; de bot koopt niets tot Claude terug is.' In de stand 'indicators' een waarschuwing: 'Deze regels worden nu niet toegepast.'
  - Toon de melding na opslaan alleen als `claude_available` waar is.
  - Verander de ondertitel in 'Jouw doel en regels voor Claude, plus wat de code altijd afdwingt'.
- **Bewijs** `plan.html:7, 21, 124`, `main.py:1512-1514`, `README.md:325-328`; `plan-after-save.png` (bron: B-03)

### [UX-12] De vrije omschrijving van een strategie kan de echte regels tegenspreken
**Ernst** laag · **Inspanning** S · **Waar** Strategie > samenvatting; Tradingplan > Instappen

- **Probleem** De omschrijving staat als grote lopende tekst boven de regels die de app zelf genereert. Pas je de JSON aan zonder de omschrijving bij te werken, dan staat 'RSI onder 35' naast de regel 'rsi14 lager dan 60'.
- **Waarom het ertoe doet** Wie de omschrijving leest in plaats van de regels, krijgt een verkeerd beeld van wanneer de bot koopt.
- **Aanbeveling** Maak de regels (`condition_nl`) de hoofdinhoud en toon de omschrijving klein, als 'Toelichting (vrije tekst)'. Waarschuw in de verschiltabel als koop-, verkoop- of stopregels wijzigen terwijl de omschrijving gelijk blijft. Toon op het plan alleen de regels.
- **Bewijs** `strategy.html:17`, `plan.html:69`; `strat-proposal-diff.png` (bron: B-20)

---

## 2. Informatiearchitectuur & navigatie

### [UX-13] De hoofdnavigatie verbergt pagina's zonder enige aanwijzing
**Ernst** hoog · **Inspanning** S–M · **Waar** Alle pagina's > `.nav-links`

- **Probleem** De zeven links staan in een horizontale strook met een verborgen scrollbalk en zonder vervaging aan de rand.
  - Op 768 en 820 px eindigt de strook precies na 'Strategie'. Nieuws en Instellingen ontbreken, en er is niet eens een afgesneden stukje dat verraadt dat er meer is.
  - Op 390 px zijn 3,5 links zichtbaar; op 1024 px valt 'Instellingen' half weg.
  - De actieve link wordt niet in beeld geschoven. Op /settings en /news zie je in de navigatie dus niet waar je bent.
  - Op mobiel is de vaste kop 94 px hoog.
- **Waarom het ertoe doet** Op een tablet lijken Nieuws en Instellingen, waar de limieten staan, niet te bestaan.
- **Aanbeveling** Snel: schuif in app.js de actieve link in beeld (`scrollIntoView({inline:'center', block:'nearest'})`) en laat de rand vervagen als de strook te breed is. Daarna: laat de links onder 900–1100 px over meerdere regels lopen, of gebruik een compacte tabbalk met 4 à 5 bestemmingen plus 'Meer'.
- **Bewijs** `app.css:150-157, 225-227`; `top-news-820.png`, `top-settings-390.png`, `nav-720.png`, `mob-top.png` (bron: F-07, D-18, G-15 deels)

### [UX-14] De vaste kop bedekt velden met focus en de plekken waar een anker naartoe springt
**Ernst** hoog · **Inspanning** S · **Waar** Alle pagina's; vooral Instellingen (#trading, #limits), Journaal (#trade-N) en Nieuws

- **Probleem** Er is geen `scroll-padding-top` of `scroll-margin`.
  - Na een sprong naar `/settings#limits`, na opslaan of na een fout staat de kaarttitel op y=21, onder een kop van 57 px (op mobiel 94 px). De titel en het pilletje 'N aangepast' zijn dan onzichtbaar.
  - Met Shift+Tab verdwijnen op Nieuws 6 van de 17 focusplekken volledig onder de kop. Op Instellingen zijn dat er 4 volledig en 3 gedeeltelijk.
  - Bij 400% zoom neemt de kop 37% van het scherm in.
- **Waarom het ertoe doet** Na een sprong weet je niet waar je bent. Toetsenbordgebruikers raken hun focus kwijt bij de instellingen voor limieten en modus (WCAG 2.4.11, AA).
- **Aanbeveling** Gebruik `html { scroll-padding-top: 72px }`, en onder 640 px `110px`. Daar loopt de navigatie over twee rijen, dus 80 px is te weinig. Maak de kop niet-vast bij weinig schermhoogte: `@media (max-height: 500px) { .nav { position: static } }`.
- **Bewijs** `app.css:121-129, 226-227`, `settings.html:17, 26`, `main.py:1674`; `settings-anchor-limits.png`, `settings-anchor-mobile.png`, `obscured.js` (bron: D-04, F-08)

### [UX-15] Overzicht, Handelen en Strategie hebben geen duidelijke eigen rol
**Ernst** middel · **Inspanning** M · **Waar** Overzicht, Handelen, Strategie, Tradingplan, Journaal

- **Probleem**
  - 'Open posities' op Overzicht toont Nu en Resultaat. De link 'Alle details ›' gaat naar Handelen, waar juist die kolommen ontbreken.
  - Dezelfde informatie staat op meerdere plekken: 'Resultaat 30 dagen' op twee pagina's (met 2 en met 5 cijfers), de limieten op drie plekken met andere tekst, en de strategiebeschrijving (105 woorden) volledig op drie pagina's.
  - Het handmatige orderformulier staat op Overzicht, niet op 'Handelen'.
  - De strategie wijzig je op Strategie én via 'Voorstel toepassen' onderaan Handelen.
  - Op Handelen wordt de ontbrekende Claude-sleutel vier keer gemeld.
  - 'Laatste cyclus per markt', het antwoord op 'waarom deed de bot dit?', staat pas op y≈1640 (desktop) en ≈2870 px (mobiel), onder de Agenda en de Trend.
- **Waarom het ertoe doet** Je weet niet welke pagina de bron is, en een link die meer belooft, geeft minder.
- **Aanbeveling**
  - Geef elke pagina één rol. Overzicht: gezondheid, resultaat en snelle acties. Handelen: logboek en bediening.
  - Maak 'Open posities' op Handelen de volledige versie (met Nu, resultaat in € en %, en afstand tot de stop) en laat de link naar `#open-posities` wijzen.
  - Zet 'Laatste cyclus per markt' direct onder de hero, en bundel Agenda, Trend en Limieten in een inklapbaar blok 'Context en limieten'.
  - Toon de strategie elders alleen als naam, één regel en de kenmerken.
  - Link vanaf Strategie naar een wachtend voorstel ('1 voorstel van Claude wacht').
  - Meld een ontbrekende sleutel maar één keer.
- **Bewijs** `dashboard.html:145-168, 175, 184-194, 208-232`, `trading.html:14, 42-54, 163-185, 267`, `strategy.html:79, 141`; `trading--desktop-light.png` (bron: A-18, G-16, G-15 deels)

### [UX-16] Vernieuwen of 'Terug' na een actie herhaalt die actie, of toont een bevestiging van iets dat niet gebeurde
**Ernst** middel · **Inspanning** M · **Waar** Strategie (Testen, Voorstel maken, Opslaan); foutpaden van /orders en /trading/action; URL's met ?saved=

- **Probleem**
  - De strategieacties en de foutpaden van orders en handelsacties tonen hun resultaat direct op het adres van het verzonden formulier. Vernieuwen verstuurt ze opnieuw, na alleen de algemene vraag van de browser en dus buiten de eigen bevestiging om. Bij 'Voorstel maken' is dat een nieuwe, betaalde Claude-aanroep.
  - Een bladwijzer naar /orders, /trading/action, /news/refresh of /journal/1/note geeft een kale foutmelding in JSON (405).
  - `/settings?saved=paper` toont 'Oefenportefeuille gereset' en `/plan?saved=1` toont 'Tradingplan opgeslagen', ook na vernieuwen of bij een hersteld tabblad.
- **Waarom het ertoe doet** Gewone gewoontes als vernieuwen, terug of een tabblad herstellen kosten zo geld of geven je onterecht het idee dat iets gelukt is.
- **Aanbeveling**
  - Stuur na Opslaan, Standaard herstellen en Voorstel maken door naar een gewone pagina (Post/Redirect/Get). Bewaar het resultaat kort op de server en stuur door naar `/strategy?result=<id>`.
  - Toon orderfouten via een eenmalige melding (zie UX-19).
  - Haal `?saved=` na het tonen uit de URL met `history.replaceState` in app.js.
  - Laat een gewone paginaweergave van deze formulieradressen (GET) met een 303 doorsturen naar de bijbehorende pagina.
- **Bewijs** `main.py:940-1026, 1447-1462, 2085-2094`; curl GET /orders → 405; `prg.js` (bron: H-08)

### [UX-17] Geen begeleiding bij het eerste gebruik
**Ernst** laag · **Inspanning** M · **Waar** Overzicht met een lege database

- **Probleem** Met een lege database start de bot direct ('Bot actief'), met de standaardstrategie, de standaardlimieten en € 1.000 oefengeld. Dat is veilig. Maar het lege tradingplan ('nog leeg') en de stappen om de bot op je eigen doel af te stemmen worden nergens genoemd.
- **Waarom het ertoe doet** Plan, limieten en strategie blijven zo op hun standaardwaarden staan.
- **Aanbeveling** Toon een wegklikbare kaart 'Aan de slag' zolang er geen plan of trades zijn. Geef per stap aan of die klaar is: markten en limieten, strategie en backtest, tradingplan, Claude-sleutel (optioneel) en de voorwaarden voor live.
- **Bewijs** `plan.html:19`, `dashboard.html:166`, `journal.html:194`; `fr-dashboard.png`, `fr-plan.png` (bron: G-17)

### [UX-18] Links en informatie staan op dezelfde plek in de kaartkop
**Ernst** laag · **Inspanning** S · **Waar** Rechts in de kaartkop (`.card-header .more`) op Overzicht, Handelen en Journaal

- **Probleem**
  - Op dezelfde plek staat soms grijze informatie ('Bitvavo, nu', 'deze maand') en soms een link ('Aanpassen ›', 'Details ›', 'Alle details ›'). Alleen de kleur verschilt.
  - 'Details ›' en 'Alle details ›' gaan allebei naar /trading.
  - Op Handelen is de hele zin 'laatste 7 dagen · 4 beslissingen · aanpassen ›' een link naar Instellingen.
- **Waarom het ertoe doet** Je kunt niet voorspellen wat klikbaar is en waar een link heen gaat.
- **Aanbeveling** Splits de plek in informatie (`.card-meta`, nooit klikbaar) en een link (`.card-link`). Formuleer elke link als werkwoord plus bestemming, zoals 'Naar Handelen' of 'Bewaartermijn aanpassen'.
- **Bewijs** `dashboard.html:146, 185`, `trading.html:188`, `app.css:251` (bron: E-20)

---

## 3. Overzicht & Handelen

### [UX-19] Elke orderfout heet 'Bitvavo is op dit moment niet bereikbaar'
**Ernst** hoog · **Inspanning** S–M · **Waar** Overzicht > Handmatige order (POST /orders)

- **Probleem** De template zet vóór elke foutmelding de tekst 'Bitvavo is op dit moment niet bereikbaar. Koersen en saldo kunnen verouderd of leeg zijn.' Wat je dan te zien krijgt:
  - '…(Order niet geplaatst: maximaal € 100.00 per order (maximum per order))'
  - '…er staat al een open positie in BTC-EUR'
  - '…maximum van 6 trades per dag bereikt'
  - in live: '…Het wachtwoord klopt niet.'

  Een weigering van de API-sleutel door Bitvavo (403, 'API Key must be of length 64') heet ook 'niet bereikbaar'. De melding staat op y≈303, het formulier op y≈2814. De URL blijft /orders (HTTP 400), geen navigatie-item is actief, en vernieuwen verstuurt de order opnieuw.
- **Waarom het ertoe doet** Je zoekt een storing bij de exchange, terwijl een eigen regel de order tegenhield. Je probeert het opnieuw of gaat zelf op Bitvavo handelen. Bovendien wordt de echte storingsmelding minder geloofwaardig.
- **Aanbeveling**
  - Gebruik een eigen variabele `order_error` en toon die als foutmelding 'Order niet geplaatst' in de kaart 'Handmatige order' (`id="order"`, `role="alert"`).
  - Toon de Bitvavo-melding alleen bij netwerkfouten en time-outs. Geef 401 en 403 een eigen tekst: 'Bitvavo weigert de API-sleutel'.
  - Stuur na een fout door naar `/?order_error=…#order`, of gebruik een eenmalige melding.
  - Schrijf het bedrag als '€ 100,00' en haal de dubbele '(maximum per order)' weg.
- **Bewijs** `dashboard.html:24`, `main.py:2084-2094`, `engine.py:1754-1756`; `sb-order-above-max.png`, `sb-order-while-paused.png`, `live-order-wrongpw.png`, `order-error-notice.png` (bron: A-02, F-02, G-02, D-06 deels, A-23 deels)

### [UX-20] De helptekst bij de handmatige order klopt niet
**Ernst** hoog · **Inspanning** S · **Waar** Overzicht > Handmatige order (helptekst onder het formulier)

- **Probleem** Er staat: 'Handmatige orders tellen niet mee voor de daglimiet van de bot.' Dat is onjuist.
  - `trades_today` telt elke uitgevoerde order, ook handmatige. Een handmatige aankoop gaat door `check_buy`, dus door het dagmaximum, de dagverlieslimiet, de afkoelperiode en de limiet op het belegde deel.
  - Na 4 handmatige orders stond er 'max. 6 (vandaag 7)' en werd de volgende aankoop geweigerd. Ook de bot kocht die dag niets meer.
  - Instellingen en de README zeggen wel 'ook handmatige orders'.
  - 'Minimaal € 10 per order' klopt maar half: verkopen kan vanaf € 5.
- **Waarom het ertoe doet** Je denkt dat handmatig handelen de bot niet raakt, terwijl je hem zo voor de rest van de dag blokkeert. Bovendien spreken twee pagina's elkaar tegen.
- **Aanbeveling**
  - Vervang de tekst door: 'Kopen: € 10 – € 100 per order. Handmatige orders tellen mee voor het maximum van {{ max }} orders per dag (vandaag {{ n }}) en vallen onder alle harde limieten en de pauze. Verkopen kan vanaf € 5; blijft er minder dan € 5 over, dan wordt de hele positie verkocht.'
  - Toon een waarschuwing in de kaart zodra `trades_today >= max - 1`.
  - Corrigeer ook README:344.
- **Bewijs** `dashboard.html:231`, `risk.py:47-63, 159-160, 179-182`, `engine.py:1749, 1778`, `settings_store.py:313`; `trade-grid3.png`, `s14.js` (bron: A-04, G-03)

### [UX-21] 'Alle cyclussen' groeit onbeperkt en duwt Gesloten posities en Evaluaties tienduizenden pixels omlaag
**Ernst** hoog · **Inspanning** M · **Waar** Handelen > Alle cyclussen

- **Probleem** Met de standaardinstellingen (elke 60 min, 2 markten, 7 dagen bewaren) zijn dat 336 beslissingen per week; de app haalt er tot 5000 op.
  - Elke rij is ongeveer 224 px hoog door de checklist.
  - Na een week gemeten: /trading is 528 KB HTML, 10.018 DOM-elementen en 80.687 px hoog op 390 px. 'Gesloten posities' begint op 79.828 px, 'Voorstel toepassen' op ongeveer 80.128 px.
  - Er is geen filter, geen paginering en niets om in te klappen.
  - Op 1440 px valt de kolom Toelichting rechts buiten beeld, op 820 px ook 'Na 24u' en 'Na 72u'.
- **Waarom het ertoe doet** Na een week normaal gebruik zijn 'wat deed de bot en waarom' en 'beoordeel het voorstel' praktisch onvindbaar. Aankopen, verkopen en blokkades verdwijnen tussen honderden keren 'Afwachten'.
- **Aanbeveling**
  - Toon standaard alleen beslissingen met een actie, blokkade of Claude-advies. Bied een keuze 'Belangrijk | Alles' en een filter per markt, met 25 rijen per pagina of 'Meer laden'.
  - Zet de checklist per rij in een uitklapblok (`<details>`), met in de tabel alleen een samenvatting als 'Afwachten · 1/2 voorwaarden'. Zet de Toelichting onder de rij in plaats van in een eigen kolom.
  - Zet Evaluaties en Gesloten posities boven het logboek, of maak er tabbladen van.
- **Bewijs** `main.py:1411-1413`, `trading.html:187-213`; `longtrading.js`, `cycles-table-1440.png` (bron: A-10, F-09)

### [UX-22] De controle in het orderformulier volgt de regels van de bot niet, en een verkoop sluit ongemerkt de hele positie
**Ernst** middel · **Inspanning** M · **Waar** Overzicht > Handmatige order

- **Probleem**
  - Het bedragveld heeft een minimum van 10 voor zowel kopen als verkopen, en geen maximum.
  - Een verkoop van € 7 blokkeert de browser, terwijl de bot verkopen vanaf € 5 toestaat.
  - Een aankoop van € 500 wordt pas op de server geweigerd.
  - Bij verkopen kies je een eurobedrag zonder de waarde van je positie te zien. Een invoer van € 1000 verkocht ongemerkt de hele positie (€ 24,89), en een restant onder € 5 wordt ook zonder melding mee verkocht.
  - Ook bij verkopen staat standaard € 25 ingevuld.
- **Waarom het ertoe doet** Je weet bij een verkoop niet hoeveel er werkelijk verkocht wordt.
- **Aanbeveling**
  - Wissel het minimum en maximum per richting in app.js: kopen van `min_trade_eur` tot `max_trade_eur`, verkopen van € 5 tot de waarde van de positie.
  - Toon bij Verkopen de positie ('Open: 0,00086 BTC ≈ € 59,83') en een keuze 'Hele positie verkopen'.
  - Meld vooraf als er minder dan € 5 overblijft.
- **Bewijs** `dashboard.html:220-222`, `engine.py:55, 1749-1756, 1802-1806`; `s8.js`, `s13.js` (bron: A-19, G-01 deels)

### [UX-23] Voorstel toepassen: knop diep onderaan, geen bevestiging achteraf, verschiltabel verdwijnt, geen weg terug
**Ernst** middel · **Inspanning** M · **Waar** Handelen > Evaluaties door Claude

- **Probleem**
  - De knop staat op y≈4643 px (1440 px breed) en ≈7073 px (390 px), en zakt verder met elke nieuwe beslissing.
  - Na het toepassen volgt een kale doorverwijzing naar de bovenkant van /trading.
  - De verschiltabel verdwijnt; alleen '· voorstel toegepast' blijft over. 'Vorige strategie terugzetten' bestaat niet.
  - Open posities houden de stop-loss die bij de aankoop is gezet; alleen de trailing stop volgt de nieuwe strategie. Dat staat nergens.
- **Waarom het ertoe doet** Je ziet niet of het gelukt is, kunt niet meer nagaan wat er veranderde, en verwacht mogelijk dat de nieuwe stop ook voor je open positie geldt.
- **Aanbeveling**
  - Toon na toepassen een bevestiging: 'Strategie "…" is nu actief. Open posities houden hun huidige stop-loss en take-profit.'
  - Houd de verschiltabel zichtbaar, onder de kop 'Wat er veranderde op 2 nov 11:07'.
  - Bewaar de vorige strategie en bied aan die terug te zetten.
  - Zet een wachtend voorstel bovenaan Handelen, en toon het als melding op Overzicht: '1 strategievoorstel wacht op je beoordeling ›'.
- **Bewijs** `trading.html:240-275`, `main.py:1340-1367, 1463`, `engine.py:1303`; `eval-1440.png`, `sb-after-apply-eval.png` (bron: A-09)

### [UX-24] 'Nu evalueren' is uitgeschakeld zonder uitleg, terwijl de tekst zegt dat je erop moet klikken
**Ernst** middel · **Inspanning** S · **Waar** Handelen > Evaluaties door Claude

- **Probleem**
  - Zonder sleutel is de knop uitgeschakeld (half doorzichtig, en daardoor bijna gelijk aan de actieve variant), zonder `title` of `aria-describedby`.
  - De lege staat zegt '…of klik op 'Nu evalueren''.
  - De beschrijving 'Elke 7 dagen kijkt claude-fable-5-1 terug' noemt de ruwe model-id en klopt zonder sleutel nooit.
  - Een toch verstuurd verzoek wordt zonder melding geaccepteerd.
- **Waarom het ertoe doet** Je wacht op een wekelijkse evaluatie die nooit komt, en krijgt een opdracht die je niet kunt uitvoeren.
- **Aanbeveling**
  - Toon zonder sleutel de tekst 'Evalueren kan pas met ANTHROPIC_API_KEY (in de serveromgeving)', gekoppeld via `aria-describedby`.
  - Maak de lege staat afhankelijk van de sleutel: 'Automatische evaluaties staan uit zolang de Claude-sleutel ontbreekt.'
  - Toon een leesbare modelnaam.
  - Geef een foutmelding als er toch een verzoek zonder sleutel binnenkomt.
- **Bewijs** `trading.html:243, 245, 273`, `main.py:1447`; `eval-card-nokey-mobile.png` (bron: A-21, F-15)

### [UX-25] Tijden van cycli kloppen niet of verouderen ongemerkt
**Ernst** middel · **Inspanning** M · **Waar** Overzicht en Handelen > hero, kaart 'Volgende cyclus', Koersen; Nieuws

- **Probleem**
  - `engine.last_run` staat alleen in het geheugen. Na een herstart tonen beide pagina's tot één interval lang 'Laatste cyclus: nog niet' naast 'Bot actief', terwijl de database het tijdstip wel kent.
  - De kaart 'Volgende cyclus' noemt geen volgend tijdstip.
  - Pagina's verversen zich niet vanzelf. In een open of hersteld tabblad blijft 'Laatste cyclus: zojuist' staan.
  - De kaart 'Koersen' heet 'Bitvavo, nu', zonder tijdstip.
- **Waarom het ertoe doet** Een planner die echt is stilgevallen, is zo niet te onderscheiden van een herstart, en een koers van uren oud lijkt actueel.
- **Aanbeveling**
  - Haal het tijdstip bij een herstart uit de database (`_load_last_run('trading cycle')`), of bewaar `last_run` in `save_state`.
  - Toon 'laatste 10:49 · volgende 11:49 (over 43 min)'. Kleur dat oranje met 'de planner draait mogelijk niet' zodra het langer dan twee keer het interval geleden is.
  - Zet een vast tijdstip bij live gegevens ('Koersen om 12:46'), en laat tijden als 'zojuist' in de browser meelopen (`data-ts`).
  - Ververs hero en status elke 30–60 s via een licht endpoint `/api/status`, zolang het tabblad zichtbaar is.
- **Bewijs** `engine.py:347, 535`, `main.py:383-395`, `trading.html:69-75`, `dashboard.html:55, 107`; `stale.js` (bron: A-05, F-12)

### [UX-26] Het resultaat van een open positie is bruto, en twee 'resultaten' staan zonder uitleg naast elkaar
**Ernst** middel · **Inspanning** S · **Waar** Overzicht > Open posities, hero, Resultaat 30 dagen

- **Probleem**
  - De kolom Resultaat is de huidige koers gedeeld door de aankoopkoers, min 1, zonder kosten. Er staat '-0,0%' bij een positie die na kosten ongeveer −0,5% staat. Elders in de app staat overal 'netto' of '(bruto)' bij.
  - 'Sinds start -€ 0,37' (inclusief open posities en kosten) en 'Resultaat 30 dagen -€ 0,20' (alleen gesloten trades) staan zonder uitleg naast elkaar.
- **Waarom het ertoe doet** Een positie lijkt rond break-even te staan, terwijl hij na kosten al verlies maakt. Twee verschillende getallen zonder uitleg wekken wantrouwen.
- **Aanbeveling**
  - Toon 'Ongerealiseerd (netto)' in € en %, inclusief de kosten van aan- en verkoop, of noem de kolom 'Koers t.o.v. instap (bruto)'.
  - Zet onder 'Sinds start' de toevoeging 'incl. open posities en kosten'.
  - Hernoem de kaart naar 'Gesloten trades · 30 dagen'.
- **Bewijs** `dashboard.html:40-44, 158, 184-194`, `journal.html:146, 161` (bron: G-14)

### [UX-27] De belangrijkste informatie staat niet bovenaan
**Ernst** middel · **Inspanning** M · **Waar** Overzicht (volgorde, hero op mobiel); Handelen > hero

- **Probleem**
  - 'Open posities' is het enige blok met actueel risico. Het staat pas op y=1890 (desktop) en ≈2800 px (390 px breed), na twee grafieken, Koersen en Saldo, en toont alleen een percentage.
  - Op mobiel staat de status van de bot als laatste in de hero. Elke extra melding duwt hem onder de rand van het scherm.
  - Op Handelen is de status een pilletje van 14 px in een element met `font-size: 32px` in de HTML, wat geen effect heeft.
- **Waarom het ertoe doet** Voor de vragen 'staat er iets open, hoe staat het ervoor, draait de bot?' moet je nu ver scrollen, zeker op je telefoon.
- **Aanbeveling**
  - Zet Open posities direct onder de hero, of vat ze daarin samen: '1 open · −€ 0,30 (−0,5%)'. Toon het resultaat in € én %.
  - Zet Koersen onder Open posities of in de kop van de koersgrafiek.
  - Zet de status op mobiel bovenaan de hero (`.hero-status { order: -1 }`), als één regel: 'Bot actief · laatste cyclus 4 min geleden'.
  - Vervang de stijl in de HTML door een klasse `.status-lg`.
- **Bewijs** `dashboard.html:27-168`, `trading.html:20`, `app.css:266-271`; `auth-dash-mobile-fold.png`, `paused-trade-hero--light.png` (bron: E-08, A-24 deels)

### [UX-28] Het evaluatierapport van Claude verschijnt met zichtbare opmaakcodes
**Ernst** middel · **Inspanning** S · **Waar** Handelen > Evaluaties door Claude

- **Probleem** De evaluator vraagt Claude het rapport (`report_nl`) in markdown te schrijven, en de engine plakt daar '**Voorstel:**' aan vast. De template toont dat als platte tekst (`white-space: pre-wrap`). Daardoor staan '## Resultaat', '## Wat ging goed' en '## Wat kan beter' letterlijk in beeld.
- **Waarom het ertoe doet** Juist het rapport waarop je een strategiewissel beoordeelt, oogt onaf en leest moeizaam.
- **Aanbeveling** Zet de markdown op de server om naar HTML, met alleen een veilige selectie: eerst alles escapen, dan alleen koppen, lijsten, nadruk en alinea's toestaan. Sta geen HTML, links of afbeeldingen toe, want het rapport is mede gebaseerd op onbetrouwbare nieuwstekst. Een kleine eigen omzetter houdt het aantal afhankelijkheden laag. Doe hetzelfde voor de toelichting bij strategievoorstellen.
- **Bewijs** `app/ai/evaluator.py:26-27`, `engine.py:1931-1932`, `trading.html:249`, `app.css:529-536`; `eval-report-raw-markdown.png`, `trade-eval--dark.png` (bron: A-13, E-11, G-12)

---

## 4. Grafieken & cijfers

### [UX-29] Bij goedkope coins worden koers, stop-loss en checklist allemaal '0,000004' of '0,0000'
**Ernst** hoog · **Inspanning** S · **Waar** Overzicht > Koersen en koersgrafiek; Handelen > checklist; melding over onbewaakte posities

- **Probleem** Prijzen worden opgemaakt met een vast aantal decimalen (maximaal 6, compact 4). Met SHIB-EUR (€ 0,0000041495) en PEPE-EUR:
  - toont de tabel '€ 0,000004';
  - zijn alle aslabels en Open, Hoog, Laag en Slot '0,000004';
  - zegt de checklist '(0,0000 t.o.v. 0,0000) ✗';
  - is een stop-loss van 5% niet te onderscheiden van de aankoopkoers.

  Zulke markten zijn wel toegestaan.
- **Waarom het ertoe doet** Je kunt niet controleren of de bot correct handelt.
- **Aanbeveling** Toon prijzen met 4 significante cijfers in plaats van een vast aantal decimalen: `decimals = max(2, 3 - floor(log10(abs(v))))`, met een uitzondering voor 0. Pas dat toe in `web.price`, `describe.format_value` en chart.js (`decimalsFor`, `priceFormat`, `minMove`). Overweeg onder 0,0001 een korte notatie als '0,0₅4150', met de volledige waarde in de tooltip.
- **Bewijs** `web.py:111-117`, `strategy/describe.py:36-47`, `chart.js:41-44, 175-178`; `shib-chart-hover.png`, `many-koersen.png` (bron: H-05)

### [UX-30] Tijden staan in UTC zonder dat erbij te zeggen
**Ernst** middel · **Inspanning** S–M · **Waar** Alle tabellen, agenda, journaal, nieuws, tradelog van de backtest

- **Probleem**
  - Alle tijden worden naar UTC omgerekend (`dt`, `isodt`, `ts`) en getoond als '2 nov 10:49', zonder tijdzone. In geen enkele template staat 'UTC'. Alleen de grafiek en de helptekst bij de agenda noemen het.
  - Een order van 11:49 Nederlandse tijd staat als 10:49. 'CPI-inflatiecijfer VS 13:30' is om 14:30 Nederlandse tijd; in de zomer is het verschil twee uur.
  - Het jaartal staat er de ene keer wel bij en de andere keer niet.
- **Waarom het ertoe doet** Je koppelt orders verkeerd aan nieuws of aan de Bitvavo-app, en schat verkeerd in wanneer de risk-off rond een event ingaat.
- **Aanbeveling** Kies één lijn voor de hele app.
  - Minimaal: zet overal 'UTC' achter de tijd of 'Tijd (UTC)' in de tabelkop, met de lokale tijd in een `<time datetime>`-element met `title`.
  - Beter: een instelbare `DISPLAY_TZ` (standaard Europe/Amsterdam), toegepast in `dt()` en in chart.js, en ook bij het tonen en invoeren van de agenda. Alleen de weergave omzetten maakt invoer en weergave juist inconsistent.
  - Let op: in het slanke Docker-image is daarvoor mogelijk het pakket tzdata nodig.
- **Bewijs** `web.py:130-151`, `chart.js:22-23, 70, 232`, `settings_store.py:217`, `trading.html:97-106` (bron: A-17, B-18, E-07, F-17, G-13)

### [UX-31] De kleur van een resultaat spreekt het getal tegen: groene nul, rode '-0,0%', grijs verlies
**Ernst** middel · **Inspanning** S · **Waar** Overzicht, Handelen, Journaal, Strategie > resultaatcijfers

- **Probleem**
  - De templates kleuren een getal groen als het groter dan of gelijk aan nul is. In een lege database zijn daardoor 'Sinds start € 0,00', 'Resultaat € 0,00' en zelfs een streepje '–' groen. Een backtest zonder trades toont '0,0%' in het groen, naast '-6,2%' in het rood.
  - `pct()` rondt pas af na het bepalen van het teken. Daardoor staat er '-0,0%' in het rood bij een verlies van € 0,38.
  - Omgekeerd maakt een specifiekere CSS-regel (`.hero .big-sub`, `td .sub` wint van `.neg`) het percentage onder 'Sinds start' en in Gesloten posities grijs, terwijl het bedrag erboven rood is.
- **Waarom het ertoe doet** Groen betekent winst. De kleur suggereert een resultaat dat er niet is, of laat een verlies juist niet zien.
- **Aanbeveling**
  - Maak een filter `pnl_class` met drie uitkomsten (winst, verlies, nul) op basis van de afgeronde waarde, en gebruik dat overal in plaats van de vergelijking met nul. Kleur een '–' nooit.
  - Rond in `pct()` en `eur()` eerst af en voorkom zo een negatieve nul.
  - Voeg in de CSS `.hero .big-sub.neg, td .sub.neg { color: var(--red) }` toe (en idem voor winst), of zet de basisregels in `:where()`.
- **Bewijs** `dashboard.html:43-44, 158, 188`, `trading.html:47, 50, 229`, `journal.html:16, 18, 34`, `strategy.html:92-93, 117`, `web.py:103-108`, `app.css:115, 265, 302`; `empty-dash-hero--light.png`, `dash-hero--light.png` (bron: E-04, E-05, A-23 deels, B-25 deels, B-09 deels)

### [UX-32] Bedragen worden op twee manieren opgemaakt
**Ernst** middel · **Inspanning** M · **Waar** Overzicht (hero tegenover de statusregel van de grafiek), backtest, Claude-budget

- **Probleem**
  - De server schrijft '-€ 0,51', chart.js '€ -0,52', op dezelfde pagina. chart.js zet '+' voor een winst in euro's, de server niet.
  - Door een gewone spatie na het euroteken breekt 'Eindwaarde € 1.000,00' over twee regels.
  - Het budget staat er op drie manieren: '$ 0,00 van $ 100', '0,00 van 100 dollar deze maand', en met '$' als eenheid.
  - Datums: '2 nov 10:49' tegenover '2 nov, 10:49 UTC'.
- **Waarom het ertoe doet** Bij een app die met geld werkt, wekt elk verschil in notatie wantrouwen.
- **Aanbeveling**
  - Hanteer één notatie: het teken vóór het euroteken ('−€ 3,20', eventueel '+€ 3,20'), een harde spatie (U+00A0) na het euroteken, en '€ 0,00' bij een afgeronde nul. Bouw die in chart.js na.
  - Voorkom dat bedragen afbreken (`.stat .value { white-space: nowrap }`).
  - Schrijf het budget overal als '$ 0,00 van $ 100'.
  - Gebruik in chart.js dezelfde datumnotatie als `dt()`.
- **Bewijs** `web.py:95-108`, `chart.js:20-23, 120-124, 323`, `dashboard.html:199`; `dash-portfolio--dark.png`, `strategy-backtest--light.png` (bron: E-06, A-23 deels, A-14 deels)

### [UX-33] Als de grafiek niet laadt: een leeg vlak, een technische melding, en oude gegevens onder een nieuw label
**Ernst** middel · **Inspanning** S · **Waar** Overzicht > Koers en acties, Portefeuillewaarde

- **Probleem**
  - Tijdens het laden en na een fout blijft het grafiekvlak van 380 of 220 px wit. Alleen een kleine regel zegt 'De grafiek kon niet worden geladen (Failed to fetch)' of '(HTTP 502)'. De melding bovenaan zegt zelfs 'Bitvavo 0 (code None): ReadTimeout'.
  - Er is geen knop 'Opnieuw proberen'.
  - Mislukt het wisselen naar '30 dagen', dan blijft de grafiek van 7 dagen staan terwijl '30 dagen' als actief is gemarkeerd en al als voorkeur is opgeslagen.
  - Laden en fouten worden niet aangekondigd aan hulpsoftware (geen `aria-busy` of `aria-live`).
- **Waarom het ertoe doet** Een lege grafiek ziet eruit alsof de app kapot is, en je leest de verkeerde periode af.
- **Aanbeveling**
  - Dim de oude grafiek tijdens het laden (doorzichtigheid 0,4, `aria-busy`).
  - Toon bij een fout de tekst 'Koersen konden niet worden opgehaald bij Bitvavo' met een knop 'Opnieuw proberen'. Zet de keuze terug naar de laatst gelukte periode, en sla een voorkeur pas op als het laden lukt.
  - Zet `aria-live="polite"` op de statusregel (`.chart-status`).
  - Toon technische details hooguit in een uitklapblok.
- **Bewijs** `chart.js:215-237, 302-329, 356-364`, `dashboard.html:24, 85, 102`; `fail-after-switch.png`, `chart-fail-on-switch.png` (bron: A-15, F-13)

### [UX-34] De schakelaar 'Adviezen tonen' staat scheef, is onzichtbaar voor schermlezers en verbergt ook blokkades
**Ernst** middel · **Inspanning** S · **Waar** Overzicht > Koers en acties > legenda

- **Probleem**
  - Een algemene CSS-regel (`label { flex-direction: column }`) zet het vinkje van 13×13 px los boven de tekst.
  - De legenda is verborgen voor hulpsoftware (`aria-hidden="true"`), maar bevat dit vinkje wel. Toetsenbordgebruikers komen erop uit zonder dat een schermlezer het noemt (axe: aria-hidden-focus, ernstig).
  - Uitzetten verbergt alle niet-uitgevoerde acties, dus ook de markeringen 'Geblokkeerd door een regel', terwijl het label alleen over adviezen spreekt.
- **Waarom het ertoe doet** De schakelaar oogt kapot, is voor hulpsoftware onbereikbaar, en verbergt juist de momenten waarop een regel een aankoop tegenhield.
- **Aanbeveling**
  - Zet het vinkje naast de tekst (`.chart-legend .toggle { flex-direction: row; align-items: center; gap: 6px }`) en maak het klikvlak minstens 24 px.
  - Verberg alleen de gekleurde vakjes (`.swatch`) voor hulpsoftware, niet de hele legenda.
  - Filter alleen adviezen (`kind === 'advice'`), of noem de schakelaar 'Adviezen en blokkades tonen'.
  - Structureel: haal de kolomopmaak van de algemene `label`-regel af en zet die op een klasse `.field`. Dan zijn ook de lokale correcties op strategy.html:56 en 142 niet meer nodig.
- **Bewijs** `dashboard.html:74-80`, `app.css:364, 458-459`, `chart.js:169-171`, `history.py:192-197`; `price-card-1d.png`, `dash-legend--light.png` (bron: A-16, E-18, D-14 deels, F-20 deels)

### [UX-35] De portefeuillegrafiek laat kleine schommelingen als een crash zien
**Ernst** laag · **Inspanning** M · **Waar** Overzicht > Portefeuillewaarde

- **Probleem** De verticale as schaalt naar het kleinste bereik, waardoor € 0,40 (0,04%) de halve hoogte vult. Verder:
  - het bovenste aslabel wordt afgesneden;
  - het label 'start' overlapt de markeringen op de laatste periode;
  - meerdere orders in dezelfde periode lopen door elkaar;
  - er is geen legenda voor de markeringen.
- **Waarom het ertoe doet** De grafiek moet laten zien wat je resultaat is, niet ruis opblazen tot drama.
- **Aanbeveling** Stel een minimaal bereik in (`autoscaleInfoProvider`, bijvoorbeeld ±1% rond de startwaarde). Toon de startlijn zonder titel maar met een label op de as. Voeg markeringen in dezelfde periode samen ('3 orders'), met de details in de tooltip. Vergroot de ruimte boven de lijn (`scaleMargins.top`).
- **Bewijs** `chart.js:256-300`; `portfolio-card-7d.png`, `dark-portfolio.png` (bron: A-14)

---

## 5. Analysepagina's (Journaal, Nieuws, Strategie, Tradingplan)

### [UX-36] Het journaalfilter geeft een kale foutmelding in JSON zolang 'Periode' op 'alles' staat
**Ernst** hoog · **Inspanning** S · **Waar** Journaal > filterformulier

- **Probleem** De optie 'alles' heeft `value=""`, terwijl de server een getal verwacht. Daardoor mislukt elke filteropdracht vanuit de standaardstand: alleen Resultaat=winst, alleen een markt, of Enter in het zoekveld. Je krijgt HTTP 422 met ruwe JSON (`{"detail":[{"type":"int_parsing",…}]}`), zonder opmaak en zonder weg terug. Het werkt alleen als je eerst een periode kiest.
- **Waarom het ertoe doet** Filteren is dé manier om terug te kijken, bijvoorbeeld naar alle verliezen of alle trades met het label 'fout'. Dat je eerst een periode moet kiezen, is niet te ontdekken.
- **Aanbeveling** Maak er `<option value="0">alles</option>` van; `days=0` betekent al 'geen filter'. Of laat de server een leeg veld negeren (`days` als `str|None` ontvangen). Voeg een test toe die het formulier met de standaardwaarden verstuurt. Zie UX-101 voor een nette foutpagina.
- **Bewijs** `journal.html:87-93`, `main.py:1571`; `journal-filter-default-period-422.png` (bron: B-01)

### [UX-37] De nieuwslijst kapt ongemerkt af op 150 berichten, waardoor telling en filters de sentimentkaarten tegenspreken
**Ernst** hoog · **Inspanning** M · **Waar** Nieuws > Berichten

- **Probleem** De pagina haalt de nieuwste 150 berichten op en groepeert pas daarna.
  - Na één echte ophaalronde (186 berichten in 6 uur) toonden '6 uur', '24 uur' en '3 dagen' exact dezelfde 136 nieuwsfeiten.
  - Het bericht over de ETH-hack, dat de ETH-kaart 'licht negatief · 1 met grote impact' maakt, viel uit de lijst. Het filter 'Telt mee' toonde alleen BTC, en de tekst zei 'telt er 1 mee'.
  - De aantallen worden over de afgekapte lijst berekend.
  - Na vernieuwen is de lijst op mobiel 22.558 px lang.
- **Waarom het ertoe doet** De pagina belooft uit te leggen welk nieuws de bot meeweegt. Juist het bericht dat het sentiment bepaalt is onvindbaar, en de telling klopt niet.
- **Aanbeveling**
  - Pas het filter 'Telt mee' op de server toe vóór de limiet, of haal alle meetellende berichten uit het venster van 48 uur op.
  - Verdeel de rest over pagina's (30 per pagina, of 'Meer laden'), en tel met een databasequery over het hele venster.
  - Meld het afkappen: 'De nieuwste 150 van 412'.
  - Laat de topberichten op de kaarten linken naar het bericht in de lijst.
- **Bewijs** `main.py:1103, 1112-1113`, `news/service.py:249-259`; `news-150cap-teltja.png`, `newsafter.js` (bron: B-02, F-09 deels)

### [UX-38] Sluitredenen 'Manual' en 'Trailing_stop': Engels, niet te filteren, en de link wordt ongemerkt genegeerd
**Ernst** middel · **Inspanning** S · **Waar** Journaal > Hoe gesloten, kop van de trade, filter; Overzicht > Laatste orders

- **Probleem** De vertaallijst voor sluitredenen (`EXIT_NL`) mist 'manual' en 'trailing_stop', terwijl de bot die redenen wel gebruikt. De standaardstrategie werkt zelfs met een trailing stop.
  - Het journaal toont 'Manual' (als blauwe link), 'manual' in de kop van de trade en 'Reden: Manual'.
  - Het filter 'Gesloten door' heeft voor beide geen optie.
  - De link `/journal?reason=manual` wordt door het filter weggegooid: je ziet ongemerkt de ongefilterde lijst.
  - 'Laatste orders' toont ruwe redenen als 'manual: handmatige test', 'claude: …' en 'rules: …'.

  `LABELS_NL` in web.py kent 'Handmatig' en 'Trailing stop' al, en history.py heeft nog een derde lijst.
- **Waarom het ertoe doet** De meest voorkomende reden voor een verkoop is niet te filteren, en een klik toont ongemerkt iets anders dan je denkt.
- **Aanbeveling**
  - Gebruik één centrale lijst met labels voor journal.py, history.py en web.py, aangevuld met 'handmatig verkocht' en 'trailing stop geraakt'.
  - Toon een melding als een filterwaarde onbekend is.
  - Haal het voorvoegsel van de bron weg uit orderredenen en toon de bron als pilletje.
  - Gebruik één macro (`ui.side()`) voor kopen en verkopen.
- **Bewijs** `journal.py:29-35`, `engine.py:977, 1184, 1757, 1808`, `main.py:1556`, `journal.html:45, 84, 131, 163, 209`, `web.py:42-44, 77-81`; `journal-reason-manual.png` (bron: B-11, E-14, G-08, A-23 deels)

### [UX-39] Het journaal schrijft handmatige trades toe aan Claude en aan 'de bot'
**Ernst** middel · **Inspanning** S–M · **Waar** Journaal > trade-kaart, ondertitel, Overige orders

- **Probleem**
  - Het label 'Claude destijds:' staat vast in de template. Bij een handmatige trade valt de tekst terug op de reden van de order. Zo staat er 'Besloten door: handmatig / onbekend' met direct daaronder 'Claude destijds: manual: handmatige test'.
  - Beslissingen uit de modus 'indicators' krijgen hetzelfde label, terwijl ze niet van Claude komen.
  - De ondertitel zegt 'Elke trade van de bot'.
  - De kaart 'Overige orders' beschrijft handmatige orders die daar niet meer in terechtkomen.
  - Je kunt bot en handmatig niet apart bekijken.
- **Waarom het ertoe doet** Het journaal moet Claude's oordeel naast de uitkomst leggen. Je eigen klik vervuilt nu de evaluaties en de lessen.
- **Aanbeveling**
  - Kies het label op basis van de bron: 'Claude', 'Regels', 'Automatisch (stop)' of 'Jij, handmatig'. Haal 'manual: ' weg en verberg een kale 'manual'.
  - Zet een pilletje 'Handmatig' in de kop van de trade.
  - Verander de ondertitel in 'Elke trade in oefenmodus, van de bot en handmatig'.
  - Voeg een filter 'Geopend door: bot / handmatig' toe, met aparte totalen.
  - Herschrijf of verwijder de kaart 'Overige orders'.
- **Bewijs** `journal.py:97-107, 360-368`, `journal.html:7, 153, 167, 201`; `journal-manual-trade.png` (bron: B-12, G-07)

### [UX-40] Actieve journaalfilters worden niet samengevat, en op mobiel staat de eerste trade op ~2000 px
**Ernst** middel · **Inspanning** M · **Waar** Journaal > Resultaat en filters

- **Probleem**
  - Na het filteren zegt de kop alleen 'Resultaat van de selectie'. Welke filters actief zijn, zie je alleen in de keuzelijsten onder de resultaatkaart.
  - Op 390 px staan de 6 tegels onder elkaar (694 px), staat het filterblok altijd open (311 px) en begint de eerste trade op 1997 px.
  - Op 1440 px wordt 'open en gesloten' afgekapt en breekt het label van het zoekveld over twee regels.
- **Waarom het ertoe doet** Je kunt niet in één oogopslag controleren of de totalen over de juiste selectie gaan.
- **Aanbeveling**
  - Toon de actieve filters onder de kop als labels ('Markt: ETH-EUR ×'), elk met een link die alleen dat filter weghaalt.
  - Zet de filters boven de resultaten in een uitklapblok ('Filters (2 actief)'), dat op mobiel standaard dicht is.
  - Zet de tegels op schermen tot 480 px in twee kolommen.
  - Kort de opties voor de status in.
- **Bewijs** `journal.html:14, 111`, `app.css:275, 718`; `journal--mobile-light.png` (bron: B-13)

### [UX-41] Rode foutmelding na een geslaagde nieuwsronde
**Ernst** middel · **Inspanning** S · **Waar** Nieuws > meldingen bovenaan

- **Probleem**
  - Na 'Nu verversen' zonder sleutel is het ophalen gelukt. Toch verschijnt een rode melding 'De laatste keer ophalen ging mis: ANTHROPIC_API_KEY ontbreekt', met daaronder een blauwe melding die hetzelfde zegt.
  - Onderaan staat 'Nieuws beoordeeld door claude-haiku-5-5', terwijl er niets wordt beoordeeld.
  - Er staat nergens hoeveel berichten er binnenkwamen.
- **Waarom het ertoe doet** Een rode fout voor een bewuste keuze (geen sleutel) leert je rode meldingen te negeren. En je ververst opnieuw, wat 15 s duurt.
- **Aanbeveling**
  - Sla 'sleutel ontbreekt' niet op als fout van de laatste ophaalronde (`last_error`).
  - Toon één informatieve melding: 'Opgehaald: 132 berichten, 0 beoordeeld; zonder Claude telt nieuws niet mee.'
  - Zet onderaan: 'Nieuwsbeoordeling: uit (geen sleutel)'.
- **Bewijs** `news/service.py:775-777`, `news.html:15-16, 114`, `main.py:1123-1128`; `news-after-refresh.png` (bron: B-16, F-14)

### [UX-42] De strategiepagina suggereert dat de strategie alleen voor BTC-EUR geldt
**Ernst** middel · **Inspanning** S–M · **Waar** Strategie > Actieve strategie en Backtest; Overzicht > kaart Strategie

- **Probleem** Onder de naam van de strategie staat 'BTC-EUR · candles van 1 uur'. De strategie draait echter op alle ingestelde markten (`MARKETS`); het veld `market` bepaalt alleen op welke markt de backtest draait. Wil je ETH testen, dan moet je de JSON aanpassen.
- **Waarom het ertoe doet** Je denkt dat ETH anders verhandeld wordt, of je past de regels voor BTC aan zonder te beseffen dat ETH meeverandert. En een backtest op één markt wordt gelezen als bewijs voor alle markten.
- **Aanbeveling** Schrijf 'Geldt voor: BTC-EUR, ETH-EUR · candles van 1 uur'. Voeg naast 'Test op N candles' een keuzelijst 'Backtest-markt' toe, of toon de backtest per markt.
- **Bewijs** `strategy.html:16`, `dashboard.html:174`, `main.py:829`, `README.md:340-341`; `strategy--desktop-light.png` (bron: B-05, G-22)

### [UX-43] Backtestresultaten zijn lastig op waarde te schatten
**Ernst** middel · **Inspanning** M · **Waar** Strategie > Backtest

- **Probleem**
  - De kop zegt '500 candles', zonder de tijdsduur (ongeveer 21 dagen).
  - Bij 0 trades staat '0,0%' in het groen naast een rode score voor kopen en vasthouden.
  - Het veld 'Test op' springt na elke test terug naar 500.
  - Indicatorwaarden staan ruw en met een punt ('rsi14 = 40.540212').
  - Er is geen grafiek van het verloop.
- **Waarom het ertoe doet** De backtest is het belangrijkste bewijs om een strategie op te slaan (UX-10).
- **Aanbeveling**
  - Toon '500 candles (≈ 21 dagen, 12 okt – 2 nov)'.
  - Toon bij 0 trades een neutrale kleur met de uitleg 'geen trades: niets getest'.
  - Laat het veld 'Test op' de gekozen waarde onthouden (`value="{{ limit }}"`).
  - Schrijf de getallen in Nederlandse notatie.
  - Voeg een kleine grafiek toe van de strategie tegenover kopen en vasthouden, met een tekstalternatief.
- **Bewijs** `strategy.html:56, 89, 92, 104, 142`; `strat-backtest.png`, `strat-backtest-loose.png` (bron: B-09)

### [UX-44] Validatiefouten bij de strategie zijn technisch, deels Engels en staan ver van de editor
**Ernst** middel · **Inspanning** M · **Waar** Strategie > JSON-editor

- **Probleem**
  - Fouten verschijnen als één melding bovenaan (op 303 px), terwijl de editor op 1216 px staat. De pagina opent bovenaan en het veld krijgt geen markering.
  - De teksten zijn ruwe uitvoer van de validatiebibliotheek (Pydantic), bijvoorbeeld 'Ongeldige strategie: : Invalid JSON: key must be a string at line 1 column 15' en 'buy.0.op: Input should be …'.
- **Waarom het ertoe doet** Zonder Claude is de JSON-editor de enige manier om de strategie aan te passen.
- **Aanbeveling**
  - Toon de fout onder het tekstvak, gekoppeld met `aria-describedby`, en zet de focus daarheen (laat het formulier naar `#json-form` springen).
  - Vertaal de fouttypes en paden naar gewone taal, bijvoorbeeld 'Koopregel 1 → vergelijking'.
  - Toon bij een fout in de JSON-opmaak de drie regels rond de plek van de fout.
- **Bewijs** `main.py:1017-1022`, `strategy.html:11`; `strat-invalid-syntax.png` (bron: B-07)

### [UX-45] Details per nieuwsbericht zijn dubbel of cryptisch
**Ernst** laag · **Inspanning** S · **Waar** Nieuws > berichten

- **Probleem**
  - Het pilletje 'telt mee' wordt direct gevolgd door 'telt mee voor BTC · …'.
  - Er staat 'impact middel · dagen', en '(0,60)' zonder uitleg.
  - Het uitklappijltje is het standaard ▶-driehoekje in plaats van de › uit de rest van de app.
  - Zonder sleutel staat er 'nog niet beoordeeld door Claude', alsof dat nog komt.
  - De Fear & Greed-index is Engels ('Fear').
  - Externe links openen zonder aanwijzing in een nieuw tabblad.
- **Waarom het ertoe doet** Kleine leesfricties stapelen zich op in een lijst van tientallen berichten.
- **Aanbeveling** Schrijf 'voor BTC · gemiddeld over 2 bronnen', 'impact middel · effect: dagen' en 'sentiment +0,60'. Gebruik dezelfde uitklapstijl als elders (`details.advanced`). Zeg 'niet beoordeeld (geen Claude-sleutel)' en 'Angst'. Voeg bij externe links een ↗-icoon toe met de tekst '(opent in nieuw tabblad)' voor schermlezers.
- **Bewijs** `news.html:23, 80-81, 90, 102`, `app.css:572-577`; `news-others-open.png` (bron: B-22)

### [UX-46] Strategiepagina zonder sleutel: een halflege kaart en een dichtgeklapte editor
**Ernst** laag · **Inspanning** S · **Waar** Strategie

- **Probleem**
  - De kaart 'Vraag Claude om een strategie' is ongeveer 600 px hoog, met alleen een verwijzing naar 'Geavanceerd hieronder'. Dat blok is dichtgeklapt en staat op mobiel pas op 1816 px.
  - Test je de strategie zonder iets te wijzigen, dan zie je 'Nog niet opgeslagen' en een opslaanknop, terwijl de vergelijking 'Geen verschil' zegt.
- **Waarom het ertoe doet** Kleine frictie bij het vinden van de editor en bij het lezen van de status.
- **Aanbeveling** Geef de melding een knop 'JSON-editor openen', of klap de editor zonder sleutel standaard open. Laat de kaart niet meerekken in hoogte (`align-self: start`). Toon bij een lege vergelijking 'Gelijk aan actieve strategie', zonder opslaanknop.
- **Bewijs** `strategy.html:47-63, 69-70, 129`; `strategy--desktop-light.png` (bron: B-23)

### [UX-47] Tradingplan: bewerkbaar en afgeleid niet gescheiden, plus kleine formulierproblemen
**Ernst** laag · **Inspanning** M · **Waar** Tradingplan

- **Probleem**
  - 'Jouw plan' en de zes kaarten die automatisch uit de instellingen worden opgebouwd, hebben dezelfde stijl, zonder kop voor het automatische deel. Op mobiel is de pagina 5.580 px lang.
  - Het veld 'Wat de bot niet doet' leest als beschrijving, niet als instructie.
  - Na het leegmaken staat er 'laatst gewijzigd' in plaats van 'nog leeg'.
  - De melding '?saved=1' komt terug bij vernieuwen.
  - De tekstvakken staan niet op één lijn (524/524/713/732 px), en de maximumlengte kapt tekst ongemerkt af.
- **Waarom het ertoe doet** Je moet zelf afleiden wat je hier kunt aanpassen.
- **Aanbeveling**
  - Maak twee secties: 'Jouw plan (bewerkbaar)' en 'Wat de bot afdwingt (automatisch)'. Geef de tweede een eigen kaartstijl met 'Wijzig in Instellingen ›', en maak die kaarten op mobiel inklapbaar.
  - Hernoem het veld naar 'Wanneer de bot nooit mag handelen'.
  - Baseer 'nog leeg' op `plan.is_empty()`.
  - Voeg een tekenteller toe en zet de helptekst onder het tekstvak.
- **Bewijs** `plan.html:15-36, 19, 27, 38-132`, `plan.py:37-41, 88-97`; `plan--desktop-light.png`, `plan-after-save.png` (bron: B-14, B-24)

---

## 6. Instellingen & inloggen

### [UX-48] Een mislukte wachtwoordcheck ziet er precies uit als een geslaagde opslag
**Ernst** kritiek · **Inspanning** S · **Waar** Instellingen > elk blok met 'Huidig wachtwoord'

- **Probleem** Getest: 'Maximum per order' van 100 naar 50 zetten zonder wachtwoord.
  - De server geeft 403 en toont de pagina opnieuw met de ingevulde waarden. Via het anker `#limits` springt de browser naar de kop van het blok.
  - In beeld staat 'Maximum per order: 50', zonder foutmelding. De rode samenvatting staat 2240 px boven het scherm, de foutmelding bij het veld 1019 px eronder (op mobiel 3653 en 1748 px).
  - Het pilletje blijft 'standaard' en de limiet blijft 100.
  - Het scherm ziet er hetzelfde uit als na een geslaagde opslag (UX-99).
- **Waarom het ertoe doet** Je denkt dat een strengere limiet, een kortere lijst markten of 'automatisch handelen uit' actief is, terwijl de bot met de oude waarden doorgaat. Dat is precies wat de wachtwoordcontrole moet voorkomen.
- **Aanbeveling**
  - Toon bij een fout direct onder de kop van het blok een foutmelding: 'Niet opgeslagen: vul je huidige wachtwoord in'.
  - Laat app.js bij het laden het eerste veld met een fout in beeld brengen (`scrollIntoView({block:'center'})`) en focus geven. Een anker in de serverrespons werkt hier niet: na een fout bepaalt het formulier zelf waar de browser naartoe springt.
  - Markeer gewijzigde maar niet-opgeslagen velden met een oranje rand.
- **Bewijs** `settings.html:11-14, 26, 90-103`, `main.py:1928-1932, 1964-1965`; `f01-limits-stepup-desktop.png`, `f01-limits-stepup-mobile.png`, `s04-stepup-missing-viewport.png` (bron: C-01)

### [UX-49] Schakelaars lijken direct te werken, maar wijzigingen gaan ongemerkt verloren
**Ernst** hoog · **Inspanning** M · **Waar** Instellingen > alle blokken

- **Probleem**
  - De schakelaars in iOS-stijl tonen direct 'Uit', maar de wijziging geldt pas na 'Opslaan' onderaan het blok. In Handelen staat die knop 1900 px lager, op mobiel 3250 px.
  - Getest: 'Bot automatisch laten handelen' omgezet en daarna naar een andere pagina gegaan. Er kwam geen waarschuwing, en Overzicht zei nog steeds 'Elke 60 minuten kijkt de bot…'.
  - Elk blok is een eigen formulier. Opslaan in het blok Claude gooide ongemerkt de wijzigingen in Handelen (interval 30) en Harde limieten (3 trades) weg.
  - Je krijgt geen waarschuwing als je de pagina verlaat met niet-opgeslagen wijzigingen.
- **Waarom het ertoe doet** Wie de bot snel via de schakelaar wil stilzetten en wegklikt, laat hem gewoon doorhandelen. En een aangepaste limiet lijkt opgeslagen terwijl dat niet zo is.
- **Aanbeveling**
  - Houd in app.js per formulier bij of er niet-opgeslagen wijzigingen zijn. Toon dan een label 'niet opgeslagen' op het blok, en een vaste balk onderaan: 'Niet-opgeslagen wijzigingen in Handelen – Opslaan / Ongedaan maken'.
  - Waarschuw bij het verlaten van de pagina zolang er zulke wijzigingen zijn (`beforeunload`).
  - Maak 'automatisch handelen' en de risk-off een directe actie met eigen bevestiging, of gebruik een keuzeknop 'Aan | Uit' die minder de indruk van direct effect wekt.
- **Bewijs** `app.js:51-57`, `app.css:627-653`, `settings.html:23-26, 105-108`, `main.py:1949-1955`; `settings1.js`, `settings2.js`, `multigroup.js` (bron: C-03, F-16)

### [UX-50] Een herstelcode is op de telefoon niet te typen: je krijgt alleen cijfers
**Ernst** hoog · **Inspanning** S · **Waar** Inloggen > /login/mfa

- **Probleem**
  - Het codeveld heeft `inputmode="numeric"`, waardoor de telefoon alleen een cijfertoetsenbord toont. Herstelcodes bestaan uit letters, cijfers en een streepje.
  - Een code met een spatie in plaats van een streepje ('snwfy qyxwz') wordt door het invoerpatroon geweigerd, met een algemene browsermelding. De server kan prima met spaties overweg.
- **Waarom het ertoe doet** Wie zijn telefoon met de authenticator kwijt is en op een andere telefoon inlogt, kan een code van papier niet overtypen. Dat is precies de situatie waarvoor herstelcodes bestaan.
- **Aanbeveling** Haal `inputmode="numeric"` weg, of laat een link 'Herstelcode gebruiken' het veld omzetten naar `inputmode="text" autocapitalize="off"`. Sta in het invoerpatroon ook spaties toe, of laat het patroon weg; de server controleert toch. Geef het veld een `title` met de verwachte vorm.
- **Bewijs** `auth/mfa.html:12, 16-19`, `auth/service.py:38-39`; `auth1.js` (bron: C-06)

### [UX-51] Overstappen naar live: één blokkade tegelijk, een kale bevestiging en een verkeerde verwijzing
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > Handelen > Modus en Uit de omgeving; Overzicht in live

- **Probleem**
  - De app noemt alleen de eerste ontbrekende voorwaarde (login, sleutel of `LIVE_TRADING_ACK`), in een kleine, rechts uitgelijnde tekst. De helptekst bij Modus noemt de login niet.
  - De bevestiging voor live is één algemene zin, zonder het maximum per order, het kapitaal of de stand van automatisch handelen.
  - De tooltip op 'Live' verminkt variabelenamen ('Auth_enabled', 'bitvavo_api_key') door `|capitalize`.
  - Na de overstap verschijnt 'LIVE_CAPITAL_EUR staat op 0 … Stel het bedrag in', zonder link en alsof het op de server moet. Het is gewoon de instelling 'Kapitaal voor de bot (live)' in het portaal.
  - In live staan vier meldingen boven de hero.
- **Waarom het ertoe doet** Je ontdekt de voorwaarden één voor één, en op het moment van beslissen ontbreekt juist de belangrijkste informatie: met hoeveel geld en binnen welke limieten.
- **Aanbeveling**
  - Toon onder Modus een checklist met ✓/✗ per voorwaarde (login, sleutel, `LIVE_TRADING_ACK`; aanbevolen: kapitaal en een sleutel met beperkte rechten), met per regel waar je het oplost.
  - Vervang de bevestiging door een paneel met de actieve limieten en het kapitaal (met een waarschuwing bij 0) en 'Typ LIVE om te bevestigen'.
  - Herschrijf de melding over het kapitaal in gewone taal, met een link naar `/settings#f-live_capital_eur`. Bundel de adviezen voor live in één 'Live-checklist'.
  - Haal `|capitalize` en de rechtse uitlijning weg.
  - De banner 'login staat uit' blijft bewust op elke pagina staan.
- **Bewijs** `main.py:118-133, 1989-2005`, `settings.html:26, 54-65, 111-123`, `settings_store.py:93-104, 267-277`; `c06-mode-mobile.png`, `live-dash-top.png` (bron: C-12, G-19, G-18)

### [UX-52] Een sleutel die Bitvavo weigert, staat overal als groen 'ingesteld'
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > Uit de omgeving; voettekst; melding op Overzicht

- **Probleem**
  - Met een ongeldige sleutel antwoordt Bitvavo met 403 ('API Key must be of length 64'). Toch toont Instellingen een groen pilletje 'ingesteld'.
  - De voettekst zegt 'Bitvavo-sleutel ingesteld' en 'Bitvavo-limiet resterend: 1000', zonder uitleg.
  - Alleen Overzicht meldt het probleem, en dan als 'niet bereikbaar'.
  - Er is geen manier om de verbinding te testen.
- **Waarom het ertoe doet** De laatste stap voor de eerste live-trade is in het portaal niet te controleren.
- **Aanbeveling**
  - Voeg een knop 'Verbinding testen' toe (een gewoon formulier, geen JavaScript nodig) die saldo en account ophaalt. Toon het resultaat: 'Sleutel werkt · fee 0,25%' of 'Geweigerd door Bitvavo: … (403)'.
  - Zeg er eerlijk bij dat Crypie de rechten van de sleutel niet kan zien.
  - Geef het pilletje drie standen: ingesteld (neutraal), werkt (groen) en geweigerd (rood).
  - Geef 401 en 403 een eigen tekst (UX-19).
  - Leg 'limiet resterend' uit, of laat het weg.
- **Bewijs** `settings.html:118`, `base.html:65`, `client.py:136-137`, `main.py:128-133`, `README.md:410-411`; `live-dashboard.png` (bron: H-09)

### [UX-53] Een markt die niet bestaat, wordt geaccepteerd en verdwijnt daarna ongemerkt
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > Markten; Handelen; Overzicht > grafieken

- **Probleem** Markten worden alleen op hun vorm gecontroleerd, niet op of ze bij Bitvavo bestaan. Met de typfout 'BTCC-EUR':
  - zegt Handelen 'Handelt op … en BTCC-EUR', maar mislukt de cyclus voor die markt (code 205);
  - ontbreekt BTCC zonder uitleg in 'Laatste cyclus per markt';
  - zegt de grafiekknop 'Probeer het over een minuut opnieuw';
  - mislukt het ophalen van de portefeuillegeschiedenis voor álle markten (502), zodat de portefeuillegrafiek verdwijnt.
- **Waarom het ertoe doet** Door een typfout handelt de bot nooit op die markt, terwijl de UI zegt van wel.
- **Aanbeveling**
  - Controleer bij het opslaan tegen de marktlijst van Bitvavo (die de app al in het geheugen bewaart): 'BTCC-EUR bestaat niet op Bitvavo. Bedoel je BTC-EUR?'.
  - Toon een markt waarvan de cyclus mislukte als kaart 'Fout', met de reden.
  - Laat de portefeuillegeschiedenis een falende markt overslaan.
  - Geef een andere tekst bij een geweigerd verzoek (4xx) dan bij een netwerkfout.
- **Bewijs** `settings_store.py:33, 694-709`, `engine.py:518-528`, `main.py:700-713`, `chart.js:235`, `client.py:258-266`; `many-btcc-desktop.png` (bron: H-06)

### [UX-54] Fouten, eenheden en uitleg zijn niet aan het veld gekoppeld, en de foutsamenvatting valt buiten beeld
**Ernst** middel · **Inspanning** S · **Waar** Instellingen (alle velden); inloggen en MFA; orderbedrag; velden van het plan

- **Probleem**
  - Getest met twee fouten (Markten 'BTC-USD', Agenda '2026-13-45'). De samenvatting staat 136 px boven het scherm, de tweede fout 1530 px lager zonder aanwijzing, en de focus staat nergens.
  - Velden hebben geen `aria-invalid` of `aria-describedby`; dat komt in geen enkele template voor.
  - De eenheid (%, €, minuten) staat los naast het veld, zodat een schermlezer alleen 'Dagverlieslimiet, draaiknop, 5' hoort.
  - Na een fout is het ingevulde wachtwoord gewist. Dat is terecht, maar er staat niet bij dat je het opnieuw moet invullen.
- **Waarom het ertoe doet** Fouten verderop in een blok worden gemist. En bij een harde limiet weet een schermlezergebruiker niet of '5' procent of euro is.
- **Aanbeveling**
  - Geef helptekst, standaardwaarde, foutmelding en eenheid een id in de macro. Koppel ze aan het veld met `aria-labelledby="{fid}-label {fid}-unit"` en `aria-describedby`, en zet `aria-invalid="true"` bij een fout.
  - Zet bovenaan het blok een foutsamenvatting met links naar de velden ('2 velden kloppen niet: Markten, Agenda'), en geef het eerste foute veld focus.
  - Zet bij het wachtwoordveld 'Vul je wachtwoord opnieuw in'.
  - Overweeg de controle door de browser uit te zetten (`novalidate`), zodat de Nederlandse meldingen van de server leidend zijn.
- **Bewijs** `settings.html:13, 37-43, 75-81`, `auth/login.html:7-15`; `c04-validation-trading-viewport.png`, `settings-error-viewport.png`, `formerr.js`, `axtree.js` (bron: C-08, D-05, D-06 deels)

### [UX-55] Onduidelijk welke velden om je wachtwoord vragen
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > 'Huidig wachtwoord' in 4 blokken

- **Probleem**
  - Elk gevoelig blok toont dezelfde zin, die de gevoelige onderdelen van álle blokken opsomt.
  - In Handelen zijn 10 van de 16 velden gevoelig, in Claude alleen het maandbudget en in Nieuws alleen de RSS-feeds. Geen enkel veld is gemarkeerd.
  - De validatie draait vóór de wachtwoordcheck, dus vaak heb je twee pogingen nodig.
  - Een fout wachtwoord telt mee voor de blokkade bij het inloggen (UX-62).
- **Waarom het ertoe doet** Je vult het wachtwoord overal in, of je vergeet het (zie UX-48).
- **Aanbeveling**
  - Markeer elk gevoelig veld met een slotje en 'vraagt wachtwoord', op basis van `SENSITIVE_KEYS`.
  - Maak het wachtwoordveld pas verplicht als je een gevoelig veld wijzigt. De controle op de server blijft gewoon staan.
  - Maak de helptekst per blok specifiek: 'Nodig voor: Maandbudget'.
- **Bewijs** `settings.html:90-103`, `settings_store.py:507-523`, `main.py:1702-1715` (bron: C-07)

### [UX-56] Instellingen is één zeer lange pagina, met een inhoudsopgave die op tabbladen lijkt
**Ernst** middel · **Inspanning** L · **Waar** Instellingen

- **Probleem**
  - De pagina is 5.882 px lang op desktop en 9.597 px op mobiel; met login aan 10.390 px.
  - De lijst met onderdelen bovenaan lijkt op een tabbladkeuze, maar heeft nooit een actieve stand en scrolt niet mee.
  - 'Oefenportefeuille' ontbreekt in die lijst.
- **Waarom het ertoe doet** De pagina is slecht te overzien en de afstand tussen veld en Opslaan is groot. Dat maakt UX-48 en UX-49 erger.
- **Aanbeveling**
  - Maak per blok een eigen pagina of tabblad (`/settings/trading`), of maak de blokken inklapbaar met een samenvatting van de huidige waarden.
  - Laat de lijst met onderdelen onder de navigatie meeschuiven en de actieve stand tonen, of geef hem de stijl van een gewone lijst met links.
  - Toon per blok een vaste balk met Opslaan en Herstellen zodra er wijzigingen zijn.
- **Bewijs** `settings.html:16-19`, `app.css:414-437, 601`; `settings--desktop-light.png` (bron: C-09)

### [UX-57] Instellingen staan in het verkeerde blok
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > indeling in blokken

- **Probleem**
  - 'Handelskosten per order' staat onder 'Harde limieten' en vraagt daardoor om je wachtwoord.
  - 'Handelskosten in backtests' staat onder Claude.
  - 'Beslissingen tonen van de laatste … dagen' is een weergave-instelling, maar staat tussen modus en risico.
  - 'Startbedrag oefenmodus' staat in Handelen, terwijl de bijbehorende reset los onderaan staat.
- **Waarom het ertoe doet** Je vindt instellingen niet waar je ze zoekt, en een kostenparameter krijgt het gewicht van een harde limiet.
- **Aanbeveling** Deel opnieuw in: Modus & markten · Bescherming · Harde limieten (alleen echte grenzen) · Kosten · Oefenportefeuille (startbedrag en reset samen) · Weergave. Maak de handelskosten (`trading_fee_pct`) expliciet niet-gevoelig.
- **Bewijs** `settings_store.py:221-242, 385-405, 522`, `settings.html:126-145` (bron: C-10)

### [UX-58] Velden blijven bewerkbaar als de schakelaar waar ze bij horen uit staat
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > risk-off, dipbewaking, nieuws, GDELT

- **Probleem**
  - 'Interval nieuws ophalen' blijft bewerkbaar, en heeft geen helptekst, als 'Nieuws automatisch ophalen' uit staat.
  - De GDELT-zoekopdracht, de vensters voor de risk-off en 'Dipbewaking: maximaal' (die niets doet bij interval 0) staan als gelijkwaardige, losse rijen onder elkaar.
- **Waarom het ertoe doet** Je stelt waarden in die niets doen.
- **Aanbeveling** Groepeer de afhankelijke velden zichtbaar onder hun schakelaar, en dim ze met de tekst 'Geldt alleen als … aan staat'. Gebruik daarvoor `readonly` of `aria-disabled`, want uitgeschakelde velden worden niet meegestuurd. Geef 'Interval nieuws ophalen' een helptekst.
- **Bewijs** `settings_store.py:143-201, 414-423, 461-467`, `settings.html:35-87`; `s03-saved-news-viewport.png` (bron: C-11)

### [UX-59] Terugzetten kan alleen per blok, met een vage vraag
**Ernst** middel · **Inspanning** M · **Waar** Instellingen > Standaard herstellen

- **Probleem**
  - 'Standaard herstellen' wist alle aanpassingen in een blok, en de bevestigingsvraag noemt niet welke.
  - 'Handelen: 2 aangepast' zegt niet om welke twee het gaat.
  - Eén veld terugzetten kan niet in de UI, terwijl de API dat wel ondersteunt (`DELETE /api/settings/{key}`).
- **Waarom het ertoe doet** Wie één waarde wil terugdraaien, verliest ook de andere aanpassingen.
- **Aanbeveling** Zet bij elk aangepast veld een link 'Terug naar standaard (X)'. Die vult de standaardwaarde in het veld in, of zet alleen die sleutel terug; voor gevoelige sleutels blijft het wachtwoord nodig. Noem in de bevestiging voor het hele blok wat er verandert, bijvoorbeeld 'Markten: BTC-EUR, ETH-EUR, SOL-EUR → BTC-EUR, ETH-EUR'.
- **Bewijs** `settings.html:41, 107`, `main.py:1830-1839` (bron: C-14)

### [UX-60] Een typfout in de naam van het Claude-model wordt zonder waarschuwing opgeslagen
**Ernst** middel · **Inspanning** S · **Waar** Instellingen > Claude > modelvelden

- **Probleem**
  - De app controleert alleen of de naam met 'claude-' begint. 'claude-haiku-4-5-typo' gaf gewoon 'opgeslagen'.
  - De kostenschatting valt dan terug op het duurste tarief.
  - De echte aanroep mislukt pas bij de volgende cyclus, en de fout verschijnt op een andere pagina.
- **Waarom het ertoe doet** Nieuwsbeoordeling of beslissingen stoppen zonder dat je het merkt.
- **Aanbeveling** Toon een waarschuwing (zonder het opslaan te blokkeren) als de naam onbekend is (niet in `PRICES`): 'Onbekend model: controleer de naam'. Of gebruik een keuzelijst met de bekende modellen plus de optie 'Ander model…'.
- **Bewijs** `settings.html:70-71`, `settings_store.py:797-801`, `ai/budget.py:44-48` (bron: C-13)

### [UX-61] De eerste authenticator koppelen: 'Annuleren' of 10 minuten wachten sluit de weg via de website af
**Ernst** middel · **Inspanning** M · **Waar** /setup → /login/mfa/setup

- **Probleem** Na het aanmaken van je account heb je 10 minuten om je authenticator te koppelen. Daarna, en ook na 'Annuleren' (wat je uitlogt), kan dat alleen nog met `python -m app.auth reset-mfa` op de server. Dat is een bewuste beveiligingskeuze, maar de pagina met de QR-code noemt geen tijdslimiet en zegt niet wat Annuleren doet.
- **Waarom het ertoe doet** Direct na de installatie zit je zonder waarschuwing buiten de website.
- **Aanbeveling**
  - Zet op de pagina: 'Rond dit binnen 10 minuten af; daarna heb je een code van de server nodig (python -m app.auth reset-mfa).'
  - Hernoem 'Annuleren' naar 'Later koppelen', met een bevestiging die het gevolg noemt. Die bevestiging moet in auth.js komen, want app.js wordt op de inlogpagina's niet geladen.
  - Overweeg 15 minuten, gelijk aan `MFA_SETUP_SECONDS`.
  - Koppelen blijft alleen mogelijk met een setup- of servercode.
- **Bewijs** `auth/service.py:28, 430-440`, `auth/routes.py:291-296, 496-498`, `auth/mfa_setup.html:26-31`; `e01-enrol-token-after-cancel.png` (bron: C-05)

### [UX-62] Buitengesloten zonder uitweg, en de blokkade staat in kale seconden
**Ernst** middel · **Inspanning** S · **Waar** /login, /login/mfa, blokkade na mislukte pogingen

- **Probleem**
  - Op /login staat geen 'Wachtwoord vergeten?' en geen verwijzing naar de commando's op de server; die staat alleen op /account. De MFA-pagina noemt `reset-mfa` niet.
  - Na 5 mislukte pogingen verschijnt 'Probeer het over 29 seconden opnieuw'. Dat loopt op tot 900 seconden, zonder aftelling en zonder waarschuwing vooraf.
  - De teller geldt per IP-adres en wordt gedeeld met de wachtwoordcontrole in Instellingen en op /account. Eén fout wachtwoord daar plus 4 loginpogingen blokkeert het inloggen.
- **Waarom het ertoe doet** Bij een blokkade moet je in de README zoeken, en je weet niet waarom inloggen niet lukt.
- **Aanbeveling**
  - Zet onder het loginformulier een uitklapper 'Wachtwoord of authenticator kwijt?' met `reset-password`, `reset-mfa` en `clear-throttle`.
  - Geef de wachttijd in gewone taal ('over 15 minuten', 'om 14:32').
  - Waarschuw na 3 fouten: 'Nog 2 pogingen'.
  - Zeg bij een blokkade in Instellingen dat ook inloggen dan geblokkeerd is.
- **Bewijs** `auth/login.html:18-21`, `auth/account.html:66`, `auth/routes.py:163-164, 717-743`, `auth/service.py:87-90`; `b03-login-throttled.png` (bron: C-15)

### [UX-63] Sessie verlopen of uitgelogd: geen uitleg, en ingevulde formulieren gaan verloren
**Ernst** middel · **Inspanning** M · **Waar** Doorverwijzing naar inloggen, /logout, tijdslimiet bij MFA

- **Probleem**
  - Verloopt je sessie terwijl je het blok Handelen opslaat, dan word je zonder melding naar `/login?next=/settings` gestuurd. Na het inloggen is je wijziging weg.
  - Na 'Uitloggen', 'Overal uitloggen' of het verlopen van de 10 minuten tussen wachtwoord en code toont /login geen uitleg.
  - Hoe lang een sessie geldig is (24 uur, vast), staat nergens.
- **Waarom het ertoe doet** Een zorgvuldig ingevulde agenda of lijst met feeds gaat ongemerkt verloren.
- **Aanbeveling**
  - Geef de reden mee in de URL (`/login?reden=verlopen|uitgelogd|overal|tijd`) en toon een melding: 'Je sessie is verlopen; je wijziging is niet opgeslagen.'
  - Toon op Account wanneer de sessie verloopt.
  - Optioneel: bewaar de formulierwaarden (zonder wachtwoorden) tijdelijk in de browser en zet ze na het inloggen terug.
- **Bewijs** `auth/routes.py:205-208, 403-405, 798-804`; `s06-session-expired.png` (bron: C-16)

### [UX-64] Herstelcodes: niet te downloaden of printen, en kopiëren mislukt ongemerkt via http
**Ernst** middel · **Inspanning** S · **Waar** Pagina met herstelcodes

- **Probleem**
  - Er zijn alleen 'Kopiëren' en 'Ik heb ze opgeslagen', en dat laatste is een gewone link.
  - Op een http-adres in het thuisnetwerk werkt het klembord niet, en dan doet Kopiëren niets, zonder melding.
  - Een schermlezer hoort de wissel naar 'Gekopieerd' niet.
  - Na vernieuwen of 'terug' zijn de codes weg.
- **Waarom het ertoe doet** De enige uitweg bij verlies van je telefoon wordt makkelijk niet (goed) bewaard.
- **Aanbeveling**
  - Voeg 'Downloaden (.txt)' en 'Afdrukken' toe.
  - Toon een melding als kopiëren niet lukt: 'Kopiëren lukt hier niet; selecteer de codes.'
  - Voeg een regel toe die een schermlezer voorleest (`role="status"`).
  - Laat 'Ik heb de codes bewaard' eerst afvinken.
- **Bewijs** `auth/recovery_codes.html:7-15`, `auth.js:13-16`; `a07-recovery.png` (bron: C-17, D-24 deels)

### [UX-65] Accountpagina: rommelige indeling en ongelijk gewicht van destructieve acties
**Ernst** middel · **Inspanning** M · **Waar** /account

- **Probleem**
  - De pagina bestaat uit vijf losse kaarten; de kaart met de status is grotendeels leeg.
  - 'Overal uitloggen' vraagt geen bevestiging.
  - 'Nieuwe herstelcodes', dat de oude codes ongeldig maakt, is grijs. 'Authenticator opnieuw instellen' is rood.
  - Een fout in het vierde formulier verschijnt bovenaan, zonder te zeggen bij welk formulier. Daarna zijn alle velden leeg.
  - Het account-icoon in de navigatie toont niet dat je op deze pagina bent.
- **Waarom het ertoe doet** Destructieve acties zijn niet consequent gemarkeerd, en op mobiel is lastig te zien bij welk formulier een fout hoort.
- **Aanbeveling**
  - Maak een sectie 'Status' (resterende codes, laatste login, sessie geldig tot) en een sectie 'Beveiliging wijzigen' met uitklapbare formulieren.
  - Geef alle acties die sessies beëindigen dezelfde rode omlijning, en laat 'Overal uitloggen' om bevestiging vragen.
  - Toon een fout in de kaart van het formulier dat je verstuurde.
  - Markeer het account-icoon als actief op /account (`aria-current`).
  - Houd de hint 'Wachtwoord kwijt?' hier en zet hem ook op /login.
- **Bewijs** `auth/account.html:10-76`, `base.html:37`; `auth-account--desktop.png`, `b02-account-error-mobile.png` (bron: C-19)

### [UX-66] Na inloggen met een herstelcode volgt geen melding
**Ernst** laag · **Inspanning** S · **Waar** /login/mfa → Overzicht

- **Probleem** Je krijgt niet te horen dat je een code hebt gebruikt, hoeveel er nog over zijn, of dat je een nieuwe authenticator nodig hebt. Alleen /account toont dat.
- **Waarom het ertoe doet** Wie zijn telefoon kwijt is, blijft codes gebruiken tot ze op zijn.
- **Aanbeveling** Stuur na een login met een herstelcode door naar `/account?herstel=1`, met de melding 'Je hebt een herstelcode gebruikt (nog 7 over). Stel een nieuwe authenticator in.'
- **Bewijs** `auth/routes.py:423-435`; `a11-after-recovery-login.png` (bron: C-18)

### [UX-67] Inlogstappen inconsistent genummerd, focus na een fout op het verkeerde veld, QR-code zonder beschrijving
**Ernst** laag · **Inspanning** S · **Waar** /setup, /login, /login/mfa/setup

- **Probleem**
  - Er staat 'Stap 1 van 2', terwijl er drie schermen zijn (account, authenticator, herstelcodes).
  - Op de QR-pagina staat '3.' in een label, los van de genummerde lijst met stap 1 en 2.
  - Na een fout staat de focus op de al ingevulde gebruikersnaam, niet op het wachtwoord.
  - De QR-code heeft geen beschrijving voor schermlezers.
  - De foutmelding plakt tegen de volgende alinea.
- **Waarom het ertoe doet** Het zijn kleine haperingen, maar wel in een flow die je zelden doorloopt en die er op dat moment toe doet.
- **Aanbeveling**
  - Nummer 'Stap 1, 2 en 3 van 3', en zet alle stappen op de QR-pagina in één genummerde lijst.
  - Geef bij een fout het wachtwoordveld focus (`autofocus`).
  - Geef de QR-code de beschrijving 'QR-code voor je authenticator-app; lukt scannen niet, gebruik de sleutel hieronder' (`role="img"`, `aria-label`).
  - Geef de foutmelding 12 px ruimte eronder.
- **Bewijs** `auth/setup.html:6`, `auth/mfa_setup.html:6-20`, `auth/login.html:11`, `auth/routes.py:450-451`; `a10-mfa-wrong.png` (bron: C-22, D-24 deels)

### [UX-68] Eenheden en notatie in Instellingen niet consequent, en de agenda staat in een smal vak
**Ernst** laag · **Inspanning** S · **Waar** Instellingen > getalvelden, Agenda

- **Probleem**
  - Er staat 'Plafond uit de omgeving: 1.000 € · …', terwijl het veld zelf het euroteken vooraan heeft.
  - Velden met € eindigen op 1157 px, velden met % op 1132 px.
  - 'Maximum aantal trades per dag' en 'Berichten per Claude-aanroep' hebben geen eenheid.
  - De agenda (30+ regels) staat in een vak van 298 px breed en 8 regels hoog.
- **Waarom het ertoe doet** Het formulier is moeizaam te scannen en de agenda is lastig te bewerken.
- **Aanbeveling** Schrijf het plafond met de eurofilter ('€ 1.000'). Geef de eenheid een vaste kolom, zodat alle velden rechts op één lijn eindigen. Voeg de eenheden 'orders' en 'berichten' toe. Laat lange tekstvakken zoals de agenda de volle breedte gebruiken.
- **Bewijs** `settings.html:42, 75-81`; `c01-agenda-field.png`, `c02-ceiling-field.png` (bron: C-23)

---

## 7. Toegankelijkheid

### [UX-69] De kleurtokens halen WCAG AA niet: statuspilletjes, knoppen, winstgroen en grijze uitlegtekst
**Ernst** hoog · **Inspanning** S · **Waar** De kleurvariabelen in `app.css :root`, dus alle pagina's

- **Probleem** De kleuren hebben te weinig contrast met hun achtergrond. Samen goed voor ruim 160 axe-meldingen.

  | Thema | Element | Contrast |
  |---|---|---|
  | Licht | 'Oefenmodus' (blauw pilletje) | 3,88:1 |
  | Licht | 'Bot actief', 'Kopen', 'telt mee' (groen pilletje) | 3,07:1 |
  | Licht | 'Pauzeren', 'Live', 'Verkopen' (rood pilletje en knop) | 4,17:1 |
  | Licht | oranje pilletje | 4,48:1 |
  | Licht | lichtblauwe knoppen | 3,67–3,98:1 |
  | Licht | groene winsttekst op wit | 3,50:1 |
  | Licht | lichtste grijs (`--text-3`, #86868b) op wit, in footer, op tegels | 3,62 / 3,32 / 3,15:1 |
  | Licht | middelste grijs (`--text-2`) op grijze vlakken (labels, niet-gekozen knoppen) | 4,07–4,41:1 |
  | Donker | elke primaire knop ('Order plaatsen', 'Opslaan'): wit op #0a84ff | 3,64:1 |
  | Donker | rood pilletje | 4,06:1 |
  | Donker | lichtste grijs op grijze vlakken | 3,52:1 |

  Rood is beter leesbaar dan groen, dus verlies valt meer op dan winst.
- **Waarom het ertoe doet** De modus, de status van de bot, de noodstop, de orderknop en de uitleg bij beslissingen over geld ('netto na kosten', plafonds) zijn slecht leesbaar, bijvoorbeeld op een telefoon buiten (WCAG 1.4.3).
- **Aanbeveling** Pas alleen de variabelen aan; de lichte achtergrondtinten mogen blijven.
  - Licht: `--green #147a3a`, `--accent #0062c4`, `--red #c4221a`, `--orange #b23c0b`, `--text-2 #636366`, `--text-3 #6a6a6f`.
  - Donker: een nieuwe `--accent-text #4aa3ff` voor het blauwe pilletje, de lichtblauwe knoppen en links; `--accent-fill #0071e3` als achtergrond van de primaire knop; `--red #ff6961`; `--text-2 #b8b8bd`; `--text-3 #aeaeb2`.
  - Gebruik `--text-2` voor de subtekst op tegels (`.stat .sub`).
  - Geef de grafiek eigen variabelen (`--chart-*`) als die de fellere kleuren moet houden.
  - Draai daarna axe opnieuw.
- **Bewijs** `app.css:17-26, 44-53, 69-78, 277-279, 321-324, 350-356, 385, 588, 613`; `axe-report.json`, `toegankelijkheid/axe-full.json`, `contrast.py` (bron: A-12, D-01, D-02, E-02, B-17 deels, C-14 deels)

### [UX-70] Met het toetsenbord zie je bijna niet waar je bent op schakelaars, de moduskeuze en het grafiekvinkje
**Ernst** hoog · **Inspanning** S · **Waar** Instellingen > schakelaars en Modus (Oefenmodus/Live); Overzicht > 'Adviezen tonen'

- **Probleem**
  - De echte invoervelden zijn onzichtbaar gemaakt. De enige aanwijzing van focus is een gloed in 12% blauw: een contrast van 1,18:1 met de kaart in licht, en 1,28–1,56:1 in donker.
  - Een algemene regel (`input:focus { outline: none }`) haalt de focusrand ook bij vinkjes en keuzerondjes weg.
  - Tekstvelden krijgen wel een blauwe rand.
- **Waarom het ertoe doet** Juist bij automatisch handelen, de risk-off en de modus zie je met het toetsenbord niet welk element actief is (WCAG 2.4.7 en 1.4.11).
- **Aanbeveling**
  - Geef alles een zichtbare focusrand: `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`.
  - Beperk de regel die de rand weghaalt tot tekstvelden: `input:not([type=checkbox]):not([type=radio]):focus`. Die regel weegt namelijk zwaarder dan `:focus-visible`.
  - Geef de schakelaars en de moduskeuze dezelfde rand: `.switch input:focus-visible + .track, .seg-radio input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 2px }`.
- **Bewijs** `app.css:20, 376-380, 628, 652, 657, 661`; `d01-switch-focus.png`, `d02-mode-focus.png`, `focus-compare2.png`, `keyboard-light.json` (bron: D-03, C-21)

### [UX-71] Halfdoorzichtig maken maakt niet-meetellend nieuws en geblokkeerde beslissingen onleesbaar
**Ernst** middel · **Inspanning** S · **Waar** Nieuws > berichten die niet meetellen (`.news-item.dim`); Handelen > geblokkeerde cycli (`tr.dim`)

- **Probleem**
  - Deze elementen worden voor 50% doorzichtig gemaakt (`opacity: 0.5`). Daardoor komt grijze tekst op 2,0:1 (donker 2,67:1) en een titel op 3,27:1. Op Nieuws levert dat 36 contrastmeldingen op.
  - Bij geblokkeerde cycli geldt hetzelfde, ook voor 'Niet uitgevoerd: <reden>' (ongeveer 2:1).
  - De link 'Notitie bewerken' haalt 4,08:1 (donker 3,49:1).
- **Waarom het ertoe doet** Juist de reden waarom iets niet meetelt of werd geblokkeerd, moet je kunnen lezen.
- **Aanbeveling**
  - Gebruik geen doorzichtigheid en geen keten van kleurvariabelen. Variabelen naar elkaar laten verwijzen op hetzelfde element komt uit op het lichtste grijs, met 3,62:1.
  - Zet expliciete kleuren: titel en grijze tekst op `--text-2`, en het pilletje 'telt niet mee' op volle sterkte.
  - Geef geblokkeerde rijen een oranje pilletje 'Geblokkeerd', met de reden in `--text-2`.
  - Geef de link 'Notitie bewerken' de nieuwe accentkleur.
- **Bewijs** `app.css:301, 538-541, 566, 734`, `news.html:72`, `trading.html:195`; `news-dim--dark.png`, `axe-full.json` (bron: D-09, E-13, B-17 deels)

### [UX-72] Links in lopende tekst zijn alleen aan hun kleur te herkennen
**Ernst** middel · **Inspanning** S · **Waar** Voettekst, ondertitel van het journaal, helpteksten op Handelen, Tradingplan

- **Probleem** Links zijn in de hele app niet onderstreept. axe meldt dit op alle 7 pagina's en op Account: het kleurverschil met de omringende tekst is maar 1,07–1,29:1. Ook met de nieuwe kleuren blijft dat ongeveer 1,1:1.
- **Waarom het ertoe doet** Je ziet niet welke woorden klikbaar zijn (WCAG 1.4.1).
- **Aanbeveling** Onderstreep links in lopende tekst met een lijn van 1 px (`.subtitle a, .form-help a, .card-desc a, footer.site a, p a, dd a, li a:not(.btn)`, met `text-underline-offset: .15em`). Losse links als 'Aanpassen ›' kunnen blijven zoals ze zijn.
- **Bewijs** `app.css:99-100`, `base.html:64`, `journal.html:7`, `trading.html:28, 65, 130`; `axe-full.json` (bron: D-10)

### [UX-73] Velden zonder label, en dubbele of verkeerd gekoppelde labels bij schakelaars en Modus
**Ernst** middel · **Inspanning** S · **Waar** Strategie > JSON-editor en omschrijving; Instellingen > schakelaars en Modus

- **Probleem**
  - Het tekstvak met de JSON heeft geen label: een schermlezer leest alleen de inhoud voor. Zonder sleutel is dit de enige manier om de strategie aan te passen.
  - Het omschrijvingsveld voor Claude heeft alleen een voorbeeldtekst, die verdwijnt zodra je typt.
  - Elke schakelaar heeft twee labels, waardoor de naam meewisselt met de stand ('Bot automatisch laten handelen Aan'). axe meldt dit 8 keer.
  - Het label van Modus verwijst naar een veld dat niet bestaat (`for="f-trading_mode"`).
- **Waarom het ertoe doet** Dit zijn WCAG-fouten (1.3.1, 3.3.2, 4.1.2) op de editor en op de belangrijkste schakelaars.
- **Aanbeveling**
  - Geef het JSON-vak een label ('Strategie (JSON)') en het omschrijvingsveld een zichtbaar label ('Wat wil je dat de bot doet?').
  - Geef elke schakelaar maar één label, en verberg het woord Aan/Uit voor schermlezers.
  - Groepeer de keuze voor Modus met een `<fieldset>` en `<legend>`, of met `role="radiogroup"` en `aria-labelledby`.
- **Bewijs** `strategy.html:52, 138`, `settings.html:39, 47-58`; `report-main.json` (velden zonder label), `axe-full.json` (bron: B-06, D-12)

### [UX-74] Invoervelden en uitgeschakelde schakelaars steken te weinig af tegen de achtergrond
**Ernst** middel · **Inspanning** S · **Waar** Alle formulieren; Instellingen > schakelaar in de stand Uit

- **Probleem** De randen van invoervelden halen 1,54:1 (donker 2,05:1). Een uitgeschakelde schakelaar haalt 1,27:1 op de kaart (donker 1,57:1). Alleen het woord 'Uit' laat zien in welke stand hij staat.
- **Waarom het ertoe doet** Slechtzienden zien niet waar een veld begint, of dat er überhaupt een schakelaar is (WCAG 1.4.11).
- **Aanbeveling** Maak een kleurvariabele voor randen van bedieningselementen: #8e8e93 in licht (3,26:1) en #86868b in donker (4,70:1). Gebruik die voor invoervelden, keuzelijsten en tekstvakken, en als rand van een schakelaar in de stand Uit.
- **Bewijs** `app.css:14-15, 370, 629-637`; `contrast.py` (bron: D-20)

### [UX-75] In de Windows-contrastmodus verdwijnen schakelaars en de gekozen modus
**Ernst** middel · **Inspanning** S · **Waar** Instellingen > schakelaars en Modus; Overzicht > keuzeknoppen; navigatie

- **Probleem** Met geforceerde kleuren (de contrastmodus van Windows):
  - verdwijnt de schakelaar helemaal;
  - is bij Modus, BTC/ETH en de periode niet te zien wat er gekozen is;
  - valt de markering van het actieve navigatie-item weg;
  - verliezen meldingen hun rand.

  De CSS heeft geen regels voor deze modus (`@media (forced-colors: active)`).
- **Waarom het ertoe doet** Wie een thema met hoog contrast nodig heeft, ziet niet of automatisch handelen aan staat, of welke modus actief is.
- **Aanbeveling** Voeg regels toe voor geforceerde kleuren. Geef schakelaars, gekozen opties, het actieve navigatie-item, meldingen en pilletjes een rand of omlijning in de systeemkleuren (`ButtonText`, `Highlight`, `CanvasText`).
- **Bewijs** `app.css:169, 434, 627-663`; `forced-combo.png` (bron: D-08)

### [UX-76] Grafieken hebben een vaste beschrijving en kondigen wijzigingen niet aan
**Ernst** middel · **Inspanning** M · **Waar** Overzicht > Koers en acties, Portefeuillewaarde

- **Probleem**
  - Beide grafieken hebben een vaste beschrijving voor schermlezers, die niet verandert bij een andere markt of periode.
  - Onder de grafieken staan goede samenvattingen, maar die zijn niet aan de grafiek gekoppeld en worden niet voorgelezen als ze veranderen.
  - Koersdetails (open, hoog, laag, slot) en de details van markeringen zijn alleen met de muis te zien.
- **Waarom het ertoe doet** Een schermlezer- of toetsenbordgebruiker kan het koersverloop niet volgen, en hoort niet of een periode geladen is (WCAG 1.1.1, 2.1.1, 4.1.3).
- **Aanbeveling**
  - Laat de samenvattingen onder de grafiek voorlezen als ze veranderen (`role="status" aria-live="polite"`) en koppel ze aan de grafiek (`aria-describedby`).
  - Werk de beschrijving na het laden bij: 'Koersgrafiek BTC-EUR, 7 dagen: van … naar …'.
  - Breid de lijst met acties uit tot een tabel met per dag open, hoog, laag, slot en de acties.
- **Bewijs** `dashboard.html:82-89, 99-102`, `chart.js:152-166, 215-236, 266-279`; `misc.js` (bron: D-13)

### [UX-77] De koppenstructuur slaat niveaus over, en trades en nieuwsberichten hebben geen kop
**Ernst** middel · **Inspanning** S · **Waar** Handelen, Nieuws, Journaal, Overzicht

- **Probleem**
  - Op Handelen gaan de koppen van niveau 1 naar 3, terug naar 2 en dan naar 4. Op Nieuws van 1 naar 3 (BTC/ETH) en dan naar 2.
  - Een trade heeft alleen de koppen 'Instap' en 'Uitstap' (niveau 4); de markt is gewone tekst.
  - Nieuwsberichten hebben geen kop.
  - axe meldt een onjuiste koppenvolgorde op Handelen, Journaal en Nieuws.
- **Waarom het ertoe doet** Met een schermlezer kun je niet van trade naar trade of van bericht naar bericht springen.
- **Aanbeveling** Maak alle kaarttitels op paginaniveau een kop van niveau 2. Maak in het journaal de markt een kop van niveau 3 en koppel die aan de trade (`aria-labelledby`). Maak nieuwstitels een kop van niveau 3. De grootte blijft gelijk via de bestaande klassen.
- **Bewijs** `trading.html:44, 57, 69, 251`, `news.html:22, 35, 72-105`, `journal.html:118, 142, 156`; `misc.js`, `axe-full.json` (bron: D-15, B-25 deels)

### [UX-78] Schermlezers horen niet welk filter actief is of bij welke groep een knop hoort, en de lege staat bij Nieuws klopt niet
**Ernst** middel · **Inspanning** S · **Waar** Nieuws > filters; Instellingen > lijst met onderdelen; Overzicht > keuzeknoppen

- **Probleem**
  - De filters op Nieuws en de lijst met onderdelen in Instellingen tonen de actieve keuze alleen visueel, zonder `aria-current`.
  - De groepen markt- en periodeknoppen hebben een naam maar geen rol, waardoor die naam niet wordt voorgelezen (axe: aria-prohibited-attr).
  - In een lege database zegt het filter 'Telt niet mee': 'Alles telt mee – Elk nieuwsfeit … weegt mee', terwijl er geen berichten zijn.
  - De periodefilter staat los van het vaste venster van 48 uur waarmee het sentiment wordt berekend, zonder dat dat wordt uitgelegd.
- **Waarom het ertoe doet** Een schermlezer weet niet welk filter aan staat (WCAG 1.3.1), en de lege staat suggereert nieuws dat er niet is.
- **Aanbeveling**
  - Zet `aria-current="true"` op de actieve link en `role="group"` op de knopgroepen.
  - Toon aantallen in de filterknoppen.
  - Toon de lege staat 'Alles telt mee' alleen als er berichten zijn.
  - Voeg de uitleg toe: 'Sentiment rekent altijd met de laatste 48 uur'.
- **Bewijs** `news.html:42, 57-66, 107-109`, `settings.html:16-19`, `dashboard.html:65, 68`; `axe-full.json` (bron: D-14 deels, B-21)

### [UX-79] Engelstalige nieuwstitels zijn niet als Engels gemarkeerd
**Ernst** middel · **Inspanning** M · **Waar** Nieuws > berichttitels en 'Ook gemeld door'; sentimentkaarten

- **Probleem** Alle standaardfeeds zijn Engelstalig, maar nergens staat `lang="en"`. Een Nederlandse schermlezerstem spreekt 'Exchange hack drains $40m…' daardoor uit met Nederlandse uitspraak.
- **Waarom het ertoe doet** De koppen zijn moeilijk te verstaan (WCAG 3.1.2, AA).
- **Aanbeveling** Markeer de titels in de lijst en de links bij 'Ook gemeld door' altijd met de taal van de bron (`lang="{{ it.lang or 'en' }}"`), want daar staat altijd de originele titel. Doe dat op de sentimentkaarten alleen als er geen Nederlandse samenvatting is. Sla per feed de taal op.
- **Bewijs** `news.html:45, 83, 90`, `config.py:59-67`; `misc.js` (bron: D-19)

### [UX-80] Geen link 'Naar de inhoud': 9 tot 11 keer Tab voordat je bij de inhoud bent
**Ernst** laag · **Inspanning** S · **Waar** `base.html`

- **Probleem** Op elke pagina komen eerst het logo, 7 navigatielinks en de themaknop, en ingelogd ook Account en Uitloggen. De pagina is wel in gebieden ingedeeld (landmarks), dus WCAG 2.4.1 wordt formeel gehaald.
- **Waarom het ertoe doet** Wie ziet maar met het toetsenbord werkt, moet bij elke nieuwe pagina eerst door de hele navigatie heen.
- **Aanbeveling** Zet bovenaan een link 'Naar de inhoud' die pas zichtbaar wordt als hij focus krijgt (`<a class="skip-link" href="#main">`), en geef de hoofdinhoud de bijbehorende id (`<main id="main" tabindex="-1">`).
- **Bewijs** `base.html:13-54`; `keyboard-light.json` (bron: D-11)

### [UX-81] Belangrijke uitleg staat alleen in een tooltip
**Ernst** laag · **Inspanning** S · **Waar** Pilletje met de modus, Laatste orders (reden), kenmerken van de strategie

- **Probleem**
  - De uitleg bij het modus-pilletje staat alleen in een tooltip (`title`), op een element dat geen focus kan krijgen.
  - De reden van een order wordt na 180 px afgekapt; de volledige tekst staat alleen in de tooltip.
  - Hetzelfde geldt voor het type van de kenmerken bij de strategie.
- **Waarom het ertoe doet** Op een touchscreen en met het toetsenbord zie je deze uitleg nooit.
- **Aanbeveling** Maak het modus-pilletje een link naar `/settings#trading`, of een knop die de uitleg zichtbaar uitklapt. Laat de reden van een order doorlopen op een volgende regel in plaats van af te kappen. Zet het type zichtbaar in het kenmerk zelf, of in een uitklapblok.
- **Bewijs** `base.html:32, 34`, `dashboard.html:243`, `app.css:303`, `strategy.html:36` (bron: D-22)

### [UX-82] Blijvende meldingen worden als dringende waarschuwing aangekondigd
**Ernst** laag · **Inspanning** S · **Waar** De macro voor meldingen in `_macros.html`

- **Probleem** Elke waarschuwing en foutmelding krijgt `role="alert"`, ook blijvende toestanden zoals een pauze, de herinneringen voor live of onbewaakte posities. Na een orderfout staan er zo twee dringende meldingen tegelijk.
- **Waarom het ertoe doet** De echt dringende melding valt weg tussen vaste banners.
- **Aanbeveling** Voeg aan de macro een optie toe (`notice(kind, text, live=false)`). Gebruik `role="alert"` alleen voor fouten van de laatste actie, `role="status"` voor bevestigingen, en geen van beide voor blijvende toestanden.
- **Bewijs** `_macros.html:20`, `base.html:56`; `order.js` (bron: D-23)

---

## 8. Visueel ontwerp & consistentie

### [UX-83] Er is weinig verschil tussen kopjes, uitleg en details
**Ernst** middel · **Inspanning** M · **Waar** Alle pagina's

- **Probleem**
  - 47–66% van alle tekens is 13 px of kleiner (op Handelen 66%), en 56–67% is grijs.
  - De CSS gebruikt 14 lettergroottes, plus nog 15, 26 en 32 px direct in de HTML.
  - De grootte van kaarttitels hangt af van de kolom, niet van het belang. 'Harde limieten' is 17 px op Handelen en 22 px op Instellingen, en de bovenste kaarten op Handelen hebben kleinere titels dan de secties eronder.
  - Er zijn vier stijlen voor tussenkopjes en vijf groottes voor grote getallen.
- **Waarom het ertoe doet** Als bijna alles klein en grijs is, verdwijnt het onderscheid tussen kerncijfer, uitleg en details.
- **Aanbeveling**
  - Leg een vaste reeks lettergroottes vast als variabelen: 12 (bijschrift), 13 (details), 14 (uitleg), 15 (lopende tekst), 17, 19 (kaarttitel), 22, 34, 24 (kerncijfer) en 44 (groot getal).
  - Maak een klasse voor kaarttitels (`.card-title`) die los staat van het kopniveau, en één stijl voor tussenkopjes (`.eyebrow`).
  - Gebruik het lichtste grijs alleen voor tijdstippen en bronnen.
- **Bewijs** `app.css:102-106, 263-265, 277-279, 706, 727, 731`, `trading.html:20, 44, 57`, `strategy.html:20, 26`; `measure-light.json` (bron: E-09)

### [UX-84] Knopstijlen worden zonder vaste regel gebruikt
**Ernst** middel · **Inspanning** S · **Waar** Strategie, Instellingen, Journaal, Handelen

- **Probleem**
  - Terugzetten ziet er op drie manieren uit: 'Standaardstrategie herstellen' is lichtblauw (net als 'Testen'), 'Standaard herstellen' is grijs en 'Oefenportefeuille resetten' is rood.
  - 'Filteren' is net zo prominent als 'Order plaatsen'.
  - Een uitgeschakelde knop is alleen half doorzichtig.
  - Twee CSS-klassen (`.btn-success`, `.grid-4`) worden nergens gebruikt.
- **Waarom het ertoe doet** Als kleur niet consequent aangeeft hoe ingrijpend een actie is, leer je er niet op te vertrouwen.
- **Aanbeveling**
  - Leg vast welke stijl waarvoor is:
    - **Primair:** de hoofdactie per formulier.
    - **Grijs:** filteren en wissen.
    - **Lichtblauw:** een veilige nevenactie.
    - **Rood:** acties die iets overschrijven of onomkeerbaar zijn, zoals resetten, de standaardstrategie herstellen, de authenticator opnieuw instellen en overal uitloggen.
  - 'Pauzeren' blijft een herkenbare rode noodstop.
  - Zet bij een uitgeschakelde knop waarom hij uit staat.
  - Haal de ongebruikte klassen weg.
- **Bewijs** `strategy.html:134, 140-141`, `settings.html:107, 140`, `journal.html:104`, `app.css:214, 350-358` (bron: E-12)

### [UX-85] Groen, rood en blauw hebben te veel betekenissen
**Ernst** laag · **Inspanning** M · **Waar** Pilletjes en labels

- **Probleem**
  - Het blauwe pilletje staat voor de modus, voor statussen ('Open', 'uitgevoerd'), voor tellers ('2 bronnen') en voor 'Nog niet opgeslagen'. Bovendien hebben links en informatieve meldingen dezelfde kleur.
  - Oranje en rood zijn vrijwel even licht (1,03:1), waardoor '~' en '✗' nauwelijks verschillen.
  - Groen en rood voor kopen en verkopen is de gangbare conventie en kan blijven.
- **Waarom het ertoe doet** Een status lijkt zo op een link of een informatieve melding.
- **Aanbeveling** Gebruik een grijs pilletje voor neutrale tellers en details. Haal de modus uit het blauwe pilletje (UX-05). Maak oranje geler en lichter, bijvoorbeeld #b45309.
- **Bewijs** `trading.html:142, 200`, `news.html:78`, `settings.html:30`, `strategy.html:70`; `trade-cycle-checks--light.png` (bron: E-03)

### [UX-86] Kaarten worden even hoog uitgerekt, en de strategiekaart op Overzicht is overvol
**Ernst** laag · **Inspanning** S · **Waar** Overzicht, Strategie, Tradingplan, Handelen, Account

- **Probleem**
  - Door de volledige strategiebeschrijving (ongeveer 15 regels, met labels in een typemachinelettertype) is de kaart 491 px hoog. 'Resultaat 30 dagen' en 'Claude-budget' rekken mee en hebben 308 px leeg.
  - Ook 'Vraag Claude' (466 van 666 px), 'Uitstappen' (462 van 914 px) en de statuskaart op Account zijn grotendeels leeg.
- **Waarom het ertoe doet** Grote witte vlakken ogen onaf en duwen de orderkaart onnodig omlaag.
- **Aanbeveling** Kap de beschrijving af na 3 regels, met een link 'Volledige uitleg ›'. Zet de strategie over de volle breedte, of maak van Resultaat en Budget compacte tegels. Laat kaarten in een rij met ongelijke inhoud niet meerekken (`align-items: start`). Gebruik het gewone lettertype voor de labels.
- **Bewijs** `dashboard.html:170-206`, `app.css:211-215, 562`; `dashboard--desktop-light.png`, `strategy--desktop-light.png` (bron: A-22, E-16)

### [UX-87] Uitlijning: verspringende cijfers, losse tegels en velden die niet op één lijn staan
**Ernst** laag · **Inspanning** S · **Waar** Journaal, Backtest, Handelen, Tradingplan, pauzemelding

- **Probleem**
  - 'Winst/verlies-factor' breekt over twee regels, waardoor die waarde 20 px lager staat dan de rest. Bij 'Eindwaarde' breekt het bedrag zelf.
  - 'Kosten' en 'Koopsignaal live' staan elk alleen op een rij.
  - Een keuzelijst is 43 px hoog, een invoerveld 45 px.
  - In de pauzemelding staan icoon en tekst niet op één lijn, door een marge van 10 px boven de knop.
- **Waarom het ertoe doet** Kerncijfers die niet op één lijn staan, zijn lastiger te vergelijken.
- **Aanbeveling** Voorkom dat labels en waarden van tegels afbreken (`nowrap`), of kort de labels in. Verdeel de tegels gelijkmatig (`repeat(auto-fill, minmax(160px, 1fr))`). Geef invoervelden en keuzelijsten dezelfde minimale hoogte van 44 px. Lijn een melding met knop verticaal in het midden uit (`.notice { align-items: center }`).
- **Bewijs** `app.css:275-279, 365-375, 399-402, 702-705`, `trading.html:46`; `journal-stats--dark.png`, `plan-fields--light.png`, `paused-dash-top--light.png` (bron: E-17)

### [UX-88] Donkere modus: de gekozen optie is donkerder dan de balk eromheen
**Ernst** laag · **Inspanning** S · **Waar** Overzicht > markt en periode; Nieuws > filters; Instellingen > Modus

- **Probleem** De gekozen optie heeft de kleur van de kaart (#1c1c1e), op een balk van #323236. Daardoor lijkt hij ingedrukt in plaats van gekozen, en de zwarte schaduw is onzichtbaar.
- **Waarom het ertoe doet** Je ziet minder snel welke modus of periode gekozen is.
- **Aanbeveling** Maak per thema een eigen kleur voor de gekozen optie (wit in licht, #636366 in donker), met een bijpassende schaduw.
- **Bewijs** `app.css:414-435, 655-661`; `dash-segmented--dark.png`, `settings-mode--dark.png` (bron: E-19)

### [UX-89] Iconen en uitklappijltjes zijn niet consequent
**Ernst** laag · **Inspanning** M · **Waar** Alle pagina's en de inlogflow

- **Probleem**
  - Er staan echte iconen naast teksttekens (›, ✓, ✗, ~, •).
  - Sommige uitklapblokken ('Ook gemeld door', 'Eerste keer?') gebruiken het standaard ▶-driehoekje, andere een eigen draaiende ›.
  - Elke lege staat heeft hetzelfde postvak-icoon.
  - Gewone kenmerken staan in een typemachinelettertype.
  - Het kleurverloop van het merk staat vast in de code, en de indigo komt nergens anders terug.
- **Waarom het ertoe doet** De app oogt minder als één geheel, en het typemachinelettertype leest moeizaam.
- **Aanbeveling** Maak één set iconen (een SVG-sprite met een macro `ui.icon()`) en één stijl voor uitklapblokken. Kies per context een passend icoon voor lege staten. Gebruik het typemachinelettertype alleen voor technische namen. Leg de merkkleuren vast als variabelen (`--brand-1`, `--brand-2`), en gebruik de indigo bijvoorbeeld voor de oefenmodus.
- **Bewijs** `app.css:548-549, 562, 573, 682`, `_macros.html:3-17, 51`, `base.html:18`; `news--desktop-light.png` (bron: E-21)

### [UX-90] De themaknop: geen weg terug naar automatisch, en de kleur van de browserbalk volgt je keuze niet
**Ernst** laag · **Inspanning** S · **Waar** Navigatie > themaknop

- **Probleem**
  - Na één klik volgt de app nooit meer het thema van je systeem.
  - De knop heeft een vaste naam en geeft niet aan welk thema nu actief is.
  - De kleur van de browserbalk volgt alleen het systeemthema. Kies je zelf licht terwijl je systeem donker is, dan staat er een zwarte balk boven een lichte pagina.
- **Waarom het ertoe doet** Een kleine maar blijvende ergernis.
- **Aanbeveling** Bied drie standen: Automatisch, Licht en Donker, met een naam die de huidige stand noemt. Wis bij Automatisch de opgeslagen keuze. Pas de kleur van de browserbalk (`meta[name=theme-color]`) aan in app.js en theme.js.
- **Bewijs** `app.js:13-31`, `theme.js:4-6`, `base.html:7-8`, `auth/_base.html:8-9` (bron: H-12, D-14 deels)

### [UX-91] Tabblad, tabbladicoon en beginscherm tonen geen modus of status, en er is geen app-icoon
**Ernst** laag · **Inspanning** S · **Waar** Paginatitel, tabbladicoon, 'Zet op beginscherm'

- **Probleem**
  - De titel is altijd 'Crypie · <pagina>', ook in live of tijdens een pauze.
  - Er is één vast tabbladicoon.
  - Er is geen app-icoon voor het beginscherm van een telefoon (manifest en apple-touch-icon ontbreken).
- **Waarom het ertoe doet** Met meerdere tabbladen open, of met een oefen- en een live-instantie naast elkaar, is de tabbladbalk de enige plek die altijd zichtbaar is.
- **Aanbeveling** Zet de status in de titel: 'LIVE · Overzicht · Crypie' en 'Gepauzeerd · Handelen · Crypie'. Maak een variant van het tabbladicoon met een rode stip (live) of oranje stip (gepauzeerd). Voeg een manifest toe met iconen van 192 en 512 px, en een apple-touch-icon.
- **Bewijs** `base.html:5, 9` (bron: H-11)

### [UX-92] Vaste ontwerpwaarden bestaan alleen voor kleur: 63 stijlen direct in de HTML, 11 verschillende afrondingen
**Ernst** laag · **Inspanning** L · **Waar** `app.css` en alle templates

- **Probleem**
  - Er zijn geen vaste waarden (tokens) voor afstanden en lettergroottes, en maar twee voor afgeronde hoeken.
  - 63 keer staat een stijl direct in de HTML (`style=`), voor lettertype, indeling en kleur.
  - Er worden afrondingen gebruikt van 2 tot 980 px.
  - Er staat een ongebruikte reservekleur (#e8912d).
- **Waarom het ertoe doet** Dit heeft geen direct effect voor de gebruiker, maar is de structurele oorzaak van UX-27, UX-83, UX-86 en UX-87.
- **Aanbeveling** Leg vaste waarden vast voor afstanden, afrondingen en lettergroottes. Vervang de stijlen in de HTML door herbruikbare onderdelen (`.metric`, `.meter`, `.card--inset`, `.inline-field`, `.eyebrow`). Voeg een automatische controle toe die nieuwe `style=` in templates signaleert.
- **Bewijs** `app.css:9-31`; zoeken op `style=` in `app/templates` (bron: E-15)

---

## 9. Mobiel & responsief

### [UX-93] Tabellen knippen belangrijke kolommen af, zonder enige aanwijzing dat je kunt scrollen
**Ernst** hoog · **Inspanning** M · **Waar** Overzicht > Open posities en Laatste orders; Handelen > Alle cyclussen, posities en trend

- **Probleem**
  - Op desktop (1440 px) toont 'Laatste orders' 528 van de 756 px. Kosten (zichtbaar als '€ 0,1'), Koers en Status ('Afgewezen') vallen weg.
  - Op 360 en 390 px toont Open posities 2 van de 7 kolommen: Resultaat, Stop-loss en Take-profit zijn onzichtbaar. Alle cyclussen toont er 2 van de 10.
  - De tabel stopt netjes aan de rand van een kolom en lijkt daardoor compleet. Op macOS, iOS en Android verschijnt de scrollbalk pas als je al scrolt.
  - Het tabelvak heeft geen naam en kan geen focus krijgen (axe: scrollable-region-focusable). In Safari kun je het met het toetsenbord niet scrollen.
- **Waarom het ertoe doet** Op je telefoon zie je niet hoe je positie ervoor staat, en op desktop niet of een order is afgewezen.
- **Aanbeveling**
  - Geef 'Laatste orders' de volle breedte, of maak de tabel compacter: kosten als tweede regel onder het bedrag, en de status als pilletje naast kopen/verkopen.
  - Toon tabellen onder 640 px als kaarten per rij, met Markt, Resultaat en Stop-loss bovenaan, of verberg minder belangrijke kolommen.
  - Laat een schaduw aan de rand zien zolang er meer te scrollen is, en zet de eerste kolom vast.
  - Geef het tabelvak een naam, laat het focus krijgen (`role="region" tabindex="0" aria-labelledby`) en geef het een zichtbare focusrand.
  - Maak een afgekapte reden uitklapbaar.
- **Bewijs** `app.css:229, 283, 303`, `dashboard.html:150, 237-252`, `trading.html:191-209`; `dash-orders--light.png`, `m-openpos.png`, `trading-decisions-360.png` (bron: A-11, D-16, E-10, F-10)

### [UX-94] Op schermen van 320–360 px scrolt de pagina zijwaarts en vallen knoppen buiten de kaart
**Ernst** hoog · **Inspanning** S · **Waar** Tradingplan, Handelen > hero, Overzicht > periodeknoppen, Instellingen

- **Probleem**
  - Op 320 px, wat overeenkomt met 400% zoom, worden pagina's breder dan het scherm:

    | Pagina | Te breed | Oorzaak |
    |---|---|---|
    | Tradingplan | +63 px | de velden hebben een minimale breedte van 340 px |
    | Handelen | +40 px | 'Nu een cyclus draaien' en 'Voorstel toepassen' mogen niet afbreken en niet naar een nieuwe regel |
    | Overzicht | +15 px | periodeknoppen |
    | Instellingen | +14 px | velden |

  - Op 360 px is Tradingplan 383 px breed en steekt de cyclusknop buiten de kaart.
  - Met de tekstafstand uit WCAG wordt Handelen op 390 px 49 px te breed.
- **Waarom het ertoe doet** Voor de noodstop en de cyclusknop moet je in twee richtingen scrollen (WCAG 1.4.10). Op veelgebruikte Android-telefoons schuift het tradingplan zijwaarts heen en weer.
- **Aanbeveling**
  - Laat velden smaller worden dan 340 px als het scherm dat vraagt (`minmax(min(340px, 100%), 1fr)`).
  - Laat velden in Instellingen onder 640 px krimpen (`.setting { grid-template-columns: minmax(0, 1fr) }`).
  - Laat knoppen in de hero en bij acties afbreken (`.btn { white-space: normal }`) en naar een nieuwe regel gaan (`.hero-status { flex-wrap: wrap }`, via een klasse).
  - Laat keuzeknoppen en de links rechts in de kaartkop op mobiel afbreken.
  - Neem 360 px op in de visuele tests.
- **Bewijs** `app.css:251, 346, 431, 619, 702`, `trading.html:31-37, 267`; `plan-320.png`, `trading-320.png`, `plan-360-card.png`, `trading-hero-360.png` (bron: D-07, F-11)

### [UX-95] Met veel markten loopt de marktkeuze uit de kaart
**Ernst** middel · **Inspanning** S · **Waar** Overzicht > marktknoppen en Koersen; Handelen > hero; voettekst

- **Probleem** De app staat 20 markten toe. Getest met 20 markten:
  - Op desktop is de balk met marktknoppen 1234 px breed en na NEAR afgesneden. Een markt die de app had onthouden, kan zo gekozen én onzichtbaar zijn.
  - Op 390 px wordt Overzicht 1072 px breed.
  - De hero zegt 'Handelt op … en … en …', met 19 keer 'en'.
  - De voettekst somt alle markten op en is op mobiel 192 px hoog.
  - Koersen sorteert alfabetisch, terwijl de rest de volgorde uit Instellingen gebruikt.
- **Waarom het ertoe doet** Op mobiel loopt de balk al vanaf ongeveer 6 markten uit beeld.
- **Aanbeveling**
  - Gebruik vanaf ongeveer 6 markten een keuzelijst (eventueel met zoekveld), of maak de balk scrollbaar met een vervagende rand en schuif de gekozen knop in beeld.
  - Vat lange lijsten samen in de hero ('BTC-EUR, ETH-EUR en 18 andere'), en in de voettekst ('Markten: 20 · bekijken').
  - Gebruik overal de volgorde uit Instellingen.
- **Bewijs** `app.css:414-432, 443`, `trading.html:26`, `base.html:64`; `many-chartcard-desktop.png`, `many-chartcard-mobile.png`, `many-dashboard-mobile.png` (bron: H-07)

### [UX-96] Grafiek op een touchscreen: 'Beweeg over de grafiek' werkt niet, en een label wordt afgekapt
**Ernst** laag · **Inspanning** S · **Waar** Overzicht > grafieken op de telefoon

- **Probleem**
  - Een gewone tik toont de tooltip niet; dat lukt alleen als je je vinger op de grafiek houdt.
  - Daarna blijft de tooltip (170×120 px, over een grafiek van 300 px) staan tot je opnieuw tikt.
  - Het label 'Koop' wordt aan de rechterrand afgekapt tot 'Koo'.
- **Waarom het ertoe doet** Op je telefoon ontdek je de details niet, of raak je de tooltip niet meer kwijt.
- **Aanbeveling** Pas de uitleg aan op het apparaat: 'Houd je vinger op de grafiek, of open de lijst hieronder'. Laat de tooltip verdwijnen zodra je loslaat (`exitMode: OnTouchEnd`). Laat rechts wat ruimte vrij (`rightOffset` van minstens 4), of gebruik kortere labels.
- **Bewijs** `dashboard.html:73`, `chart.js:152-166`; `m-price.png`, `chart-longpress-mobile.png` (bron: F-19, A-24 deels)

### [UX-97] Kleine tikvlakken, inzoomen op iPhone bij tekstvelden, en geen ruimte voor de notch
**Ernst** laag · **Inspanning** S · **Waar** Grafiekbediening, links in kaartkoppen, uitklapblokken; alle formulieren

- **Probleem**
  - Veel tikvlakken zijn klein: periode- en marktknoppen 28 px hoog, links als 'Details ›' en 'Notitie toevoegen' 21 px, de onderdelen in Instellingen 32 px. Dat haalt WCAG 2.5.8 wel, maar niet de 44 pt die Apple aanraadt.
  - Alle invoervelden hebben een lettergrootte van 15 px, waardoor iPhone-Safari inzoomt zodra je erin tikt.
  - De pagina gebruikt het hele scherm (`viewport-fit=cover`), maar houdt geen ruimte vrij voor de notch en afgeronde hoeken.
- **Waarom het ertoe doet** Je tikt vaker mis, en de pagina verspringt als je een order of limiet invult.
- **Aanbeveling**
  - Maak de periode- en marktknoppen op touchscreens minstens 40 px hoog, en geef links en uitklapblokken 8–10 px extra ruimte boven en onder.
  - Gebruik onder 640 px een lettergrootte van 16 px in invoervelden.
  - Houd in navigatie, pagina en voettekst ruimte vrij voor de notch (`env(safe-area-inset-*)`).
  - Dit is niet op een echt iOS-toestel getest.
- **Bewijs** `app.css:365-367, 436-438, 493`, `base.html:6`; `report-main.json` (kleine tikvlakken), `inputs.js` (bron: F-20, F-21)

### [UX-98] Lange URL's in journaalnotities breken de opmaak
**Ernst** laag · **Inspanning** S · **Waar** Journaal > Jouw notitie

- **Probleem** Een gewone nieuws-URL van ongeveer 150 tekens maakt de pagina op 390 px 443 px breed. De URL is bovendien niet klikbaar.
- **Waarom het ertoe doet** Bij een trade de bron plakken waarop je handelde, is een voor de hand liggende manier van journaling.
- **Aanbeveling** Laat lange woorden in notities en rapporten afbreken (`.prose, .report { overflow-wrap: anywhere }`). Doe dat niet in tabelcellen, want dan breken bedragen midden in het getal. Maak URL's klikbaar met een filter dat de tekst eerst veilig maakt en alleen http(s)-links toestaat (`rel="noopener noreferrer"`).
- **Bewijs** `app.css:528, 684`, `journal.html:175`; `longnote-mobile.png` (bron: H-10)

---

## 10. Feedback, states & prestaties

### [UX-99] Na een actie zie je geen bevestiging
**Ernst** hoog · **Inspanning** M · **Waar** Overzicht (order), Handelen (cyclus, voorstel), Nieuws (verversen), Instellingen (opslaan), Journaal (notitie)

- **Probleem**
  - Na een geslaagde order kom je zonder melding bovenaan Overzicht terecht. Het resultaat staat alleen in 'Laatste orders', op y≈2720, en ook daar is de kolom Status afgekapt (UX-93).
  - Een te hoog verkoopbedrag sloot zonder melding de hele positie.
  - Na 'Nu een cyclus draaien', 'Voorstel toepassen' en 'Nu verversen' volgt geen melding.
  - Bij Instellingen en de journaalnotitie staat de groene melding bovenaan de pagina, terwijl de browser naar het blok of de trade springt. De melding staat dan 136 tot 5.502 px boven het scherm, en de kop van het blok valt onder de navigatie.
  - Het scherm na een geslaagde opslag is gelijk aan dat na een fout (UX-48).
  - Alleen Pauzeren en Hervatten geven wel zichtbare feedback: daar verandert het pilletje.
- **Waarom het ertoe doet** 'Is mijn order uitgevoerd?' is na een klik de belangrijkste vraag. Zonder antwoord ga je zoeken, of klik je nog een keer.
- **Aanbeveling**
  - Gebruik één mechanisme voor eenmalige meldingen (via een ondertekende cookie of de sessie), en toon de melding waar je kijkt:
    - in het opgeslagen blok, direct onder de kop;
    - in de orderkaart;
    - of als vaste melding onder de navigatie (`role="status"`).
  - Voorbeelden:
    - 'Gekocht: 0,01224 ETH voor € 25,00 tegen € 2.037,09 (oefengeld) · kosten € 0,06'
    - 'Cyclus gedraaid: BTC afwachten · ETH afwachten'
    - 'Nieuws ververst: 12 nieuwe berichten'
    - 'Notitie opgeslagen', in de trade zelf
  - Haal `?saved=` na het tonen uit de URL.
  - Laat 'Handelen hervatten' terugkeren naar de pagina waar je vandaan kwam.
- **Bewijs** `main.py:1128, 1463, 1673-1675, 1907-1918, 1966, 2094`, `settings.html:11`, `journal.html:11`; `sb-order-valid-buy.png`, `s02-saved-trading-viewport.png`, `s03-saved-news-viewport.png`, `note-after-save-mobile.png`, `settings-aftersave-mobile.png` (bron: A-03, F-03, C-04, B-10, D-06 deels)

### [UX-100] Je ziet niet dat een actie bezig is, en dubbel versturen wordt niet voorkomen
**Ernst** hoog · **Inspanning** S · **Waar** Handelen (cyclus, evalueren, toepassen), Nieuws (verversen), Strategie (Testen, Voorstel maken), Overzicht (order)

- **Probleem** app.js regelt alleen de bevestigingsvragen.
  - 150 ms na een tik is 'Nu een cyclus draaien' nog gewoon klikbaar. Drie snelle tikken gaven 3 verzoeken en 3 cycli.
  - 'Nu verversen' duurde 15,6 tot 17,2 s, zonder enige feedback.
  - Twee tikken op 'Order plaatsen' gaven 2 bevestigingsvragen en 2 verzoeken. De tweede order eindigt in een foutmelding, terwijl de eerste wel lukte.
  - Na 'Testen' kom je bovenaan de pagina uit, terwijl de resultaten 1.000 px lager staan (op mobiel 3.392 px).
- **Waarom het ertoe doet** Dat levert extra cycli en extra Claude-kosten op, en verwarrende foutmeldingen na een actie die gewoon lukte.
- **Aanbeveling**
  - Voeg in app.js een bezig-toestand toe, ná de bevestigingsvraag en alleen als het versturen niet is afgebroken (`!ev.defaultPrevented`):
    - markeer het formulier als bezig (`aria-busy="true"`);
    - toon een tekst uit `data-busy` op de knop ('Cyclus loopt…', 'Nieuws ophalen… (kan ~20 s duren)');
    - blokkeer een tweede verzending.
  - Schakel de knop niet direct uit. Dan verdwijnt `name=action` uit het verzoek en geeft de server een 422. Schakel hem uit na een vertraging van 0 ms (`setTimeout(…, 0)`).
  - Zet de knop terug als je met 'terug' op de pagina komt (`pageshow`).
  - Laat formulieren naar hun resultaat springen (`/strategy#backtest`, `/news#berichten`).
  - Geef orders een eenmalig kenmerk, zodat de server een dubbele order herkent.
- **Bewijs** `app.js:1-58`, `trading.html:37, 243, 267`, `news.html:9`, `strategy.html:55, 140`; `dbl.js`, `order2.js`, curl POST /news/refresh 15,63 s (bron: F-04, B-08, A-08 deels)

### [UX-101] Fouten eindigen op kale, technische pagina's
**Ernst** middel · **Inspanning** S · **Waar** Handelen > Nu een cyclus draaien tijdens een storing; onbekende adressen; inlogpagina's met login uit; fouten in formulieren

- **Probleem**
  - Is Bitvavo traag of onbereikbaar, dan duurt 'Nu een cyclus draaien' tot 10 s en eindigt in een 500-fout met alleen de tekst 'Internal Server Error'. De API-versie van dezelfde actie vangt de fout wel netjes af.
  - Een onbekende pagina geeft `{"detail":"Not Found"}`, en op mobiel staat die in een te breed scherm (980 px).
  - Met de login uit geven /login, /setup en /account een 500-fout.
  - De uitloglink intypen (GET /logout) en sommige formulierfouten geven ruwe JSON.
  - Er is geen centrale foutafhandeling.
- **Waarom het ertoe doet** Juist tijdens een storing, als je wilt ingrijpen, kom je op een pagina zonder uitleg en zonder weg terug.
- **Aanbeveling**
  - Vang Bitvavo-fouten af in `trading_action`: 'Cyclus niet gedraaid: Bitvavo reageert niet (time-out na 10 s). De automatische cyclus probeert het later opnieuw.'
  - Voeg centrale foutafhandeling toe (voor HTTPException en 500) die bij een gewone pagina-aanvraag een foutpagina in de normale opmaak toont (`error.html` op basis van base.html), met een knop 'Naar overzicht'. Paden onder /api/ houden JSON.
  - Stuur de inlogpagina's met de login uit door naar het overzicht.
  - Toon bij GET /logout een kleine pagina met een uitlogknop.
- **Bewijs** `main.py:1213-1220, 1445-1447, 1666-1669, 1936-1945`, `engine.py:515-518`, `client.py:112`; `down-cycle-500.png`, `notfound--mobile-light.png`, `login-authoff--desktop-light.png`, `run-8061.log` (bron: A-08, F-05, C-20)

### [UX-102] Overzicht wacht op Bitvavo, en bij een storing verdwijnt ook het oefensaldo
**Ernst** middel · **Inspanning** M · **Waar** Overzicht (GET /)

- **Probleem**
  - Alleen het Overzicht haalt op de server live gegevens op. De eerste byte komt na 0,2–0,6 s, tegen minder dan 25 ms voor andere pagina's.
  - Antwoordt Bitvavo niet, dan komt de eerste byte pas na ongeveer 10 s, en daarna wachten de grafieken nog eens 10 s.
  - Koersen en saldo worden samen opgehaald. Mislukt het ophalen van de koersen, dan staat er in oefenmodus '–' en 'Het oefensaldo kon niet worden geladen', terwijl dat saldo gewoon lokaal is opgeslagen.
- **Waarom het ertoe doet** Dit is de pagina die je op je telefoon opent om te zien hoe het ervoor staat.
- **Aanbeveling**
  - Haal het saldo los van de koersen op.
  - Bewaar de koersen 10–20 s, gebruik een korte wachttijd (2 s), en val terug op de laatst bekende koers ('koers van 11:42').
  - Vraag alleen de markten op die je nodig hebt, en haal koersen en saldo tegelijk op, of laad de koersen achteraf via een API.
- **Bewijs** `main.py:2018-2024`, `client.py:112, 273-276`, `broker.py:126-127`; `dashboard-bitvavo-down-final.png` (bron: F-06)

### [UX-103] Statische bestanden worden niet lang bewaard en niet gecomprimeerd
**Ernst** laag · **Inspanning** S · **Waar** `/static/*`

- **Probleem** Bestanden met een versienummer in de URL (`?v=13`) krijgen geen instructie om lang bewaard te worden (`Cache-Control`). Ze worden ook niet gecomprimeerd: de grafiekbibliotheek is 198 KB, en gecomprimeerd zou dat 62 KB zijn.
- **Waarom het ertoe doet** Het kost onnodig dataverkeer op mobiel.
- **Aanbeveling**
  - Geef bestanden met een versienummer `Cache-Control: public, max-age=31536000, immutable`.
  - Comprimeer /static, in de app of in Traefik.
  - Comprimeer geen pagina's met geheimen (MFA-setup, herstelcodes), vanwege het BREACH-risico.
- **Bewijs** `main.py:584`, `web.py:22`; curl -D (bron: F-18)

---

## 11. Microcopy

### [UX-104] De uitleg van beslissingen is technisch, herhaalt zichzelf, en de tekens ✓ ✗ ~ hebben geen tekstalternatief
**Ernst** middel · **Inspanning** M · **Waar** Handelen > Laatste cyclus per markt en Alle cyclussen; Journaal; Tradingplan

- **Probleem**
  - De checklist toont interne namen ('rsi14 lager dan 35', 'dd7d minstens 5 (zacht filter)'), terwijl de labels op Overzicht 'RSI 14' en 'EMA 200' zeggen.
  - ✓, ✗ en ~ hebben op Handelen geen legenda en geen tekstalternatief. Schermlezers als NVDA en JAWS slaan '~' standaard over.
  - Een zacht filter dat niet gehaald is, staat in de lijst als '~' maar in de toelichting als '✗'. Op Tradingplan betekent '~' gewoon 'zacht filter'.
  - De toelichting (65–68 woorden) herhaalt alle voorwaarden, en schrijft de nieuwsscore als '+0.55'.
  - Vakjargon (trailing stop, drawdown, PlanB-stijl) staat er zonder uitleg.
- **Waarom het ertoe doet** De transparantie is een van de sterkste punten van Crypie. Het antwoord op 'waarom kocht hij niet?' moet je in één oogopslag kunnen lezen.
- **Aanbeveling**
  - Vertaal de interne namen naar leesbare tekst: 'RSI 14 onder 35', '7-daagse daling ≥ 5%', 'koers boven EMA 200'.
  - Verberg het teken voor schermlezers en voeg een verborgen tekst toe: 'Voldaan', 'Niet voldaan' of 'Zacht filter niet gehaald'.
  - Zet één zichtbare legenda bij de eerste checklist: '✓ gehaald · ✗ niet gehaald · ~ zacht filter (weegt mee, blokkeert niet)'.
  - Vat de toelichting samen in één zin: 'Geen koopsignaal: RSI 41 moet onder 35'.
  - Leg vakjargon uit met een afkorting-uitleg (`<abbr>`) of een begrippenlijst.
- **Bewijs** `_macros.html:47-57`, `strategy/describe.py:50-58`, `trading.html:144, 153, 198, 205`, `plan.html:73, 77`; `last-cycle.png`, `cycles-table-1440.png`, `axtree.js` (bron: A-20, G-11, D-17)

### [UX-105] Dezelfde regels en drempels worden per pagina anders beschreven
**Ernst** middel · **Inspanning** S · **Waar** Handelen, Tradingplan, Instellingen, Nieuws

- **Probleem**
  - De dagverlieslimiet wordt op drie manieren beschreven:
    - Plan: 'bij 5% gerealiseerd verlies koopt de bot niets meer'.
    - Handelen: 'pauze bij 5% verlies'. Dat is onjuist, want er wordt geen pauze gezet.
    - Instellingen: 'gerealiseerd plus ongerealiseerd'. Dat is de juiste.
  - Impact heeft drie benamingen voor twee drempels:
    - De kaart van een coin zegt 'waarvan 1 met grote impact', maar telt alles vanaf impact 2.
    - De lijst noemt hetzelfde bericht 'impact middel'.
    - Het plan spreekt van 'hoge impact', en de schokpauze gebruikt juist impact 3.
- **Waarom het ertoe doet** Je kunt niet voorspellen wanneer de bot stopt met kopen of op nieuws reageert.
- **Aanbeveling**
  - Bouw de teksten over regels op één plek op (een macro of Python-functie) en gebruik die op Handelen, Plan en Instellingen. Bijvoorbeeld: 'geen aankopen meer vandaag bij 5% verlies (incl. open posities)'.
  - Leg één schaal vast (klein, middel, groot) en schrijf 'middel of groot' waar de drempel vanaf 2 ligt.
- **Bewijs** `plan.html:56, 82, 105`, `trading.html:61`, `settings_store.py:302`, `risk.py:163`, `news.html:42, 102`, `news/service.py:614`; `news--desktop-light.png` (bron: G-09, B-15, A-06 deels)

### [UX-106] 'Trade', 'order' en 'positie' lopen door elkaar, en 'Winstkans' is misleidend
**Ernst** middel · **Inspanning** S · **Waar** Limieten, journaal, resultaten

- **Probleem**
  - 'Trades per dag' telt eigenlijk orders, waarbij kopen en verkopen apart tellen. Handelen zegt daardoor 'vandaag 3', terwijl het journaal '2 in totaal' trades noemt.
  - Er staat '1 afgeronde trades'.
  - 'Winstkans' is een historisch percentage. De backtest noemt hetzelfde 'winstgevend'.
  - 'Cyclussen' en 'cycli' staan door elkaar.
  - Kosten heten 'per kant', 'per order' en 'fee'.
- **Waarom het ertoe doet** Wie 6 trades per dag instelt, denkt aan 6 aankopen, maar krijgt er in de praktijk 3.
- **Aanbeveling**
  - Leg de begrippen vast: een order is één koop of verkoop, een trade is een aankoop plus de bijbehorende verkoop.
  - Noem de limiet 'Maximum aantal orders per dag' en toon '3 van 6 orders vandaag'.
  - Vervang 'Winstkans' door 'Winstgevende trades (%)'.
  - Gebruik overal 'cycli' en 'kosten per order (0,25%)'.
  - Gebruik enkelvoud bij 1: '1 afgeronde trade'.
- **Bewijs** `risk.py:47-62`, `trading.html:48, 51, 62, 188`, `journal.html:14, 17, 28`, `dashboard.html:189`, `strategy.html:95-96`, `plan.html:59` (bron: G-10, B-25 deels)

### [UX-107] Te veel uitlegtekst; op Nieuws duwt die de berichten ver omlaag
**Ernst** middel · **Inspanning** S · **Waar** Nieuws, Instellingen, Journaal, Tradingplan, Overzicht

- **Probleem**
  - Op Nieuws staat boven de lijst een alinea van 98 woorden (13 à 14 regels op mobiel). De kerncijfers ('Van de 6 nieuwsfeiten tellen er 3 mee') staan pas aan het eind, en het eerste bericht begint op 1.663 px.
  - Instellingen bestaat voor ongeveer 78% uit helptekst.
  - De ondertitel van het journaal telt 47 woorden.
  - De uitleg bij het wachtwoordveld somt tien soorten instellingen op.
- **Waarom het ertoe doet** Belangrijke waarschuwingen verdwijnen tussen de uitleg. Je slaat de tekst over, ook de tekst die er wel toe doet.
- **Aanbeveling**
  - Zet de kerncijfers in de filterknoppen ('Telt mee (3)') en de uitleg in een uitklapblok 'Hoe telt nieuws mee?'. Maak de kaarten per coin op mobiel compacter.
  - Houd ondertitels op één zin, en zet uitleg over de werking in een uitklapblok 'Hoe werkt dit?'.
  - Haal bij het inkorten geen veiligheidsinformatie weg, zoals wat doorloopt tijdens een pauze of wat de limieten precies tellen. Zet die juist vooraan.
- **Bewijs** `news.html:69`, `journal.html:7`, `settings.html:96`, `trading.html:189`; `news-mobile-berichten.png`, `words.js` (bron: B-19, G-20)

---

## Roadmap

### Nu (snelle winst, < 1 dag totaal)
Allemaal kleine aanpassingen in code, templates of CSS, met direct effect op veiligheid, toegankelijkheid of kapotte functies.

UX-01 (alleen het filter op modus), UX-04, UX-13 (actieve link in beeld en vervagende rand), UX-14, UX-20, UX-24, UX-34, UX-36, UX-38, UX-41, UX-48, UX-50, UX-69, UX-70, UX-94, UX-100

### Volgende sprint
De rest van de kritieke en hoge punten, plus de middelgrote punten die daar direct op aansluiten. Bouw hier eenmalige meldingen, Post/Redirect/Get en `bot_health()` één keer, zodat UX-16, UX-19 en UX-99 dezelfde basis gebruiken (en UX-25 later ook).

UX-02, UX-03, UX-05, UX-06, UX-07, UX-08, UX-09, UX-10, UX-11, UX-16, UX-19, UX-21, UX-22, UX-23, UX-26, UX-28, UX-29, UX-30, UX-31, UX-33, UX-37, UX-39, UX-42, UX-49, UX-51, UX-52, UX-54, UX-60, UX-62, UX-64, UX-71, UX-72, UX-73, UX-74, UX-77, UX-78, UX-93, UX-95, UX-99, UX-101, UX-104, UX-105, UX-106

### Later / groter
Herindeling van pagina's en instellingen, het ontwerpsysteem, en afwerking.

UX-12, UX-15, UX-17, UX-18, UX-25, UX-27, UX-32, UX-35, UX-40, UX-43, UX-44, UX-45, UX-46, UX-47, UX-53, UX-55, UX-56, UX-57, UX-58, UX-59, UX-61, UX-63, UX-65, UX-66, UX-67, UX-68, UX-75, UX-76, UX-79, UX-80, UX-81, UX-82, UX-83, UX-84, UX-85, UX-86, UX-87, UX-88, UX-89, UX-90, UX-91, UX-92, UX-96, UX-97, UX-98, UX-102, UX-103, UX-107


## Opvolging

Stand na de uitvoering en twee rondes na de controles (november 2026, branch
`claude/happy-cannon-b2obag`): wat er met elke bevinding is gebeurd. *Opgelost* betekent dat het
probleem weg is, ook als de oplossing afwijkt van de aanbeveling (dat staat er dan bij). *Deels*
betekent dat een deel bewust of noodgedwongen bleef liggen, met de reden. De controles: de tests
(`pytest`), axe-core op alle pagina's (licht en donker, 1440 en 390 px, ook met alle blokken van
Instellingen open: 0 bevindingen), metingen in de draaiende app (paginahoogte, aandeel kleine tekst,
veldhoogtes, wachttijden met een hangende Bitvavo) en steekproeven met en zonder JavaScript.

| Id | Status | Toelichting |
|----|--------|-------------|
| UX-01 | opgelost | Elke pagina toont alleen de huidige modus. In live staan open oefenposities apart op Handelen (*Ook open in Oefenmodus*, met link vanaf het overzicht), in `/api/positions?mode=paper`, en een reset van de oefenportefeuille (met wachtwoord) sluit ze, ook in live; bij de overstap kan dat meteen. Onder *Gesloten posities* telt de verwijzing naar de andere modus alleen gesloten trades, met een link naar die trades in het journaal. |
| UX-02 | opgelost | Na een weigering houdt het formulier markt, richting en bedrag, ook een geweigerd bedrag als "1e3" of "-5"; de bevestiging noemt modus, markt, richting en bedrag. |
| UX-03 | opgelost | Bedrag- en limietvelden accepteren een decimale komma; onduidelijke getallen (1.000, 2,500) worden geweigerd, met één regel voor orders en instellingen. |
| UX-04 | opgelost | Een blokreset zet nooit de modus terug; een reset die naar live zou schakelen wordt geweigerd, ook via de API (409). |
| UX-05 | opgelost | Rode balk "LIVE · echt geld op Bitvavo" op elke pagina, eigen indigo voor Oefenmodus, de modus in tabtitel en ondertitels, en LIVE-bevestigingen bij cyclus en strategie. |
| UX-06 | opgelost | Eén status uit `bot_health()` (Actief, Actief · koopt niet, Noodstop actief, Uitgeschakeld, Storing) op Overzicht, Handelen en `/api/status`. |
| UX-07 | opgelost | Bij een koersstoring staan alle posities als onbewaakt; "Bewaking … laatst gelukt" met interval, en drie limietmeters op Handelen. |
| UX-08 | opgelost | `NOTIFY_URL` (ntfy of webhook) met de acht voorgestelde meldingen, begrensd per onderwerp en per uur, plus *Testmelding sturen*. |
| UX-09 | opgelost | Eén naam (Noodstop) en één uitlegzin uit één bron, die nu ook zegt dat een verkoop op nieuws alleen wacht; de bevestiging vooraf noemt dat en de Claude-kosten. Zonder wachtwoord, op elke pagina zichtbaar. |
| UX-10 | opgelost | Opslaan slaat de geteste versie op, met bevestiging in live of bij een zwakke backtest, een versiecontrole tegen oude tabbladen en *Vorige strategie terugzetten*. |
| UX-11 | opgelost | Het plan zegt of Claude het nu leest en wat er zonder Claude gebeurt; de melding na opslaan belooft niets wat niet gebeurt. |
| UX-12 | opgelost | Regels eerst, de omschrijving klein als toelichting. Elk verschiloverzicht waarschuwt als de regels veranderen en de omschrijving niet, ook het voorstel op Handelen waar je het toepast. |
| UX-13 | opgelost | Navigatielinks breken naar een eigen regel; op de telefoon één scrollende rij met een vervagende rand en de huidige pagina in beeld. |
| UX-14 | opgelost | `scroll-padding` volgt de gemeten hoogte van de kop; ankers en focus vallen niet meer onder de kop of de opslaanbalk. |
| UX-15 | opgelost | Overzicht vat samen, Handelen toont de details (status, voorstel, posities, limieten, cycli), Strategie linkt naar een wachtend voorstel. |
| UX-16 | opgelost | Post/Redirect/Get voor alle formulieren met eenmalige meldingen, testen en voorstellen op `/strategy?result=…`, GET op formulieradressen naar de pagina, `?saved=` uit de adresbalk. |
| UX-17 | opgelost | Kaart *Aan de slag* met vijf stappen, klaar of niet, te verbergen (ook zonder JavaScript). |
| UX-18 | opgelost | Informatie als `card-meta`, en per kaart hoogstens één link als werkwoord plus bestemming. |
| UX-19 | opgelost | Orderfouten in de orderkaart, met eigen teksten voor eigen regels, time-out, geweigerde sleutel, weigering door Bitvavo en een onbekende uitkomst. |
| UX-20 | opgelost | De helptekst klopt: grenzen per richting, telt mee voor de orders per dag, alleen verkopen tijdens de noodstop. Bij maximum 0 belooft geen pagina dat kopen terugkomt (en de reden heeft één dubbele punt); voorbij het maximum staat "7 orders vandaag (maximum 6)" in plaats van "7 van 6". |
| UX-21 | opgelost | *Alle cycli* staat standaard op Belangrijk, met een marktfilter en 25 per pagina (in SQL gepagineerd), onderaan de pagina; op de telefoon geen zijwaartse tabel. |
| UX-22 | opgelost | Controle per richting in het formulier; een verkoop toont de positie, waarschuwt bij een rest onder € 5 en biedt *Hele positie verkopen*. |
| UX-23 | opgelost | Voorstel bovenaan Handelen met verschil en bevestiging, een melding na toepassen, een blijvende Vóór/Na-tabel en *Vorige strategie terugzetten*. |
| UX-24 | opgelost | De uitgeschakelde knop zegt waarom; zonder sleutel staan ook de automatische evaluaties uit, en dat staat erbij. |
| UX-25 | opgelost | Laatste en volgende cyclus overleven een herstart, lopen live mee en worden oranje als de planner stilvalt; de pagina's vragen elke minuut `/api/status` op. |
| UX-26 | opgelost | Open posities netto (kosten van aan- én verkoop) met één formule; *Sinds start* en *Gesloten trades · 30 dagen* zeggen wat ze meetellen. |
| UX-27 | opgelost | Status en noodstop bovenaan (op de telefoon eerst), open posities direct daarna; geen inline stijlen meer. |
| UX-28 | opgelost | Een veilige markdown-subset (filter `md`: eerst escapen, geen links, afbeeldingen of HTML) voor het rapport en de toelichting van Claude. |
| UX-29 | opgelost | Vier significante cijfers in koersen, stops, checklist en grafieken; een stop-loss-exit bewaart zijn koers volledig, dus het beslislog toont "Stop-loss geraakt op € 0,000004056" (getest met SHIB). |
| UX-30 | opgelost | Anders dan aanbevolen: geen `DISPLAY_TZ`. De HTML zegt "UTC" en de browser toont elke tijd in je eigen zone met de zonenaam; de agenda blijft UTC-invoer, en dat staat erbij. |
| UX-31 | opgelost | `pnl_class` op de afgeronde waarde, geen negatieve nul, nul en '–' nooit groen; chart.js volgt dezelfde regel. |
| UX-32 | opgelost | Eén notatie (teken vóór €, harde spatie, '$ 0,00 van $ 100'), ook in chart.js; bedragen breken niet af. |
| UX-33 | opgelost | De grafiek dimt tijdens het laden, toont een begrijpelijke fout met *Opnieuw proberen* en springt terug naar wat getoond wordt. |
| UX-34 | opgelost | *Claude-adviezen tonen* staat naast zijn tekst, is zichtbaar voor schermlezers en verbergt alleen adviezen. |
| UX-35 | opgelost | De as zoomt niet verder in dan ±1% rond de start; orders per candle gebundeld, met een legenda. |
| UX-36 | opgelost | *alles* en een leeg veld betekenen geen periodefilter; geen JSON-fout meer. |
| UX-37 | opgelost | Alle berichten van de periode geteld (tot 5000, en dat staat erbij), aantallen in de filters, 30 per pagina, standaard 48 uur. |
| UX-38 | opgelost | Eén Nederlandse lijst sluitredenen, ook voor de grafiek; handmatig en trailing stop zijn filterbaar en een onbekend filter wordt gemeld. |
| UX-39 | opgelost | Handmatige trades heten "Jij, handmatig" met "Jouw reden"; filter *Geopend door* met totalen per bron. |
| UX-40 | opgelost | Actieve filters als wegklikbare labels, filters ingeklapt op de telefoon; de eerste trade staat op ~1150 px in plaats van ~2000. |
| UX-41 | opgelost | Geen sleutel is geen fout meer: één info-melding na verversen en "Nieuwsbeoordeling: uit" in de voettekst. |
| UX-42 | opgelost | "Geldt voor BTC-EUR, ETH-EUR" op Strategie en Overzicht; `market` uitgelegd als backtestmarkt. Testen op een andere markt verandert de strategie niet meer, dus de actieve strategie blijft "Gelijk aan de actieve strategie". |
| UX-43 | opgelost | Backtest met periode, oordeel tegenover kopen en vasthouden, grafiek met tekstalternatief, waarschuwing bij weinig trades en bruto/netto benoemd. |
| UX-44 | opgelost | Validatiefouten in gewoon Nederlands met het pad en de regels rond een syntaxfout, onder de editor, met focus. |
| UX-45 | opgelost | Nieuwsdetails zonder herhaling, in het Nederlands, met één impactschaal; externe links gemarkeerd. |
| UX-46 | opgelost | Zonder sleutel geen halflege Claude-kaart en een open editor; "Gelijk aan de actieve strategie" bij een test zonder wijziging. |
| UX-47 | opgelost | Bewerkbaar en automatisch gescheiden, "nog leeg" klopt, tekenteller en een fout bij het veld. |
| UX-48 | opgelost | "Niet opgeslagen: …" bovenaan het blok, fout en focus bij het wachtwoordveld, resterende pogingen, en alleen de echt bewerkte velden blijven gemarkeerd (ook zonder JavaScript: regeleinden in tekstvakken tellen niet meer als wijziging). |
| UX-49 | opgelost | Niet-opgeslagen wijzigingen gemarkeerd per veld en blok, een vaste opslaanbalk met terugdraaien en een waarschuwing bij weggaan. Sla je één blok op terwijl een ander wijzigingen heeft, dan vraagt de pagina eerst en staan die wijzigingen na het opslaan weer in hun blok, nog als niet opgeslagen, met erbij of het andere blok wel of niet is opgeslagen. Annuleer je daarna nog een vraag (bijvoorbeeld bij de reset), dan wordt niets bewaard. Op de telefoon is de balk 85 px (was 206): één statusregel en *Opslaan* plus *Terugdraaien* naast elkaar, en de lijst onderdelen blijft dan niet meer staan. |
| UX-50 | opgelost | Een apart formulier *Herstelcode gebruiken* met een gewoon toetsenbord; werkt zonder JavaScript. |
| UX-51 | opgelost | Alle live-voorwaarden tegelijk met hun oplossing in een paar woorden en de variabele ernaast, advies met links (zonder sleutel geen dubbel advies om hem te testen), een paneel met de limieten en "Typ LIVE". |
| UX-52 | opgelost | Een geweigerde sleutel wordt onthouden en met de reden getoond in Instellingen, de voettekst en het overzicht; knop *Verbinding testen*. |
| UX-53 | opgelost | Nieuwe markten worden gecontroleerd tegen Bitvavo; een opgeslagen markt die Bitvavo niet kent (ook uit `MARKETS`) krijgt een waarschuwing bij het veld, ook onder de samenvatting van het ingeklapte blok Handelen met een link naar het veld, en "geen koers" met reden bij Koersen. Een weigering heet in de grafiek een weigering, met *Markten controleren* in plaats van *Opnieuw proberen*. |
| UX-54 | opgelost | Fouten, eenheden en uitleg via `aria-describedby` aan het veld, `aria-invalid`, en een foutsamenvatting met links die de focus krijgt. |
| UX-55 | opgelost | Label "vraagt wachtwoord" per veld, uitleg per blok, en de pagina zegt vooraf of jouw wijziging het wachtwoord nodig heeft. |
| UX-56 | opgelost | Instellingen begint als overzicht: elk blok ingeklapt tot titel plus één samenvattingsregel (1.520 px op 1440, 1.860 px op 390, was 8.200 en 12.800), met een vaste lijst onderdelen links en smaller één meeschuivende rij. Wat aandacht vraagt (een onbekende markt of een onbekend model) staat ook onder een ingeklapt blok. Anders dan aanbevolen geen aparte pagina per blok: zo blijven alle links naar `#blok` en `#row-veld` werken. Zonder JavaScript staat alles open. |
| UX-57 | opgelost | Anders dan aanbevolen blijven de handelskosten om je wachtwoord vragen: de beveiligingstest en de README leggen vast dat ze gevoelig zijn (oefenorders betalen ze en de netto resultaten rekenen ermee), en die controle mag niet losser. Ze staan wel als kosten in een eigen blok Kosten ("Geen limieten"), samen met de backtestkosten, en het wachtwoordveld zegt waarom. Bescherming is nu een eigen blok met een eigen formulier en wachtwoordveld (`group=bescherming`): opslaan stuurt de modus en de markten niet mee, en meegestuurde velden van Handelen worden genegeerd. Verder Weergave apart en Oefenportefeuille met startbedrag en reset samen. |
| UX-58 | opgelost | Afhankelijke velden zijn grijs en alleen-lezen zolang hun schakelaar uit staat; de waarde blijft bewaard. De zin "Geldt alleen als … aan staat" verschijnt alleen dan. |
| UX-59 | opgelost | *Terug naar standaard* per veld; de blokreset noemt elke wijziging, laat de modus staan en staat uit als er niets is aangepast. |
| UX-60 | opgelost | Een onbekend model wordt opgeslagen met een waarschuwing bij het veld, ook tijdens het typen en in `/api/settings`. |
| UX-61 | opgelost | Tijdslimiet met tijdstip, *Later koppelen* met bevestiging, en opnieuw beginnen via dezelfde setup-link, maar alleen in het eerste uur na het aanmaken en alleen in de browser die het account aanmaakte (een ondertekend cookie, dat *Later koppelen* overleeft). Wie alleen het token uit de log en het wachtwoord heeft, kan dus ook in dat uur geen eigen authenticator koppelen; daarvoor is `reset-mfa` op de server nodig. De log toont het token niet meer zodra het account bestaat. De limiet zelf bleef 10 minuten. |
| UX-62 | opgelost | Wachttijd in minuten met tijdstip, een waarschuwing bij nog 1-2 pogingen, en de uitweg (`clear-throttle`) op /login en /account. |
| UX-63 | opgelost | /login zegt waarom je er bent en stuurt je terug, ook via de koppelstappen (het `next`-adres blijft heel); niet-opgeslagen instellingen blijven in het tabblad bewaard en zijn terug te zetten, met een tijd met zone. |
| UX-64 | opgelost | Downloaden, afdrukken en kopiëren met een terugval en een statusregel; *Verder* pas na "Ik heb de codes bewaard". De codes zelf blijven het antwoord op een POST: vernieuwen laat de browser opnieuw versturen. |
| UX-65 | opgelost | Account ingedeeld in Status, Beveiliging wijzigen, Overal uitloggen en hulp; destructieve acties rood met bevestiging; het accounticoon is gemarkeerd op /account. |
| UX-66 | opgelost | Na een login met een herstelcode staat hoeveel codes er over zijn, als waarschuwing bij 2 of minder. |
| UX-67 | opgelost | Stappen consequent genummerd, één lijst op de QR-pagina, de QR-code met tekstalternatief, focus op het wachtwoord na een fout. |
| UX-68 | opgelost | Eenheden in vaste kolommen, Nederlandse notatie bij plafonds en standaarden, agenda en feeds breed en monospace. |
| UX-69 | opgelost | Nieuwe kleurtokens voor licht en donker; axe color-contrast 0 op alle pagina's. |
| UX-70 | opgelost | Een globale focusring van 2 px, ook op schakelaars, de moduskeuze en het grafiekvinkje. |
| UX-71 | opgelost | Geen doorzichtigheid meer; gedimde rijen hebben een leesbare grijze tekst. |
| UX-72 | opgelost | Links in lopende tekst zijn onderstreept; axe link-in-text-block 0. |
| UX-73 | opgelost | Eén label per schakelaar (`role=switch`), Modus als fieldset met legend, de delen als fieldsets. |
| UX-74 | opgelost | De rand van invoervelden en uitgeschakelde schakelaars haalt 3:1. |
| UX-75 | opgelost | Regels voor `forced-colors`: native schakelaars en randen rond gekozen opties, meldingen en knoppen; de legenda van de grafiek houdt zijn kleuren. |
| UX-76 | opgelost | Het `aria-label` van de grafiek wordt na elke lading herschreven, de status is een live region, en er is een tabel met koersen per dag. |
| UX-77 | opgelost | Koppen zonder overgeslagen niveaus op elke pagina; trades en nieuwsfeiten hebben een kop; axe heading-order 0. |
| UX-78 | opgelost | Filters als `role=group` met een naam, `aria-current` en aantallen; de lege staat bij Nieuws klopt. |
| UX-79 | opgelost | Engelse titels krijgen `lang="en"`. |
| UX-80 | opgelost | *Naar de inhoud* is de eerste tabstop. |
| UX-81 | opgelost | Belangrijke uitleg staat zichtbaar in de pagina; geen `title`-tooltips meer als enige bron. |
| UX-82 | opgelost | `ui.notice` kiest alert, status of geen rol; blijvende toestanden worden niet meer als dringend aangekondigd. |
| UX-83 | opgelost | Typeschaal met tokens: uitleg 14 px in een donkerder grijs, meta (tijden, bronnen) 13 px in een lichter grijs, één stijl voor kleine kopjes, één h3-stijl, drie maten voor getallen. Tekst van 13 px of kleiner: Instellingen 20% (was 78%), Overzicht 18%, Strategie 30%, Handelen 12%. |
| UX-84 | opgelost | Vaste knoprollen (primair, grijs, tint, gevaar, stop); uitgeschakeld met een stippelrand in plaats van doorzichtigheid. |
| UX-85 | opgelost | De modus is uit het blauw, neutrale pillen zijn grijs. Oranje is geler en rood dieper: 1,39:1 tussen beide (was 1,09:1), beide minstens 4,5:1 op elk vlak; in donker 1,39:1. |
| UX-86 | opgelost | Kaarten worden niet meer uitgerekt; de strategiekaart op Overzicht is ingekort. |
| UX-87 | opgelost | Tegels en velden uitgelijnd (subgrid); rijen tegels laten er nooit één alleen staan (6, 3+3 of 2+2+2), "(+1 open)" staat in de subregel, en invoervelden en keuzelijsten zijn op elke breedte 44 px. |
| UX-88 | opgelost | Het gekozen segment is in donkere modus lichter dan de balk, en vet. |
| UX-89 | opgelost | Eén set SVG-iconen, ook voor de vinkjes in checklists, de opsommingstekens op Tradingplan en de pijltjes bij links; het logo gebruikt de merkkleur-tokens en een test houdt de tabbladiconen gelijk. |
| UX-90 | opgelost | De themaknop wisselt tussen automatisch, licht en donker; de browserbalk volgt. |
| UX-91 | opgelost | Tabtitel met LIVE en Noodstop, varianten van het tabbladicoon, een manifest en app-iconen. |
| UX-92 | opgelost | Ontwerptokens en hulpklassen; nul inline stijlen in de templates, bewaakt door een test. |
| UX-93 | opgelost | Scrollschaduw, benoemde en focusbare scrollvakken, tabellen als kaarten op de telefoon; *Laatste orders* over de volle breedte. |
| UX-94 | opgelost | Geen zijwaarts scrollen op 320-390 px, op alle pagina's. |
| UX-95 | opgelost | De marktknoppen breken af binnen de kaart; de voettekst noemt er drie plus "en N andere". |
| UX-96 | opgelost | Helptekst per apparaat, de tooltip sluit bij loslaten, en het label wordt niet meer afgekapt. |
| UX-97 | opgelost | Tikvlakken van 40 px op touch, invoer van 16 px onder 640 px, ruimte voor de notch. |
| UX-98 | opgelost | Lange woorden en URL's breken af; adressen in notities zijn klikbaar en eindigen bij een aanhalingsteken of punthaak. |
| UX-99 | opgelost | Eenmalige meldingen na elke actie (gesigneerd cookie), in het blok, de kaart of het deel van de pagina waar je landt (ook Nieuws verversen, Plan opslaan en Strategie opslaan of terugzetten). De foutpagina toont ze niet, zodat ze op de volgende pagina één keer verschijnen. |
| UX-100 | opgelost | Formulieren zijn na de eerste klik vergrendeld met een bezigtekst; een eenmalig `order_token` voorkomt een dubbele order, en geeft het formulier vrij als Crypie de order weigerde voor er iets naar Bitvavo ging. |
| UX-101 | opgelost | HTML-foutpagina's met een weg terug (JSON voor `/api` en voor clients die geen HTML vragen); een Bitvavo-fout bij een formulier wordt een melding; foutteksten noemen geen interne veldnamen. |
| UX-102 | opgelost | Koersen en saldo parallel met een grens van 3 s, de laatst bekende koersen met hun tijdstip, het oefensaldo altijd zichtbaar. De grafieken wachten hoogstens 5 s en vragen het na een storing 15 s niet opnieuw (gemeten: 0,003 s in plaats van 10 s). |
| UX-103 | opgelost | Een jaar cache voor statische bestanden met versie, gzip behalve op de inlog- en accountpagina's. |
| UX-104 | opgelost | Eerst een samenvatting, dan leesbare voorwaarden met tekst voor de tekens (gehaald, niet gehaald, zacht filter) en één legenda. Vakjargon staat in *Begrippen uitgelegd* in plaats van `<abbr>`. |
| UX-105 | opgelost | Eén bron per regel: de noodstopzin, de harde limieten (één macro voor Handelen en Plan, met de namen uit Instellingen en de echte decimalen, dus "2,5%" en niet "3%") en de impactschaal "middel of groot". |
| UX-106 | opgelost | "Maximum aantal orders per dag", "Winnende trades" (overal netto, ook in de backtest), "cycli", "per order", enkelvoud bij 1. De engine houdt intern "trades per dag" en "kill switch" (vastgelegd door beveiligingstests): elke pagina vertaalt dat, foutmeldingen van `/api/orders` niet. |
| UX-107 | opgelost | Instellingen heeft met alle blokken open 1.203 woorden (was 1.226 vóór de fixes, 1.806 na de vorige ronde); Handelen alleen 251. Elke helptekst is ingekort zonder veiligheidsinformatie weg te laten, "Opslaan geldt alleen voor dit blok" en dubbele uitleg zijn weg, en de loginbanner noemt `AUTH_ENABLED=true` als monospace hulp. Zonder JavaScript is de pagina 6.920 px op 1440 en 11.020 op 390 (was 5.880 en 9.600): hoger door de vier blokken die UX-57 vroeg en de live-checklist (UX-51), niet door meer tekst. Nieuws: "Hoe telt nieuws mee?" staat naast *Nu verversen*, kortere meldingen en kaarten, compactere coinkaarten op de telefoon en de regel "1–6 van 6" alleen bij meer pagina's. Het eerste bericht staat op 1.186 px op 390 (was 1.664) en 811 op 1440 (was 840); met de kaart Hele markt 1.308 en 853 (was 1.856 en 898). Journaal en Tradingplan bleven zoals in de vorige ronde. |
