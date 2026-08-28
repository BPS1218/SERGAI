/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * ✅ FINAL: Google login + sidebar riwayat (Google Sheets) + Shortcut Ctrl+B
 */

import { CONFIG } from "./config.js";
import { asksergAI, formatBotResponse } from "./api.js";

// ===== DOM Elements =====
const chatBox = document.getElementById("chatBox");
const userInput = document.getElementById("userInput");
const typingIndicator = document.getElementById("typingIndicator");
const emptyState = document.getElementById("emptyState");
const sendBtn = document.getElementById("sendBtn");
const infoBtn = document.getElementById("infoBtn");
const clearBtn = document.getElementById("clearBtn");
const infoModal = document.getElementById("infoModal");
const closeModalBtn = document.getElementById("closeModalBtn");
const sidebar = document.getElementById("sidebar");
const sidebarOpen = document.getElementById("sidebarOpen");
const sidebarClose = document.getElementById("sidebarClose"); // ✅ Ikon tutup di kiri bawah
const newChatBtn = document.getElementById("newChatBtn");
const logoutBtn = document.getElementById("logoutBtn");
const sessionList = document.getElementById("sessionList");

// ===== State =====
let chatHistory = [];
let userInfo = null;
let currentSessionId = null;
let isWaitingResponse = false;

// ===== GOOGLE SHEETS HELPER =====
function sheetPost(payload) {
  return fetch(CONFIG.googleScriptUrl, {
    method: "POST",
    body: JSON.stringify(payload),
  })
    .then((r) => r.json())
    .catch((e) => console.warn("Sheet error:", e));
}

function logMessage(role, content, model = "") {
  if (!userInfo) return;
  sheetPost({
    action: "save_message",
    session_id: currentSessionId,
    email: userInfo.email,
    name: userInfo.name,
    unit: userInfo.unit,
    role,
    content,
    model,
  });
}

// ===== SESSION MANAGEMENT =====
function getSessionId() {
  let sid = localStorage.getItem("sergai_session_id");
  if (!sid) {
    sid = (crypto.randomUUID && crypto.randomUUID()) || "s" + Date.now();
    localStorage.setItem("sergai_session_id", sid);
  }
  return sid;
}

function startNewSession() {
  const sid = (crypto.randomUUID && crypto.randomUUID()) || "s" + Date.now();
  localStorage.setItem("sergai_session_id", sid);
  currentSessionId = sid;
  chatHistory = [];
  chatBox.innerHTML = "";
  emptyState.style.display = "flex";
  chatBox.appendChild(emptyState);
  setTimeout(() => addBotMessage(CONFIG.branding.welcomeMessage, true), 300);
  // ✅ Sidebar TIDAK ditutup di sini — hanya ditutup lewat ikon kiri bawah / Ctrl+B
}

function loadSessions() {
  if (!userInfo) return;

  // ✅ 1) Tampilkan cache localStorage dulu (langsung terlihat)
  const cached = JSON.parse(
    localStorage.getItem("sergai_sessions_cache") || "null",
  );
  if (cached && cached.length) {
    renderSessionList(cached);
  } else if (sessionList) {
    // ✅ 2) Kalau belum ada cache, tampilkan spinner
    sessionList.innerHTML =
      '<div class="sb-loading"><div class="sb-spinner"></div>Memuat riwayat…</div>';
  }

  // ✅ 3) Fetch asli → update cache & render final
  sheetPost({ action: "list_sessions", email: userInfo.email }).then((res) => {
    if (res && res.ok) {
      const sessions = res.sessions || [];
      localStorage.setItem("sergai_sessions_cache", JSON.stringify(sessions));
      renderSessionList(sessions);
    }
  });
}

// ✅ Ubah URL dalam teks jadi link yang bisa diklik
function linkify(text) {
  const urlRegex = /(https?:\/\/[^\s<>"']+)/g;
  return text.replace(
    urlRegex,
    (url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer" class="msg-link">${url}</a>`,
  );
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function renderSessionList(sessions) {
  if (!sessionList) return;
  sessionList.innerHTML = "";
  if (!sessions.length) {
    sessionList.innerHTML = '<div class="sb-empty">Belum ada percakapan.</div>';
    return;
  }
  sessions.forEach((s) => {
    const item = document.createElement("div");
    item.className =
      "sb-item" + (s.session_id === currentSessionId ? " active" : "");
    const t = new Date(s.updated_at);
    const tstr =
      t.toLocaleDateString("id-ID", { day: "2-digit", month: "2-digit" }) +
      " " +
      t.toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" });
    item.innerHTML =
      '<i class="fas fa-comment-dots"></i>' +
      '<span class="sb-item-text">' +
      escapeHtml(s.title || "(Percakapan)") +
      "</span>" +
      '<span class="sb-item-time">' +
      tstr +
      "</span>" +
      '<button class="sb-item-del" title="Hapus sesi"><i class="fas fa-trash"></i></button>';
    item.addEventListener("click", () => openSession(s.session_id));
    item.querySelector(".sb-item-del").addEventListener("click", (e) => {
      e.stopPropagation();
      if (!confirm("Hapus percakapan ini dari riwayat?")) return;
      sheetPost({ action: "delete_session", session_id: s.session_id }).then(
        () => {
          if (s.session_id === currentSessionId) startNewSession();
          loadSessions();
        },
      );
    });
    sessionList.appendChild(item);
  });
}

function openSession(sid) {
  currentSessionId = sid;
  localStorage.setItem("sergai_session_id", sid);
  sheetPost({ action: "get_session", session_id: sid }).then((res) => {
    chatBox.innerHTML = "";
    chatHistory = [];
    const msgs = (res && res.messages) || [];
    if (!msgs.length) {
      emptyState.style.display = "flex";
      chatBox.appendChild(emptyState);
    } else {
      emptyState.style.display = "none";
      msgs.forEach((m) => appendMessage(m.content, m.role));
    }
    loadSessions(); // refresh highlight "active" di sidebar
    scrollToBottom();
    // ✅ Sidebar TIDAK ditutup di sini
  });
}

// ===== SIDEBAR USER & TOGGLE =====
function setupSidebarUser() {
  const n = document.getElementById("sbUserName");
  const u = document.getElementById("sbUserUnit");
  const a = document.getElementById("sbUserAva");
  if (n) n.textContent = userInfo.name;
  if (u) u.textContent = userInfo.unit;
  if (a && userInfo.picture)
    a.innerHTML = '<img src="' + userInfo.picture + '" alt="">';
}

function closeSidebar() {
  if (sidebar) sidebar.classList.remove("open");
}

function toggleSidebar() {
  if (sidebar) sidebar.classList.toggle("open");
}

// ===== INITIALIZATION =====
document.addEventListener("DOMContentLoaded", () => {
  userInfo = JSON.parse(localStorage.getItem("sergai_user") || "null");
  if (!userInfo || !userInfo.email) {
    window.location.href = "/";
    return;
  }

  currentSessionId = getSessionId();

  const statusText = document.getElementById("statusText");
  if (statusText) {
    statusText.textContent = `Halo, ${userInfo.name.split(" ")[0]}! Tanya Data, sergAI Jawab!`;
  }

  setupSidebarUser();
  loadChatState();
  loadSessions();

  setTimeout(() => addBotMessage(CONFIG.branding.welcomeMessage, true), 500);

  setupEventListeners();
  document.title = `${CONFIG.branding.name} - ${CONFIG.branding.tagline}`;
});

// ===== EVENT LISTENERS =====
function setupEventListeners() {
  userInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter" && !isWaitingResponse) sendMessage();
  });

  sendBtn?.addEventListener("click", () => {
    if (!isWaitingResponse) sendMessage();
  });

  infoBtn?.addEventListener("click", showInfo);
  clearBtn?.addEventListener("click", clearChat);
  closeModalBtn?.addEventListener("click", closeModal);
  infoModal?.addEventListener("click", (e) => {
    if (e.target === infoModal) closeModal();
  });
  userInput.addEventListener("input", autoResizeInput);

  // Sidebar Events
  newChatBtn?.addEventListener("click", startNewSession);
  sidebarOpen?.addEventListener("click", toggleSidebar);
  sidebarClose?.addEventListener("click", closeSidebar); // ✅ Klik ikon bawah saat sidebar terbuka

  logoutBtn?.addEventListener("click", () => {
    if (!confirm("Keluar dari akun Anda?")) return;
    localStorage.removeItem("sergai_user");
    localStorage.removeItem("sergai_session_id");
    window.location.href = "/";
  });

  // ✅ Shortcut Keyboard: Ctrl+B (atau Cmd+B di Mac) untuk toggle sidebar
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
      e.preventDefault(); // Mencegah browser mengeksekusi default bold
      toggleSidebar();
    }
  });
}

// ===== MESSAGE HANDLING =====
function sendMessage() {
  const text = userInput.value.trim();
  if (!text || isWaitingResponse) return;

  addUserMessage(text);
  logMessage("user", text);
  userInput.value = "";
  isWaitingResponse = true;
  sendBtn.disabled = true;
  showTyping();

  asksergAI(text, userInfo.email)
    .then((apiResult) => {
      console.log("🔍 TABLE PAYLOAD:", apiResult.table);
      const formatted = formatBotResponse(apiResult, text);
      addBotMessage(formatted.text, false, formatted.meta, apiResult.table);
      // ✅ Log model_used (gemini/openai) dari backend ke Google Sheets
      const modelUsed = apiResult.meta?.model_used || "";
      const wasFallback = apiResult.meta?.fallback ? " (fallback)" : "";
      logMessage("bot", formatted.text, modelUsed + wasFallback);
    })
    .catch((err) => {
      console.error("Unexpected error:", err);
      addBotMessage(CONFIG.fallback.apiError, false);
    })
    .finally(() => {
      hideTyping();
      isWaitingResponse = false;
      sendBtn.disabled = false;
      userInput.focus();
      saveChatState();
      loadSessions();
    });
}

function addUserMessage(text) {
  hideEmptyState();
  chatBox.appendChild(createMessageElement(text, "user"));
  scrollToBottom();
  chatHistory.push({
    role: "user",
    content: text,
    timestamp: new Date().toISOString(),
  });
}

function addBotMessage(
  text,
  showQuickReplies = false,
  meta = {},
  table = null,
) {
  hideEmptyState();
  chatBox.appendChild(createMessageElement(text, "bot", meta, table));
  if (showQuickReplies) showQuickReplyButtons();
  scrollToBottom();
  chatHistory.push({
    role: "bot",
    content: text,
    meta,
    timestamp: new Date().toISOString(),
  });
}

function appendMessage(text, sender) {
  hideEmptyState();
  chatBox.appendChild(createMessageElement(text, sender));
  chatHistory.push({
    role: sender,
    content: text,
    timestamp: new Date().toISOString(),
  });
}

// ===== UI HELPERS =====
function getUserAvatarHtml() {
  if (userInfo && userInfo.picture) {
    // ✅ Naikkan resolusi foto profil Google (default s96 → s200)
    const pic = userInfo.picture.replace(/=s\d+(?:-c)?$/i, "=s200-c");
    return '<img src="' + pic + '" alt="" />';
  }
  return '<i class="fas fa-user"></i>'; // fallback kalau foto tidak ada
}

function createMessageElement(text, sender, meta = {}, table = null) {
  const div = document.createElement("div");
  div.className = `message ${sender}`;
  const avatar =
    sender === "bot" ? '<i class="fas fa-robot"></i>' : getUserAvatarHtml();

  let bubbleContent = linkify(text);

  // ✅ Sisipkan tabel SEBELUM bagian 📊 Data (kalau ada tabel & pesan bot)
  if (sender === "bot" && table) {
    const tableHtml = buildTableHtml(table);

    // Cari posisi penyisipan: sebelum 💡 Catatan
    const insertMarkers = ["💡 Catatan:"];
    let insertPos = -1;
    for (const marker of insertMarkers) {
      const idx = bubbleContent.indexOf(marker);
      if (idx !== -1) {
        insertPos = idx;
        break;
      }
    }

    if (insertPos !== -1) {
      bubbleContent =
        bubbleContent.slice(0, insertPos) +
        tableHtml +
        bubbleContent.slice(insertPos);
    } else {
      bubbleContent += tableHtml; // Fallback: di akhir
    }
  }

  div.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="bubble">${bubbleContent}</div>
      <div class="message-time">${formatTime(new Date())}</div>
    </div>
  `;
  if (table) {
    div
      .querySelector(".table-download-btn")
      ?.addEventListener("click", () => downloadTableExcel(table));
  }
  return div;
}

// ===== TABEL DATA (di dalam bubble) + UNDUH XLSX =====
function buildTableHtml(table) {
  let html =
    '<div class="table-card-inner">' +
    '<div class="table-header">' +
    '<div class="table-title">📋 ' +
    escapeHtml(table.title || "Tabel Data") +
    "</div>" +
    '<button class="table-download-btn">⬇️ Unduh Excel</button>' +
    "</div>" +
    '<div class="table-scroll"><table class="data-table"><thead><tr>';
  (table.columns || []).forEach(
    (c) => (html += "<th>" + escapeHtml(c) + "</th>"),
  );
  html += "</tr></thead><tbody>";
  (table.rows || []).forEach((r) => {
    html +=
      "<tr>" +
      r.map((v) => "<td>" + escapeHtml(v) + "</td>").join("") +
      "</tr>";
  });
  html +=
    "</tbody></table></div>" +
    '<div class="table-meta">' +
    (table.total_rows || (table.rows || []).length) +
    " baris • Sumber: " +
    escapeHtml(table.source || "-") +
    "</div>" +
    "</div>";
  return html;
}

function downloadTableExcel(table) {
  const name = (table.title || "data-sergai").replace(/[\\/:*?"<>|]+/g, "_");
  const toNum = (v) => {
    const s = String(v ?? "").trim();
    if (/^-?\d{1,3}(,\d{3})+(\.\d+)?$/.test(s))
      return parseFloat(s.replace(/,/g, ""));
    if (/^-?\d+(\.\d+)?$/.test(s)) return parseFloat(s);
    return s;
  };
  if (window.XLSX) {
    const aoa = [table.columns].concat(
      table.rows.map((r) => r.map((v, i) => (i === 0 ? v : toNum(v)))),
    );
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Data");
    XLSX.writeFile(wb, name + ".xlsx");
  } else {
    // Fallback CSV bila CDN gagal dimuat
    const esc = (v) => {
      v = String(v ?? "");
      return /[",\n;]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    };
    const lines = [table.columns.map(esc).join(",")].concat(
      table.rows.map((r) => r.map(esc).join(",")),
    );
    const blob = new Blob(["\ufeff" + lines.join("\n")], {
      type: "text/csv;charset=utf-8;",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name + ".csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }
}

function showQuickReplyButtons() {
  const container = document.createElement("div");
  container.className = "quick-replies";
  CONFIG.quickReplies.forEach((reply) => {
    const btn = document.createElement("button");
    btn.className = "quick-reply-btn";
    btn.innerHTML = `${reply.icon} ${reply.text}`;
    btn.onclick = () => {
      userInput.value = reply.query;
      sendMessage();
    };
    container.appendChild(btn);
  });
  chatBox.appendChild(container);
  scrollToBottom();
}

function showTyping() {
  typingIndicator.classList.add("active");
  chatBox.appendChild(typingIndicator);
  scrollToBottom();
}

function hideTyping() {
  typingIndicator.classList.remove("active");
}

function hideEmptyState() {
  if (emptyState && emptyState.style.display !== "none") {
    emptyState.style.display = "none";
  }
}

function scrollToBottom() {
  chatBox.scrollTop = chatBox.scrollHeight;
}

function formatTime(date) {
  return date.toLocaleTimeString("id-ID", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function autoResizeInput() {
  this.style.height = "auto";
  this.style.height = this.scrollHeight + "px";
}

// ===== CHAT MANAGEMENT =====
function clearChat() {
  if (!confirm("Hapus percakapan ini dari layar dan riwayat?")) return;

  // ✅ Hapus session saat ini di Google Sheets (hilang dari sidebar)
  if (currentSessionId) {
    sheetPost({ action: "delete_session", session_id: currentSessionId }).then(
      () => {
        loadSessions(); // refresh daftar riwayat
      },
    );
  }

  // ✅ Bersihkan layar + langsung buat sesi baru
  startNewSession();

  localStorage.removeItem("sergai_chat_history");
}

function showInfo() {
  infoModal?.classList.add("active");
}

function closeModal() {
  infoModal?.classList.remove("active");
}

// ===== PERSISTENCE =====
function saveChatState() {
  try {
    localStorage.setItem(
      "sergai_chat_history",
      JSON.stringify({
        history: chatHistory.slice(-50),
        timestamp: Date.now(),
      }),
    );
  } catch (e) {
    console.warn("Failed to save chat state:", e);
  }
}

function loadChatState() {
  try {
    const saved = localStorage.getItem("sergai_chat_history");
    if (!saved) return;
  } catch (e) {
    console.warn("Failed to load chat state:", e);
  }
}
