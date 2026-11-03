/* Overview page charts: price candles with the bot's actions, and the portfolio value.
 * Uses TradingView Lightweight Charts (vendored in /static/vendor). Colours come from the
 * design tokens in app.css, so light and dark mode stay in sync with the rest of the app.
 *
 * Notation follows the server (app/web.py): '-€ 0,52' with a no-break space, prices with 4
 * significant digits, and times in the browser's own time zone with its name ('2 nov 11:49
 * CET'), like app.js does for the rest of the page. Daily candles are UTC days.
 *
 * States: while loading, the old chart stays visible but dimmed (aria-busy); after an error
 * the chart and the buttons go back to what is shown, with a 'Opnieuw proberen' button. The
 * line under each chart is a polite live region and the chart's description. */
(function () {
  "use strict";
  var LWC = window.LightweightCharts;
  var priceCard = document.getElementById("price-chart-card");
  var portfolioCard = document.getElementById("portfolio-chart-card");
  if (!priceCard || !portfolioCard) return;

  var priceEl = document.getElementById("price-chart");
  var priceWrap = document.getElementById("price-chart-wrap");
  var priceTip = document.getElementById("price-chart-tooltip");
  var priceStatus = document.getElementById("price-chart-status");
  var priceDetails = document.getElementById("price-chart-details");
  var priceList = document.getElementById("price-chart-actions");
  var adviceToggle = document.getElementById("price-chart-advice");
  var portfolioEl = document.getElementById("portfolio-chart");
  var portfolioWrap = document.getElementById("portfolio-chart-wrap");
  var portfolioTip = document.getElementById("portfolio-chart-tooltip");
  var portfolioStatus = document.getElementById("portfolio-chart-status");
  var portfolioRange = document.getElementById("portfolio-chart-range");

  if (!LWC) {
    var missing = "De grafiek kon niet worden geladen: het grafiekbestand ontbreekt. De cijfers staan in de tabellen op deze pagina.";
    priceStatus.textContent = missing;
    portfolioStatus.textContent = missing;
    return;
  }

  // The controls, legends and chart areas only mean something with a chart: the page shows
  // them from here on.
  [priceCard, portfolioCard].forEach(function (card) {
    Array.prototype.forEach.call(card.querySelectorAll(".chart-controls, .chart-legend, .chart-wrap"), function (node) {
      node.hidden = false;
    });
  });
  if (adviceToggle && adviceToggle.closest("label")) adviceToggle.closest("label").hidden = false;
  if (priceDetails) priceDetails.hidden = false;

  var markets = JSON.parse(priceCard.getAttribute("data-markets") || "[]");
  var PERIOD_NAMES = { "1d": "1 dag", "7d": "7 dagen", "30d": "30 dagen", "90d": "90 dagen" };
  var prefs = { market: markets[0], period: "7d", showAdvice: true };
  try {
    var saved = JSON.parse(localStorage.getItem("crypie-chart") || "{}");
    if (markets.indexOf(saved.market) >= 0) prefs.market = saved.market;
    if (PERIOD_NAMES[saved.period]) prefs.period = saved.period;
    if (saved.showAdvice === false) prefs.showAdvice = false;
  } catch (e) {}

  // ---- Notation (the same as the server) ---------------------------------------------

  var NBSP = " ";
  var MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"];
  var numberFormats = {};
  function nlNumber(value, decimals) {
    if (!numberFormats[decimals]) {
      numberFormats[decimals] = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    }
    return numberFormats[decimals].format(value);
  }
  // '€ 1.234,50', '-€ 3,20', '+€ 2,50' (signed), never '-€ 0,00'.
  function eur(value, signed) {
    if (value == null || !isFinite(value)) return "–";
    var v = Math.round(value * 100) / 100;
    if (v === 0) v = 0;
    var sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
    return sign + "€" + NBSP + nlNumber(Math.abs(v), 2);
  }
  // A change as a fraction -> '+2,3%', '-0,5%', '0,0%'.
  function pct(fraction) {
    if (fraction == null || !isFinite(fraction)) return "–";
    var v = Math.round(fraction * 1000) / 10;
    if (v === 0) v = 0;
    return (v > 0 ? "+" : "") + nlNumber(v, 1) + "%";
  }
  function pnlClass(value) {
    var v = Math.round(value * 100) / 100;
    return v > 0 ? "pos" : v < 0 ? "neg" : "zero";
  }
  // Decimals for 4 significant digits, at least 2: 45.000,12 · 0,5234 · 0,000004150.
  function sigDecimals(value) {
    var a = Math.abs(value || 0);
    if (!a || !isFinite(a)) return 2;
    return Math.max(2, Math.min(12, 3 - Math.floor(Math.log10(a))));
  }
  function price(value, decimals) {
    if (value == null || !isFinite(value)) return "–";
    return "€" + NBSP + nlNumber(value, decimals == null ? sigDecimals(value) : decimals);
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function zoneName(d) {
    try {
      var parts = new Intl.DateTimeFormat("nl-NL", { timeZoneName: "short" }).formatToParts(d);
      for (var i = 0; i < parts.length; i++) if (parts[i].type === "timeZoneName") return parts[i].value;
    } catch (e) {}
    var off = -d.getTimezoneOffset();
    if (!off) return "UTC";
    return "UTC" + (off > 0 ? "+" : "-") + Math.floor(Math.abs(off) / 60) + (Math.abs(off) % 60 ? ":" + pad(Math.abs(off) % 60) : "");
  }
  // A moment in the browser's zone: '2 nov 11:49 CET'.
  function moment(ts) {
    var d = new Date(ts * 1000);
    return d.getDate() + " " + MONTHS[d.getMonth()] + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + " " + zoneName(d);
  }
  // A daily candle is a UTC day: '2 nov'.
  function utcDay(ts) {
    var d = new Date(ts * 1000);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
  }
  function barText(ts, interval) { return interval === "1d" ? utcDay(ts) : moment(ts); }
  function zoneNow() { return zoneName(new Date()); }

  // Axis labels in local time (daily candles in UTC days). TickMarkType: 0 year, 1 month,
  // 2 day of month, 3 time, 4 time with seconds.
  function tickFormatter(interval) {
    return function (time, type) {
      var d = new Date(time * 1000);
      var utc = interval === "1d";
      var year = utc ? d.getUTCFullYear() : d.getFullYear();
      var month = utc ? d.getUTCMonth() : d.getMonth();
      var day = utc ? d.getUTCDate() : d.getDate();
      if (type === 0) return String(year);
      if (type === 1) return MONTHS[month];
      if (type === 2) return String(day);
      return pad(d.getHours()) + ":" + pad(d.getMinutes());
    };
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || (fallback ? cssVar(fallback) : "");
  }
  function tokens() {
    return {
      text: cssVar("--text-2"),
      grid: cssVar("--hairline"),
      crosshair: cssVar("--text-3"),
      green: cssVar("--chart-green", "--green"),
      red: cssVar("--chart-red", "--red"),
      accent: cssVar("--chart-accent", "--accent"),
      accentTint: cssVar("--accent-tint"),
      orange: cssVar("--chart-orange", "--orange"),
      card: cssVar("--card")
    };
  }

  // Only for the tooltip HTML; everything else is built with DOM nodes and textContent.
  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // el("span", "class", "text", child, ...): an element with text content, never parsed HTML.
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    for (var i = 3; i < arguments.length; i++) {
      if (arguments[i] != null) node.appendChild(typeof arguments[i] === "string" ? document.createTextNode(arguments[i]) : arguments[i]);
    }
    return node;
  }
  function countNl(n, one, many) {
    return n + " " + (n === 1 ? one : many || one + "s");
  }
  function intervalNl(interval) {
    return { "15m": "15 minuten", "1h": "1 uur", "4h": "4 uur", "1d": "1 dag" }[interval] || interval;
  }

  // ---- Loading and errors -------------------------------------------------------------

  var EXIT_ON_TOUCH_END = LWC.TrackingModeExitMode ? LWC.TrackingModeExitMode.OnTouchEnd : 0;

  function getJson(url) {
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 20000) : null;
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        clearTimeout(timer);
        if (r.ok) return r.json();
        return r.json().catch(function () { return {}; }).then(function (body) {
          var err = new Error("HTTP " + r.status);
          err.status = r.status;
          err.detail = body && typeof body.detail === "string" ? body.detail : "";
          // 'unreachable', 'busy', 'refused' or 'key' (app/main.py ChartUnavailable).
          err.reason = body && typeof body.reason === "string" ? body.reason : "";
          throw err;
        });
      }, function (err) {
        clearTimeout(timer);
        throw err;
      });
  }
  // What went wrong, for people; the technical text goes into a <details>. "what" is what
  // this chart needs from Bitvavo.
  function friendly(err, what) {
    if (err && err.name === "AbortError") return "Bitvavo antwoordde niet op tijd.";
    if (err && err.status === 502 && (err.reason === "key" || /Bitvavo 40[13]\b/.test(err.detail || ""))) {
      return "Bitvavo weigert de API-sleutel, dus " + (what || "de koersen") + " konden niet worden opgehaald.";
    }
    if (err && err.status === 502 && err.reason === "refused") {
      // Not an outage: trying again gives the same answer (UX-53).
      return (err.detail || "Bitvavo weigerde het verzoek") + ". Bestaat de markt nog op Bitvavo? Controleer je markten in Instellingen.";
    }
    if (err && err.status === 502 && err.detail && (err.reason === "unreachable" || err.reason === "busy")) {
      return err.detail + ". " + (what ? what.charAt(0).toUpperCase() + what.slice(1) : "De koersen") + " konden niet worden opgehaald.";
    }
    if (err && err.status === 502) {
      var text = (what || "de koersen") + " konden niet worden opgehaald bij Bitvavo.";
      return text.charAt(0).toUpperCase() + text.slice(1);
    }
    if (err && err.status === 401) return "Je bent niet meer ingelogd. Vernieuw de pagina om opnieuw in te loggen.";
    if (err && err.status === 404) return "Deze markt wordt niet meer gevolgd.";
    if (err && err.status) return "De grafiek kon niet worden geladen (fout " + err.status + ").";
    return "Geen verbinding met Crypie. Controleer je internetverbinding.";
  }
  function technical(err) {
    if (!err) return "";
    if (err.name === "AbortError") return "Geen antwoord binnen 20 seconden.";
    return (err.status ? "HTTP " + err.status : err.name || "Fout") + (err.detail ? ": " + err.detail : err.message && !err.status ? ": " + err.message : "");
  }
  function setBusy(wrap, busy) {
    wrap.classList.toggle("is-loading", busy);
    wrap.setAttribute("aria-busy", busy ? "true" : "false");
  }
  function overlay(wrap, text) {
    var box = wrap.querySelector(".chart-overlay");
    if (!text) { if (box) box.remove(); return; }
    if (!box) { box = el("div", "chart-overlay"); wrap.appendChild(box); }
    box.textContent = text;
  }
  function showError(status, message, err, retry) {
    status.textContent = "";
    status.appendChild(el("span", "chart-error", message));
    status.appendChild(document.createElement("br"));
    if (err && (err.reason === "refused" || err.reason === "key")) {
      // Trying again gives the same refusal: point to where it can be fixed instead.
      var fix = el("a", "btn btn-tint btn-sm", err.reason === "key" ? "Sleutel controleren" : "Markten controleren");
      fix.href = err.reason === "key" ? "/settings#omgeving" : "/settings#row-markets";
      status.appendChild(fix);
    } else {
      var again = el("button", "btn btn-tint btn-sm", "Opnieuw proberen");
      again.type = "button";
      again.addEventListener("click", retry);
      status.appendChild(again);
    }
    var tech = technical(err);
    if (tech) {
      var box = el("details", null, null, el("summary", null, "Technische details"), el("p", "mono", tech));
      status.appendChild(box);
    }
  }

  // ---- Chart options -------------------------------------------------------------------

  function chartOptions(t, interval, priceFormatter) {
    return {
      autoSize: true,
      layout: {
        background: { type: "solid", color: "transparent" },
        textColor: t.text,
        fontFamily: getComputedStyle(document.body).fontFamily,
        fontSize: 12,
        attributionLogo: false
      },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.18, bottom: 0.08 } },
      // rightOffset leaves room for the marker label of the last candle ('Koop').
      timeScale: { borderVisible: false, timeVisible: interval !== "1d", secondsVisible: false, rightOffset: 5, tickMarkFormatter: tickFormatter(interval) },
      crosshair: {
        mode: 0,
        vertLine: { color: t.crosshair, width: 1, style: 3, labelBackgroundColor: t.crosshair },
        horzLine: { color: t.crosshair, width: 1, style: 3, labelBackgroundColor: t.crosshair }
      },
      localization: {
        locale: "nl-NL",
        timeFormatter: function (time) { return barText(time, interval); },
        priceFormatter: priceFormatter
      },
      // On a touchscreen the details go away when the finger lifts.
      trackingMode: { exitMode: EXIT_ON_TOUCH_END },
      handleScale: { axisPressedMouseMove: { time: true, price: false } },
      handleScroll: { vertTouchDrag: false }
    };
  }

  function placeTooltip(tip, wrap, point, html) {
    if (!point) { tip.hidden = true; return; }
    tip.innerHTML = html;
    tip.hidden = false;
    var w = wrap.clientWidth, h = wrap.clientHeight;
    var left = point.x + 16, top = point.y - 12;
    if (left + tip.offsetWidth > w - 8) left = Math.max(8, point.x - tip.offsetWidth - 16);
    if (top + tip.offsetHeight > h - 4) top = Math.max(4, h - tip.offsetHeight - 4);
    if (top < 4) top = 4;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  // The symbol for a kind of action: 'mixed' (several kinds in one candle) is the neutral dot.
  function markerShape(kind, time, text, t) {
    if (kind === "buy") return { time: time, position: "belowBar", shape: "arrowUp", color: t.green, text: text, size: 1.4 };
    if (kind === "sell") return { time: time, position: "aboveBar", shape: "arrowDown", color: t.red, text: text, size: 1.4 };
    if (kind === "blocked") return { time: time, position: "aboveBar", shape: "square", color: t.orange, text: text, size: 1 };
    if (kind === "advice") return { time: time, position: "aboveBar", shape: "circle", color: t.accent, text: text, size: 1 };
    return { time: time, position: "aboveBar", shape: "circle", color: t.accent, text: text, size: 1.2 };
  }

  function markerFor(action, t, withCoin) {
    return markerShape(action.kind, action.bar_time, withCoin ? action.market.split("-")[0] : action.short, t);
  }

  // What one candle's worth of actions is, for its symbol: an executed order outweighs what
  // the bot only considered, a blockade outweighs a plain advice.
  function groupKind(acts) {
    var kinds = {};
    acts.forEach(function (a) { kinds[a.kind] = true; });
    if (kinds.buy && kinds.sell) return "mixed";
    if (kinds.buy) return "buy";
    if (kinds.sell) return "sell";
    return kinds.blocked ? "blocked" : "advice";
  }

  var GROUP_WORDS = { buy: "koop", sell: "verkoop", blocked: "geblokkeerd", advice: "Claude" };

  // The label of one candle: '4× geblokkeerd' when the same kind repeats, '3 acties' when
  // they differ. An executed order names the candle on its own ('Stop-loss'), whatever the
  // bot also considered that hour. Short: labels of neighbouring candles would overlap.
  function groupText(acts) {
    var orders = acts.filter(function (a) { return a.executed; });
    if (orders.length) acts = orders;
    if (acts.length === 1) return acts[0].short;
    var kind = acts[0].kind, same = acts.every(function (a) { return a.kind === kind; });
    return same ? acts.length + "× " + GROUP_WORDS[kind] : acts.length + " acties";
  }

  function actionHtml(a, count) {
    var how = "";
    if (a.price != null) how += price(a.price);
    if (a.amount_quote != null && a.executed) how += " · " + eur(a.amount_quote);
    var label = count > 1 ? count + "× " + a.label.charAt(0).toLowerCase() + a.label.slice(1) : a.label;
    var detail = a.detail ? '<span class="detail">' + escapeHtml(a.detail) + "</span>" : "";
    return '<div class="t-action"><strong>' + escapeHtml(label) + "</strong>" + (how ? " · " + escapeHtml(how) : "") + detail + "</div>";
  }

  // The tooltip for one candle. The bot runs several cycles an hour, so the same rule can
  // block it five times in one candle: those become one line with a count ('5× kopen
  // geblokkeerd') at the price of the last one. Executed orders are always listed one by one.
  function actionsHtml(acts) {
    var groups = [], byKey = {};
    acts.forEach(function (a) {
      var key = a.executed ? a.id : a.kind + "|" + a.label + "|" + a.detail;
      var g = byKey[key];
      if (!g) { g = byKey[key] = { action: a, count: 0 }; groups.push(g); }
      g.action = a;
      g.count += 1;
    });
    return groups.map(function (g) { return actionHtml(g.action, g.count); }).join("");
  }

  // ---- Price chart ----------------------------------------------------------------------

  var priceChart = null, candleSeries = null, priceMarkers = null;
  var priceData = { market: null, period: null, candles: [], actions: [], interval: "1h", decimals: 2 };
  var priceSeq = 0;
  var showAdvice = prefs.showAdvice;

  // The toggle hides Claude's advice only; blocked orders always stay visible.
  function visibleActions() {
    return priceData.actions.filter(function (a) { return showAdvice || a.kind !== "advice"; });
  }

  function priceAxisFormatter(decimals, last) {
    // Axis labels: whole euros for expensive coins, full precision for cheap ones.
    var d = Math.abs(last) >= 1000 ? 0 : decimals;
    return function (v) { return nlNumber(v, d); };
  }

  function buildPriceChart() {
    var t = tokens();
    priceChart = LWC.createChart(priceEl, chartOptions(t, priceData.interval));
    candleSeries = priceChart.addSeries(LWC.CandlestickSeries, {
      upColor: t.green, downColor: t.red, borderVisible: false,
      wickUpColor: t.green, wickDownColor: t.red,
      priceLineVisible: false, lastValueVisible: true
    });
    priceMarkers = LWC.createSeriesMarkers(candleSeries, []);
    priceChart.subscribeCrosshairMove(function (param) {
      if (!param.time || !param.point) { priceTip.hidden = true; return; }
      var bar = param.seriesData.get(candleSeries);
      if (!bar) { priceTip.hidden = true; return; }
      var d = priceData.decimals;
      var html = '<div class="t-title">' + escapeHtml(barText(param.time, priceData.interval)) + "</div>" +
        '<div class="t-row"><span>Open</span><b>' + price(bar.open, d) + "</b></div>" +
        '<div class="t-row"><span>Hoog</span><b>' + price(bar.high, d) + "</b></div>" +
        '<div class="t-row"><span>Laag</span><b>' + price(bar.low, d) + "</b></div>" +
        '<div class="t-row"><span>Slot</span><b>' + price(bar.close, d) + "</b></div>";
      html += actionsHtml(visibleActions().filter(function (a) { return a.bar_time === param.time; }));
      placeTooltip(priceTip, priceWrap, param.point, html);
    });
  }

  function renderPrice() {
    var t = tokens();
    var last = priceData.candles.length ? priceData.candles[priceData.candles.length - 1].close : 0;
    priceChart.applyOptions(chartOptions(t, priceData.interval, priceAxisFormatter(priceData.decimals, last)));
    candleSeries.applyOptions({
      upColor: t.green, downColor: t.red, wickUpColor: t.green, wickDownColor: t.red,
      priceFormat: { type: "price", precision: priceData.decimals, minMove: Math.pow(10, -priceData.decimals) }
    });
    candleSeries.setData(priceData.candles);
    priceMarkers.setMarkers(priceMarkersFor(t));
    priceChart.timeScale().fitContent();
    renderPriceTable();
  }

  // One marker per candle. The bot runs several cycles an hour and a rule (risk-off, an order
  // still underway, the daily limit) blocks it every cycle, so one candle used to carry a
  // stack of 'Geblokkeerd' labels on top of each other. Now a candle gets one square with a
  // count, and a run of candles that are all blocked (or all advice) gets one label with the
  // total for the run ('48× geblokkeerd'), on the highest candle so the neighbouring squares
  // do not cover it; the other candles are just the symbol the legend explains. Orders are
  // always labelled. The tooltip and the table under the chart still list everything.
  function priceMarkersFor(t) {
    var index = {}, highs = [];
    priceData.candles.forEach(function (c, i) { index[c.time] = i; highs.push(c.high); });
    var groups = {}, order = [];
    visibleActions().forEach(function (a) {
      if (index[a.bar_time] === undefined) return;
      if (!groups[a.bar_time]) { groups[a.bar_time] = []; order.push(a.bar_time); }
      groups[a.bar_time].push(a);
    });
    order.sort(function (a, b) { return index[a] - index[b]; });
    var markers = order.map(function (time) {
      var acts = groups[time];
      return { time: time, i: index[time], kind: groupKind(acts), count: acts.length, text: groupText(acts) };
    });
    var start = 0;
    while (start < markers.length) {
      var end = start, kind = markers[start].kind;
      while (end + 1 < markers.length && markers[end + 1].kind === kind && markers[end + 1].i === markers[end].i + 1) end += 1;
      if (end > start && (kind === "blocked" || kind === "advice")) {
        var total = 0, top = start;
        for (var k = start; k <= end; k += 1) {
          total += markers[k].count;
          if (highs[markers[k].i] > highs[markers[top].i]) top = k;
          markers[k].text = "";
        }
        markers[top].text = total + "× " + GROUP_WORDS[kind];
      }
      start = end + 1;
    }
    return markers.map(function (m) { return markerShape(m.kind, m.time, m.text, t); });
  }

  // Like app.js does for the tables on the page: a table that scrolls sideways becomes a named
  // region that the keyboard can reach. Checked again when the <details> opens (closed, it has
  // no width) and app.js re-checks on resize.
  function scrollRegion(wrap) {
    var scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
    if (scrolls) {
      wrap.setAttribute("tabindex", "0");
      wrap.setAttribute("role", "region");
      wrap.setAttribute("aria-label", wrap.getAttribute("data-region") + " (schuift zijwaarts)");
    } else {
      wrap.removeAttribute("tabindex");
      wrap.removeAttribute("role");
      wrap.removeAttribute("aria-label");
    }
  }
  if (priceDetails) {
    priceDetails.addEventListener("toggle", function () {
      Array.prototype.forEach.call(priceList.querySelectorAll(".table-wrap"), scrollRegion);
    });
  }

  // The day a candle belongs to: a UTC day for daily candles, else the local day.
  function dayKey(ts) {
    if (priceData.interval === "1d") return utcDay(ts);
    var d = new Date(ts * 1000);
    return d.getDate() + " " + MONTHS[d.getMonth()];
  }

  // The text alternative: per day open, high, low and close, plus the actions as a list.
  function renderPriceTable() {
    priceList.textContent = "";
    var candles = priceData.candles;
    if (candles.length) {
      var days = [];
      var byKey = {};
      candles.forEach(function (c) {
        var key = dayKey(c.time);
        var day = byKey[key];
        if (!day) { day = byKey[key] = { key: key, open: c.open, high: c.high, low: c.low, close: c.close, actions: 0 }; days.push(day); }
        day.high = Math.max(day.high, c.high);
        day.low = Math.min(day.low, c.low);
        day.close = c.close;
      });
      visibleActions().forEach(function (a) {
        var day = byKey[dayKey(a.bar_time)];
        if (day) day.actions += 1;
      });
      var d = priceData.decimals;
      var table = el("table", "small");
      var head = el("tr", null, null);
      ["Dag", "Open", "Hoog", "Laag", "Slot", "Acties"].forEach(function (name, i) {
        var th = el("th", i ? "num" : null, name);
        th.scope = "col";
        head.appendChild(th);
      });
      table.appendChild(head);
      days.slice().reverse().forEach(function (day) {
        var th = el("th", null, day.key);
        th.scope = "row";
        table.appendChild(el("tr", null, null, th,
          el("td", "num", price(day.open, d)), el("td", "num", price(day.high, d)),
          el("td", "num", price(day.low, d)), el("td", "num", price(day.close, d)),
          el("td", "num", String(day.actions))));
      });
      var wrap = el("div", "table-wrap", null, table);
      wrap.setAttribute("data-region", "Koersen per dag");
      priceList.appendChild(el("h3", "mt-2", "Per dag" + (priceData.interval === "1d" ? " (UTC-dagen)" : " (" + zoneNow() + ")")));
      priceList.appendChild(wrap);
      scrollRegion(wrap);
    }
    var acts = visibleActions().slice().reverse();
    priceList.appendChild(el("h3", "mt-4", "Acties"));
    if (!acts.length) {
      priceList.appendChild(el("p", "muted small", "Nog geen acties in deze periode voor " + priceData.market + "."));
      return;
    }
    var list = el("ul", "list");
    acts.slice(0, 50).forEach(function (a) {
      var how = el("span", "how", a.price != null ? price(a.price) : "");
      if (a.amount_quote != null && a.executed) {
        how.appendChild(document.createElement("br"));
        how.appendChild(document.createTextNode(eur(a.amount_quote)));
      }
      list.appendChild(el("li", null, null,
        el("span", "when", moment(a.time)),
        el("span", "what", null, el("strong", null, a.label), a.detail ? el("span", "detail", a.detail) : null),
        how));
    });
    priceList.appendChild(list);
    if (acts.length > 50) priceList.appendChild(el("p", "muted small", "Alleen de laatste 50 van " + acts.length + " acties."));
  }

  function describePrice() {
    var c = priceData.candles;
    var executed = priceData.actions.filter(function (a) { return a.executed; }).length;
    var advice = priceData.actions.filter(function (a) { return a.kind === "advice"; }).length;
    var blocked = priceData.actions.filter(function (a) { return a.kind === "blocked"; }).length;
    var when = PERIOD_NAMES[priceData.period] || priceData.period;
    if (!c.length) {
      priceEl.setAttribute("aria-label", "Koersgrafiek " + priceData.market + ", " + when + ": geen koersen.");
      priceStatus.textContent = "Bitvavo gaf geen koersen voor " + priceData.market + " in deze periode.";
      return;
    }
    var first = c[0].open, last = c[c.length - 1].close;
    var hi = Math.max.apply(null, c.map(function (x) { return x.high; }));
    var lo = Math.min.apply(null, c.map(function (x) { return x.low; }));
    var d = priceData.decimals;
    var counts = countNl(executed, "order") + ", " + countNl(advice, "advies", "adviezen") + " en " + countNl(blocked, "blokkade", "blokkades");
    priceEl.setAttribute("aria-label", "Koersgrafiek " + priceData.market + ", " + when + ": van " + price(first, d) + " naar " +
      price(last, d) + " (" + pct(last / first - 1) + "), hoogste " + price(hi, d) + ", laagste " + price(lo, d) + ". " + counts + ".");
    priceStatus.textContent = priceData.market + ", " + when + ": van " + price(first, d) + " naar " + price(last, d) + " (" + pct(last / first - 1) + ") · " +
      counts + " · candles van " + intervalNl(priceData.interval) + " · tijden in " + (priceData.interval === "1d" ? "UTC-dagen" : zoneNow());
  }

  function loadPrice(market, period) {
    var id = ++priceSeq;
    setActive(marketGroup, "data-market", market);
    setActive(periodGroup, "data-period", period);
    setBusy(priceWrap, true);
    if (!priceChart) overlay(priceWrap, "Grafiek wordt geladen…");
    priceStatus.textContent = "Grafiek van " + market + " (" + PERIOD_NAMES[period] + ") wordt geladen…";
    getJson("/api/chart/" + encodeURIComponent(market) + "?period=" + encodeURIComponent(period))
      .then(function (data) {
        if (id !== priceSeq) return; // a newer choice came in meanwhile
        priceData.market = market;
        priceData.period = period;
        priceData.candles = data.candles;
        priceData.actions = data.actions;
        priceData.interval = data.interval;
        var last = data.candles.length ? data.candles[data.candles.length - 1].close : 0;
        priceData.decimals = sigDecimals(last);
        if (!priceChart) buildPriceChart();
        overlay(priceWrap, data.candles.length ? null : "Geen koersen in deze periode.");
        renderPrice();
        setBusy(priceWrap, false);
        describePrice();
        prefs.market = market;
        prefs.period = period;
        persist();
      })
      .catch(function (err) {
        if (id !== priceSeq) return;
        setBusy(priceWrap, false);
        var message = friendly(err);
        if (priceData.market) {
          // Back to what the chart shows, so the buttons never label old data.
          setActive(marketGroup, "data-market", priceData.market);
          setActive(periodGroup, "data-period", priceData.period);
          message += " Je ziet nog " + priceData.market + " over " + PERIOD_NAMES[priceData.period] + ".";
        } else {
          overlay(priceWrap, "Geen grafiek: " + friendly(err));
        }
        showError(priceStatus, message, err, function () { loadPrice(market, period); });
      });
  }

  // ---- Portfolio chart ------------------------------------------------------------------

  var portfolioChart = null, valueSeries = null, valueMarkers = null, startLine = null;
  var portfolioData = { period: null, points: [], actions: [], interval: "1h", startEur: null };
  var portfolioSeq = 0;

  // The value axis never zooms in further than ±1% around the middle (or the start amount),
  // so a few cents up or down do not look like a crash; the start line stays in view.
  function portfolioScale(original) {
    var res = original();
    if (!res || !res.priceRange) return res;
    var lo = res.priceRange.minValue, hi = res.priceRange.maxValue;
    if (portfolioData.startEur) {
      lo = Math.min(lo, portfolioData.startEur);
      hi = Math.max(hi, portfolioData.startEur);
    }
    var mid = (lo + hi) / 2;
    var half = Math.abs(portfolioData.startEur || mid) * 0.01;
    if (hi - lo < 2 * half) {
      lo = Math.min(lo, mid - half);
      hi = Math.max(hi, mid + half);
    }
    return { priceRange: { minValue: lo, maxValue: hi }, margins: res.margins };
  }

  // The value axis: LWC passes more than the price, so not eur() itself (its 2nd argument
  // would add a '+').
  function axisEur(value) { return eur(value); }

  function buildPortfolioChart() {
    var t = tokens();
    portfolioChart = LWC.createChart(portfolioEl, chartOptions(t, portfolioData.interval, axisEur));
    portfolioChart.applyOptions({ rightPriceScale: { scaleMargins: { top: 0.22, bottom: 0.12 } } });
    valueSeries = portfolioChart.addSeries(LWC.AreaSeries, {
      lineColor: t.accent, topColor: t.accentTint, bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerRadius: 5, crosshairMarkerBorderColor: t.card, crosshairMarkerBorderWidth: 2,
      priceFormat: { type: "custom", minMove: 0.01, formatter: function (v) { return eur(v); } },
      autoscaleInfoProvider: portfolioScale
    });
    valueMarkers = LWC.createSeriesMarkers(valueSeries, []);
    portfolioChart.subscribeCrosshairMove(function (param) {
      if (!param.time || !param.point) { portfolioTip.hidden = true; return; }
      var pt = param.seriesData.get(valueSeries);
      if (!pt) { portfolioTip.hidden = true; return; }
      var html = '<div class="t-title">' + escapeHtml(barText(param.time, portfolioData.interval)) + "</div>" +
        '<div class="t-row"><span>Waarde</span><b>' + eur(pt.value) + "</b></div>";
      if (portfolioData.startEur) {
        html += '<div class="t-row"><span>Sinds start</span><b>' + pct(pt.value / portfolioData.startEur - 1) + "</b></div>";
      }
      html += actionsHtml(portfolioData.actions.filter(function (a) { return a.bar_time === param.time; }));
      placeTooltip(portfolioTip, portfolioWrap, param.point, html);
    });
  }

  // One marker per period: several orders in the same candle become '3×' (the
  // tooltip lists them).
  function portfolioMarkers(t, times) {
    var groups = {}, order = [];
    portfolioData.actions.forEach(function (a) {
      if (!times[a.bar_time]) return;
      if (!groups[a.bar_time]) { groups[a.bar_time] = []; order.push(a.bar_time); }
      groups[a.bar_time].push(a);
    });
    return order.map(function (time) {
      var acts = groups[time];
      if (acts.length === 1) return markerFor(acts[0], t, true);
      return markerShape(groupKind(acts), time, acts.length + "×", t); // short: neighbouring markers must not overlap
    });
  }

  function renderPortfolio() {
    var t = tokens();
    portfolioChart.applyOptions(chartOptions(t, portfolioData.interval, axisEur));
    portfolioChart.applyOptions({ rightPriceScale: { scaleMargins: { top: 0.22, bottom: 0.12 } } });
    valueSeries.applyOptions({ lineColor: t.accent, topColor: t.accentTint, crosshairMarkerBorderColor: t.card });
    valueSeries.setData(portfolioData.points);
    if (startLine) { valueSeries.removePriceLine(startLine); startLine = null; }
    if (portfolioData.startEur && portfolioData.points.length) {
      // No title on the line (it covered the markers); its value is labelled on the axis.
      startLine = valueSeries.createPriceLine({
        price: portfolioData.startEur, color: t.crosshair, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: ""
      });
    }
    var times = {};
    portfolioData.points.forEach(function (p) { times[p.time] = true; });
    valueMarkers.setMarkers(portfolioMarkers(t, times));
    portfolioChart.timeScale().fitContent();
  }

  function describePortfolio() {
    var pts = portfolioData.points;
    var when = PERIOD_NAMES[portfolioData.period] || portfolioData.period;
    portfolioRange.textContent = "laatste " + when;
    if (!pts.length) {
      portfolioEl.setAttribute("aria-label", "Waarde van de portefeuille, " + when + ": nog geen gegevens.");
      portfolioStatus.textContent = "Nog geen gegevens om de waarde te berekenen.";
      return;
    }
    var first = pts[0].value, last = pts[pts.length - 1].value;
    var diff = last - first;
    var change = eur(diff, true) + ", " + pct(first ? diff / first : 0);
    var orders = countNl(portfolioData.actions.length, "order");
    portfolioEl.setAttribute("aria-label", "Waarde van de portefeuille, " + when + ": van " + eur(first) + " naar " + eur(last) + " (" + change + "). " + orders + ".");
    portfolioStatus.textContent = "";
    portfolioStatus.appendChild(document.createTextNode(when.charAt(0).toUpperCase() + when.slice(1) + ": van " + eur(first) + " naar "));
    portfolioStatus.appendChild(el("strong", null, eur(last)));
    portfolioStatus.appendChild(document.createTextNode(" ("));
    portfolioStatus.appendChild(el("span", pnlClass(diff), change));
    portfolioStatus.appendChild(document.createTextNode(") · " + orders + " in deze periode · tijden in " + (portfolioData.interval === "1d" ? "UTC-dagen" : zoneNow())));
    // A market without prices is left out instead of breaking the chart: say which and why.
    (portfolioData.skipped || []).forEach(function (s) {
      portfolioStatus.appendChild(el("span", "block neg", "Zonder " + s.market + ": " + s.reason + "."));
    });
  }

  function loadPortfolio(period) {
    var id = ++portfolioSeq;
    setBusy(portfolioWrap, true);
    if (!portfolioChart) overlay(portfolioWrap, "Grafiek wordt geladen…");
    portfolioStatus.textContent = "Portefeuillewaarde (" + PERIOD_NAMES[period] + ") wordt geladen…";
    getJson("/api/portfolio/history?period=" + encodeURIComponent(period))
      .then(function (data) {
        if (id !== portfolioSeq) return;
        portfolioData.period = period;
        portfolioData.points = data.points;
        portfolioData.actions = data.actions;
        portfolioData.interval = data.interval;
        portfolioData.startEur = data.start_eur;
        portfolioData.skipped = data.skipped || [];
        if (!portfolioChart) buildPortfolioChart();
        overlay(portfolioWrap, data.points.length ? null : "Nog geen gegevens.");
        renderPortfolio();
        setBusy(portfolioWrap, false);
        describePortfolio();
      })
      .catch(function (err) {
        if (id !== portfolioSeq) return;
        setBusy(portfolioWrap, false);
        var message = friendly(err, "de koersen of het saldo");
        if (portfolioData.period) message += " Je ziet nog de laatste " + PERIOD_NAMES[portfolioData.period] + ".";
        else overlay(portfolioWrap, "Geen grafiek: " + message);
        showError(portfolioStatus, message, err, function () { loadPortfolio(period); });
      });
  }

  // ---- Controls and theme -------------------------------------------------------------

  function persist() {
    try { localStorage.setItem("crypie-chart", JSON.stringify(prefs)); } catch (e) {}
  }
  function setActive(group, attr, value) {
    Array.prototype.forEach.call(group.querySelectorAll("button"), function (b) {
      var on = b.getAttribute(attr) === value;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  var marketGroup = priceCard.querySelector('[data-role="market"]');
  var periodGroup = priceCard.querySelector('[data-role="period"]');
  if (adviceToggle) adviceToggle.checked = showAdvice;

  marketGroup.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-market]");
    if (!b) return;
    loadPrice(b.getAttribute("data-market"), priceData.period || prefs.period);
  });
  periodGroup.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-period]");
    if (!b) return;
    var period = b.getAttribute("data-period");
    loadPrice(priceData.market || prefs.market, period);
    loadPortfolio(period);
  });
  if (adviceToggle) {
    adviceToggle.addEventListener("change", function () {
      showAdvice = adviceToggle.checked;
      prefs.showAdvice = showAdvice;
      persist();
      if (priceChart && priceData.market) { renderPrice(); describePrice(); }
    });
  }

  function rethemeSoon() {
    // Tokens change with the data-theme attribute; wait a frame so computed styles are new.
    requestAnimationFrame(function () {
      if (priceChart && priceData.market) renderPrice();
      if (portfolioChart && portfolioData.period) renderPortfolio();
    });
  }
  new MutationObserver(rethemeSoon).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", rethemeSoon);

  loadPrice(prefs.market, prefs.period);
  loadPortfolio(prefs.period);
})();
