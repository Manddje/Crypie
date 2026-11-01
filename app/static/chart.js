/* Overview page charts: price candles with the bot's actions, and the portfolio value.
 * Uses TradingView Lightweight Charts (vendored in /static/vendor). Colors come from the
 * design tokens in app.css so light and dark mode stay in sync with the rest of the app. */
(function () {
  "use strict";
  var LWC = window.LightweightCharts;
  var priceCard = document.getElementById("price-chart-card");
  var portfolioCard = document.getElementById("portfolio-chart-card");
  if (!LWC || !priceCard || !portfolioCard) return;

  var markets = JSON.parse(priceCard.getAttribute("data-markets") || "[]");
  var state = { market: markets[0], period: "7d", showAdvice: true };
  try {
    var saved = JSON.parse(localStorage.getItem("crypie-chart") || "{}");
    if (markets.indexOf(saved.market) >= 0) state.market = saved.market;
    if (saved.period) state.period = saved.period;
    if (saved.showAdvice === false) state.showAdvice = false;
  } catch (e) {}

  var fmtEur = new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR" });
  var fmtPct = new Intl.NumberFormat("nl-NL", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: "exceptZero" });
  var fmtDay = new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short", timeZone: "UTC" });
  var fmtMoment = new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function tokens() {
    return {
      text: cssVar("--text-2"),
      grid: cssVar("--hairline"),
      crosshair: cssVar("--text-3"),
      green: cssVar("--green"),
      red: cssVar("--red"),
      accent: cssVar("--accent"),
      accentTint: cssVar("--accent-tint"),
      orange: cssVar("--orange"),
      card: cssVar("--card")
    };
  }
  function decimalsFor(value) {
    var v = Math.abs(value || 0);
    return v >= 10 ? 2 : v >= 0.1 ? 4 : 6;
  }
  function fmtPrice(value, decimals) {
    if (value == null) return "–";
    return "€ " + new Intl.NumberFormat("nl-NL", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(value);
  }
  function fmtTime(ts, interval) {
    var d = new Date(ts * 1000);
    return interval === "1d" ? fmtDay.format(d) : fmtMoment.format(d);
  }
  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function timeFormatter(interval) {
    return function (time) { return fmtTime(time, interval) + (interval === "1d" ? "" : " UTC"); };
  }

  function chartOptions(t, interval, decimals) {
    var priceFormatter = decimals == null ? undefined : (function () {
      // Axis labels: whole euros for expensive coins, full precision for cheap ones.
      var axisDecimals = decimals <= 2 ? 0 : decimals;
      var f = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: axisDecimals, maximumFractionDigits: axisDecimals });
      return function (v) { return f.format(v); };
    })();
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
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.16, bottom: 0.08 } },
      timeScale: { borderVisible: false, timeVisible: interval !== "1d", secondsVisible: false, rightOffset: 2 },
      crosshair: {
        mode: 0,
        vertLine: { color: t.crosshair, width: 1, style: 3, labelBackgroundColor: t.crosshair },
        horzLine: { color: t.crosshair, width: 1, style: 3, labelBackgroundColor: t.crosshair }
      },
      localization: { locale: "nl-NL", timeFormatter: timeFormatter(interval), priceFormatter: priceFormatter },
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

  function markerFor(action, t, withCoin) {
    var coin = withCoin ? action.market.split("-")[0] : null;
    var text = withCoin ? coin : action.short;
    if (action.kind === "buy") return { time: action.bar_time, position: "belowBar", shape: "arrowUp", color: t.green, text: text, size: 1.4 };
    if (action.kind === "sell") return { time: action.bar_time, position: "aboveBar", shape: "arrowDown", color: t.red, text: text, size: 1.4 };
    if (action.kind === "blocked") return { time: action.bar_time, position: "aboveBar", shape: "square", color: t.orange, text: text, size: 1 };
    return { time: action.bar_time, position: "aboveBar", shape: "circle", color: t.accent, text: text, size: 1 };
  }

  function actionHtml(a, decimals) {
    var how = "";
    if (a.price != null) how += fmtPrice(a.price, decimals);
    if (a.amount_quote != null && a.executed) how += " · " + fmtEur.format(a.amount_quote);
    var detail = a.detail ? '<span class="detail">' + escapeHtml(a.detail) + "</span>" : "";
    return '<div class="t-action"><strong>' + escapeHtml(a.label) + "</strong>" + (how ? " · " + how : "") + detail + "</div>";
  }

  // ---- Price chart ----------------------------------------------------------------

  var priceEl = document.getElementById("price-chart");
  var priceTip = document.getElementById("price-chart-tooltip");
  var priceStatus = document.getElementById("price-chart-status");
  var priceList = document.getElementById("price-chart-actions");
  var adviceToggle = document.getElementById("price-chart-advice");
  var priceChart = null, candleSeries = null, priceMarkers = null;
  var priceData = { candles: [], actions: [], interval: "1h", decimals: 2 };

  function buildPriceChart() {
    var t = tokens();
    priceChart = LWC.createChart(priceEl, chartOptions(t, priceData.interval, priceData.decimals));
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
      var html = '<div class="t-title">' + fmtTime(param.time, priceData.interval) + (priceData.interval === "1d" ? "" : " UTC") + "</div>" +
        '<div class="t-row"><span>Open</span><b>' + fmtPrice(bar.open, d) + "</b></div>" +
        '<div class="t-row"><span>Hoog</span><b>' + fmtPrice(bar.high, d) + "</b></div>" +
        '<div class="t-row"><span>Laag</span><b>' + fmtPrice(bar.low, d) + "</b></div>" +
        '<div class="t-row"><span>Slot</span><b>' + fmtPrice(bar.close, d) + "</b></div>";
      visibleActions().forEach(function (a) {
        if (a.bar_time === param.time) html += actionHtml(a, d);
      });
      placeTooltip(priceTip, priceEl.parentNode, param.point, html);
    });
  }

  function visibleActions() {
    return priceData.actions.filter(function (a) { return state.showAdvice || a.executed; });
  }

  function renderPrice() {
    var t = tokens();
    priceChart.applyOptions(chartOptions(t, priceData.interval, priceData.decimals));
    candleSeries.applyOptions({
      upColor: t.green, downColor: t.red, wickUpColor: t.green, wickDownColor: t.red,
      priceFormat: { type: "price", precision: priceData.decimals, minMove: Math.pow(10, -priceData.decimals) }
    });
    candleSeries.setData(priceData.candles);
    var barTimes = {};
    priceData.candles.forEach(function (c) { barTimes[c.time] = true; });
    var markers = visibleActions()
      .filter(function (a) { return barTimes[a.bar_time]; })
      .map(function (a) { return markerFor(a, t, false); });
    priceMarkers.setMarkers(markers);
    priceChart.timeScale().fitContent();
    renderActionList();
  }

  function renderActionList() {
    var acts = visibleActions().slice().reverse();
    if (!acts.length) {
      priceList.innerHTML = '<p class="muted small">Nog geen acties in deze periode voor ' + escapeHtml(state.market) + ".</p>";
      return;
    }
    var d = priceData.decimals;
    var html = '<ul class="list">';
    acts.slice(0, 50).forEach(function (a) {
      var how = a.price != null ? fmtPrice(a.price, d) : "";
      if (a.amount_quote != null && a.executed) how += "<br>" + fmtEur.format(a.amount_quote);
      html += '<li><span class="when">' + fmtTime(a.time, "1h") + '</span><span class="what"><strong>' + escapeHtml(a.label) + "</strong>" +
        (a.detail ? '<span class="detail">' + escapeHtml(a.detail) + "</span>" : "") + '</span><span class="how">' + how + "</span></li>";
    });
    html += "</ul>";
    if (acts.length > 50) html += '<p class="muted small">Alleen de laatste 50 van ' + acts.length + " acties.</p>";
    priceList.innerHTML = html;
  }

  function loadPrice() {
    priceStatus.textContent = "Grafiek wordt geladen…";
    var market = state.market, period = state.period;
    fetch("/api/chart/" + encodeURIComponent(market) + "?period=" + encodeURIComponent(period), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (market !== state.market || period !== state.period) return; // stale response
        priceData.candles = data.candles;
        priceData.actions = data.actions;
        priceData.interval = data.interval;
        var last = data.candles.length ? data.candles[data.candles.length - 1].close : 0;
        priceData.decimals = decimalsFor(last);
        if (!priceChart) buildPriceChart();
        renderPrice();
        var executed = data.actions.filter(function (a) { return a.executed; }).length;
        var advice = data.actions.length - executed;
        if (!data.candles.length) priceStatus.textContent = "Bitvavo gaf geen koersen voor " + market + ".";
        else priceStatus.textContent = market + " · candles van " + intervalNl(data.interval) + " · " + countNl(executed, "order") + " en " + countNl(advice, "advies", "adviezen") + " in deze periode · tijden in UTC";
      })
      .catch(function (err) {
        priceStatus.textContent = "De grafiek kon niet worden geladen (" + err.message + "). Probeer het over een minuut opnieuw.";
      });
  }

  function intervalNl(interval) {
    return { "15m": "15 minuten", "1h": "1 uur", "4h": "4 uur", "1d": "1 dag" }[interval] || interval;
  }
  function countNl(n, one, many) {
    many = many || one + "s";
    return n + " " + (n === 1 ? one : many);
  }

  // ---- Portfolio chart ------------------------------------------------------------

  var portfolioEl = document.getElementById("portfolio-chart");
  var portfolioTip = document.getElementById("portfolio-chart-tooltip");
  var portfolioStatus = document.getElementById("portfolio-chart-status");
  var portfolioRange = document.getElementById("portfolio-chart-range");
  var portfolioChart = null, valueSeries = null, valueMarkers = null, startLine = null;
  var portfolioData = { points: [], actions: [], interval: "1h", startEur: null };

  function buildPortfolioChart() {
    var t = tokens();
    portfolioChart = LWC.createChart(portfolioEl, chartOptions(t, portfolioData.interval));
    valueSeries = portfolioChart.addSeries(LWC.AreaSeries, {
      lineColor: t.accent, topColor: t.accentTint, bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerRadius: 5, crosshairMarkerBorderColor: t.card, crosshairMarkerBorderWidth: 2,
      priceFormat: { type: "custom", minMove: 0.01, formatter: function (v) { return fmtEur.format(v); } }
    });
    valueMarkers = LWC.createSeriesMarkers(valueSeries, []);
    portfolioChart.subscribeCrosshairMove(function (param) {
      if (!param.time || !param.point) { portfolioTip.hidden = true; return; }
      var pt = param.seriesData.get(valueSeries);
      if (!pt) { portfolioTip.hidden = true; return; }
      var html = '<div class="t-title">' + fmtTime(param.time, portfolioData.interval) + (portfolioData.interval === "1d" ? "" : " UTC") + "</div>" +
        '<div class="t-row"><span>Waarde</span><b>' + fmtEur.format(pt.value) + "</b></div>";
      if (portfolioData.startEur) {
        html += '<div class="t-row"><span>Sinds start</span><b>' + fmtPct.format(pt.value / portfolioData.startEur - 1) + "</b></div>";
      }
      portfolioData.actions.forEach(function (a) {
        if (a.bar_time === param.time) html += actionHtml(a, decimalsFor(a.price));
      });
      placeTooltip(portfolioTip, portfolioEl.parentNode, param.point, html);
    });
  }

  function renderPortfolio() {
    var t = tokens();
    portfolioChart.applyOptions(chartOptions(t, portfolioData.interval));
    valueSeries.applyOptions({ lineColor: t.accent, topColor: t.accentTint, crosshairMarkerBorderColor: t.card });
    valueSeries.setData(portfolioData.points);
    if (startLine) { valueSeries.removePriceLine(startLine); startLine = null; }
    if (portfolioData.startEur && portfolioData.points.length) {
      startLine = valueSeries.createPriceLine({
        price: portfolioData.startEur, color: t.crosshair, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "start"
      });
    }
    var times = {};
    portfolioData.points.forEach(function (p) { times[p.time] = true; });
    valueMarkers.setMarkers(portfolioData.actions
      .filter(function (a) { return times[a.bar_time]; })
      .map(function (a) { return markerFor(a, t, true); }));
    portfolioChart.timeScale().fitContent();
  }

  function loadPortfolio() {
    portfolioStatus.textContent = "Grafiek wordt geladen…";
    var period = state.period;
    fetch("/api/portfolio/history?period=" + encodeURIComponent(period), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (period !== state.period) return;
        portfolioData.points = data.points;
        portfolioData.actions = data.actions;
        portfolioData.interval = data.interval;
        portfolioData.startEur = data.start_eur;
        if (!portfolioChart) buildPortfolioChart();
        renderPortfolio();
        portfolioRange.textContent = "laatste " + period.slice(0, -1) + (period === "1d" ? " dag" : " dagen");
        if (!data.points.length) { portfolioStatus.textContent = "Nog geen gegevens om de waarde te berekenen."; return; }
        var first = data.points[0].value, last = data.points[data.points.length - 1].value;
        var diff = last - first;
        portfolioStatus.innerHTML = "Van " + fmtEur.format(first) + " naar <strong>" + fmtEur.format(last) + "</strong> (" +
          '<span class="' + (diff >= 0 ? "pos" : "neg") + '">' + (diff >= 0 ? "+" : "") + fmtEur.format(diff) + ", " + fmtPct.format(first ? diff / first : 0) + "</span>) · " +
          countNl(data.actions.length, "order") + " in deze periode";
      })
      .catch(function (err) {
        portfolioStatus.textContent = "De grafiek kon niet worden geladen (" + err.message + ").";
      });
  }

  // ---- Controls and theme -------------------------------------------------------------

  function persist() {
    try { localStorage.setItem("crypie-chart", JSON.stringify(state)); } catch (e) {}
  }
  function setActive(group, attr, value) {
    Array.prototype.forEach.call(group.querySelectorAll("button"), function (b) {
      b.classList.toggle("active", b.getAttribute(attr) === value);
      b.setAttribute("aria-pressed", b.getAttribute(attr) === value ? "true" : "false");
    });
  }
  var marketGroup = priceCard.querySelector('[data-role="market"]');
  var periodGroup = priceCard.querySelector('[data-role="period"]');
  setActive(marketGroup, "data-market", state.market);
  setActive(periodGroup, "data-period", state.period);
  adviceToggle.checked = state.showAdvice;

  marketGroup.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-market]");
    if (!b) return;
    state.market = b.getAttribute("data-market");
    setActive(marketGroup, "data-market", state.market);
    persist();
    loadPrice();
  });
  periodGroup.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-period]");
    if (!b) return;
    state.period = b.getAttribute("data-period");
    setActive(periodGroup, "data-period", state.period);
    persist();
    loadPrice();
    loadPortfolio();
  });
  adviceToggle.addEventListener("change", function () {
    state.showAdvice = adviceToggle.checked;
    persist();
    if (priceChart) renderPrice();
  });

  function rethemeSoon() {
    // Tokens change with the data-theme attribute; wait a frame so computed styles are new.
    requestAnimationFrame(function () {
      if (priceChart) renderPrice();
      if (portfolioChart) renderPortfolio();
    });
  }
  new MutationObserver(rethemeSoon).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", rethemeSoon);

  loadPrice();
  loadPortfolio();
})();
