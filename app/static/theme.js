/* Apply the saved light/dark choice before the page paints. Loaded in <head>, so it stays a
 * separate file: the Content-Security-Policy allows no inline scripts.
 *
 * Three choices: automatic (no data-theme: follow the system), light and dark. The browser bar
 * colour (meta theme-color) follows the choice. app.js uses window.CrypieTheme for the button. */
(function () {
  "use strict";
  // Scripts are on: CSS may fold what only scripts can open again (.js-only, the settings
  // blocks). Set before the page paints, so nothing jumps.
  document.documentElement.classList.add("js");
  var KEY = "crypie-theme";
  var COLORS = { light: "#f5f5f7", dark: "#000000" };

  function metas() {
    return document.querySelectorAll('meta[name="theme-color"]');
  }

  function apply(choice) {
    var root = document.documentElement;
    if (choice === "light" || choice === "dark") {
      root.setAttribute("data-theme", choice);
    } else {
      choice = "auto";
      root.removeAttribute("data-theme");
    }
    Array.prototype.forEach.call(metas(), function (meta) {
      var media = meta.getAttribute("data-media") || meta.getAttribute("media") || "";
      if (!meta.getAttribute("data-media")) meta.setAttribute("data-media", media);
      var systemDark = media.indexOf("dark") >= 0;
      if (choice === "auto") {
        meta.setAttribute("media", media);
        meta.setAttribute("content", systemDark ? COLORS.dark : COLORS.light);
      } else {
        // Both tags get the chosen colour, whatever the system prefers.
        meta.setAttribute("content", COLORS[choice]);
      }
    });
    return choice;
  }

  function saved() {
    try {
      var t = localStorage.getItem(KEY);
      return t === "light" || t === "dark" ? t : "auto";
    } catch (e) {
      return "auto";
    }
  }

  function save(choice) {
    try {
      if (choice === "auto") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch (e) {}
    return apply(choice);
  }

  window.CrypieTheme = { apply: apply, saved: saved, save: save };
  apply(saved());
})();
