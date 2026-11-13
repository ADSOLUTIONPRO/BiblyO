// SPDX-License-Identifier: EUPL-1.2
// Bascule du thème : le choix est gardé dans un cookie, lu par le serveur pour éviter tout flash.
document.addEventListener("alpine:init", function () {
  Alpine.data("themeToggle", function () {
    return {
      dark: document.documentElement.getAttribute("data-theme") === "dark",
      get label() { return this.dark ? "Thème clair" : "Thème sombre"; },
      get showSun() { return this.dark; },
      get showMoon() { return !this.dark; },
      toggle: function () {
        this.dark = !this.dark;
        var value = this.dark ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", value);
        var secure = window.location.protocol === "https:" ? "; Secure" : "";
        document.cookie = "biblyo_theme=" + value + "; Path=/; Max-Age=31536000; SameSite=Lax" + secure;
      },
    };
  });
});
