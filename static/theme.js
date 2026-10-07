// Sayfa çizilmeden önce kayıtlı temayı uygular (beyaz ekran yanıp sönmesin diye ayrı ve erken yüklenir).
(function () {
  var theme = null;
  try { theme = localStorage.getItem("theme"); } catch (e) { /* gizli pencere vb. */ }
  if (!theme) theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", theme);
})();
