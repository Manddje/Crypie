// Starts Swagger UI on /docs (only served with API_DOCS_ENABLED=true); no inline script needed.
(function () {
  var el = document.getElementById("swagger-ui");
  if (!el || typeof SwaggerUIBundle === "undefined") return;
  SwaggerUIBundle({
    url: el.getAttribute("data-openapi-url"),
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  });
})();
