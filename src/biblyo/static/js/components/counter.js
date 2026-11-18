// SPDX-License-Identifier: EUPL-1.2
// Comptoir : une douchette tape un code puis Entrée, parfois très vite. Le champ se vide aussitôt, les codes partent un
// par un dans l'ordre. Le serveur décide de tout le reste, y compris de faire attendre les codes pendant un choix.
// Un scan part de `scanner`, pas du formulaire : sous WebKit et Firefox, un bouton cliqué ne prend pas le focus, et
// htmx enverrait le scan suivant à l'adresse de ce bouton (« Valider », « Annuler »…).
document.addEventListener("alpine:init", function () {
  var DOUBLE_SCAN_MS = 1500;
  Alpine.data("counter", function () {
    return {
      queue: [],
      lastValue: "",
      lastAt: 0,
      busy: false,
      after: null,
      init: function () {
        var self = this;
        // « Nouveau prêt » : la préparation gardée dans cet onglet est oubliée, rien n'est proposé ensuite.
        if (this.$el.hasAttribute("data-nouveau")) {
          try { sessionStorage.removeItem("biblyo.comptoir"); } catch (error) { /* stockage indisponible */ }
        }
        this.resume();
        // Une action par bouton, n'importe où dans la page (« Annuler » un retour…), réarme la lecture d'un même code.
        document.addEventListener("htmx:beforeRequest", function (event) {
          var elt = event.detail.elt;
          if (elt && elt.tagName === "BUTTON") self.lastValue = "";
        });
        this.$refs.form.addEventListener("htmx:afterRequest", function (event) {
          if (event.detail.elt === self.$refs.scanner) self.done();
        });
        // Un bouton qui envoie sa requête se désactive et perd le focus : sans ceci, un code scanné à cet instant
        // partirait dans le vide. Le focus revient au champ tout de suite ; le scan attend son tour (hx-sync).
        // Exception : « Lecteurs suivants », « Choisir l'exemplaire »… gardent la personne dans la liste qu'elle parcourt
        // (`data-focus-after`). Un champ de recherche des sélecteurs garde toujours son focus pendant la frappe.
        this.$refs.form.addEventListener("htmx:beforeRequest", function (event) {
          var elt = event.detail.elt;
          if (!elt || elt.tagName !== "BUTTON") return;
          self.lastValue = "";
          self.after = elt.getAttribute("data-focus-after");
          if (!self.after) self.$refs.field.focus();
        });
        // Après « Valider », « Annuler » ou « Renouveler », le bouton a disparu : le focus revient au champ, pour que la
        // douchette écrive toujours au bon endroit. Un focus encore en place (cadre, date, choix) n'est pas déplacé.
        this.$refs.form.addEventListener("htmx:afterSettle", function () {
          self.remember();
          self.announce();
          if (self.$refs.restored) self.$refs.restored.replaceChildren();
          var wanted = self.after ? document.querySelector(self.after) : null;
          self.after = null;
          if (wanted) { wanted.focus(); return; }
          var active = document.activeElement;
          if (!active || active === document.body || !active.isConnected) self.refocus();
        });
      },
      enter: function () {
        var value = this.$refs.field.value.trim();
        this.$refs.field.value = "";
        if (!value) return;
        // Retours (L8) : le même code lu deux fois de suite (douchette qui rebondit) est ignoré la seconde fois, au
        // lieu d'afficher « rien à rendre ». Un clic sur un bouton (« Annuler »…) réarme la lecture. Au comptoir,
        // le serveur répond déjà « déjà dans la liste ».
        var now = Date.now();
        var guarded = this.$el.querySelector("[data-double-scan]") !== null;
        if (guarded && value === this.lastValue && now - this.lastAt < DOUBLE_SCAN_MS) { this.echo(); return; }
        this.lastValue = value;
        this.lastAt = now;
        this.queue.push(value);
        this.pump();
      },
      // Une douchette réglée pour finir par Tab : le code part comme avec Entrée, le focus reste dans le champ.
      tab: function (event) {
        if (!this.$refs.field.value.trim()) return;
        event.preventDefault();
        this.enter();
      },
      echo: function () {
        var zone = this.$el.querySelector("[data-double-scan]");
        if (!zone) return;
        zone.textContent = "";
        window.setTimeout(function () { zone.textContent = zone.getAttribute("data-double-scan"); }, 40);
      },
      pump: function () {
        if (this.busy || !this.queue.length) return;
        this.busy = true;
        this.$refs.code.value = this.queue.shift();
        htmx.trigger(this.$refs.scanner, "scan");
      },
      // Le champ garde le focus, même quand un choix est demandé : un code scanné ensuite va en attente. Si le focus
      // allait au premier choix, l'Entrée de la douchette le choisirait en silence (D5). On choisit au clic ou par Tab.
      // Le comptoir en cours, dans cet onglet seulement : des identifiants, jamais un nom. Il sert à proposer de reprendre
      // le lecteur quand on revient d'une fiche de livre avec un exemplaire à ajouter.
      // Une préparation encore proposée (pas reprise, pas écartée) reste gardée telle quelle.
      remember: function () {
        var zone = document.getElementById("comptoir-etat");
        if (!zone || !window.sessionStorage) return;
        var fields = [];
        zone.querySelectorAll('input[type=hidden][name=lecteur], input[type=hidden][name=ex], input[name=cadre]:checked, input[name=responsable]:checked, input[type=hidden][name^=propose_]').forEach(function (input) {
          fields.push([input.name.replace(/^propose_/, ""), input.value]);
        });
        try { sessionStorage.setItem("biblyo.comptoir", JSON.stringify({ at: Date.now(), fields: fields })); } catch (error) { return; }
      },
      resume: function () {
        var self = this;
        var saved = null;
        try { saved = JSON.parse(sessionStorage.getItem("biblyo.comptoir") || "null"); } catch (error) { saved = null; }
        if (!this.$el.hasAttribute("data-reprise") || !saved || Date.now() - saved.at > 30 * 60 * 1000) { this.remember(); return; }
        var reader = saved.fields.filter(function (pair) { return pair[0] === "lecteur"; });
        if (!reader.length) { this.remember(); return; }
        // Proposée, jamais reprise : le serveur affiche « Reprendre ce lecteur » ou « Choisir un autre lecteur ».
        saved.fields.forEach(function (pair) {
          var input = document.createElement("input");
          input.type = "hidden"; input.name = "propose_" + pair[0]; input.value = pair[1];
          self.$refs.restored.appendChild(input);
        });
        htmx.process(this.$refs.restore);  // Alpine démarre parfois avant qu'htmx ait traité la page
        htmx.trigger(this.$refs.restore, "restore");
      },
      // Le message de la réponse, recopié dans une région d'annonce qui existe depuis le chargement : un lecteur d'écran
      // le lit à coup sûr. Vidée d'abord, pour qu'un même message (deux renouvellements de suite) soit relu.
      announce: function () {
        var region = this.$refs.announce;
        if (!region) return;
        var found = document.querySelector("#comptoir-etat [data-annonce]");
        region.textContent = "";
        if (found) window.setTimeout(function () { region.textContent = found.textContent.trim(); }, 60);
      },
      refocus: function () {
        this.$refs.field.focus();
      },
      done: function () {
        this.busy = false;
        this.$refs.code.value = "";
        this.refocus();
        this.pump();
      },
    };
  });
});
