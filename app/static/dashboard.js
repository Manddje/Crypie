/* Overview page: the manual order form and the bot's status in the hero. The page works
 * without this file (the server checks everything again); it only adds:
 *
 *   - the amount as read ('= € 25,50') under the field, with the same rules as the server:
 *     a decimal comma is fine, '1.000' is refused as unclear, and the minimum and maximum
 *     depend on the direction (buy: min_trade_eur to max_trade_eur, sell: from € 5);
 *   - for a sell: the open position, a warning when less than € 5 would remain, and
 *     'Hele positie verkopen';
 *   - a button text and a confirmation built from the form ('LIVE · echt geld: BTC-EUR
 *     kopen voor € 25,00?'). app.js asks the question from data-confirm, which is filled in
 *     here just before (this listener sits on the form, app.js on the document);
 *   - a refresh of the status block every minute while the tab is visible
 *     (/api/dashboard/status renders it with the page's own template).
 */
(function () {
  "use strict";

  var NBSP = " ";
  var nf2 = new Intl.NumberFormat("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function eur(value) {
    var v = Math.round(value * 100) / 100;
    if (v === 0) v = 0;
    return (v < 0 ? "-" : "") + "€" + NBSP + nf2.format(Math.abs(v));
  }
  function amountText(value) {
    var text = (Math.round(value * 1e8) / 1e8).toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
    var parts = text.split(".");
    return new Intl.NumberFormat("nl-NL").format(parseInt(parts[0], 10)) + (parts[1] ? "," + parts[1] : "");
  }

  // Same rules as parse_amount_eur() in app/main.py.
  function parseAmount(text) {
    var cleaned = String(text || "").replace(/[€ \s]/g, "");
    if (!cleaned) return { empty: true };
    if (cleaned.length > 32) return { error: "Het bedrag is te lang." };
    var m = /^(\d+)(?:[.,](\d+))?$/.exec(cleaned);
    if (!m) return { error: "Geen geldig bedrag. Schrijf het als 25 of 25,50." };
    var frac = m[2] || "";
    if (frac.length > 2) {
      if (frac.length === 3 && m[1].replace(/^0+/, "")) {
        return { error: "'" + cleaned + "' is onduidelijk: een punt of komma kan ook duizendtallen scheiden. Schrijf 1000 of 25,50." };
      }
      return { error: "Gebruik hoogstens twee cijfers achter de komma." };
    }
    return { value: parseFloat(m[1] + "." + (frac || "0")) };
  }

  // -- The order form ----------------------------------------------------------------------
  var form = document.getElementById("order-form");
  if (form) {
    var live = form.getAttribute("data-mode") === "live";
    var minBuy = parseFloat(form.getAttribute("data-min-buy")) || 0;
    var maxBuy = parseFloat(form.getAttribute("data-max-buy")) || Infinity;
    var minSell = parseFloat(form.getAttribute("data-min-sell")) || 0;
    var positions = {};
    try { positions = JSON.parse(form.getAttribute("data-positions") || "{}"); } catch (e) {}
    var market = form.querySelector('select[name="market"]');
    var amount = document.getElementById("order-amount");
    var check = document.getElementById("order-amount-check");
    var sellAllRow = document.getElementById("order-sell-all");
    var sellAll = form.querySelector('input[name="sell_all"]');
    var hint = sellAllRow && sellAllRow.querySelector(".dash-sell-all-hint");
    var positionLine = document.getElementById("order-position");
    var submit = document.getElementById("order-submit");
    var defaultAmount = form.getAttribute("data-default-amount") || "";
    var touched = amount.value !== "" && amount.value !== defaultAmount;
    var lastSide = null;

    if (hint) hint.hidden = true; // the row only shows while 'Verkopen' is chosen
    amount.removeAttribute("pattern"); // the checks below give a clearer message

    var side = function () {
      var chosen = form.querySelector('input[name="side"]:checked');
      return chosen ? chosen.value : "buy";
    };
    var update = function (showErrors) {
      var s = side();
      var m = market.value;
      var coin = m.split("-")[0];
      var pos = positions[m] || null;
      var whole = s === "sell" && sellAll && sellAll.checked;

      // Switching direction: a default buy amount is no sell amount, and back.
      if (s !== lastSide && lastSide !== null && !touched) {
        amount.value = s === "buy" ? defaultAmount : "";
      }
      lastSide = s;
      if (sellAllRow) sellAllRow.hidden = s !== "sell";
      amount.disabled = !!whole;
      amount.required = !whole;

      var parsed = whole ? { whole: true } : parseAmount(amount.value);
      var problem = "";
      if (parsed.error) problem = parsed.error;
      else if (parsed.empty) problem = "Vul een bedrag in.";
      else if (!whole && s === "buy" && parsed.value < minBuy) problem = "Kopen kan vanaf " + eur(minBuy) + ".";
      else if (!whole && s === "buy" && parsed.value > maxBuy) problem = "Maximaal " + eur(maxBuy) + " per aankoop (het maximum per order).";
      else if (!whole && s === "sell" && parsed.value < minSell) problem = "Verkopen kan vanaf " + eur(minSell) + ".";
      amount.setCustomValidity(problem);
      amount.setAttribute("aria-invalid", problem && showErrors ? "true" : "false");
      check.classList.toggle("is-error", !!(problem && showErrors));
      if (problem) check.textContent = showErrors ? problem : "";
      else if (whole) check.textContent = pos && pos.value_eur != null ? "≈ " + eur(pos.value_eur) + " (de hele positie)" : "de hele positie";
      else check.textContent = "= " + eur(parsed.value);

      // What happens to the position.
      var line = "", warn = false;
      if (s === "sell") {
        if (!pos) {
          line = "Er is geen open positie in " + m + " om te verkopen.";
          warn = true;
        } else {
          line = "Open positie in " + m + ": " + amountText(pos.amount) + " " + coin + (pos.value_eur != null ? " ≈ " + eur(pos.value_eur) : "") + ".";
          if (!whole && !problem && pos.value_eur != null) {
            if (parsed.value >= pos.value_eur) {
              line += " Dat is meer dan de positie waard is: de hele positie wordt verkocht.";
              warn = true;
            } else if (pos.value_eur - parsed.value < minSell) {
              line += " Er blijft dan minder dan " + eur(minSell) + " over; dan wordt de hele positie verkocht.";
              warn = true;
            }
          }
        }
      } else if (pos) {
        line = "Er staat al een open positie in " + m + (pos.value_eur != null ? " (≈ " + eur(pos.value_eur) + ")" : "") + "; kopen kan pas als die verkocht is.";
        warn = true;
      } else {
        line = "Een aankoop opent een positie in " + m + "; de stop-loss en take-profit van de strategie bewaken die.";
      }
      positionLine.textContent = line;
      positionLine.classList.toggle("is-warn", warn);

      // The button says what it does.
      var label;
      if (whole) label = "Hele positie " + coin + " verkopen";
      else if (problem) label = live ? "Echte order plaatsen" : "Order plaatsen";
      else label = coin + " " + (s === "buy" ? "kopen" : "verkopen") + " voor " + eur(parsed.value);
      if (live && !problem) label += " (echt geld)";
      if (!submit.classList.contains("is-busy")) submit.textContent = label;

      // The question app.js asks before sending.
      var who = live ? "LIVE · echt geld: " : "Oefenmodus (fictief geld): ";
      var question;
      if (whole) {
        question = who + m + ": de hele positie verkopen" + (pos ? " (" + amountText(pos.amount) + " " + coin + (pos.value_eur != null ? " ≈ " + eur(pos.value_eur) : "") + ")" : "") + "?";
      } else if (!problem) {
        question = who + m + " " + (s === "buy" ? "kopen" : "verkopen") + " voor " + eur(parsed.value) + "?";
        if (warn && s === "sell" && pos) question += " Let op: de hele positie wordt verkocht.";
      } else {
        question = who + "deze order plaatsen?";
      }
      if (live) question += " Dit is een echte order op Bitvavo.";
      form.setAttribute("data-confirm", question);
    };

    // A problem with the amount is shown once the owner typed one (or tried to send), and
    // stays visible while other fields change.
    var showing = touched;
    form.addEventListener("input", function (ev) {
      if (ev.target === amount) {
        touched = amount.value !== "" && amount.value !== defaultAmount;
        showing = amount.value !== "";
      }
      update(showing);
    });
    form.addEventListener("change", function () { update(showing); });
    // The browser blocks a submit with a wrong amount: show why next to the field.
    amount.addEventListener("invalid", function () { showing = true; update(true); });
    // Just before app.js asks: the question with the values as they are now.
    form.addEventListener("submit", function () { update(true); });
    update(showing);

    // After a refused order the form came back with ?market=…&side=…&amount=…; those values
    // are in the form now, so the address can be plain again (a reload starts afresh).
    if (window.history && history.replaceState && window.URL) {
      try {
        var url = new URL(window.location.href);
        var changed = false;
        ["market", "side", "amount", "sell_all"].forEach(function (name) {
          if (url.searchParams.has(name)) { url.searchParams.delete(name); changed = true; }
        });
        if (changed) history.replaceState(history.state, "", url.pathname + url.search + url.hash);
      } catch (e) {}
    }
  }

  // -- The status in the hero: refreshed while the page stays open --------------------------
  var box = document.getElementById("dash-status");
  var announce = document.getElementById("dash-status-announce");
  if (box && window.fetch) {
    var labelOf = function () {
      var pill = box.querySelector(".status .pill");
      return pill ? pill.textContent.trim() : "";
    };
    var lastLabel = labelOf();
    var lastPoll = Date.now();
    var poll = function () {
      lastPoll = Date.now();
      fetch("/api/dashboard/status", { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || typeof data.html !== "string") return;
          // Never pull the kill switch away from under a finger or a keyboard focus.
          if (box.contains(document.activeElement) || box.querySelector("form[data-submitting]")) return;
          box.innerHTML = data.html; // rendered and escaped by the server's template
          if (window.CrypieTime) {
            window.CrypieTime.localise(box);
            window.CrypieTime.refresh();
          }
          var label = labelOf();
          if (announce && label && label !== lastLabel) announce.textContent = "Status van de bot: " + label;
          lastLabel = label;
        })
        .catch(function () {});
    };
    setInterval(function () { if (!document.hidden) poll(); }, 60000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && Date.now() - lastPoll > 60000) poll();
    });
  }
})();
