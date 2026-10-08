/*
 * Doküman Asistanı — arayüz mantığı.
 * Güvenlik notu: sunucudan veya kullanıcıdan gelen hiçbir metin HTML olarak eklenmez;
 * ya textContent ile yazılır ya da önce kaçışlanır (escapeHtml). Oturum bilgisi HttpOnly
 * çerezde durur, JavaScript tarafından okunamaz.
 */
"use strict";

const state = {
  user: null,
  health: null,
  authMode: "login",       // login | register | setup
  chats: [],
  currentChatId: null,
  mode: "auto",
  streaming: false,
  abort: null,
  docs: [],
  datasets: [],
  docPoll: null,
  healthPoll: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------- Yardımcılar ---------------- */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message, type) {
  const node = el("div", { class: "toast" + (type === "error" ? " error" : ""), text: message });
  $("toast-area").appendChild(node);
  setTimeout(() => node.remove(), type === "error" ? 6000 : 3000);
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString("tr-TR", { day: "numeric", month: "short", year: "numeric" });
  } catch (e) { return ""; }
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("Sunucuya ulaşılamıyor. Uygulamanın çalıştığından emin olun.", 0);
  }
  if (response.status === 401 && state.user && !path.startsWith("/api/auth/")) {
    sessionExpired();
    throw new ApiError("Oturumunuz sona erdi, lütfen tekrar giriş yapın.", 401);
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* gövde yok */ }
  if (!response.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = "Girilen bilgiler geçersiz.";
    throw new ApiError(detail || "Bir hata oluştu (" + response.status + ").", response.status);
  }
  return data;
}

/* ---------------- Başlangıç ve giriş ---------------- */

async function init() {
  bindEvents();
  updateThemeButton();
  try { state.health = await api("/api/health"); } catch (e) { state.health = null; }
  try {
    state.user = await api("/api/auth/me");
    enterApp();
  } catch (e) {
    showAuth(state.health && state.health.setup_required ? "setup" : "login");
  }
}

function showAuth(mode) {
  state.authMode = mode;
  $("app").hidden = true;
  $("auth-screen").hidden = false;
  $("auth-error").hidden = true;
  const registrationOpen = state.health && state.health.registration_open;
  const texts = {
    login: ["Giriş yap", "Devam etmek için hesabınızla giriş yapın.", "Giriş yap"],
    register: ["Hesap oluştur", "Yeni bir hesap oluşturun.", "Kayıt ol"],
    setup: ["Kurulum", "Hoş geldiniz! İlk olarak yönetici hesabını oluşturun. Diğer kullanıcıları daha sonra siz ekleyeceksiniz.", "Yönetici hesabını oluştur"],
  }[mode];
  $("auth-title").textContent = texts[0];
  $("auth-lead").textContent = texts[1];
  $("auth-submit").textContent = texts[2];
  $("field-name").hidden = mode === "login";
  $("auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  const canSwitch = mode !== "setup" && registrationOpen;
  $("auth-switch").hidden = !canSwitch;
  $("auth-switch-text").textContent = mode === "login" ? "Hesabınız yok mu?" : "Zaten hesabınız var mı?";
  $("auth-switch-btn").textContent = mode === "login" ? "Kayıt ol" : "Giriş yap";
  (mode === "login" ? $("auth-email") : $("auth-name")).focus();
}

async function submitAuth(event) {
  event.preventDefault();
  const button = $("auth-submit");
  const body = { email: $("auth-email").value.trim(), password: $("auth-password").value };
  if (state.authMode !== "login") body.name = $("auth-name").value.trim();
  button.disabled = true;
  try {
    const path = state.authMode === "login" ? "/api/auth/login" : "/api/auth/register";
    state.user = await api(path, { method: "POST", body });
    $("auth-password").value = "";
    try { state.health = await api("/api/health"); } catch (e) { /* önemsiz */ }
    enterApp();
  } catch (e) {
    $("auth-error").textContent = e.message;
    $("auth-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

function sessionExpired() {
  stopStreaming();
  state.user = null;
  clearInterval(state.docPoll);
  clearInterval(state.healthPoll);
  closeAll();
  showAuth("login");
  $("auth-error").textContent = "Oturumunuz sona erdi, lütfen tekrar giriş yapın.";
  $("auth-error").hidden = false;
}

async function logout() {
  stopStreaming();
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* yine de çık */ }
  state.user = null;
  state.currentChatId = null;
  clearInterval(state.docPoll);
  clearInterval(state.healthPoll);
  closeAll();
  showAuth("login");
}

function enterApp() {
  $("auth-screen").hidden = true;
  $("app").hidden = false;
  const user = state.user;
  $("user-name").textContent = user.name;
  $("user-email").textContent = user.email;
  $("user-avatar").textContent = user.name.split(/\s+/).map((p) => p[0] || "").join("").slice(0, 2).toUpperCase();
  $("open-admin-btn").hidden = !user.is_admin;
  $("share-option").hidden = !user.is_admin;
  newChat();
  loadChats();
  loadSources();
  renderHealthBanner();
  clearInterval(state.healthPoll);
  state.healthPoll = setInterval(refreshHealth, 30000);
}

async function refreshHealth() {
  try { state.health = await api("/api/health"); } catch (e) { return; }
  renderHealthBanner();
}

function renderHealthBanner() {
  const banner = $("status-banner");
  const h = state.health;
  banner.replaceChildren();
  if (!h) { banner.hidden = true; return; }
  let node = null;
  if (!h.ollama) {
    node = el("div", { class: "alert alert-warn" },
      el("strong", { text: "Yapay zekâ motoru (Ollama) çalışmıyor. " }),
      "Ollama uygulamasını açın; birkaç saniye içinde bu uyarı kendiliğinden kaybolur.");
  } else {
    const missing = [];
    if (!h.chat_model_ready) missing.push(h.chat_model);
    if (!h.embed_model_ready) missing.push(h.embed_model);
    if (missing.length) {
      node = el("div", { class: "alert alert-warn" },
        el("strong", { text: "Eksik model: " }), "Terminalde şunu çalıştırın: ",
        ...missing.map((m, i) => [i ? " ve " : "", el("code", { text: "ollama pull " + m })]));
    }
  }
  if (node) banner.appendChild(node);
  banner.hidden = !node;
}

/* ---------------- Sohbet listesi ---------------- */

async function loadChats() {
  try {
    state.chats = await api("/api/chats");
  } catch (e) { return; }
  renderChatList();
}

function renderChatList() {
  const list = $("chat-list");
  list.replaceChildren();
  if (!state.chats.length) {
    list.appendChild(el("div", { class: "empty-hint", text: "Henüz sohbet yok." }));
    return;
  }
  for (const chat of state.chats) {
    const item = el("div", { class: "chat-item" + (chat.id === state.currentChatId ? " active" : "") },
      el("button", { class: "chat-title", text: chat.title, title: chat.title, onclick: () => selectChat(chat.id) }),
      el("span", { class: "chat-actions" },
        el("button", { class: "icon-btn", title: "Yeniden adlandır", "aria-label": "Yeniden adlandır", onclick: () => renameChat(chat) }, icon("edit")),
        el("button", { class: "icon-btn", title: "Sil", "aria-label": "Sil", onclick: () => deleteChat(chat) }, icon("trash"))));
    list.appendChild(item);
  }
}

function setChatTitle(title) {
  $("chat-title").textContent = title || "Yeni sohbet";
}

async function renameChat(chat) {
  const title = window.prompt("Sohbetin yeni adı:", chat.title);
  if (!title || !title.trim()) return;
  try {
    await api("/api/chats/" + chat.id, { method: "PATCH", body: { title: title.trim() } });
    chat.title = title.trim();
    renderChatList();
    if (chat.id === state.currentChatId) setChatTitle(chat.title);
  } catch (e) { toast(e.message, "error"); }
}

async function deleteChat(chat) {
  if (!window.confirm("“" + chat.title + "” sohbeti silinsin mi? Bu işlem geri alınamaz.")) return;
  try {
    await api("/api/chats/" + chat.id, { method: "DELETE" });
    state.chats = state.chats.filter((c) => c.id !== chat.id);
    if (chat.id === state.currentChatId) newChat();
    renderChatList();
  } catch (e) { toast(e.message, "error"); }
}

function newChat() {
  stopStreaming();
  state.currentChatId = null;
  setChatTitle("Yeni sohbet");
  renderWelcome();
  renderChatList();
  closeSidebar();
  $("question").focus();
}

async function selectChat(chatId) {
  stopStreaming();
  state.currentChatId = chatId;
  const chat = state.chats.find((c) => c.id === chatId);
  setChatTitle(chat ? chat.title : "");
  renderChatList();
  closeSidebar();
  const inner = $("messages-inner");
  inner.replaceChildren(el("div", { class: "empty-hint", text: "Yükleniyor…" }));
  let messages;
  try {
    messages = await api("/api/chats/" + chatId + "/messages");
  } catch (e) {
    if (state.currentChatId === chatId) inner.replaceChildren(el("div", { class: "alert alert-error", text: e.message }));
    return;
  }
  if (state.currentChatId !== chatId) return; // bu arada başka sohbete geçildi
  inner.replaceChildren();
  for (const message of messages) {
    if (message.role === "user") inner.appendChild(userMessage(message.content));
    else {
      const view = botMessage();
      finishBotMessage(view, message);
      inner.appendChild(view.root);
    }
  }
  scrollToBottom(true);
}

/* ---------------- Mesaj görünümü ---------------- */

function renderWelcome() {
  const hasSources = state.docs.some((d) => d.status === "ready") || state.datasets.length > 0;
  const card = (iconName, title, text, action) => el("button", { class: "welcome-card", onclick: action },
    el("strong", {}, icon(iconName), title), el("span", { text }));
  const welcome = el("div", { class: "welcome" },
    el("span", { class: "brand-logo" }, icon("logo")),
    el("h2", { text: "Merhaba " + (state.user ? state.user.name.split(" ")[0] : "") + ", nasıl yardımcı olabilirim?" }),
    el("p", { text: hasSources
      ? "Yüklenen dokümanlara ve veri tablolarına dayanarak soruları cevaplarım ve her bilginin kaynağını gösteririm."
      : "Başlamak için bir doküman veya veri dosyası yükleyin. Her şey bu bilgisayarda kalır." }),
    el("div", { class: "welcome-cards" },
      card("library", "Doküman yükle", "PDF, Word veya metin dosyası", () => openSources("docs")),
      card("table", "Veri yükle", "Excel veya CSV ile analiz ve grafik", () => openSources("data")),
      hasSources ? card("logo", "Örnek soru", "“Bu dokümanın ana konuları neler?”",
        () => { $("question").value = "Bu dokümanın ana konuları neler?"; onQuestionInput(); $("question").focus(); }) : null));
  $("messages-inner").replaceChildren(welcome);
}

function userMessage(text) {
  return el("div", { class: "msg msg-user" }, el("div", { class: "bubble", text }));
}

function botMessage() {
  const route = el("div", { class: "route" });
  const content = el("div", { class: "content" });
  const extras = el("div", {});
  const tools = el("div", { class: "msg-tools" });
  const body = el("div", { class: "bot-body" }, route, content, extras, tools);
  const root = el("div", { class: "msg msg-bot" }, el("span", { class: "bot-avatar" }, icon("logo")), body);
  return { root, route, content, extras, tools, text: "", sources: [] };
}

function routeBadge(mode) {
  return mode === "data"
    ? el("span", { class: "route-badge", title: "Cevap, tablolarınızda çalıştırılan bir SQL sorgusuyla hesaplandı" }, icon("table"), "Veri tablosu · SQL")
    : el("span", { class: "route-badge", title: "Cevap, dokümanlarınızda yapılan aramayla bulundu" }, icon("library"), "Dokümanlar");
}

function showTyping(view, label) {
  view.content.replaceChildren(el("span", { class: "typing" }, el("span"), el("span"), el("span")),
    el("span", { class: "typing-label", text: label || "" }));
}

/* Güvenli, küçük bir Markdown çevirici: önce her şeyi kaçışlar, sonra sınırlı biçimlendirme ekler. */
function renderMarkdown(text, sourceCount) {
  const blocks = String(text || "").split(/```/);
  let html = "";
  blocks.forEach((block, index) => {
    if (index % 2 === 1) {
      html += "<pre><code>" + escapeHtml(block.replace(/^[a-z]*\n/i, "")) + "</code></pre>";
      return;
    }
    let listType = null;
    let paragraph = [];
    const flushParagraph = () => {
      if (paragraph.length) html += "<p>" + paragraph.join("<br>") + "</p>";
      paragraph = [];
    };
    const closeList = () => { if (listType) { html += "</" + listType + ">"; listType = null; } };
    for (const rawLine of block.split("\n")) {
      const line = rawLine.trimEnd();
      let m;
      if (!line.trim()) { flushParagraph(); closeList(); continue; }
      if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
        flushParagraph(); closeList();
        html += "<h4>" + inline(m[1], sourceCount) + "</h4>";
      } else if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
        flushParagraph();
        if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
        html += "<li>" + inline(m[1], sourceCount) + "</li>";
      } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushParagraph();
        if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
        html += "<li>" + inline(m[1], sourceCount) + "</li>";
      } else {
        closeList();
        paragraph.push(inline(line, sourceCount));
      }
    }
    flushParagraph(); closeList();
  });
  return html;
}

function inline(text, sourceCount) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  out = out.replace(/\[(\d{1,2})\]/g, (match, n) => {
    const num = Number(n);
    if (sourceCount && num >= 1 && num <= sourceCount) {
      return '<button type="button" class="cite" data-cite="' + num + '" title="Kaynağı göster">' + num + "</button>";
    }
    return '<span class="cite">' + num + "</span>";
  });
  return out;
}

function renderContent(view, text, sourceCount, streaming) {
  view.content.innerHTML = renderMarkdown(text, sourceCount);
  view.content.classList.toggle("cursor", Boolean(streaming));
}

function finishBotMessage(view, message) {
  view.text = message.content || "";
  view.sources = message.sources || [];
  view.root.classList.toggle("msg-error", Boolean(message.error));
  renderContent(view, view.text, view.sources.length, false);
  view.extras.replaceChildren();
  view.route.replaceChildren();
  if (message.mode && !message.error) view.route.appendChild(routeBadge(message.mode));

  const cited = view.sources.filter((s) => s.cited);
  if (cited.length) {
    const wrap = el("div", { class: "sources" }, el("div", { class: "sources-label", text: "Kaynaklar" }));
    for (const source of cited) {
      wrap.appendChild(el("button", { class: "source-chip", onclick: () => openSourceModal(source) },
        el("span", { class: "num", text: source.number }),
        el("span", { class: "name", text: source.filename }),
        source.page ? el("span", { class: "page", text: "s. " + source.page }) : null));
    }
    view.extras.appendChild(wrap);
  }
  if (message.table) view.extras.appendChild(tableCard(message.table));
  if (message.chart) view.extras.appendChild(chartCard(message.chart));
  if (message.sql) {
    view.extras.appendChild(el("details", { class: "sql" }, el("summary", { text: "Kullanılan SQL" }), el("pre", { text: message.sql })));
  }
  if (message.suggestions && message.suggestions.length) {
    view.extras.appendChild(el("div", { class: "suggestions" },
      message.suggestions.map((s) => el("button", { class: "suggestion", text: s, onclick: () => sendQuestion(s) }))));
  }
  view.tools.replaceChildren();
  if (view.text && !message.error) {
    view.tools.appendChild(el("button", { class: "icon-btn", title: "Cevabı kopyala", "aria-label": "Cevabı kopyala",
      onclick: () => copyText(view.text.replace(/\s?\[\d{1,2}\]/g, "")) }, icon("copy")));
  }
  view.root._sources = view.sources;
}

function openSourceModal(source) {
  $("source-modal-title").textContent = "[" + source.number + "] " + source.filename + (source.page ? " · sayfa " + source.page : "");
  $("source-modal-text").textContent = source.text;
  openModal("source-modal");
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Panoya kopyalandı.");
  } catch (e) {
    toast("Kopyalanamadı. Tarayıcı izin vermedi.", "error");
  }
}

function tableCard(table) {
  const numeric = table.columns.map((_, i) => table.rows.length > 0 && table.rows.every((r) => r[i] === null || typeof r[i] === "number"));
  const head = el("tr", {}, table.columns.map((c, i) => el("th", { class: numeric[i] ? "num" : null, text: c })));
  const rows = table.rows.map((row) => el("tr", {}, row.map((v) =>
    el("td", { class: typeof v === "number" ? "num" : null, text: v === null ? "" : (typeof v === "number" ? v.toLocaleString("tr-TR") : String(v)) }))));
  const toTSV = () => [table.columns, ...table.rows].map((r) => r.map((v) => (v === null ? "" : String(v).replace(/[\t\n]/g, " "))).join("\t")).join("\n");
  const toCSV = () => [table.columns, ...table.rows].map((r) => r.map((v) => {
    const s = v === null ? "" : String(v);
    return /[";\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(";")).join("\n");
  return el("div", { class: "card" },
    el("div", { class: "card-head" }, icon("table"),
      el("span", { class: "title", text: "Sonuç (" + table.row_count + " satır)" }),
      el("button", { class: "btn btn-sm", onclick: () => copyText(toTSV()) }, icon("copy"), "Kopyala"),
      el("button", { class: "btn btn-sm", onclick: () => downloadBlob("﻿" + toCSV(), "sonuc.csv", "text/csv;charset=utf-8") }, icon("download"), "CSV")),
    el("div", { class: "table-wrap" }, el("table", { class: "data" }, el("thead", {}, head), el("tbody", {}, rows))));
}

function downloadBlob(content, filename, type) {
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type }));
  const a = el("a", { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const CHART_COLORS = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#84cc16", "#f97316", "#64748b", "#a855f7"];

function chartCard(chart) {
  const canvas = el("canvas", {});
  const card = el("div", { class: "card" },
    el("div", { class: "card-head" }, icon("chart"), el("span", { class: "title", text: "Grafik" }),
      el("button", { class: "btn btn-sm", onclick: () => canvas.toBlob((b) => b && downloadBlob(b, "grafik.png")) }, icon("download"), "PNG")),
    el("div", { class: "chart-wrap" }, canvas));
  if (typeof window.Chart === "undefined") {
    card.querySelector(".chart-wrap").replaceChildren(el("div", { class: "empty-hint", text: "Grafik kütüphanesi yüklenemedi." }));
    return card;
  }
  requestAnimationFrame(() => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    const textColor = dark ? "#c7c7d1" : "#4b4b57";
    const gridColor = dark ? "rgba(255,255,255,.08)" : "rgba(0,0,0,.06)";
    const pie = chart.style === "pie";
    new window.Chart(canvas, {
      type: chart.style,
      data: {
        labels: chart.x_labels,
        datasets: chart.series.map((s, i) => ({
          label: s.name,
          data: s.values,
          backgroundColor: pie ? CHART_COLORS : CHART_COLORS[i % CHART_COLORS.length],
          borderColor: pie ? (dark ? "#1a1a20" : "#fff") : CHART_COLORS[i % CHART_COLORS.length],
          borderWidth: pie ? 2 : (chart.style === "line" ? 2 : 0),
          borderRadius: chart.style === "bar" ? 6 : 0,
          tension: 0.3,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: pie || chart.series.length > 1, labels: { color: textColor } } },
        scales: pie ? {} : {
          x: { ticks: { color: textColor }, grid: { display: false } },
          y: { ticks: { color: textColor }, grid: { color: gridColor }, beginAtZero: true },
        },
      },
    });
  });
  return card;
}

function scrollToBottom(force) {
  const box = $("messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

/* ---------------- Soru gönderme (akan cevap) ---------------- */

function onQuestionInput() {
  const box = $("question");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 200) + "px";
  if (!state.streaming) $("send-btn").disabled = !box.value.trim();
}

function setStreaming(on) {
  state.streaming = on;
  const button = $("send-btn");
  button.classList.toggle("stop", on);
  button.setAttribute("aria-label", on ? "Durdur" : "Gönder");
  $("send-icon").setAttribute("href", on ? "#i-stop" : "#i-send");
  button.disabled = on ? false : !$("question").value.trim();
}

function stopStreaming() {
  if (state.abort) state.abort.abort();
  state.abort = null;
  if (state.streaming) setStreaming(false);
}

async function sendQuestion(text) {
  const question = (text || "").trim();
  if (!question || state.streaming) return;
  $("question").value = "";
  onQuestionInput();

  const inner = $("messages-inner");
  if (inner.querySelector(".welcome")) inner.replaceChildren();
  inner.appendChild(userMessage(question));
  const view = botMessage();
  inner.appendChild(view.root);
  showTyping(view, { data: "SQL sorgusu hazırlanıyor…", docs: "Dokümanlarda aranıyor…" }[state.mode] || "Soru inceleniyor…");
  scrollToBottom(true);

  const controller = new AbortController();
  state.abort = controller;
  setStreaming(true);
  const askedChatId = state.currentChatId;
  let chatId = askedChatId;
  let pending = false;
  let finished = false;

  const flush = () => {
    pending = false;
    if (!finished) { renderContent(view, view.text, null, true); scrollToBottom(false); }
  };

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, chat_id: askedChatId, mode: state.mode }),
      signal: controller.signal,
    });
    if (response.status === 401) { sessionExpired(); return; }
    if (!response.ok) {
      let detail = "Bir hata oluştu.";
      try { detail = (await response.json()).detail || detail; } catch (e) { /* yok */ }
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.type === "meta") {
          chatId = event.chat_id;
          if (state.currentChatId === askedChatId) {
            state.currentChatId = chatId;
            if (askedChatId === null) loadChats().then(() => {
              const chat = state.chats.find((c) => c.id === chatId);
              if (chat && state.currentChatId === chatId) setChatTitle(chat.title);
            });
          }
        } else if (event.type === "route") {
          view.route.replaceChildren(routeBadge(event.mode));
          if (!view.text) showTyping(view, event.mode === "data" ? "SQL sorgusu hazırlanıyor ve çalıştırılıyor…" : "Dokümanlarda aranıyor…");
        } else if (event.type === "token") {
          view.text += event.text;
          if (!pending) { pending = true; requestAnimationFrame(flush); }
        } else if (event.type === "done") {
          finished = true;
          finishBotMessage(view, event.message);
        } else if (event.type === "error") {
          finished = true;
          finishBotMessage(view, { content: "⚠️ " + event.message, error: true });
        }
      }
    }
    if (!finished) finishBotMessage(view, { content: view.text || "Cevap alınamadı.", error: !view.text });
  } catch (e) {
    finished = true;
    if (e.name === "AbortError") {
      finishBotMessage(view, { content: (view.text ? view.text + "\n\n" : "") + "*(durduruldu)*" });
    } else {
      finishBotMessage(view, { content: "⚠️ " + (e.message || "Bağlantı hatası, tekrar deneyin."), error: true });
    }
  } finally {
    if (state.abort === controller) { state.abort = null; setStreaming(false); }
    scrollToBottom(false);
    await loadChats();
    if (state.currentChatId === chatId) {
      const chat = state.chats.find((c) => c.id === chatId);
      if (chat) setChatTitle(chat.title);
    }
  }
}

/* ---------------- Kaynaklar (dokümanlar ve tablolar) ---------------- */

async function loadSources() {
  try {
    [state.docs, state.datasets] = await Promise.all([api("/api/documents"), api("/api/datasets")]);
  } catch (e) { return; }
  renderSources();
  const processing = state.docs.some((d) => d.status === "processing");
  clearInterval(state.docPoll);
  if (processing) state.docPoll = setInterval(loadSources, 2000);
}

function renderSources() {
  const ready = state.docs.filter((d) => d.status === "ready").length + state.datasets.length;
  $("sources-count").textContent = ready;
  if ($("messages-inner").querySelector(".welcome")) renderWelcome();

  const docList = $("doc-list");
  docList.replaceChildren();
  if (!state.docs.length) docList.appendChild(el("li", { class: "empty-hint", text: "Henüz doküman yok." }));
  for (const doc of state.docs) {
    const status = {
      ready: el("span", { class: "badge badge-ok", text: "Hazır" }),
      processing: el("span", { class: "badge badge-warn" }, el("span", { class: "spinner" }), "İşleniyor"),
      error: el("span", { class: "badge badge-danger", text: "Hata" }),
    }[doc.status];
    const meta = [status];
    if (doc.shared) meta.push(el("span", { class: "badge badge-accent", text: "Şirket" }));
    if (doc.page_count) meta.push(el("span", { text: doc.page_count + " sayfa" }));
    meta.push(el("span", { text: formatSize(doc.size_bytes) }));
    meta.push(el("span", { text: formatDate(doc.created_at) }));
    if (doc.shared && doc.owner_name) meta.push(el("span", { text: "· " + doc.owner_name }));
    docList.appendChild(el("li", { class: "item" },
      el("span", { class: "file-icon" }, icon("logo")),
      el("div", { class: "info" },
        el("div", { class: "name", text: doc.filename }),
        el("div", { class: "meta" }, meta),
        doc.error ? el("div", { class: "err", text: doc.error }) : null),
      doc.can_delete ? el("button", { class: "icon-btn", title: "Sil", "aria-label": "Sil", onclick: () => deleteDocument(doc) }, icon("trash")) : null));
  }

  const dataList = $("data-list");
  dataList.replaceChildren();
  if (!state.datasets.length) dataList.appendChild(el("li", { class: "empty-hint", text: "Henüz veri tablosu yok." }));
  for (const ds of state.datasets) {
    dataList.appendChild(el("li", { class: "item" },
      el("span", { class: "file-icon" }, icon("table")),
      el("div", { class: "info" },
        el("div", { class: "name", text: ds.table_name }),
        el("div", { class: "meta" },
          el("span", { text: ds.filename }), el("span", { text: "· " + ds.row_count + " satır" }),
          el("span", { text: "· " + ds.columns.length + " kolon" })),
        el("div", { class: "meta", text: ds.columns.slice(0, 8).join(", ") + (ds.columns.length > 8 ? "…" : "") })),
      el("button", { class: "icon-btn", title: "Sil", "aria-label": "Sil", onclick: () => deleteDataset(ds) }, icon("trash"))));
  }
}

async function uploadDocuments(files) {
  const status = $("doc-upload-status");
  for (const file of files) {
    status.replaceChildren(el("span", { class: "spinner" }), " " + file.name + " yükleniyor…");
    const form = new FormData();
    form.append("file", file);
    form.append("shared", state.user.is_admin && $("doc-shared").checked ? "true" : "false");
    try {
      await api("/api/documents", { method: "POST", body: form });
    } catch (e) {
      toast(file.name + ": " + e.message, "error");
    }
  }
  status.replaceChildren();
  $("doc-file").value = "";
  loadSources();
}

async function uploadDataset(file) {
  const status = $("data-upload-status");
  status.replaceChildren(el("span", { class: "spinner" }), " " + file.name + " yükleniyor…");
  try {
    const tables = await api("/api/datasets", { method: "POST", body: (() => { const f = new FormData(); f.append("file", file); return f; })() });
    toast(tables.map((t) => t.table_name + " (" + t.row_count + " satır)").join(", ") + " hazır.");
  } catch (e) {
    toast(e.message, "error");
  }
  status.replaceChildren();
  $("data-file").value = "";
  loadSources();
}

async function deleteDocument(doc) {
  if (!window.confirm("“" + doc.filename + "” silinsin mi?")) return;
  try {
    await api("/api/documents/" + doc.id, { method: "DELETE" });
    loadSources();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteDataset(ds) {
  if (!window.confirm("“" + ds.table_name + "” tablosu silinsin mi?")) return;
  try {
    await api("/api/datasets/" + ds.id, { method: "DELETE" });
    loadSources();
  } catch (e) { toast(e.message, "error"); }
}

function setupDropzone(zoneId, inputId, onFiles) {
  const zone = $(zoneId);
  const input = $(inputId);
  input.addEventListener("change", () => input.files.length && onFiles(Array.from(input.files)));
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    if (e.dataTransfer.files.length) onFiles(Array.from(e.dataTransfer.files));
  });
}

/* ---------------- Çekmece, pencereler, tema ---------------- */

function openSources(tab) {
  switchTab(tab || "docs");
  $("sources-drawer").hidden = false;
  $("drawer-overlay").hidden = false;
  closeSidebar();
  loadSources();
}

function closeSources() {
  $("sources-drawer").hidden = true;
  $("drawer-overlay").hidden = true;
}

function switchTab(tab) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("tab-docs").hidden = tab !== "docs";
  $("tab-data").hidden = tab !== "data";
}

function openModal(id) {
  document.querySelectorAll(".modal").forEach((m) => { m.hidden = m.id !== id; });
  $("modal-overlay").hidden = false;
}

function closeModals() {
  document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; });
  $("modal-overlay").hidden = true;
}

function closeAll() {
  closeModals();
  closeSources();
  closeSidebar();
}

function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebar-overlay").hidden = true;
}

function toggleTheme() {
  const dark = document.documentElement.getAttribute("data-theme") !== "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* önemsiz */ }
  updateThemeButton();
}

function updateThemeButton() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  $("theme-icon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  $("theme-label").textContent = dark ? "Açık tema" : "Koyu tema";
}

async function submitPassword(event) {
  event.preventDefault();
  try {
    await api("/api/auth/password", { method: "POST", body: { old_password: $("old-password").value, new_password: $("new-password").value } });
    $("old-password").value = "";
    $("new-password").value = "";
    closeModals();
    toast("Şifreniz değiştirildi. Diğer cihazlardaki oturumlar kapatıldı.");
  } catch (e) {
    $("password-error").textContent = e.message;
    $("password-error").hidden = false;
  }
}

/* ---------------- Yönetim ---------------- */

async function openAdmin() {
  closeSidebar();
  $("admin-error").hidden = true;
  openModal("admin-modal");
  await loadUsers();
}

function adminError(message) {
  $("admin-error").textContent = message;
  $("admin-error").hidden = false;
}

async function loadUsers() {
  let users;
  try { users = await api("/api/admin/users"); } catch (e) { adminError(e.message); return; }
  const tbody = $("users-tbody");
  tbody.replaceChildren();
  for (const u of users) {
    const self = u.id === state.user.id;
    tbody.appendChild(el("tr", {},
      el("td", { text: u.name + (self ? " (siz)" : "") }),
      el("td", { text: u.email }),
      el("td", {}, el("span", { class: "badge " + (u.is_admin ? "badge-accent" : ""), text: u.is_admin ? "Yönetici" : "Kullanıcı" })),
      el("td", {}, el("div", { class: "actions" },
        self ? null : el("button", { class: "btn btn-sm", text: u.is_admin ? "Yetkiyi al" : "Yönetici yap",
          onclick: () => updateUser(u.id, { is_admin: !u.is_admin }) }),
        el("button", { class: "btn btn-sm", text: "Şifre sıfırla", onclick: () => resetPassword(u) }),
        self ? null : el("button", { class: "btn btn-sm btn-danger", text: "Sil", onclick: () => removeUser(u) })))));
  }
}

async function updateUser(id, body) {
  try {
    await api("/api/admin/users/" + id, { method: "PATCH", body });
    $("admin-error").hidden = true;
    loadUsers();
  } catch (e) { adminError(e.message); }
}

async function resetPassword(user) {
  const password = window.prompt(user.name + " için yeni şifre (en az 8 karakter):");
  if (!password) return;
  await updateUser(user.id, { password });
  toast("Şifre güncellendi.");
}

async function removeUser(user) {
  if (!window.confirm(user.name + " ve tüm sohbetleri, dokümanları ve tabloları silinsin mi? Bu işlem geri alınamaz.")) return;
  try {
    await api("/api/admin/users/" + user.id, { method: "DELETE" });
    loadUsers();
  } catch (e) { adminError(e.message); }
}

async function submitNewUser(event) {
  event.preventDefault();
  const body = {
    name: $("nu-name").value.trim(),
    email: $("nu-email").value.trim(),
    password: $("nu-password").value,
    is_admin: $("nu-admin").checked,
  };
  try {
    await api("/api/admin/users", { method: "POST", body });
    ["nu-name", "nu-email", "nu-password"].forEach((id) => { $(id).value = ""; });
    $("nu-admin").checked = false;
    $("admin-error").hidden = true;
    toast(body.name + " eklendi. Geçici şifreyi kendisine iletin.");
    loadUsers();
  } catch (e) { adminError(e.message); }
}

/* ---------------- Olay bağlama ---------------- */

function bindEvents() {
  $("auth-form").addEventListener("submit", submitAuth);
  $("auth-switch-btn").addEventListener("click", () => showAuth(state.authMode === "login" ? "register" : "login"));
  $("logout-btn").addEventListener("click", logout);
  $("new-chat-btn").addEventListener("click", newChat);
  $("theme-btn").addEventListener("click", toggleTheme);
  $("open-sources-btn").addEventListener("click", () => openSources("docs"));
  $("open-admin-btn").addEventListener("click", openAdmin);
  $("open-password-btn").addEventListener("click", () => { closeSidebar(); $("password-error").hidden = true; openModal("password-modal"); });
  $("password-form").addEventListener("submit", submitPassword);
  $("new-user-form").addEventListener("submit", submitNewUser);
  $("drawer-overlay").addEventListener("click", closeSources);
  $("modal-overlay").addEventListener("click", closeModals);
  document.querySelectorAll("[data-close='sources']").forEach((b) => b.addEventListener("click", closeSources));
  document.querySelectorAll("[data-close='modal']").forEach((b) => b.addEventListener("click", closeModals));
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModals(); closeSources(); closeSidebar(); } });

  $("menu-btn").addEventListener("click", () => { $("sidebar").classList.add("open"); $("sidebar-overlay").hidden = false; });
  $("sidebar-overlay").addEventListener("click", closeSidebar);

  document.querySelectorAll("#mode-switch button").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    document.querySelectorAll("#mode-switch button").forEach((x) => x.classList.toggle("active", x === b));
  }));

  const question = $("question");
  question.addEventListener("input", onQuestionInput);
  question.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!state.streaming) sendQuestion(question.value);
    }
  });
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.streaming) stopStreaming();
    else sendQuestion(question.value);
  });

  // Cevap içindeki [1] gibi atıflara tıklanınca ilgili kaynağı göster.
  $("messages").addEventListener("click", (e) => {
    const cite = e.target.closest("button.cite");
    if (!cite) return;
    const message = cite.closest(".msg-bot");
    const sources = (message && message._sources) || [];
    const source = sources.find((s) => s.number === Number(cite.dataset.cite));
    if (source) openSourceModal(source);
  });

  setupDropzone("doc-dropzone", "doc-file", uploadDocuments);
  setupDropzone("data-dropzone", "data-file", (files) => uploadDataset(files[0]));
}

init();
