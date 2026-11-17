/* Handelen: notice when the bot's status changes while the page is open.
 *
 * The hero carries what the page showed when it was rendered (data-level, data-label,
 * data-last-cycle, data-exits-ok). app.js asks /api/status every 60 s while the tab is visible
 * (light: no Bitvavo calls) and passes the answer on; when something changed, this
 * says so in the hero (a polite live region that is already in the page, so screen readers hear
 * it once) with a link to reload the page. It never changes the status itself: the
 * server-rendered page stays the source of truth, and without JavaScript nothing is missing
 * except this hint.
 */
(function () {
  "use strict";
  var hero = document.querySelector("[data-status-url]");
  if (!hero) return;
  var note = hero.querySelector("[data-status-note]");
  if (!note) return;
  var shown = {
    level: hero.getAttribute("data-level") || "",
    label: hero.getAttribute("data-label") || "",
    lastCycle: Date.parse(hero.getAttribute("data-last-cycle") || "") || 0,
    exitsOk: hero.getAttribute("data-exits-ok") === "true",
  };
  var told = "";

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function change(h) {
    var last = Date.parse(h.last_cycle || "") || 0;
    if (h.level !== shown.level || h.label !== shown.label) {
      return "De status is nu: " + h.label + ".";
    }
    if (Boolean(h.exits_ok) !== shown.exitsOk) {
      return h.exits_ok
        ? "De bewaking van stop-loss en take-profit werkt weer."
        : "De bewaking van stop-loss en take-profit loopt achter.";
    }
    if (last && Math.abs(last - shown.lastCycle) >= 1000) {
      var d = new Date(last);
      var zone = window.CrypieTime && window.CrypieTime.zone ? " " + window.CrypieTime.zone(d) : "";
      return "Er is een nieuwe cyclus gedraaid (" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + zone + ").";
    }
    return "";
  }

  function show(text) {
    if (!text || text === told) return;
    told = text;
    note.textContent = "";
    note.appendChild(document.createTextNode(text + " "));
    var link = document.createElement("a");
    link.href = window.location.pathname + window.location.search;
    link.textContent = "Pagina vernieuwen";
    note.appendChild(link);
    note.hidden = false;
  }

  // app.js asks /api/status every minute while the tab is visible, and when it becomes
  // visible again; it hands the answer to the page with this event.
  document.addEventListener("crypie:status", function (ev) {
    var body = ev.detail;
    if (body && body.health) show(change(body.health));
  });
})();
