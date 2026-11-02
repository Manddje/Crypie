/* Handelen: notice when the bot's status changes while the page is open.
 *
 * The hero carries what the page showed when it was rendered (data-level, data-label,
 * data-last-cycle, data-exits-ok). Every 60 s while the tab is visible, and when it becomes
 * visible again, this asks /api/status (light: no Bitvavo calls) and, when something changed,
 * says so in the hero with a link to reload the page. It never changes the status itself: the
 * server-rendered page stays the source of truth, and without JavaScript nothing is missing
 * except this hint.
 */
(function () {
  "use strict";
  var hero = document.querySelector("[data-status-url]");
  if (!hero || !window.fetch) return;
  var note = hero.querySelector("[data-status-note]");
  if (!note) return;
  var url = hero.getAttribute("data-status-url");
  var shown = {
    level: hero.getAttribute("data-level") || "",
    label: hero.getAttribute("data-label") || "",
    lastCycle: Date.parse(hero.getAttribute("data-last-cycle") || "") || 0,
    exitsOk: hero.getAttribute("data-exits-ok") === "true",
  };
  var busy = false;
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
      return "Er is een nieuwe cyclus gedraaid (" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ").";
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
    // Announced once, politely: the page itself did not change.
    note.setAttribute("role", "status");
    note.hidden = false;
  }

  function check() {
    if (busy || document.hidden) return;
    busy = true;
    fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) {
        var type = r.headers.get("content-type") || "";
        // Logged out (a redirect to the login page) or an error: say nothing.
        return r.ok && type.indexOf("json") >= 0 ? r.json() : null;
      })
      .then(function (body) {
        if (body && body.health) show(change(body.health));
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }

  setInterval(check, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
})();
