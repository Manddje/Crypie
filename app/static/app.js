/* Behaviour shared by every page. No inline scripts or on*= handlers in the templates (the
 * Content-Security-Policy forbids them); markup opts in through data attributes instead.
 * Everything here only improves a page that already works without JavaScript.
 *
 *   <form data-confirm="Vraag?">            asks before the form is submitted
 *   <button data-confirm="Vraag?">          asks before this submit button submits
 *   <form data-confirm-live="Vraag?" data-current-mode="paper">
 *                                           asks only when the form switches to live mode
 *   <form data-confirm-if-live="Vraag?">    asks only while the app trades live
 *   <button data-busy="Cyclus loopt…">      text on the button while the form is being sent;
 *                                           every form is locked against a second submit
 *                                           (opt out with <form data-no-busy>)
 *   <form data-track-changes>               fields edited but not saved get .is-changed
 *   <time datetime="…Z" data-local[="year"]>  shown in the browser's time zone, with its name
 *   <time datetime="…Z" data-ts>            relative time ('4 min geleden'), kept up to date
 *   .table-wrap[data-region="Naam"]         an overflowing table becomes a named, focusable
 *                                           region; <table class="table-stack"> gets labels
 *   <details open data-collapse-mobile>     closed on load on a phone, unless the address
 *                                           points into it or it holds an error
 *   <textarea maxlength="…" data-char-count="id">
 *                                           keeps '120 van 2.000 tekens' in #id up to date
 *
 * While the tab is visible it asks /api/status every minute (light: no Bitvavo calls). When
 * the kill switch was switched elsewhere, the navigation pill, the tab title and the tab icon
 * follow; pages listen to the 'crypie:status' event on document for the full answer.
 *
 * On load it also: removes one-off query parameters (?saved=, ?flash=) from the address,
 * brings the first field with an error into view and focuses it, scrolls the current page into
 * view in the navigation, and measures the sticky header for scroll-padding.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var each = function (selector, fn, scope) {
    Array.prototype.forEach.call((scope || document).querySelectorAll(selector), fn);
  };

  // -- Theme: automatic, light, dark ---------------------------------------------------------
  var THEME_NAMES = { auto: "automatisch", light: "licht", dark: "donker" };
  var THEME_NEXT = { auto: "light", light: "dark", dark: "auto" };
  var themeBtn = document.getElementById("theme-toggle");
  var theme = window.CrypieTheme;
  function labelTheme(choice) {
    if (!themeBtn) return;
    var now = THEME_NAMES[choice];
    var follow = choice === "auto" ? " (volgt je systeem)" : "";
    themeBtn.setAttribute("aria-label", "Thema: " + now + follow + ". Kies " + THEME_NAMES[THEME_NEXT[choice]] + ".");
    themeBtn.setAttribute("title", "Thema: " + now + follow);
  }
  if (themeBtn && theme) {
    themeBtn.hidden = false;
    labelTheme(theme.saved());
    themeBtn.addEventListener("click", function () {
      labelTheme(theme.save(THEME_NEXT[theme.saved()]));
    });
  }

  // -- Sticky header: its height drives scroll-padding (anchors, focus, error fields) --------
  var header = document.querySelector("header.nav");
  var navInner = header && header.querySelector(".nav-inner");
  var brand = header && header.querySelector(".brand");
  var navRight = header && header.querySelector(".nav-right");
  function measureHeader() {
    if (!header) return;
    // Links, mode and buttons do not fit on one row: give the links a row of their own
    // instead of pushing the buttons onto a stray second line.
    if (navInner && brand && navRight) {
      navInner.classList.remove("is-stacked");
      if (navRight.getBoundingClientRect().top > brand.getBoundingClientRect().bottom - 4) {
        navInner.classList.add("is-stacked");
      }
    }
    var fixed = window.getComputedStyle(header).position === "sticky";
    root.style.setProperty("--header-h", (fixed ? header.offsetHeight : 0) + "px");
    root.style.setProperty("--banner-h", "0px"); // the measured header includes the banner
  }

  // -- Navigation: show the current page and fade the side with more links -----------------
  var navLinks = document.querySelector(".nav-links");
  function navScrollState() {
    if (!navLinks) return;
    var max = navLinks.scrollWidth - navLinks.clientWidth;
    var state = "none";
    if (max > 2) {
      var x = navLinks.scrollLeft;
      state = x <= 2 ? "start" : x >= max - 2 ? "end" : "both";
    }
    navLinks.setAttribute("data-scroll", state);
  }
  if (navLinks) {
    var current = navLinks.querySelector('[aria-current="page"]');
    if (current && navLinks.scrollWidth > navLinks.clientWidth) {
      var box = navLinks.getBoundingClientRect();
      var link = current.getBoundingClientRect();
      navLinks.scrollLeft += link.left - box.left - (box.width - link.width) / 2;
    }
    navLinks.addEventListener("scroll", navScrollState, { passive: true });
  }

  // -- Wide tables: a named, focusable region while they overflow ----------------------------
  function regionName(wrap) {
    var name = wrap.getAttribute("data-region");
    if (name) return name;
    var box = wrap.closest("section, article, .card");
    var heading = box && box.querySelector("h2, h3, h4, .card-title");
    return heading ? heading.textContent.trim() : "Tabel";
  }
  function tableRegions() {
    each(".table-wrap", function (wrap) {
      var scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
      if (scrolls) {
        wrap.setAttribute("tabindex", "0");
        wrap.setAttribute("role", "region");
        wrap.setAttribute("aria-label", regionName(wrap) + " (schuift zijwaarts)");
      } else if (wrap.getAttribute("role") === "region") {
        wrap.removeAttribute("tabindex");
        wrap.removeAttribute("role");
        wrap.removeAttribute("aria-label");
      }
    });
  }
  // <table class="table-stack">: copy the column names into each cell for the narrow layout.
  each("table.table-stack", function (table) {
    var names = [];
    each("tr:first-child th, thead th", function (th, i) { names[i] = th.textContent.trim(); }, table);
    each("tr", function (tr) {
      Array.prototype.forEach.call(tr.children, function (cell, i) {
        if (cell.tagName === "TD" && names[i] && !cell.hasAttribute("data-label")) cell.setAttribute("data-label", names[i]);
      });
    }, table);
  });

  var resizeTimer = null;
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      measureHeader();
      navScrollState();
      tableRegions();
    }, 120);
  }
  window.addEventListener("resize", onResize);
  measureHeader();
  navScrollState();
  tableRegions();

  // -- Times: local time zone and live relative times ----------------------------------------
  var MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"];
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function zoneName(d) {
    try {
      var parts = new Intl.DateTimeFormat("nl-NL", { timeZoneName: "short" }).formatToParts(d);
      for (var i = 0; i < parts.length; i++) if (parts[i].type === "timeZoneName") return parts[i].value;
    } catch (e) {}
    var off = -d.getTimezoneOffset();
    if (!off) return "UTC";
    var abs = Math.abs(off);
    return "UTC" + (off > 0 ? "+" : "-") + Math.floor(abs / 60) + (abs % 60 ? ":" + pad(abs % 60) : "");
  }
  function parseMoment(el) {
    var raw = el.getAttribute("data-ts") || el.getAttribute("datetime") || "";
    if (/^\d+$/.test(raw)) return new Date(parseInt(raw, 10) * 1000);
    var d = new Date(raw);
    return isNaN(d.getTime()) ? null : d;
  }
  function localText(d, withYear) {
    return d.getDate() + " " + MONTHS[d.getMonth()] + (withYear ? " " + d.getFullYear() : "") +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + " " + zoneName(d);
  }
  function localTimes(scope) {
    each("time[data-local]", function (el) {
      if (el.hasAttribute("data-localised")) return;
      var d = parseMoment(el);
      if (!d) return;
      var utc = el.textContent.trim();
      el.textContent = localText(d, el.getAttribute("data-local") === "year");
      if (!el.hasAttribute("title")) el.setAttribute("title", utc);
      el.setAttribute("data-localised", "");
    }, scope);
  }
  // Same wording as ago() in app/web.py.
  function relative(d, now) {
    var seconds = Math.round((now - d.getTime()) / 1000);
    if (seconds <= -60) {
      var ahead = -seconds;
      if (ahead < 3600) return "over " + Math.floor(ahead / 60) + " min";
      if (ahead < 86400) return "over " + Math.floor(ahead / 3600) + " uur";
      var daysAhead = Math.floor(ahead / 86400);
      return daysAhead === 1 ? "morgen" : "over " + daysAhead + " dagen";
    }
    seconds = Math.max(0, seconds);
    if (seconds < 60) return "zojuist";
    if (seconds < 3600) return Math.floor(seconds / 60) + " min geleden";
    if (seconds < 86400) return Math.floor(seconds / 3600) + " uur geleden";
    var days = Math.floor(seconds / 86400);
    return days === 1 ? "gisteren" : days + " dagen geleden";
  }
  function relativeTimes() {
    var now = Date.now();
    each("[data-ts]", function (el) {
      var d = parseMoment(el);
      if (!d) return;
      var text = relative(d, now);
      if (el.textContent !== text) el.textContent = text;
      if (el.tagName === "TIME" && el.getAttribute("title") && !el.hasAttribute("data-localised")) {
        el.setAttribute("title", localText(d, false));
        el.setAttribute("data-localised", "");
      }
    });
  }
  localTimes();
  relativeTimes();
  setInterval(function () { if (!document.hidden) relativeTimes(); }, 30000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) relativeTimes(); });
  window.CrypieTime = { localise: localTimes, refresh: relativeTimes, zone: zoneName };

  // -- One-off query parameters: refresh or Back must not show 'opgeslagen' again ------------
  var ONE_SHOT = ["saved", "flash"];
  if (window.history && history.replaceState && window.URL) {
    try {
      var url = new URL(window.location.href);
      var changed = false;
      ONE_SHOT.forEach(function (name) {
        if (url.searchParams.has(name)) { url.searchParams.delete(name); changed = true; }
      });
      if (changed) history.replaceState(history.state, "", url.pathname + url.search + url.hash);
    } catch (e) {}
  }

  // -- After a failed submit: bring the first error into view and give it focus ---------------
  // Order: a marked field, else the field next to an error message, else the error notice.
  (function focusFirstError() {
    var target = document.querySelector(
      '[aria-invalid="true"], .has-error input:not([type="hidden"]), .has-error select, .has-error textarea'
    );
    if (!target) {
      var message = document.querySelector(".field-error, .setting-error");
      var form = message && message.closest("form");
      target = form && form.querySelector('input:not([type="hidden"]):not([disabled]), select, textarea');
    }
    if (!target) {
      target = document.querySelector('.notice-error[role="alert"]');
      if (target && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    }
    if (!target) return;
    // Wait for the browser's own jump to an #anchor, then centre the error.
    window.requestAnimationFrame(function () {
      target.scrollIntoView({ block: "center" });
      try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
    });
  })();

  // -- Confirmations -------------------------------------------------------------------------
  // A submit button with its own question (runs before the form's submit event).
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("button[data-confirm], input[data-confirm]") : null;
    if (!btn || btn.disabled) return;
    if (!window.confirm(btn.getAttribute("data-confirm"))) ev.preventDefault();
  });

  function liveMode() {
    return document.body && document.body.getAttribute("data-mode") === "live";
  }

  // -- Busy state: one submit per form, with feedback ----------------------------------------
  function setBusy(form, submitter) {
    form.setAttribute("aria-busy", "true");
    form.setAttribute("data-submitting", "");
    if (submitter && submitter.classList) {
      submitter.classList.add("is-busy");
      var busy = submitter.getAttribute("data-busy");
      if (busy && submitter.tagName === "BUTTON") {
        if (!submitter.hasAttribute("data-idle")) submitter.setAttribute("data-idle", submitter.innerHTML);
        submitter.textContent = busy;
      }
    }
    // Disable only after the browser has read the form: a disabled submitter would drop its
    // name=action from the request.
    setTimeout(function () {
      each('button[type="submit"], button:not([type]), input[type="submit"]', function (b) {
        if (!b.disabled) { b.disabled = true; b.setAttribute("data-busy-disabled", ""); }
      }, form);
    }, 0);
    // A response that does not leave the page (a download) must not lock it for good.
    setTimeout(function () { clearBusy(form); }, 60000);
  }
  function clearBusy(form) {
    form.removeAttribute("aria-busy");
    form.removeAttribute("data-submitting");
    each("[data-busy-disabled]", function (b) { b.disabled = false; b.removeAttribute("data-busy-disabled"); }, form);
    each(".is-busy", function (b) {
      b.classList.remove("is-busy");
      if (b.hasAttribute("data-idle")) { b.innerHTML = b.getAttribute("data-idle"); b.removeAttribute("data-idle"); }
    }, form);
  }
  // Back to this page from the browser cache: the forms are usable again.
  window.addEventListener("pageshow", function (ev) {
    if (ev.persisted) each("form[data-submitting]", clearBusy);
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-submitting")) {
      ev.preventDefault(); // already on its way: a second click must not send it twice
      return;
    }
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      ev.preventDefault();
      return;
    }
    var ifLive = form.getAttribute("data-confirm-if-live");
    if (ifLive && liveMode() && !window.confirm(ifLive)) {
      ev.preventDefault();
      return;
    }
    var live = form.getAttribute("data-confirm-live");
    if (live) {
      var chosen = form.querySelector('input[name="trading_mode"]:checked');
      if (chosen && chosen.value === "live" && form.getAttribute("data-current-mode") !== "live") {
        if (!window.confirm(live)) {
          ev.preventDefault();
          return;
        }
      }
    }
    if (ev.defaultPrevented || form.hasAttribute("data-no-busy")) return;
    setBusy(form, ev.submitter || document.activeElement);
  });

  // -- Forms that show unsaved edits ---------------------------------------------------------
  function isChanged(field) {
    if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
    if (field.tagName === "SELECT") {
      return Array.prototype.some.call(field.options, function (o) { return o.selected !== o.defaultSelected; });
    }
    return field.value !== field.defaultValue;
  }
  each("form[data-track-changes]", function (form) {
    form.addEventListener("input", function (ev) {
      var field = ev.target;
      if (!field || !field.name || field.type === "password" || field.type === "hidden") return;
      var box = field.closest(".setting, .field") || field;
      if (field.type === "radio") {
        each('input[type="radio"][name="' + field.name + '"]', function (r) {
          (r.closest(".setting, .field") || r).classList.toggle("is-changed", isChanged(field) || r.checked !== r.defaultChecked);
        }, form);
        return;
      }
      box.classList.toggle("is-changed", isChanged(field));
    });
  });

  // -- Long blocks fold on a phone -----------------------------------------------------------
  if (window.matchMedia && window.matchMedia("(max-width: 640px)").matches) {
    var hash = window.location.hash.slice(1);
    each("details[data-collapse-mobile][open]", function (box) {
      var target = hash ? document.getElementById(hash) : null;
      if (target && box.contains(target)) return;
      if (box.querySelector('[aria-invalid="true"], .field-error')) return;
      box.removeAttribute("open");
    });
  }

  // -- Character counters --------------------------------------------------------------------
  function numberNl(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }
  each("textarea[data-char-count]", function (field) {
    var counter = document.getElementById(field.getAttribute("data-char-count"));
    var max = parseInt(field.getAttribute("maxlength"), 10);
    if (!counter || !max) return;
    function update() {
      var used = field.value.length;
      counter.textContent = numberNl(used) + " van " + numberNl(max) + " tekens";
      counter.classList.toggle("is-near", used >= max * 0.9);
      counter.classList.toggle("is-full", used >= max);
    }
    field.addEventListener("input", update);
    update();
  });

  // -- The kill switch may change in another tab: keep the navigation honest -----------------
  var modePill = document.querySelector("a.mode-pill");
  function applyPaused(paused) {
    if (!modePill || !document.body) return;
    var was = document.body.hasAttribute("data-paused");
    if (paused === was) return;
    if (paused) document.body.setAttribute("data-paused", "true");
    else document.body.removeAttribute("data-paused");
    modePill.classList.toggle("is-paused", paused);
    modePill.setAttribute("href", paused ? "/trading" : "/settings#trading");
    var extra = modePill.querySelector(".mode-extra");
    var hidden = modePill.querySelector(".visually-hidden");
    var stateText = modePill.getAttribute("data-paused-text") || "";
    if (paused && !extra) {
      extra = document.createElement("span");
      extra.className = "mode-extra";
      extra.textContent = "· noodstop";
      modePill.insertBefore(extra, hidden);
      if (hidden && stateText) hidden.textContent += " " + stateText + ".";
    } else if (!paused && extra) {
      extra.parentNode.removeChild(extra);
      if (hidden && stateText) hidden.textContent = hidden.textContent.replace(" " + stateText + ".", "");
    }
    var title = document.title.replace("Noodstop · ", "");
    if (paused) title = title.indexOf("LIVE · ") === 0 ? "LIVE · Noodstop · " + title.slice(7) : "Noodstop · " + title;
    document.title = title;
    var icon = document.querySelector('link[rel="icon"]');
    if (icon && document.body.getAttribute("data-mode") !== "live") {
      var href = icon.getAttribute("href") || "";
      icon.setAttribute("href", paused ? href.replace("/favicon.svg", "/favicon-paused.svg") : href.replace("/favicon-paused.svg", "/favicon.svg"));
    }
  }
  var statusBusy = false;
  function pollStatus() {
    if (statusBusy || document.hidden || !window.fetch) return;
    statusBusy = true;
    fetch("/api/status", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (r) {
        var type = r.headers.get("content-type") || "";
        // Logged out (a redirect to the login page) or an error: change nothing.
        return r.ok && type.indexOf("json") >= 0 ? r.json() : null;
      })
      .then(function (body) {
        if (!body || !body.health) return;
        if (typeof body.health.paused === "boolean") applyPaused(body.health.paused);
        var ev;
        try { ev = new CustomEvent("crypie:status", { detail: body }); } catch (e) { return; }
        document.dispatchEvent(ev);
      })
      .catch(function () {})
      .then(function () { statusBusy = false; });
  }
  if (modePill) {
    setInterval(pollStatus, 60000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) pollStatus(); });
  }

  // -- Settings: the on/off label next to a switch follows the checkbox ----------------------
  each(".switch input", function (box) {
    box.addEventListener("change", function () {
      var state = box.parentElement.querySelector(".state");
      if (state) state.textContent = box.checked ? "Aan" : "Uit";
    });
  });
})();
