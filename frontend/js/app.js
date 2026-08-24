/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * UI Interactions & Chat Management
 * ✅ UPDATED: Fixed module imports + event listeners
 */

import { CONFIG } from "./config.js";
import { asksergAI, formatBotResponse, initModelSelector } from "./api.js";

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

// ===== State =====
let chatHistory = [];
let userName = "";
let isFirstMessage = true;
let isWaitingResponse = false;

// ===== INITIALIZATION =====
document.addEventListener("DOMContentLoaded", () => {
  // Load saved state
  loadChatState();

  // Show welcome message
  setTimeout(() => {
    addBotMessage(CONFIG.branding.welcomeMessage, true);
  }, 500);

  // Setup all event listeners
  setupEventListeners();

  // ✅ INISIALISASI DROPDOWN MODEL (TAMBAH INI)
  initModelSelector();

  // Update UI with branding
  document.title = `${CONFIG.branding.name} - ${CONFIG.branding.tagline}`;
});

// ===== EVENT LISTENERS =====
function setupEventListeners() {
  // Enter key to send message
  userInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter" && !isWaitingResponse) {
      sendMessage();
    }
  });

  // Send button click
  sendBtn?.addEventListener("click", () => {
    if (!isWaitingResponse) sendMessage();
  });

  // Info button
  infoBtn?.addEventListener("click", showInfo);

  // Clear chat button
  clearBtn?.addEventListener("click", clearChat);

  // Close modal button
  closeModalBtn?.addEventListener("click", closeModal);

  // Close modal when clicking outside
  infoModal?.addEventListener("click", (e) => {
    if (e.target === infoModal) closeModal();
  });

  // Auto-resize input (optional)
  userInput.addEventListener("input", autoResizeInput);
}

// ===== MESSAGE HANDLING =====
function sendMessage() {
  const text = userInput.value.trim();
  if (!text || isWaitingResponse) return;

  // Add user message
  addUserMessage(text);
  userInput.value = "";
  isWaitingResponse = true;
  sendBtn.disabled = true;

  // Show typing indicator
  showTyping();

  // Call API
  asksergAI(text, generateUserId())
    .then((apiResult) => {
      const formatted = formatBotResponse(apiResult, text);
      addBotMessage(formatted.text, false, formatted.meta);
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
    });
}

function addUserMessage(text) {
  hideEmptyState();

  const messageEl = createMessageElement(text, "user");
  chatBox.appendChild(messageEl);
  scrollToBottom();

  // Save to history
  chatHistory.push({
    role: "user",
    content: text,
    timestamp: new Date().toISOString(),
  });

  // Detect name from first message
  if (!userName && isFirstMessage) {
    userName = extractName(text) || "Teman";
    isFirstMessage = false;
  }
}

function addBotMessage(text, showQuickReplies = false, meta = {}) {
  hideEmptyState();

  const messageEl = createMessageElement(text, "bot", meta);
  chatBox.appendChild(messageEl);

  // Show quick replies for first bot message
  if (showQuickReplies && isFirstMessage) {
    showQuickReplyButtons();
    isFirstMessage = false;
  }

  scrollToBottom();

  // Save to history
  chatHistory.push({
    role: "bot",
    content: text,
    meta,
    timestamp: new Date().toISOString(),
  });
}

// ===== UI HELPERS =====
function createMessageElement(text, sender, meta = {}) {
  const div = document.createElement("div");
  div.className = `message ${sender}`;

  const avatar =
    sender === "bot"
      ? '<i class="fas fa-robot"></i>'
      : '<i class="fas fa-user"></i>';

  // ✅ Citation sudah include di dalam 'text' dari formatBotResponse (api.js)
  // Jadi tidak perlu ditambahkan lagi di sini untuk menghindari duplikasi

  div.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="bubble">${text}</div>
      <div class="message-time">${formatTime(new Date())}</div>
    </div>
  `;

  return div;
}

function showQuickReplyButtons() {
  const container = document.createElement("div");
  container.className = "quick-replies";
  // ✅ Margin sudah diatur di CSS, tidak perlu inline style

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

// ===== UTILITY FUNCTIONS =====
function extractName(text) {
  const patterns = [
    /nama\s+(saya\s+)?([a-zA-Z\s]+)/i,
    /saya\s+([a-zA-Z\s]+)/i,
    /^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)$/,
  ];

  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match && match[2]) {
      const name = match[2].trim();
      if (name.length >= 2 && name.length <= 30) {
        return name;
      }
    }
  }
  return null;
}

function generateUserId() {
  return (
    localStorage.getItem("sergai_user_id") ||
    (localStorage.setItem(
      "sergai_user_id",
      crypto.randomUUID?.() || Date.now().toString(),
    ) &&
      localStorage.getItem("sergai_user_id"))
  );
}

// ===== CHAT MANAGEMENT =====
function clearChat() {
  if (!confirm("Hapus semua riwayat percakapan?")) return;

  chatBox.innerHTML = "";
  chatBox.appendChild(emptyState);
  emptyState.style.display = "flex";
  chatHistory = [];
  userName = "";
  isFirstMessage = true;

  setTimeout(() => {
    addBotMessage(CONFIG.branding.welcomeMessage, true);
  }, 300);

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
        userName,
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

    const { history, userName: savedName } = JSON.parse(saved);
    if (savedName) userName = savedName;

    // Optional: Restore history (disabled by default)
    // if (history?.length) {
    //   history.forEach(msg => {
    //     const el = createMessageElement(msg.content, msg.role, msg.meta);
    //     chatBox.appendChild(el);
    //   });
    //   hideEmptyState();
    // }
  } catch (e) {
    console.warn("Failed to load chat state:", e);
  }
}

// ✅ TIDAK PERLU window.xxx karena sudah pakai event listeners + modules
