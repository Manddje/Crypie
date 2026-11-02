// Login pages: theme preference and the copy button for recovery codes. Kept out of the HTML
// so the Content-Security-Policy can forbid inline scripts.
(function () {
  try {
    var t = localStorage.getItem("crypie-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}

  document.addEventListener("DOMContentLoaded", function () {
    var buttons = document.querySelectorAll("button[data-copy]");
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener("click", function () {
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(btn.getAttribute("data-copy")).then(function () {
          btn.textContent = btn.getAttribute("data-copied") || "Gekopieerd";
        });
      });
    });
  });
})();
