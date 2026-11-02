/* Login pages (auth/*.html): small improvements on pages that work without them. Kept out of
 * the HTML so the Content-Security-Policy can forbid inline scripts; the theme comes from
 * theme.js in <head>.
 *
 *   <form data-confirm="Vraag?">      asks before the form is sent ('Later koppelen')
 *   <form data-auth-form>             one submit only; the button shows its data-busy text
 *   [data-copy] [data-download] [data-print] [data-saved-check] [data-continue]
 *                                     the recovery codes: copy (with a fallback over http),
 *                                     a .txt download, print, and 'Ik heb de codes bewaard'
 *   [data-draft-note]                 shown when Instellingen kept unsaved edits in this tab
 */
(function () {
  "use strict";

  function each(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  // -- Confirmation and one submit per form ---------------------------------------------------
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-submitting")) { ev.preventDefault(); return; }
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) { ev.preventDefault(); return; }
    form.setAttribute("data-submitting", "");
    var button = ev.submitter || form.querySelector('button[type="submit"]');
    if (button && button.getAttribute("data-busy")) {
      button.setAttribute("data-idle", button.textContent);
      button.textContent = button.getAttribute("data-busy");
    }
    // Disable after the browser has read the form (a disabled button sends no name=value).
    setTimeout(function () { if (button) button.disabled = true; }, 0);
  });
  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;
    each("form[data-submitting]", function (form) {
      form.removeAttribute("data-submitting");
      Array.prototype.forEach.call(form.querySelectorAll("button[data-idle]"), function (b) {
        b.textContent = b.getAttribute("data-idle");
        b.removeAttribute("data-idle");
        b.disabled = false;
      });
    });
  });

  // -- Instellingen kept a draft in this tab (sessionStorage, never passwords) ----------------
  try {
    var draft = window.sessionStorage.getItem("crypie-settings-draft");
    if (draft && draft !== "{}") each("[data-draft-note]", function (el) { el.hidden = false; });
  } catch (e) {}

  // -- Recovery codes -------------------------------------------------------------------------
  var status = document.querySelector("[data-copy-status]");
  function say(text) {
    if (!status) return;
    status.textContent = "";
    // A fresh text node, so screen readers announce it again on a second click.
    window.setTimeout(function () { status.textContent = text; }, 30);
  }
  function selectCodes(id) {
    var list = id && document.getElementById(id);
    if (!list || !window.getSelection) return false;
    var range = document.createRange();
    range.selectNodeContents(list);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  }
  function copyFallback(btn) {
    var copied = false;
    if (selectCodes(btn.getAttribute("data-copy-target"))) {
      try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
    }
    if (copied) {
      btn.textContent = btn.getAttribute("data-copied") || "Gekopieerd";
      say("De herstelcodes zijn gekopieerd.");
    } else {
      say("Kopiëren lukt hier niet (dat kan alleen via https). De codes zijn geselecteerd: kopieer ze met Ctrl+C of Cmd+C, of gebruik Downloaden of Afdrukken.");
    }
  }
  each("[data-recovery-actions]", function (el) { el.hidden = false; });
  each("button[data-copy]", function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () {
          btn.textContent = btn.getAttribute("data-copied") || "Gekopieerd";
          say("De herstelcodes zijn gekopieerd.");
        }, function () { copyFallback(btn); });
      } else {
        copyFallback(btn);
      }
    });
  });
  each("button[data-download]", function (btn) {
    btn.addEventListener("click", function () {
      var now = new Date();
      var lines = [
        btn.getAttribute("data-heading") || "Crypie: herstelcodes",
        "Gemaakt op " + now.toLocaleString("nl-NL") + ". Elke code werkt één keer.",
        "",
        btn.getAttribute("data-download"),
        ""
      ];
      try {
        var blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = btn.getAttribute("data-filename") || "herstelcodes.txt";
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        say("Het bestand " + a.download + " is gedownload. Bewaar het op een veilige plek.");
      } catch (e) {
        say("Downloaden lukt hier niet. Kopieer of druk de codes af.");
      }
    });
  });
  each("button[data-print]", function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });
  // 'Verder' only after 'Ik heb de codes bewaard' (without JavaScript it always works).
  each("[data-saved-check]", function (box) {
    var go = document.querySelector("[data-continue]");
    if (!go) return;
    function sync() {
      if (box.checked) go.removeAttribute("aria-disabled");
      else go.setAttribute("aria-disabled", "true");
    }
    go.addEventListener("click", function (ev) {
      if (!box.checked) {
        ev.preventDefault();
        say("Vink eerst aan dat je de codes hebt bewaard.");
        box.focus();
      }
    });
    box.addEventListener("change", sync);
    sync();
  });
})();
