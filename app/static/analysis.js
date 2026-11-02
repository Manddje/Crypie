/* Small improvements for the analysis pages (Journaal, Tradingplan, Strategie, Nieuws). Every
 * page works without this file; it only makes long pages shorter on a phone and gives live
 * feedback while you type. No inline scripts: markup opts in through data attributes.
 *
 *   <details open data-collapse-mobile>      closed on load on narrow screens, unless the
 *                                            address points into it or it holds an error
 *   <textarea maxlength="…" data-char-count="id-of-counter">
 *                                            keeps '120 van 2000 tekens' in the counter up to date
 *   <form data-tested-for="editor-id">       holds the tested JSON in its hidden strategy_json;
 *                                            when the editor no longer matches it, the element
 *                                            [data-tested-note] in the form is shown
 */
(function () {
  "use strict";

  var each = function (selector, fn, scope) {
    Array.prototype.forEach.call((scope || document).querySelectorAll(selector), fn);
  };

  // -- Fold long blocks on a phone -----------------------------------------------------------
  var narrow = window.matchMedia && window.matchMedia("(max-width: 640px)").matches;
  if (narrow) {
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

  // -- Strategy: the tested strategy versus the editor ---------------------------------------
  function canonical(text) {
    try {
      return JSON.stringify(JSON.parse(text));
    } catch (e) {
      return null;
    }
  }
  each("form[data-tested-for]", function (form) {
    var editor = document.getElementById(form.getAttribute("data-tested-for"));
    var tested = form.querySelector('input[name="strategy_json"]');
    var note = form.querySelector("[data-tested-note]");
    if (!editor || !tested || !note) return;
    var reference = canonical(tested.value);
    function update() {
      var same = reference !== null && canonical(editor.value) === reference;
      note.hidden = same;
    }
    editor.addEventListener("input", update);
    update();
  });
})();
