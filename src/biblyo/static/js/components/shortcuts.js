// SPDX-License-Identifier: EUPL-1.2
// Raccourcis clavier : seulement ceux qui mènent quelque part. La liste affichée vient du serveur.
document.addEventListener("alpine:init", function () {
  var SEQUENCE_DELAY = 1200;

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
      handle: function (event) {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
          event.preventDefault();
          openDialog("palette");
          return;
        }
        if (event.metaKey || event.ctrlKey || event.altKey || typing(event) || document.querySelector("dialog[open]")) return;
        if (this.waiting) {
          this.waiting = false;
          window.clearTimeout(this.timer);
          var link = document.querySelector('[data-goto="' + CSS.escape(event.key.toLowerCase()) + '"]');
          if (link) link.click();
          return;
        }
        if (event.key === "/") {
          event.preventDefault();
          openDialog("palette");
        } else if (event.key === "?") {
          openDialog("raccourcis");
        } else if (event.key.toLowerCase() === "n") {
          var add = document.querySelector("[data-shortcut-new]");
          if (add) add.click();
        } else if (["p", "r"].indexOf(event.key.toLowerCase()) !== -1) {
          // Prêt et retour rapides (plan de L4, § 5) : seulement si le serveur a affiché l'action pour ce compte
          var desk = document.querySelector('[data-shortcut-key="' + event.key.toLowerCase() + '"]');
          if (desk) desk.click();
        } else if (event.key.toLowerCase() === "g") {
          this.waiting = true;
          var self = this;
          this.timer = window.setTimeout(function () { self.waiting = false; }, SEQUENCE_DELAY);
        }
      },
    };
  });
});
