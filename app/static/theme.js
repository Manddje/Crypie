/* Apply the saved light/dark choice before the page paints. Loaded in <head>, so it stays a
 * separate file: the Content-Security-Policy allows no inline scripts. */
(function () {
  try {
    var t = localStorage.getItem("crypie-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
