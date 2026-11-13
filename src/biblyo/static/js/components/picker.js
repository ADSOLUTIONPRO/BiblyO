// SPDX-License-Identifier: EUPL-1.2
// Sélecteur de lecteur : liste déroulante au clavier. Une douchette tape le numéro puis Entrée : un seul résultat suffit.
document.addEventListener("alpine:init", function () {
  Alpine.data("patronPicker", function () {
    return {
      active: -1,
      enterPending: false,
      init: function () {
        var self = this;
        this.$refs.options.addEventListener("htmx:afterSettle", function () { self.loaded(); });
      },
      options: function () { return Array.from(this.$refs.options.querySelectorAll("[role=option]")); },
      expand: function (open) { this.$refs.input.setAttribute("aria-expanded", open ? "true" : "false"); },
      loaded: function () {
        if (this.$refs.search.hidden) { this.close(); return; } // réponse tardive, un lecteur est déjà choisi
        var list = this.options();
        this.active = -1;
        this.expand(list.length > 0);
        if (this.enterPending) {
          this.enterPending = false;
          if (list.length === 1) this.choose(list[0]);
        }
      },
      mark: function () {
        var list = this.options();
        var active = this.active;
        list.forEach(function (option, index) {
          option.classList.toggle("on", index === active);
          option.setAttribute("aria-selected", index === active ? "true" : "false");
        });
        if (list[active]) {
          this.$refs.input.setAttribute("aria-activedescendant", list[active].id);
          list[active].scrollIntoView({ block: "nearest" });
        } else {
          this.$refs.input.removeAttribute("aria-activedescendant");
        }
      },
      next: function () {
        var count = this.options().length;
        if (count) { this.active = (this.active + 1) % count; this.mark(); }
      },
      previous: function () {
        var count = this.options().length;
        if (count) { this.active = (this.active - 1 + count) % count; this.mark(); }
      },
      // Entrée : le résultat surligné, sinon le seul résultat ; sinon on interroge tout de suite, sans attendre le délai.
      enter: function () {
        var list = this.options();
        if (list[this.active]) { this.choose(list[this.active]); return; }
        if (this.$refs.input.value.trim() === "") return;
        this.enterPending = true;
        htmx.trigger(this.$refs.input, "search");
      },
      pick: function (event) {
        var option = event.target.closest("[role=option]");
        if (option) this.choose(option);
      },
      choose: function (option) {
        this.$refs.value.value = option.getAttribute("data-id");
        this.$refs.pickedLabel.textContent = option.getAttribute("data-label");
        if (this.$refs.labelValue) this.$refs.labelValue.value = option.getAttribute("data-label");
        this.$refs.picked.hidden = false;
        this.$refs.search.hidden = true;
        this.close();
        this.$refs.input.value = "";
        this.$el.dispatchEvent(new CustomEvent("picker:chosen", { bubbles: true, detail: { id: option.getAttribute("data-id") } }));
      },
      close: function () {
        this.$refs.options.replaceChildren();
        this.active = -1;
        this.expand(false);
        this.$refs.input.removeAttribute("aria-activedescendant");
      },
      clear: function () {
        this.$refs.value.value = "";
        this.$refs.pickedLabel.textContent = "";
        if (this.$refs.labelValue) this.$refs.labelValue.value = "";
        this.$refs.picked.hidden = true;
        this.$refs.search.hidden = false;
        this.$refs.input.focus();
      },
    };
  });

  // Formulaire d'inscription : ce qui ne concerne pas cette personne est masqué, le serveur l'ignore de toute façon.
  Alpine.data("registrationForm", function () {
    return {
      category: "adult",
      home: "aucun",
      init: function () {
        this.category = this.$el.getAttribute("data-category");
        this.home = this.$el.getAttribute("data-home");
      },
      setCategory: function (event) { this.category = event.target.value; },
      setHome: function (event) { this.home = event.target.value; },
      get isAdult() { return this.category === "adult"; },
      get isMinor() { return this.category === "minor"; },
      get homeExisting() { return this.home === "existant"; },
      get homeNew() { return this.home === "nouveau"; },
    };
  });

  Alpine.data("printPage", function () {
    return { print: function () { window.print(); } };
  });
});
