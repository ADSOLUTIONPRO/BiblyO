// SPDX-License-Identifier: EUPL-1.2
// Sans choix enregistré, suit le système. Chargé avant l'affichage pour éviter le flash de thème.
(function () {
  var root = document.documentElement;
  if (root.hasAttribute("data-theme")) return;
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.setAttribute("data-theme", dark ? "dark" : "light");
})();
