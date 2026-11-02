/* Behaviour shared by every page. No inline scripts or on*= handlers in the templates (the
 * Content-Security-Policy forbids them); markup opts in through data attributes instead:
 *
 *   <form data-confirm="Vraag?">            asks before the form is submitted
 *   <button data-confirm="Vraag?">          asks before this submit button submits
 *   <form data-confirm-live="Vraag?" data-current-mode="paper">
 *                                           asks only when the form switches to live mode
 */
(function () {
  "use strict";

  // Theme toggle in the navigation bar.
  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var root = document.documentElement;
      var current = root.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("crypie-theme", next); } catch (e) {}
    });
  }

  // A submit button with its own question (runs before the form's submit event).
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("button[data-confirm], input[data-confirm]") : null;
    if (!btn || btn.disabled) return;
    if (!window.confirm(btn.getAttribute("data-confirm"))) ev.preventDefault();
  });

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!(form instanceof HTMLFormElement)) return;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      ev.preventDefault();
      return;
    }
    var live = form.getAttribute("data-confirm-live");
    if (live) {
      var chosen = form.querySelector('input[name="trading_mode"]:checked');
      if (chosen && chosen.value === "live" && form.getAttribute("data-current-mode") !== "live") {
        if (!window.confirm(live)) ev.preventDefault();
      }
    }
  });

  // Settings: the on/off label next to a switch follows the checkbox.
  Array.prototype.forEach.call(document.querySelectorAll(".switch input"), function (box) {
    box.addEventListener("change", function () {
      var state = box.parentElement.querySelector(".state");
      if (state) state.textContent = box.checked ? "Aan" : "Uit";
    });
  });
})();
