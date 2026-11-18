// SPDX-License-Identifier: EUPL-1.2
// Scanner universel (lot L8). Une douchette USB ou Bluetooth est un clavier (HID) : elle tape le code très vite, puis
// Entrée ou Tab. Ce fichier :
// - ouvre la fenêtre « Scanner » depuis le bouton permanent ;
// - fait partir un code par Tab comme par Entrée ;
// - sur un poste de prêt où elle est activée (désactivée par défaut), capte hors de tout champ une frappe **rapide**
//   terminée par Entrée ou Tab, et l'envoie au bon endroit : au champ de scan de la page (comptoir, retours, rangement,
//   inventaire), sinon à la fenêtre « Scanner ». Jamais pendant la saisie d'un formulaire ou d'un mot de passe, jamais
//   quand une autre fenêtre est ouverte ;
// - mesure, dans l'assistant « Configurer ma douchette », la touche finale et la vitesse de frappe.
// Le serveur ne fait qu'identifier : rien n'est écrit parce qu'un code a été lu.
(function () {
  "use strict";

  var BURST_GAP = 60; // ms au plus entre deux caractères d'une même lecture
  var BURST_AVERAGE = 45; // ms en moyenne : une main est bien plus lente
  var MIN_LENGTH = 3;
  var TEST_IDLE_MS = 600; // assistant : sans touche finale, la lecture est jugée finie après ce silence
  var PREFERENCE = "biblyo.scanner.partout";

  // Désactivée par défaut : elle s'active poste par poste, sur les ordinateurs de la bibliothèque (assistant de la douchette).
  function globalEnabled() {
    try { return window.localStorage.getItem(PREFERENCE) === "oui"; } catch (error) { return false; }
  }

  function setGlobal(enabled) {
    try { window.localStorage.setItem(PREFERENCE, enabled ? "oui" : "non"); } catch (error) { /* stockage indisponible */ }
  }

  function editable(element) {
    if (!element || element === document.body) return false;
    if (element.isContentEditable) return true;
    return ["INPUT", "TEXTAREA", "SELECT"].indexOf(element.tagName) !== -1;
  }

  // --- La fenêtre « Scanner » -----------------------------------------------------------------------------------
  function dialog() { return document.getElementById("scanner"); }

  function openScanner() {
    var box = dialog();
    if (!box) return null;
    if (!box.open) box.showModal();
    var field = box.querySelector("[data-scan-field]");
    if (field) field.focus();
    return field;
  }

  document.addEventListener("click", function (event) {
    var opener = event.target.closest && event.target.closest("[data-scan-open]");
    if (!opener || !dialog()) return;
    event.preventDefault();
    openScanner();
  });

  // --- Champs de scan : Tab comme Entrée, champ vidé et focus rendu après la réponse ---------------------------
  // Une double lecture est signalée par le serveur (« déjà dans la liste », « déjà compté », « déjà sur cette
  // étagère ») ; seuls les retours l'écartent aussi dans le navigateur (counter.js), un retour étant immédiat.

  function submitField(field) {
    if (field.hasAttribute("data-scan-enter")) {
      // Comptoir et retours : leur composant reçoit la touche Entrée et gère sa file de codes.
      field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    } else if (field.form) {
      field.form.requestSubmit();
    }
  }

  document.addEventListener("keydown", function (event) {
    var field = event.target;
    if (!field.matches || !field.matches("[data-scan-field]:not([data-scan-enter])")) return;
    if (event.key === "Tab" && field.value.trim()) {
      event.preventDefault();
      field.form.requestSubmit();
    }
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    var form = event.detail.elt && event.detail.elt.closest && event.detail.elt.closest("[data-scan-form]");
    if (!form) return;
    var field = form.querySelector("[data-scan-field]");
    if (!field) return;
    field.value = "";
    field.focus();
  });

  // --- Capture globale : une lecture hors de tout champ ----------------------------------------------------------
  var burst = { text: "", first: 0, previous: 0 };

  function reset() { burst = { text: "", first: 0, previous: 0 }; }

  function deliver(code) {
    var zone = document.querySelector("main [data-scan-field]");
    var field = zone || openScanner();
    if (!field) return;
    field.focus();
    field.value = code;
    submitField(field);
  }

  document.addEventListener("keydown", function (event) {
    if (!globalEnabled() || event.ctrlKey || event.metaKey || event.altKey) { reset(); return; }
    if (editable(event.target) || document.querySelector("dialog[open]")) { reset(); return; }
    var now = event.timeStamp;
    if (event.key === "Enter" || event.key === "Tab") {
      var count = burst.text.length;
      var average = count > 1 ? (burst.previous - burst.first) / (count - 1) : Infinity;
      if (count >= MIN_LENGTH && now - burst.previous <= BURST_GAP * 2 && average <= BURST_AVERAGE) {
        event.preventDefault();
        event.stopImmediatePropagation();
        window.biblyoScanBurstAt = now;
        deliver(burst.text);
      }
      reset();
      return;
    }
    if (event.key.length !== 1) { reset(); return; }
    if (!burst.text || now - burst.previous > BURST_GAP) burst = { text: "", first: now, previous: now };
    burst.text += event.key;
    burst.previous = now;
    // Les raccourcis d'une lettre (P, R, N, G…) attendent un instant et se taisent pendant une lecture : à partir
    // de trois caractères rapides, c'en est une. Deux touches rapides (« G » puis « U ») restent un raccourci.
    if (burst.text.length >= MIN_LENGTH) window.biblyoScanBurstAt = now;
  }, true);

  // --- Assistant « Configurer ma douchette » ---------------------------------------------------------------------
  function setupTest(form) {
    var field = form.querySelector("[name=essai]");
    var suffix = form.querySelector("[name=suffixe]");
    var interval = form.querySelector("[name=intervalle]");
    var times = [];
    var timer = null;

    function finish(kind) {
      window.clearTimeout(timer);
      if (!field.value) { times = []; return; }
      suffix.value = kind;
      var gaps = times.length > 1 ? (times[times.length - 1] - times[0]) / (times.length - 1) : 0;
      interval.value = String(Math.round(gaps));
      times = [];
      if (window.htmx) window.htmx.trigger(form, "submit"); else form.submit();
    }

    field.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        finish(event.key === "Enter" ? "enter" : "tab");
        return;
      }
      if (event.key.length !== 1) return;
      if (!times.length) field.value = "";
      times.push(event.timeStamp);
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { finish("none"); }, TEST_IDLE_MS);
    });
    form.addEventListener("htmx:afterRequest", function () {
      field.value = "";
      field.focus();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-scan-test]").forEach(setupTest);
    function showState() {
      document.querySelectorAll("[data-scan-global-state]").forEach(function (zone) {
        zone.textContent = zone.getAttribute(globalEnabled() ? "data-on" : "data-off");
      });
    }
    document.querySelectorAll("[data-scan-global-toggle]").forEach(function (toggle) {
      toggle.checked = globalEnabled();
      toggle.addEventListener("change", function () { setGlobal(toggle.checked); showState(); });
    });
    showState();
  });
})();
