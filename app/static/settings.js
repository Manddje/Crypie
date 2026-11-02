/* Instellingen: only improves a page that works without it (CSP: no inline scripts).
 *
 * Per block (form[data-settings-form]):
 *   - fields that differ from what is saved (data-saved) are marked orange, the block gets a
 *     'Niet opgeslagen' pill, its Opslaan bar sticks to the bottom of the screen, the list of
 *     parts shows it, and leaving the page asks first (beforeunload);
 *   - 'Wijzigingen terugdraaien' puts the saved values back;
 *   - 'Terug naar standaard' (per field) fills in the value from the server environment;
 *   - fields that only work while their switch is on (data-depends-on) are read-only and grey
 *     while it is off: their value is kept and still sent;
 *   - the current-password field says whether your edits need it, and a save that needs it
 *     without one is stopped here (the server checks it anyway);
 *   - a hard limit that becomes more than twice as loose asks once more (data-loosen);
 *   - the live confirmation only shows while Live is chosen;
 *   - what you typed is kept in this tab (sessionStorage, never passwords or the live
 *     confirmation) while the form is sent, so it can be put back if the session expired;
 *   - saving one block while another has edits asks first, and those edits come back (still
 *     marked as not saved) after the page reloads, instead of being lost silently.
 */
(function () {
  "use strict";

  var forms = Array.prototype.slice.call(document.querySelectorAll("form[data-settings-form]"));
  if (!forms.length) return;
  var DRAFT_KEY = "crypie-settings-draft";
  var DRAFT_MAX_AGE_MS = 30 * 60 * 1000;
  var leaving = false;

  function each(scope, selector, fn) {
    Array.prototype.forEach.call(scope.querySelectorAll(selector), fn);
  }

  // -- Values ---------------------------------------------------------------------------------
  function fieldsOf(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name && el.type !== "hidden" && el.type !== "password" && el.type !== "submit" &&
        el.type !== "button" && !el.hasAttribute("data-no-draft") && el.name !== "confirm_loosen";
    });
  }
  function valueOf(el) {
    if (el.type === "checkbox") return el.checked ? "true" : "false";
    return el.value;
  }
  function radioGroup(form, name) {
    return form.querySelector('[data-radio-group="' + name + '"]');
  }
  function savedOf(form, el) {
    if (el.type === "radio") {
      var group = radioGroup(form, el.name);
      return group ? group.getAttribute("data-saved") : null;
    }
    return el.getAttribute("data-saved");
  }
  function normal(text) {
    return String(text == null ? "" : text).replace(/\r\n/g, "\n").trim();
  }
  function isChanged(form, el) {
    var saved = savedOf(form, el);
    if (saved === null) return false;
    if (el.type === "radio") {
      var chosen = form.querySelector('input[name="' + el.name + '"]:checked');
      return !!chosen && chosen.value !== saved;
    }
    return normal(valueOf(el)) !== normal(saved);
  }
  function rowOf(el) {
    return el.closest(".setting");
  }
  function labelOf(row) {
    var label = row && row.querySelector(".setting-label");
    return label ? label.textContent.replace(/vraagt wachtwoord/, "").trim() : "";
  }

  // -- Numbers as the server reads them (app/settings_store.py parse_decimal) ----------------
  function parseNumber(text) {
    var t = String(text).replace(/[\s  ]/g, "");
    if (!t) return { error: "Vul een getal in." };
    if ((t.match(/[.,]/g) || []).length > 1) return { ambiguous: true };
    var m = /^([+-]?)(\d*)(?:([.,])(\d*))?$/.exec(t);
    if (!m || !(m[2] || m[4])) return { error: "Dit is geen getal." };
    if (m[3] && (m[4] || "").length === 3 && m[2].replace(/^0+/, "")) return { ambiguous: true };
    return { value: parseFloat((m[1] || "") + (m[2] || "0") + "." + (m[4] || "0")) };
  }
  function checkNumber(input) {
    var note = input.closest(".setting-control").querySelector("[data-number-check]");
    if (!note) return;
    var parsed = parseNumber(input.value);
    var text = "";
    if (parsed.ambiguous) text = "Onduidelijk: een punt of komma kan ook duizendtallen scheiden. Schrijf 1000 of 2,5.";
    else if (parsed.error && input.value.trim()) text = parsed.error;
    note.textContent = text;
    note.hidden = !text;
  }

  // -- Dependent fields -----------------------------------------------------------------------
  function parentOn(form, name) {
    var boxes = form.querySelectorAll('input[type="checkbox"][name="' + name + '"]');
    if (boxes.length) return boxes[0].checked;
    var field = form.querySelector('[name="' + name + '"]:not([type="hidden"])');
    if (!field) return true;
    var parsed = parseNumber(field.value);
    return !("value" in parsed) || parsed.value !== 0;
  }
  function syncDependents(form) {
    each(form, "[data-depends-on]", function (row) {
      var off = !parentOn(form, row.getAttribute("data-depends-on"));
      row.classList.toggle("is-off", off);
      each(row, "input:not([type=hidden]), textarea", function (el) {
        if (off) {
          el.setAttribute("readonly", "");
          el.setAttribute("aria-disabled", "true");
        } else {
          el.removeAttribute("readonly");
          el.removeAttribute("aria-disabled");
        }
      });
    });
  }

  // -- Unsaved edits per block ----------------------------------------------------------------
  function changedRows(form) {
    var rows = [];
    fieldsOf(form).forEach(function (el) {
      var row = rowOf(el);
      if (row && isChanged(form, el) && rows.indexOf(row) < 0) rows.push(row);
    });
    return rows;
  }
  function tocLink(form) {
    var block = form.closest("section[id]");
    return block ? document.querySelector('[data-toc="' + block.id + '"]') : null;
  }
  function update(form) {
    var rows = changedRows(form);
    each(form, ".setting", function (row) {
      if (!row.hasAttribute("data-password-row")) row.classList.toggle("is-changed", rows.indexOf(row) >= 0);
    });
    var dirty = rows.length > 0;
    form.classList.toggle("is-dirty", dirty);
    var pill = form.querySelector("[data-dirty-pill]");
    if (pill) pill.hidden = !dirty;
    var status = form.querySelector("[data-dirty-status]");
    if (status) {
      var title = form.getAttribute("data-block-title") || "dit blok";
      status.textContent = dirty
        ? (rows.length === 1 ? "1 wijziging" : rows.length + " wijzigingen") + " in " + title +
          " nog niet opgeslagen: " + rows.map(labelOf).join(", ") + "."
        : "";
    }
    var undo = form.querySelector("[data-undo]");
    if (undo) undo.hidden = !dirty;
    var link = tocLink(form);
    var marker = link && link.querySelector("[data-toc-dirty]");
    if (marker) marker.hidden = !dirty;
    // The password: say whether these edits need it.
    var need = form.querySelector("[data-password-need]");
    if (need) {
      var sensitive = rows.filter(function (r) { return r.hasAttribute("data-sensitive"); }).map(labelOf);
      need.textContent = sensitive.length
        ? "Nodig voor je wijziging" + (sensitive.length === 1 ? "" : "en") + ": " + sensitive.join(", ") + "."
        : dirty ? "Niet nodig voor je wijzigingen." : "";
      need.hidden = !need.textContent;
      need.classList.toggle("is-needed", sensitive.length > 0);
    }
    syncDependents(form);
  }
  function anyDirty() {
    return forms.some(function (f) { return f.classList.contains("is-dirty"); });
  }

  function restore(form, values) {
    fieldsOf(form).forEach(function (el) {
      if (!(el.name in values)) return;
      var v = values[el.name];
      if (el.type === "checkbox") el.checked = v === "true";
      else if (el.type === "radio") el.checked = el.value === v;
      else el.value = v;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    update(form);
  }
  function savedValues(form) {
    var values = {};
    fieldsOf(form).forEach(function (el) {
      var saved = savedOf(form, el);
      if (saved !== null) values[el.name] = saved;
    });
    return values;
  }

  // -- Drafts in this tab (session expired while saving) --------------------------------------
  function readDrafts() {
    try {
      var raw = window.sessionStorage.getItem(DRAFT_KEY);
      var data = raw ? JSON.parse(raw) : {};
      return data && typeof data === "object" ? data : {};
    } catch (e) { return {}; }
  }
  function writeDrafts(data) {
    try {
      if (Object.keys(data).length) window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify(data));
      else window.sessionStorage.removeItem(DRAFT_KEY);
    } catch (e) {}
  }
  function blockKey(form) {
    var block = form.closest("section[id]");
    return block ? block.id : "";
  }
  // ``carriedBy``: the title of the block that was saved while this one had edits too; those
  // edits come back by themselves after the save (only one block is sent per save).
  function saveDraft(form, carriedBy) {
    var values = {};
    fieldsOf(form).forEach(function (el) {
      if (el.type === "radio") { if (el.checked) values[el.name] = el.value; }
      else values[el.name] = valueOf(el);
    });
    var drafts = readDrafts();
    drafts[blockKey(form)] = { at: Date.now(), values: values, carriedBy: carriedBy || "" };
    writeDrafts(drafts);
  }
  function titleOf(form) {
    return form.getAttribute("data-block-title") || "dit blok";
  }
  function clockOf(when) {
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var zone = window.CrypieTime && window.CrypieTime.zone ? window.CrypieTime.zone(when) : "";
    return pad(when.getHours()) + ":" + pad(when.getMinutes()) + (zone ? " " + zone : "");
  }
  function dropDraft(key) {
    var drafts = readDrafts();
    if (key in drafts) { delete drafts[key]; writeDrafts(drafts); }
  }
  function draftNotice(form, message, primary, secondary) {
    var feedback = form.querySelector(".settings-feedback");
    if (!feedback) return null;
    var box = document.createElement("div");
    box.className = "notice notice-warn";
    box.setAttribute("role", "status");
    var body = document.createElement("div");
    body.className = "body";
    var text = document.createElement("p");
    text.textContent = message;
    var actions = document.createElement("div");
    actions.className = "actions";
    [primary, secondary].forEach(function (spec, i) {
      if (!spec) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = i === 0 ? "btn btn-primary btn-sm" : "btn btn-sm";
      button.textContent = spec.label;
      button.addEventListener("click", function () {
        spec.run();
        box.remove();
      });
      actions.appendChild(button);
    });
    body.appendChild(text);
    body.appendChild(actions);
    box.appendChild(body);
    feedback.appendChild(box);
    return box;
  }
  function focusFirstChange(form) {
    var first = changedRows(form)[0];
    var field = first && first.querySelector("input:not([type=hidden]), select, textarea");
    if (field) field.focus();
  }
  function offerDraft(form, draft) {
    draftNotice(
      form,
      "Je wijzigingen in dit blok van " + clockOf(new Date(draft.at)) + " zijn niet opgeslagen " +
        "(bijvoorbeeld omdat je sessie was verlopen). Ze staan nog in deze browser.",
      { label: "Wijzigingen terugzetten", run: function () { restore(form, draft.values); dropDraft(blockKey(form)); focusFirstChange(form); } },
      { label: "Weggooien", run: function () { dropDraft(blockKey(form)); } }
    );
  }
  // Another block was saved while this one had edits: put them back at once (still marked
  // as not saved, so leaving the page asks again) and say so.
  function carryDraft(form, draft) {
    restore(form, draft.values);
    dropDraft(blockKey(form));
    if (!form.classList.contains("is-dirty")) return;
    draftNotice(
      form,
      "Nog niet opgeslagen: je wijzigingen in " + titleOf(form) + " staan er weer, maar alleen " +
        draft.carriedBy + " is opgeslagen. Sla ze op met '" + titleOf(form) + " opslaan', of draai ze terug.",
      null,
      { label: "Wijzigingen terugdraaien", run: function () { restore(form, savedValues(form)); syncLive(form); } }
    );
  }
  function otherDirty(form) {
    return forms.filter(function (f) { return f !== form && f.classList.contains("is-dirty"); });
  }

  // -- Loosening a hard limit -----------------------------------------------------------------
  function loosened(form) {
    var out = [];
    each(form, "[data-loosen]", function (row) {
      var input = row.querySelector("input[data-number]");
      if (!input) return;
      var before = parseNumber(input.getAttribute("data-saved"));
      var after = parseNumber(input.value);
      if (!("value" in before) || !("value" in after) || before.value === after.value) return;
      var b = before.value, a = after.value, rule = row.getAttribute("data-loosen"), loose;
      if (rule === "higher") loose = b > 0 ? a > 2 * b : a > 0;
      else if (rule === "lower") loose = b > 0 && a < b / 2;
      else loose = a === 0 || (b > 0 && a > 2 * b);
      if (loose) {
        var unit = row.getAttribute("data-unit") || "";
        // The same notation as the message after saving: '€ 50,50', '€ 250', '2,5%'.
        var fmt = function (n) {
          var money = unit === "€" || unit === "$";
          var whole = Math.round(n * 100) % 100 === 0;
          var t = n.toLocaleString("nl-NL", {
            minimumFractionDigits: money && !whole ? 2 : 0,
            maximumFractionDigits: 2
          });
          return money ? unit + "\u00a0" + t : unit === "%" ? t + "%" : unit ? t + " " + unit : t;
        };
        out.push("• " + row.getAttribute("data-label") + ": " + fmt(b) + " → " + fmt(a) +
          (rule === "capital" && a === 0 ? " (het hele Bitvavo-account)" : ""));
      }
    });
    return out;
  }

  // -- Live: the confirmation only matters while Live is chosen --------------------------------
  function syncLive(form) {
    var panel = form.querySelector("[data-live-confirm]");
    if (!panel) return;
    var chosen = form.querySelector('input[name="trading_mode"]:checked');
    var live = !!chosen && chosen.value === "live";
    panel.hidden = !live && !panel.classList.contains("has-error");
    return live;
  }

  // -- Wiring ---------------------------------------------------------------------------------
  var drafts = readDrafts();
  forms.forEach(function (form) {
    var key = blockKey(form);
    // Just saved (a message in this block) or sent back with the values: the draft is done.
    var saved = form.querySelector(".settings-feedback .notice-ok") || form.hasAttribute("data-submitted");
    var draft = drafts[key];
    if (draft && (saved || Date.now() - draft.at > DRAFT_MAX_AGE_MS)) {
      dropDraft(key);
    } else if (draft) {
      var current = {};
      fieldsOf(form).forEach(function (el) {
        if (el.type === "radio") { if (el.checked) current[el.name] = el.value; }
        else current[el.name] = valueOf(el);
      });
      var differs = Object.keys(draft.values || {}).some(function (n) {
        return n in current && normal(current[n]) !== normal(draft.values[n]);
      });
      if (!differs) dropDraft(key);
      else if (draft.carriedBy) carryDraft(form, draft);
      else offerDraft(form, draft);
    }

    each(form, "[data-reset-field]", function (btn) {
      btn.hidden = false;
      btn.addEventListener("click", function () {
        var name = btn.getAttribute("data-reset-field");
        var value = btn.getAttribute("data-default");
        var values = {};
        values[name] = value;
        restore(form, values);
        var field = form.querySelector('[name="' + name + '"]:not([type="hidden"])');
        if (field) field.focus();
      });
    });
    var undo = form.querySelector("[data-undo]");
    if (undo) {
      undo.addEventListener("click", function () {
        restore(form, savedValues(form));
        syncLive(form);
      });
    }
    each(form, "input[data-number]", function (input) {
      input.addEventListener("input", function () { checkNumber(input); });
    });
    each(form, "input[data-model]", function (input) {
      var known = Array.prototype.map.call(document.querySelectorAll("#claude-models option"), function (o) { return o.value; });
      var warn = input.closest(".setting").querySelector("[data-model-warn]");
      input.addEventListener("input", function () {
        if (warn) warn.hidden = !input.value.trim() || known.indexOf(input.value.trim()) >= 0;
      });
    });
    form.addEventListener("input", function () { update(form); });
    form.addEventListener("change", function () {
      update(form);
      if (syncLive(form)) {
        var confirmField = form.querySelector("#confirm_live");
        if (confirmField && document.activeElement && document.activeElement.name === "trading_mode") {
          confirmField.scrollIntoView({ block: "nearest" });
        }
      }
    });

    // A save or reset that needs the password, without one: say so at the field instead of
    // sending it (and before app.js asks its confirmation question).
    function missingPassword(action) {
      var password = form.querySelector('input[type="password"][name="current_password"]');
      var needsPassword = action === "reset"
        ? form.hasAttribute("data-reset-needs-password")
        : action === "reset_paper" || changedRows(form).some(function (r) { return r.hasAttribute("data-sensitive"); });
      if (!password || !needsPassword || password.value) return false;
      var need = form.querySelector("[data-password-need]");
      if (need) {
        need.textContent = action === "reset"
          ? "Vul je huidige wachtwoord in: dit blok terugzetten raakt instellingen met een slotje."
          : "Vul je huidige wachtwoord in om dit op te slaan.";
        need.hidden = false;
      }
      password.setAttribute("aria-invalid", "true");
      password.scrollIntoView({ block: "center" });
      password.focus({ preventScroll: true });
      return true;
    }
    function actionOf(button) {
      return button && button.name === "action" ? button.value : "save";
    }
    form.addEventListener("click", function (ev) {
      var button = ev.target.closest ? ev.target.closest('button[type="submit"]') : null;
      if (!button || button.disabled) return;
      if (missingPassword(actionOf(button))) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    });
    each(form, 'input[type="password"]', function (password) {
      password.addEventListener("input", function () { password.removeAttribute("aria-invalid"); });
    });

    form.addEventListener("submit", function (ev) {
      var action = actionOf(ev.submitter || document.activeElement);
      if (missingPassword(action)) {
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      if (action === "save") {
        var loose = loosened(form);
        if (loose.length) {
          var question = "Je maakt " + (loose.length === 1 ? "deze harde limiet" : "deze harde limieten") +
            " in één keer meer dan twee keer zo ruim:\n" + loose.join("\n") + "\n\nWeet je het zeker?";
          if (!window.confirm(question)) {
            ev.preventDefault();
            ev.stopPropagation();
            return;
          }
          var yes = form.querySelector('input[name="confirm_loosen"]');
          if (!yes) {
            yes = document.createElement("input");
            yes.type = "hidden";
            yes.name = "confirm_loosen";
            form.appendChild(yes);
          }
          yes.value = "1";
          if (yes.type === "checkbox") yes.checked = true;
        }
      }
      if (ev.defaultPrevented) return;
      // Only this block is sent. Edits in other blocks would be lost with the page: ask, and
      // keep them in this tab so they come back (still unsaved) after the save.
      var others = otherDirty(form);
      if (others.length) {
        var names = others.map(titleOf);
        var question = "Je hebt ook niet-opgeslagen wijzigingen in " + names.join(", ") + ". " +
          "Alleen " + titleOf(form) + " wordt nu opgeslagen. De andere wijzigingen staan er na het " +
          "opslaan weer, nog niet opgeslagen.\n\nNu " + titleOf(form) + " opslaan?";
        if (!window.confirm(question)) {
          ev.preventDefault();
          ev.stopPropagation();
          return;
        }
        others.forEach(function (other) { saveDraft(other, titleOf(form)); });
      }
      leaving = true;
      saveDraft(form);
    });

    update(form);
    syncLive(form);
  });

  // A later question (app.js data-confirm) may still cancel the submit: then the page stays.
  // This listener is added after app.js's, so it runs after it.
  document.addEventListener("submit", function (ev) {
    if (ev.defaultPrevented && forms.indexOf(ev.target) >= 0) leaving = false;
  });
  // Back to this page from the browser cache: it may leave again only after asking.
  window.addEventListener("pageshow", function () { leaving = false; });
  window.addEventListener("beforeunload", function (ev) {
    if (leaving || !anyDirty()) return;
    ev.preventDefault();
    ev.returnValue = "";
  });

  // Error summary: a link moves the focus to its field.
  each(document, "[data-focus-link]", function (link) {
    link.addEventListener("click", function (ev) {
      var target = document.getElementById(link.getAttribute("href").slice(1));
      if (!target) return;
      ev.preventDefault();
      target.scrollIntoView({ block: "center" });
      target.focus({ preventScroll: true });
    });
  });

  // -- Which block is in view: the list of parts shows it -------------------------------------
  var links = Array.prototype.slice.call(document.querySelectorAll("[data-toc]"));
  if (links.length && "IntersectionObserver" in window) {
    var visible = {};
    var mark = function () {
      var current = null;
      links.forEach(function (a) {
        var id = a.getAttribute("data-toc");
        if (visible[id] && current === null) current = id;
      });
      links.forEach(function (a) {
        if (a.getAttribute("data-toc") === current) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
    };
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      mark();
    }, { rootMargin: "-30% 0px -55% 0px" });
    links.forEach(function (a) {
      var block = document.getElementById(a.getAttribute("data-toc"));
      if (block) observer.observe(block);
    });
  }
})();
