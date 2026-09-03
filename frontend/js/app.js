/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * Google login + sidebar riwayat + candidate selection
 */

import { CONFIG } from "./config.js";

import { asksergAI, formatBotResponse } from "./api.js";

// ==========================================================
// DOM ELEMENTS
// ==========================================================

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

const sidebarClose = document.getElementById("sidebarClose");

const newChatBtn = document.getElementById("newChatBtn");

const logoutBtn = document.getElementById("logoutBtn");

const sessionList = document.getElementById("sessionList");

// ==========================================================
// STATE
// ==========================================================

let chatHistory = [];

let userInfo = null;

let currentSessionId = null;

let isWaitingResponse = false;

/**
 * Menyimpan pertanyaan asli apabila backend
 * meminta user memilih kandidat.
 *
 * Contoh:
 *
 * pendingCandidateQuestion = "penduduk"
 *
 * Kemudian user memilih:
 * "Jumlah Penduduk Menurut Kecamatan"
 */
let pendingCandidateQuestion = null;

// ==========================================================
// GOOGLE SHEETS HELPER
// ==========================================================

function sheetPost(payload) {
  return fetch(CONFIG.googleScriptUrl, {
    method: "POST",

    body: JSON.stringify(payload),
  })
    .then((response) => response.json())
    .catch((error) => {
      console.warn("Sheet error:", error);

      return null;
    });
}

function logMessage(role, content, model = "") {
  if (!userInfo) {
    return;
  }

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

// ==========================================================
// SESSION MANAGEMENT
// ==========================================================

function getSessionId() {
  let sessionId = localStorage.getItem("sergai_session_id");

  if (!sessionId) {
    sessionId = (crypto.randomUUID && crypto.randomUUID()) || `s${Date.now()}`;

    localStorage.setItem("sergai_session_id", sessionId);
  }

  return sessionId;
}

function startNewSession() {
  const sessionId =
    (crypto.randomUUID && crypto.randomUUID()) || `s${Date.now()}`;

  localStorage.setItem("sergai_session_id", sessionId);

  currentSessionId = sessionId;

  chatHistory = [];

  pendingCandidateQuestion = null;

  removeCandidateSelections();

  chatBox.innerHTML = "";

  emptyState.style.display = "flex";

  chatBox.appendChild(emptyState);

  setTimeout(() => {
    addBotMessage(CONFIG.branding.welcomeMessage, true);
  }, 300);
}

function loadSessions() {
  if (!userInfo) {
    return;
  }

  const cached = JSON.parse(
    localStorage.getItem("sergai_sessions_cache") || "null",
  );

  if (cached && cached.length) {
    renderSessionList(cached);
  } else if (sessionList) {
    sessionList.innerHTML = `
      <div class="sb-loading">
        <div class="sb-spinner"></div>
        Memuat riwayat…
      </div>
      `;
  }

  sheetPost({
    action: "list_sessions",

    email: userInfo.email,
  }).then((result) => {
    if (result && result.ok) {
      const sessions = result.sessions || [];

      localStorage.setItem("sergai_sessions_cache", JSON.stringify(sessions));

      renderSessionList(sessions);
    }
  });
}

// ==========================================================
// TEXT HELPERS
// ==========================================================

function linkify(text) {
  const urlRegex = /(https?:\/\/[^\s<>"']+)/g;

  return String(text || "").replace(
    urlRegex,

    (url) =>
      `<a
          href="${url}"
          target="_blank"
          rel="noopener noreferrer"
          class="msg-link"
        >${url}</a>`,
  );
}

function escapeHtml(value) {
  const div = document.createElement("div");

  div.textContent = String(value ?? "");

  return div.innerHTML;
}

// ==========================================================
// SESSION LIST
// ==========================================================

function renderSessionList(sessions) {
  if (!sessionList) {
    return;
  }

  sessionList.innerHTML = "";

  if (!sessions.length) {
    sessionList.innerHTML = '<div class="sb-empty">Belum ada percakapan.</div>';

    return;
  }

  sessions.forEach((session) => {
    const item = document.createElement("div");

    item.className =
      "sb-item" + (session.session_id === currentSessionId ? " active" : "");

    const time = new Date(session.updated_at);

    const timeString =
      time.toLocaleDateString("id-ID", {
        day: "2-digit",

        month: "2-digit",
      }) +
      " " +
      time.toLocaleTimeString("id-ID", {
        hour: "2-digit",

        minute: "2-digit",
      });

    item.innerHTML = `
        <i class="fas fa-comment-dots"></i>

        <span class="sb-item-text">
          ${escapeHtml(session.title || "(Percakapan)")}
        </span>

        <span class="sb-item-time">
          ${timeString}
        </span>

        <button
          class="sb-item-del"
          title="Hapus sesi"
        >
          <i class="fas fa-trash"></i>
        </button>
        `;

    item.addEventListener("click", () => {
      openSession(session.session_id);
    });

    const deleteButton = item.querySelector(".sb-item-del");

    deleteButton?.addEventListener("click", (event) => {
      event.stopPropagation();

      if (!confirm("Hapus percakapan ini dari riwayat?")) {
        return;
      }

      sheetPost({
        action: "delete_session",

        session_id: session.session_id,
      }).then(() => {
        if (session.session_id === currentSessionId) {
          startNewSession();
        }

        loadSessions();
      });
    });

    sessionList.appendChild(item);
  });
}

function openSession(sessionId) {
  currentSessionId = sessionId;

  pendingCandidateQuestion = null;

  localStorage.setItem("sergai_session_id", sessionId);

  sheetPost({
    action: "get_session",

    session_id: sessionId,
  }).then((result) => {
    chatBox.innerHTML = "";

    chatHistory = [];

    const messages = (result && result.messages) || [];

    if (!messages.length) {
      emptyState.style.display = "flex";

      chatBox.appendChild(emptyState);
    } else {
      emptyState.style.display = "none";

      messages.forEach((message) => {
        appendMessage(message.content, message.role);
      });
    }

    loadSessions();

    scrollToBottom();
  });
}

// ==========================================================
// SIDEBAR
// ==========================================================

function setupSidebarUser() {
  const name = document.getElementById("sbUserName");

  const unit = document.getElementById("sbUserUnit");

  const avatar = document.getElementById("sbUserAva");

  if (name) {
    name.textContent = userInfo.name;
  }

  if (unit) {
    unit.textContent = userInfo.unit;
  }

  if (avatar && userInfo.picture) {
    avatar.innerHTML = `<img
        src="${userInfo.picture}"
        alt=""
      >`;
  }
}

function closeSidebar() {
  sidebar?.classList.remove("open");
}

function toggleSidebar() {
  sidebar?.classList.toggle("open");
}

// ==========================================================
// INITIALIZATION
// ==========================================================

document.addEventListener("DOMContentLoaded", () => {
  userInfo = JSON.parse(localStorage.getItem("sergai_user") || "null");

  if (!userInfo || !userInfo.email) {
    window.location.href = "/";

    return;
  }

  currentSessionId = getSessionId();

  const statusText = document.getElementById("statusText");

  if (statusText) {
    const firstName = (userInfo.name || "Sahabat Data").split(" ")[0];

    statusText.textContent = `Halo, ${firstName}! Tanya Data, sergAI Jawab!`;
  }

  setupSidebarUser();

  loadChatState();

  loadSessions();

  setTimeout(() => {
    addBotMessage(CONFIG.branding.welcomeMessage, true);
  }, 500);

  setupEventListeners();

  document.title = `${CONFIG.branding.name} - ${CONFIG.branding.tagline}`;
});

// ==========================================================
// EVENT LISTENERS
// ==========================================================

function setupEventListeners() {
  userInput.addEventListener("keypress", (event) => {
    if (event.key === "Enter" && !isWaitingResponse) {
      sendMessage();
    }
  });

  sendBtn?.addEventListener("click", () => {
    if (!isWaitingResponse) {
      sendMessage();
    }
  });

  infoBtn?.addEventListener("click", showInfo);

  clearBtn?.addEventListener("click", clearChat);

  closeModalBtn?.addEventListener("click", closeModal);

  infoModal?.addEventListener("click", (event) => {
    if (event.target === infoModal) {
      closeModal();
    }
  });

  userInput.addEventListener("input", autoResizeInput);

  newChatBtn?.addEventListener("click", startNewSession);

  sidebarOpen?.addEventListener("click", toggleSidebar);

  sidebarClose?.addEventListener("click", closeSidebar);

  logoutBtn?.addEventListener("click", () => {
    if (!confirm("Keluar dari akun Anda?")) {
      return;
    }

    localStorage.removeItem("sergai_user");

    localStorage.removeItem("sergai_session_id");

    window.location.href = "/";
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "b") {
      event.preventDefault();

      toggleSidebar();
    }
  });
}

// ==========================================================
// SEND MESSAGE
// ==========================================================

function sendMessage() {
  const text = userInput.value.trim();

  if (!text || isWaitingResponse) {
    return;
  }

  /**
   * User mengetik pertanyaan baru,
   * artinya candidate lama sudah tidak berlaku.
   */
  pendingCandidateQuestion = null;

  removeCandidateSelections();

  addUserMessage(text);

  logMessage("user", text);

  userInput.value = "";

  userInput.style.height = "auto";

  isWaitingResponse = true;

  sendBtn.disabled = true;

  showTyping();

  asksergAI(text, userInfo.email)
    .then((apiResult) => {
      console.log("🔍 TABLE PAYLOAD:", apiResult.table);

      console.log("🔎 RESPONSE META:", apiResult.meta);

      const formatted = formatBotResponse(apiResult, text);

      addBotMessage(
        formatted.text,

        false,

        formatted.meta,

        apiResult.table,
      );

      /**
       * Backend belum menjawab data.
       * Backend meminta user memilih salah satu kandidat.
       */
      if (apiResult.success && apiResult.meta?.type === "candidate_selection") {
        pendingCandidateQuestion = apiResult.meta.original_question || text;

        renderCandidateChoices(
          apiResult.meta.candidates || [],

          apiResult.meta.candidate_count || 0,
        );
      }

      const modelUsed = apiResult.meta?.model_used || "";

      const wasFallback = apiResult.meta?.fallback ? " (fallback)" : "";

      logMessage(
        "bot",

        formatted.text,

        modelUsed + wasFallback,
      );
    })
    .catch((error) => {
      console.error("Unexpected error:", error);

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

// ==========================================================
// CANDIDATE SELECTION
// ==========================================================

function removeCandidateSelections() {
  document
    .querySelectorAll(".candidate-selection")
    .forEach((element) => element.remove());
}

function renderCandidateChoices(candidates, totalCandidates = 0) {
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return;
  }

  removeCandidateSelections();

  const wrapper = document.createElement("div");

  wrapper.className = "candidate-selection";

  // ==========================================
  // HEADER
  // ==========================================

  const header = document.createElement("div");

  header.className = "candidate-selection-head";

  header.textContent = "Pilih data yang paling sesuai:";

  wrapper.appendChild(header);

  // ==========================================
  // CANDIDATES
  // ==========================================

  candidates.forEach((candidate, index) => {
    const button = document.createElement("button");

    button.type = "button";

    button.className = "candidate-option";

    const number = document.createElement("span");

    number.className = "candidate-number";

    number.textContent = String(index + 1);

    const content = document.createElement("span");

    content.className = "candidate-content";

    const title = document.createElement("span");

    title.className = "candidate-title";

    title.textContent = candidate.title || "Tanpa judul";

    content.appendChild(title);

    // ======================================
    // SOURCE
    // ======================================

    if (Array.isArray(candidate.sources) && candidate.sources.length > 0) {
      const source = document.createElement("small");

      source.className = "candidate-source";

      source.textContent = candidate.sources.join(" • ");

      content.appendChild(source);
    }

    button.appendChild(number);

    button.appendChild(content);

    button.addEventListener("click", () => {
      selectCandidate(candidate, wrapper, button);
    });

    wrapper.appendChild(button);
  });

  // ==========================================
  // INFO JUMLAH
  // ==========================================

  if (totalCandidates > candidates.length) {
    const info = document.createElement("div");

    info.className = "candidate-more-info";

    info.textContent = `Menampilkan ${candidates.length} dari ${totalCandidates} data yang ditemukan.`;

    wrapper.appendChild(info);
  }

  // ==========================================
  // PILIHAN "TIDAK ADA"
  // ==========================================

  const noneButton = document.createElement("button");

  noneButton.type = "button";

  noneButton.className = "candidate-option candidate-none";

  const noneNumber = document.createElement("span");

  noneNumber.className = "candidate-number";

  noneNumber.innerHTML = '<i class="fas fa-pen"></i>';

  const noneContent = document.createElement("span");

  noneContent.className = "candidate-content";

  const noneTitle = document.createElement("span");

  noneTitle.className = "candidate-title";

  noneTitle.textContent = "Data yang saya maksud tidak ada di pilihan";

  const noneDescription = document.createElement("small");

  noneDescription.className = "candidate-source";

  noneDescription.textContent =
    "Saya akan mengetik kebutuhan data dengan lebih rinci.";

  noneContent.appendChild(noneTitle);

  noneContent.appendChild(noneDescription);

  noneButton.appendChild(noneNumber);

  noneButton.appendChild(noneContent);

  noneButton.addEventListener("click", () => {
    wrapper.remove();

    pendingCandidateQuestion = null;

    addBotMessage(
      "Silakan tuliskan data yang Anda maksud dengan lebih rinci, misalnya indikator, kategori, wilayah, atau tahun yang dibutuhkan.",

      false,

      {
        type: "candidate_refine",

        model_used: "none",
      },
    );

    saveChatState();

    userInput.focus();
  });

  wrapper.appendChild(noneButton);

  chatBox.appendChild(wrapper);

  scrollToBottom();
}

// ==========================================================
// USER MEMILIH KANDIDAT
// ==========================================================

async function selectCandidate(candidate, wrapper, clickedButton) {
  if (!candidate?.id || !pendingCandidateQuestion || isWaitingResponse) {
    return;
  }

  const originalQuestion = pendingCandidateQuestion;

  // ==========================================
  // KUNCI TOMBOL
  // ==========================================

  wrapper.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });

  wrapper.querySelectorAll(".candidate-option").forEach((button) => {
    button.classList.remove("selected");
  });

  clickedButton?.classList.add("selected");

  /**
   * Tampilkan kandidat terpilih sebagai bubble user.
   */
  addUserMessage(candidate.title);

  logMessage("user", candidate.title);

  isWaitingResponse = true;

  sendBtn.disabled = true;

  showTyping();

  try {
    /**
     * PENTING:
     *
     * question tetap pertanyaan asli.
     *
     * Contoh:
     * question = "penduduk"
     *
     * selected_candidate_id =
     * candidate:xxxx
     *
     * Backend kemudian resolve record yang dipilih.
     */
    const apiResult = await asksergAI(
      originalQuestion,

      userInfo.email,

      {
        selectedCandidateId: candidate.id,
      },
    );

    console.log("✅ SELECTED CANDIDATE:", candidate);

    console.log("🔍 TABLE PAYLOAD:", apiResult.table);

    console.log("🔎 RESPONSE META:", apiResult.meta);

    const formatted = formatBotResponse(
      apiResult,

      originalQuestion,
    );

    addBotMessage(
      formatted.text,

      false,

      formatted.meta,

      apiResult.table,
    );

    const modelUsed = apiResult.meta?.model_used || "";

    const wasFallback = apiResult.meta?.fallback ? " (fallback)" : "";

    logMessage(
      "bot",

      formatted.text,

      modelUsed + wasFallback,
    );

    pendingCandidateQuestion = null;

    wrapper.classList.add("candidate-selection-done");
  } catch (error) {
    console.error("Candidate selection error:", error);

    addBotMessage(
      CONFIG.fallback.apiError,

      false,
    );

    /**
     * Jika request gagal,
     * hidupkan tombol lagi.
     */
    wrapper.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
  } finally {
    hideTyping();

    isWaitingResponse = false;

    sendBtn.disabled = false;

    userInput.focus();

    saveChatState();

    loadSessions();
  }
}

// ==========================================================
// MESSAGE MANAGEMENT
// ==========================================================

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

  chatBox.appendChild(
    createMessageElement(
      text,

      "bot",

      meta,

      table,
    ),
  );

  if (showQuickReplies) {
    showQuickReplyButtons();
  }

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

// ==========================================================
// AVATAR
// ==========================================================

function getUserAvatarHtml() {
  if (userInfo && userInfo.picture) {
    const picture = userInfo.picture.replace(/=s\d+(?:-c)?$/i, "=s200-c");

    return `<img ` + `src="${picture}" ` + `alt="" />`;
  }

  return '<i class="fas fa-user"></i>';
}

function buildPublicationResultsHtml(meta = {}) {
  const publications = Array.isArray(meta.publications)
    ? meta.publications
    : [];

  if (meta.type !== "publication_results" || publications.length === 0) {
    return "";
  }

  let html = '<div class="publication-results">';

  publications.forEach((publication) => {
    const title = escapeHtml(publication.title || "Publikasi BPS");

    const rawAbstract = String(
      publication.abstract || "Deskripsi publikasi tidak tersedia.",
    );

    // Bersihkan tag HTML dari abstract WebAPI
    const cleanAbstract = rawAbstract
      .replace(/<br\s*\/?>/gi, " ")
      .replace(/<[^>]*>/g, " ")
      .replace(/\s+/g, " ")
      .trim();

    const abstract = escapeHtml(cleanAbstract);

    const cover = String(publication.cover || "").trim();

    const pdf = String(publication.pdf || "").trim();

    html += '<article class="publication-card">';

    // ======================================================
    // COVER
    // ======================================================

    html += '<div class="publication-cover-wrap">';

    if (cover) {
      html +=
        `<img ` +
        `class="publication-cover" ` +
        `src="${escapeHtml(cover)}" ` +
        `alt="Cover ${title}" ` +
        `loading="lazy" ` +
        `referrerpolicy="no-referrer">`;
    } else {
      html +=
        '<div class="publication-cover-placeholder">' +
        '<i class="fas fa-book-open"></i>' +
        "</div>";
    }

    html += "</div>";

    // ======================================================
    // ISI CARD
    // ======================================================

    html +=
      '<div class="publication-content">' +
      `<div class="publication-title">` +
      `${title}` +
      `</div>` +
      `<div class="publication-abstract">` +
      `${abstract}` +
      `</div>`;

    // ======================================================
    // LINK PDF
    // ======================================================

    if (pdf) {
      html +=
        `<a ` +
        `class="publication-link" ` +
        `href="${escapeHtml(pdf)}" ` +
        `target="_blank" ` +
        `rel="noopener noreferrer">` +
        '<i class="fas fa-file-pdf"></i>' +
        "<span>Lihat Publikasi</span>" +
        '<i class="fas fa-arrow-up-right-from-square"></i>' +
        "</a>";
    }

    html += "</div>" + "</article>";
  });

  html += "</div>";

  return html;
}

// ==========================================================
// CREATE MESSAGE
// ==========================================================

function createMessageElement(text, sender, meta = {}, table = null) {
  const div = document.createElement("div");

  div.className = `message ${sender}`;

  const avatar =
    sender === "bot" ? '<i class="fas fa-robot"></i>' : getUserAvatarHtml();

  if (sender === "bot" && table && table.source_note) {
    text = stripDuplicateSourceFromBotText(text, table);
  }

  let bubbleContent = linkify(text);

  // ==========================================================
  // HASIL PENCARIAN PUBLIKASI
  // ==========================================================

  if (sender === "bot" && meta && meta.type === "publication_results") {
    bubbleContent += buildPublicationResultsHtml(meta);
  }

  // ==========================================
  // TABLE
  // ==========================================

  if (sender === "bot" && table) {
    const tableHtml = buildTableHtml(table);

    const insertMarkers = [
      "• Definisi:",

      "Definisi:",

      "💡 Catatan:",

      "📊 Data:",

      "📖 Sumber:",
    ];

    let insertPosition = -1;

    for (const marker of insertMarkers) {
      const index = bubbleContent.indexOf(marker);

      if (index !== -1 && (insertPosition === -1 || index < insertPosition)) {
        insertPosition = index;
      }
    }

    if (insertPosition !== -1) {
      bubbleContent =
        bubbleContent.slice(0, insertPosition) +
        tableHtml +
        bubbleContent.slice(insertPosition);
    } else {
      bubbleContent += tableHtml;
    }
  }

  div.innerHTML = `
    <div class="message-avatar">
      ${avatar}
    </div>

    <div class="message-content">

      <div class="bubble">
        ${bubbleContent}
      </div>

      <div class="message-time">
        ${formatTime(new Date())}
      </div>

    </div>
    `;

  if (table) {
    div.querySelector(".table-download-btn")?.addEventListener("click", () => {
      downloadTableExcel(table);
    });
  }

  return div;
}

function stripDuplicateSourceFromBotText(text, table) {
  if (!table || !table.source_note) {
    return String(text || "");
  }

  let cleaned = String(text || "");

  // Hapus sumber yang dibuat ulang oleh AI,
  // karena sumber asli sudah tampil melalui table.source_note.
  cleaned = cleaned
    .replace(/(?:^|\n)\s*📖\s*Sumber\s*:[^\n]*/gi, "")
    .replace(/(?:^|\n)\s*📚\s*Sumber\s*:[^\n]*/gi, "")
    .replace(/(?:^|\n)\s*📘\s*Sumber\s*:[^\n]*/gi, "")
    .replace(/(?:^|\n)\s*[•\-]?\s*Sumber\s*:[^\n]*/gi, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return cleaned;
}
// ==========================================================
// TABLE
// ==========================================================

function buildTableHtml(table) {
  const hasMultiHeader =
    Array.isArray(table.header_rows) && table.header_rows.length > 0;

  const bodyRowspans = Array.isArray(table.body_rowspans)
    ? table.body_rowspans
    : [];

  const rowspanStartMap = new Map();

  const coveredCells = new Set();

  bodyRowspans.forEach((item) => {
    const row = Number(item.row);

    const col = Number(item.col);

    const rowspan = Math.max(1, Number(item.rowspan || 1));

    rowspanStartMap.set(`${row}:${col}`, {
      rowspan,
      value: item.value,
    });

    for (let r = row + 1; r < row + rowspan; r += 1) {
      coveredCells.add(`${r}:${col}`);
    }
  });

  let html =
    '<div class="table-card-inner">' +
    '<div class="table-header">' +
    '<div class="table-title">' +
    "📋 " +
    escapeHtml(table.title || "Tabel Data") +
    "</div>" +
    '<button class="table-download-btn">' +
    "⬇️ Unduh Excel" +
    "</button>" +
    "</div>" +
    '<div class="table-scroll">' +
    `<table class="data-table${hasMultiHeader ? " has-multi-header" : ""}">` +
    "<thead>";

  // ========================================================
  // HEADER BERTINGKAT
  // ========================================================

  if (hasMultiHeader) {
    table.header_rows.forEach((headerRow, rowIndex) => {
      html += `<tr class="header-level header-level-${rowIndex + 1}">`;

      headerRow.forEach((cell) => {
        const colspan = Math.max(1, Number(cell.colspan || 1));

        const rowspan = Math.max(1, Number(cell.rowspan || 1));

        html +=
          `<th` +
          ` colspan="${colspan}"` +
          ` rowspan="${rowspan}"` +
          `>` +
          escapeHtml(cell.label || "") +
          "</th>";
      });

      html += "</tr>";
    });
  } else {
    // ======================================================
    // HEADER BIASA
    // ======================================================

    html += "<tr>";

    (table.columns || []).forEach((column) => {
      html += "<th>" + escapeHtml(column) + "</th>";
    });

    html += "</tr>";
  }

  html += "</thead>" + "<tbody>";

  // ========================================================
  // ISI TABEL + ROWSPAN BODY
  // ========================================================

  (table.rows || []).forEach((row, rowIndex) => {
    html += "<tr>";

    row.forEach((value, columnIndex) => {
      const cellKey = `${rowIndex}:${columnIndex}`;

      if (coveredCells.has(cellKey)) {
        return;
      }

      const spanInfo = rowspanStartMap.get(cellKey);

      const rowspan = spanInfo?.rowspan || 1;

      const displayValue = spanInfo?.value ?? value;

      const cellClass = columnIndex === 0 ? "table-row-label" : "table-value";

      const rowspanAttr = rowspan > 1 ? ` rowspan="${rowspan}"` : "";

      html +=
        `<td` +
        ` class="${cellClass}"` +
        `${rowspanAttr}` +
        `>` +
        escapeHtml(displayValue) +
        "</td>";
    });

    html += "</tr>";
  });

  html += "</tbody>" + "</table>" + "</div>";

  // ========================================================
  // KETERANGAN SIMBOL YANG BENAR-BENAR MUNCUL
  // ========================================================
  const symbolNotes = Array.isArray(table.symbol_notes)
    ? table.symbol_notes
    : [];

  if (symbolNotes.length > 0) {
    html +=
      '<div class="table-symbol-notes">' +
      '<div class="table-symbol-notes-title"><strong>Keterangan:</strong></div>';

    symbolNotes.forEach((item) => {
      html +=
        '<div class="table-symbol-note-item">' +
        '<span class="table-symbol-code">' +
        escapeHtml(item.symbol || "") +
        "</span>" +
        " : " +
        '<span class="table-symbol-meaning">' +
        escapeHtml(item.meaning || "") +
        "</span>" +
        "</div>";
    });

    html += "</div>";
  }

  // ========================================================
  // META TABEL
  // ========================================================
  // table.source TIDAK ditampilkan lagi.
  // Yang ditampilkan hanya jumlah baris.

  html +=
    '<div class="table-meta">' +
    (table.total_rows || (table.rows || []).length) +
    " baris" +
    "</div>";

  // ========================================================
  // SUMBER ASLI DARI GOOGLE SHEET
  // ========================================================
  // Hanya sumber ini yang dipertahankan.

  if (table.source_note) {
    html +=
      '<div class="table-source-note">' +
      escapeHtml(table.source_note) +
      "</div>";
  }

  html += "</div>";

  return html;
}

// ==========================================================
// DOWNLOAD EXCEL
// ==========================================================

function downloadTableExcel(table) {
  const name = (table.title || "data-sergai").replace(/[\\/:*?"<>|]+/g, "_");

  const toNumber = (value) => {
    const string = String(value ?? "").trim();

    if (/^-?\d{1,3}(,\d{3})+(\.\d+)?$/.test(string)) {
      return parseFloat(string.replace(/,/g, ""));
    }

    if (/^-?\d+(\.\d+)?$/.test(string)) {
      return parseFloat(string);
    }

    return string;
  };

  if (window.XLSX) {
    const arrayOfArrays = [table.columns].concat(
      table.rows.map((row) =>
        row.map((value, index) => (index === 0 ? value : toNumber(value))),
      ),
    );

    const worksheet = XLSX.utils.aoa_to_sheet(arrayOfArrays);

    const workbook = XLSX.utils.book_new();

    XLSX.utils.book_append_sheet(
      workbook,

      worksheet,

      "Data",
    );

    XLSX.writeFile(
      workbook,

      `${name}.xlsx`,
    );

    return;
  }

  // ==========================================
  // FALLBACK CSV
  // ==========================================

  const escapeCsv = (value) => {
    const string = String(value ?? "");

    if (/[",\n;]/.test(string)) {
      return '"' + string.replace(/"/g, '""') + '"';
    }

    return string;
  };

  const lines = [table.columns.map(escapeCsv).join(",")].concat(
    table.rows.map((row) => row.map(escapeCsv).join(",")),
  );

  const blob = new Blob(["\ufeff" + lines.join("\n")], {
    type: "text/csv;charset=utf-8;",
  });

  const anchor = document.createElement("a");

  anchor.href = URL.createObjectURL(blob);

  anchor.download = `${name}.csv`;

  anchor.click();

  URL.revokeObjectURL(anchor.href);
}

// ==========================================================
// QUICK REPLIES
// ==========================================================

function showQuickReplyButtons() {
  const container = document.createElement("div");

  container.className = "quick-replies";

  CONFIG.quickReplies.forEach((reply) => {
    const button = document.createElement("button");

    button.className = "quick-reply-btn";

    button.innerHTML = `${reply.icon} ${reply.text}`;

    button.onclick = () => {
      userInput.value = reply.query;

      sendMessage();
    };

    container.appendChild(button);
  });

  chatBox.appendChild(container);

  scrollToBottom();
}

// ==========================================================
// TYPING
// ==========================================================

function showTyping() {
  typingIndicator.classList.add("active");

  chatBox.appendChild(typingIndicator);

  scrollToBottom();
}

function hideTyping() {
  typingIndicator.classList.remove("active");
}

// ==========================================================
// UI HELPERS
// ==========================================================

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

  this.style.height = `${this.scrollHeight}px`;
}

// ==========================================================
// CHAT MANAGEMENT
// ==========================================================

function clearChat() {
  if (!confirm("Hapus percakapan ini dari layar dan riwayat?")) {
    return;
  }

  pendingCandidateQuestion = null;

  removeCandidateSelections();

  if (currentSessionId) {
    sheetPost({
      action: "delete_session",

      session_id: currentSessionId,
    }).then(() => {
      loadSessions();
    });
  }

  startNewSession();

  localStorage.removeItem("sergai_chat_history");
}

// ==========================================================
// INFO MODAL
// ==========================================================

function showInfo() {
  infoModal?.classList.add("active");
}

function closeModal() {
  infoModal?.classList.remove("active");
}

// ==========================================================
// LOCAL STORAGE
// ==========================================================

function saveChatState() {
  try {
    localStorage.setItem(
      "sergai_chat_history",

      JSON.stringify({
        history: chatHistory.slice(-50),

        timestamp: Date.now(),
      }),
    );
  } catch (error) {
    console.warn("Failed to save chat state:", error);
  }
}

function loadChatState() {
  try {
    const saved = localStorage.getItem("sergai_chat_history");

    if (!saved) {
      return;
    }

    /**
     * Riwayat utama saat ini tetap berasal
     * dari Google Sheets.
     *
     * localStorage hanya menjadi cache.
     */
  } catch (error) {
    console.warn("Failed to load chat state:", error);
  }
}
