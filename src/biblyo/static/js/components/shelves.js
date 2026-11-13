// SPDX-License-Identifier: EUPL-1.2
// Amélioration progressive des étagères. Sans ce fichier, tout reste possible par formulaire et au clavier (D4).
// Le glisser-déposer remplit un formulaire ordinaire : le serveur reste seul juge, avec les mêmes contrôles.
(function () {
  "use strict";

  var dragged = null;

  function clearMarks() {
    document.querySelectorAll(".drop-before, .drop-end").forEach(function (element) {
      element.classList.remove("drop-before", "drop-end");
    });
  }

  function dropSpot(node) {
    if (!node.closest) return null;
    return node.closest(".spi[data-rank]") || node.closest(".lvl[data-level]");
  }

  document.addEventListener("dragstart", function (event) {
    var spine = event.target.closest && event.target.closest(".spi[data-copy]");
    if (!spine) return;
    dragged = spine;
    spine.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", spine.getAttribute("data-copy"));
  });

  document.addEventListener("dragover", function (event) {
    if (!dragged) return;
    var spot = dropSpot(event.target);
    if (!spot || spot === dragged) return;
    event.preventDefault();
    clearMarks();
    spot.classList.add(spot.matches(".spi") ? "drop-before" : "drop-end");
  });

  document.addEventListener("drop", function (event) {
    if (!dragged) return;
    var spot = dropSpot(event.target);
    var form = document.getElementById("f-drag");
    if (!spot || spot === dragged || !form) return;
    event.preventDefault();
    var level = spot.closest(".lvl[data-level]");
    form.elements.copie.value = dragged.getAttribute("data-copy");
    form.elements.version.value = dragged.getAttribute("data-version");
    form.elements.lieu.value = level.getAttribute("data-level");
    form.elements.version_lieu.value = level.getAttribute("data-level-version");
    form.elements.rang.value = spot.matches(".spi")
      ? spot.getAttribute("data-rank")
      : Number(level.getAttribute("data-count")) + 1;
    form.submit();
  });

  document.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    clearMarks();
  });

  // Plan de salle : les flèches passent au meuble le plus proche dans cette direction (D5).
  var DIRECTIONS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

  document.addEventListener("keydown", function (event) {
    var direction = DIRECTIONS[event.key];
    var tile = event.target.closest && event.target.closest(".room-plan a.furn");
    if (!direction || !tile) return;
    var from = tile.getBoundingClientRect();
    var best = null;
    var bestScore = Infinity;
    tile.closest(".room-plan").querySelectorAll("a.furn").forEach(function (other) {
      if (other === tile) return;
      var box = other.getBoundingClientRect();
      var dx = box.left - from.left;
      var dy = box.top - from.top;
      var along = dx * direction[0] + dy * direction[1];
      if (along <= 0) return;
      var score = along + 3 * Math.abs(dx * direction[1] + dy * direction[0]);
      if (score < bestScore) {
        bestScore = score;
        best = other;
      }
    });
    if (best) {
      event.preventDefault();
      best.focus();
    }
  });
})();
