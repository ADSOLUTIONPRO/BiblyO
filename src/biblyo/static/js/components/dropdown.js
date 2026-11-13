// SPDX-License-Identifier: EUPL-1.2
// Menu déroulant : clavier (flèches, Échap), clic à l'extérieur, focus rendu au bouton.
document.addEventListener("alpine:init", function () {
  Alpine.data("dropdown", function () {
    return {
      open: false,
      get expanded() { return this.open ? "true" : "false"; },
      get closed() { return !this.open; },
      items: function () { return Array.from(this.$refs.panel.querySelectorAll(".mi-i")); },
      toggle: function () {
        this.open = !this.open;
        if (this.open) this.$nextTick(this.focusFirst.bind(this));
      },
      close: function () { this.open = false; },
      closeAndRefocus: function () {
        if (!this.open) return;
        this.open = false;
        this.$refs.button.focus();
      },
      focusFirst: function () {
        var list = this.items();
        if (list.length) list[0].focus();
      },
      move: function (step) {
        var list = this.items();
        var index = list.indexOf(document.activeElement);
        list[(index + step + list.length) % list.length].focus();
      },
      next: function () { this.move(1); },
      previous: function () { this.move(-1); },
    };
  });
});
