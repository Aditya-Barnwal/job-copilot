// Runs before first paint: apply the saved theme, else follow the system setting.
(function () {
  var t = null;
  try { t = localStorage.getItem("jc-theme"); } catch (e) { /* storage blocked */ }
  if (t !== "light" && t !== "dark") {
    t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
