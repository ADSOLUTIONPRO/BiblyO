// SPDX-License-Identifier: EUPL-1.2
// Palette de commandes : filtre les destinations réelles, rendues par le serveur selon les droits du compte.
document.addEventListener("alpine:init", function () {
  function normalize(text) {
    return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  Alpine.data("palette", function () {
    return {
      query: "",
      active: 0,
      matches: -1, // état réactif : le DOM filtré, lui, ne déclenche aucune mise à jour d'Alpine
      get noResult() { return this.matches === 0; },
      entries: function () { return Array.from(this.$refs.results.querySelectorAll(".it")); },
      visible: function () { return this.entries().filter(function (el) { return !el.hidden; }); },
      filter: function () {
        var query = normalize(this.query.trim());
        this.entries().forEach(function (el) {
          // Les notices sont déjà filtrées par le serveur : leur correspondance peut porter sur un champ non affiché.
          if (el.hasAttribute("data-server")) return;
          el.hidden = query !== "" && normalize(el.textContent).indexOf(query) === -1;
        });
        this.$refs.results.querySelectorAll("[data-group]").forEach(function (group) {
          group.hidden = group.querySelector(".it:not([hidden]), [data-note]") === null;
        });
        this.matches = this.visible().length;
        this.active = 0;
        this.mark();
      },
      mark: function () {
        var list = this.visible();
        var active = this.active;
        this.entries().forEach(function (el) { el.classList.remove("on"); });
        if (list[active]) {
          list[active].classList.add("on");
          list[active].scrollIntoView({ block: "nearest" });
        }
      },
      next: function () {
        var count = this.visible().length;
        if (count) { this.active = (this.active + 1) % count; this.mark(); }
      },
      previous: function () {
        var count = this.visible().length;
        if (count) { this.active = (this.active - 1 + count) % count; this.mark(); }
      },
      go: function () {
        var target = this.visible()[this.active];
        if (target) target.click();
      },
    };
  });
});
