// SPDX-License-Identifier: EUPL-1.2
// Comportements communs : jeton CSRF pour HTMX, fenêtres <dialog>, notifications.
(function () {
  "use strict";

  var TOAST_DELAY = 6000;

  // --- HTMX : le jeton CSRF accompagne toute requête.
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) event.detail.headers["X-CSRF-Token"] = meta.content;
  });

  // --- Fenêtres : ouverture et fermeture par attributs, y compris dans un fragment HTMX.
  // Safari ne donne pas le focus à un bouton cliqué : le navigateur ne peut donc pas le lui rendre à la fermeture.
  var openers = new WeakMap();
  document.addEventListener("close", function (event) {
    var opener = openers.get(event.target);
    if (opener && document.contains(opener)) opener.focus();
    openers.delete(event.target);
  }, true);

  function focusFirstInput(dialog) {
    var input = dialog.querySelector("[autofocus]");
    if (input) input.focus();
  }

  // Fichier trop lourd : dit tout de suite, sans attendre le refus du serveur, qui reste la vraie protection.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("input[type=file][data-max-bytes]")) return;
    var file = input.files[0];
    var tooBig = file && file.size > Number(input.getAttribute("data-max-bytes"));
    input.setCustomValidity(tooBig ? input.getAttribute("data-too-big") : "");
    if (tooBig) input.reportValidity();
  });

  // Zone de dépôt : un fichier glissé sur [data-dropzone] remplit son champ, comme un choix au clavier ou à la souris.
  function dropzoneName(zone, input) {
    var name = zone.querySelector("[data-dropzone-name]");
    if (name) name.textContent = input.files && input.files[0] ? input.files[0].name : "";
  }

  document.addEventListener("change", function (event) {
    var zone = event.target.closest && event.target.closest("[data-dropzone]");
    if (zone && event.target.matches("input[type=file]")) dropzoneName(zone, event.target);
  });

  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = event.target.closest && event.target.closest("[data-dropzone]");
      if (!zone) return;
      event.preventDefault();
      zone.classList.add("is-over");
    });
  });

  document.addEventListener("dragleave", function (event) {
    var zone = event.target.closest && event.target.closest("[data-dropzone]");
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("is-over");
  });

  document.addEventListener("drop", function (event) {
    var zone = event.target.closest && event.target.closest("[data-dropzone]");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("is-over");
    var input = zone.querySelector("input[type=file]");
    if (!input || !event.dataTransfer || !event.dataTransfer.files.length) return;
    input.files = event.dataTransfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });

  // Échap ferme la fenêtre ouverte, y compris là où le moteur ne le fait pas de lui-même (WebKit sous Linux).
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    var open = document.querySelectorAll("dialog[open]");
    if (open.length) open[open.length - 1].close();
  });

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-open-dialog]");
    if (opener) {
      var target = document.getElementById(opener.getAttribute("data-open-dialog"));
      if (target && !target.open) {
        openers.set(target, opener);
        target.showModal();
        focusFirstInput(target);
      }
      return;
    }
    var closer = event.target.closest("[data-close-dialog]");
    if (closer) {
      var parent = closer.closest("dialog");
      if (parent) parent.close();
      return;
    }
    // Un clic sur le fond de la fenêtre arrive sur le <dialog> lui-même.
    if (event.target.tagName === "DIALOG") {
      var box = event.target.getBoundingClientRect();
      var inside = event.clientX >= box.left && event.clientX <= box.right &&
        event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) event.target.close();
    }
  });

  // --- Notifications : messages du serveur, ou en-tête HX-Trigger {"toast": {"kind": …, "text": …}}.
  function dismissLater(toast) {
    window.setTimeout(function () { toast.remove(); }, TOAST_DELAY);
  }

  function showToast(kind, text) {
    var zone = document.getElementById("toasts");
    if (!zone) return;
    var toast = document.createElement("div");
    toast.className = "toast t-" + (kind === "late" ? "late" : "ok");
    var message = document.createElement("span");
    message.textContent = text;
    var close = document.createElement("button");
    close.type = "button";
    close.setAttribute("data-close-toast", "");
    close.textContent = "Fermer";
    toast.append(message, close);
    zone.append(toast);
    dismissLater(toast);
  }

  document.addEventListener("toast", function (event) {
    var detail = event.detail || {};
    if (detail.text) showToast(detail.kind, detail.text);
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-close-toast]");
    if (button) button.closest(".toast").remove();
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("#toasts .toast").forEach(dismissLater);
  });
})();
