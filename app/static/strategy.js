/* Strategie: the save form below a test holds the tested JSON (its hidden strategy_json). When
 * the editor no longer matches it, the note [data-tested-note] in that form is shown, so you
 * know the button saves the tested version and not what you typed since. The page works
 * without this file.
 *
 *   <form data-tested-for="editor-id"> … <p data-tested-note hidden>…</p></form>
 */
(function () {
  "use strict";

  var each = function (selector, fn, scope) {
    Array.prototype.forEach.call((scope || document).querySelectorAll(selector), fn);
  };

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
