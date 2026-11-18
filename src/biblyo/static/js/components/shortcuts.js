// SPDX-License-Identifier: EUPL-1.2
// Raccourcis clavier : seulement ceux qui mènent quelque part. La liste affichée vient du serveur.
document.addEventListener("alpine:init", function () {
  var SEQUENCE_DELAY = 1200;
  // Une douchette tape une dizaine de caractères en quelques dizaines de millisecondes : une carte « P-000123 » lue
  // hors d'un champ ne doit pas ouvrir le prêt. Une action d'une lettre attend donc un instant, et se tait si d'autres
  // touches arrivent aussitôt ; pendant une lecture, plus rien n'est pris pour un raccourci (lot L8).
  var SINGLE_KEY_DELAY = 90;
  var BURST_QUIET = 250; // ms : une lecture vue par scanner.js (3 caractères ou plus) fait taire l'action en attente

  function typing(event) {
    var element = event.target;
    return element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].indexOf(element.tagName) !== -1;
  }

  function openDialog(id) {
    var dialog = document.getElementById(id);
    if (!dialog || dialog.open) return;
    dialog.showModal();
    var input = dialog.querySelector("[autofocus]");
    if (input) input.focus();
  }

  Alpine.data("shortcuts", function () {
    return {
      waiting: false,
      timer: null,
      pending: null,
      later: function (action) {
        window.clearTimeout(this.pending);
        this.pending = window.setTimeout(function () {
          var burst = window.biblyoScanBurstAt || 0;
          if (burst && window.performance.now() - burst < BURST_QUIET) return;
          action();
        }, SINGLE_KEY_DELAY);
      },
      handle: function (event) {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
          event.preventDefault();
          openDialog("palette");
          return;
        }
        if (event.metaKey || event.ctrlKey || event.altKey || typing(event) || document.querySelector("dialog[open]")) return;
        // Une autre touche arrive avant l'action en attente : c'était le début d'un code, pas un raccourci.
        if (event.key.length === 1 || event.key === "Enter" || event.key === "Tab") window.clearTimeout(this.pending);
        if (this.waiting) {
          this.waiting = false;
          window.clearTimeout(this.timer);
          var link = document.querySelector('[data-goto="' + CSS.escape(event.key.toLowerCase()) + '"]');
          if (link) this.later(function () { link.click(); });
          return;
        }
        if (event.key === "/") {
          event.preventDefault();
          this.later(function () { openDialog("palette"); });
        } else if (event.key === "?") {
          this.later(function () { openDialog("raccourcis"); });
        } else if (event.key.toLowerCase() === "n") {
          var add = document.querySelector("[data-shortcut-new]");
          if (add) this.later(function () { add.click(); });
        } else if (["p", "r"].indexOf(event.key.toLowerCase()) !== -1) {
          // Prêt et retour rapides (plan de L4, § 5) : seulement si le serveur a affiché l'action pour ce compte
          var desk = document.querySelector('[data-shortcut-key="' + event.key.toLowerCase() + '"]');
          if (desk) this.later(function () { desk.click(); });
        } else if (event.key.toLowerCase() === "g") {
          this.waiting = true;
          var self = this;
          this.timer = window.setTimeout(function () { self.waiting = false; }, SEQUENCE_DELAY);
        }
      },
    };
  });
});
