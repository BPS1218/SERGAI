/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * Menangani komunikasi dengan backend & BPS WebAPI
 */

import { CONFIG, getApiHeaders } from "./config.js";

/**
 * Kirim pertanyaan ke backend orchestrator
 *
 * @param {string} question - Pertanyaan pengguna
 * @param {string} userId - ID pengguna
 * @param {Object} options - Context tambahan
 *
 * options:
 * {
 *   selectedCandidateId: "candidate:xxxx"
 * }
 *
 * @returns {Promise<Object>}
 */
export async function asksergAI(question, userId = "web-user", options = {}) {
  try {
    // ==========================================================
    // VALIDASI PERTANYAAN
    // ==========================================================

    const cleanQuestion = String(question || "").trim();

    if (!cleanQuestion) {
      return {
        success: false,
        error: "Pertanyaan tidak boleh kosong.",
        type: "validation",
      };
    }

    // ==========================================================
    // REQUEST CONTEXT
    // ==========================================================
    //
    // Context ini digunakan antara lain saat pengguna memilih
    // salah satu kandidat data yang sebelumnya diberikan backend.
    //
    // app.js mengirim:
    //
    // {
    //   selectedCandidateId: candidate.id
    // }
    //
    // Backend mengharapkan:
    //
    // {
    //   context: {
    //     selected_candidate_id: "candidate:xxxx"
    //   }
    // }
    //
    // ==========================================================

    const context = {};

    if (options?.selectedCandidateId) {
      context.selected_candidate_id = String(
        options.selectedCandidateId,
      ).trim();
    }

    // ==========================================================
    // CONTROLLER + TIMEOUT
    // ==========================================================

    const controller = new AbortController();

    const timeoutId = setTimeout(() => controller.abort(), CONFIG.api.timeout);

    // ==========================================================
    // REQUEST BODY
    // ==========================================================

    const requestBody = {
      question: cleanQuestion,

      userId,

      timestamp: new Date().toISOString(),

      context: Object.keys(context).length > 0 ? context : null,
    };

    console.log("📤 SERGAI REQUEST:", requestBody);

    // ==========================================================
    // CALL BACKEND
    // ==========================================================

    const response = await fetch(CONFIG.api.endpoint, {
      method: "POST",

      headers: getApiHeaders(),

      body: JSON.stringify(requestBody),

      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    // ==========================================================
    // HTTP ERROR
    // ==========================================================

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    // ==========================================================
    // PARSE RESPONSE
    // ==========================================================

    const data = await response.json();

    console.log("📥 SERGAI RESPONSE:", data);

    if (!data.answer) {
      throw new Error("Respons tidak valid: tidak ada jawaban");
    }

    // ==========================================================
    // RESPONSE KE APP.JS
    // ==========================================================

    return {
      success: true,

      answer: data.answer,

      table: data.table || null,

      meta: data.meta || {},

      sources: data.sources || [],

      timestamp: new Date().toLocaleTimeString("id-ID"),
    };
  } catch (error) {
    console.error("❌ API Error:", error);

    // ==========================================================
    // TIMEOUT
    // ==========================================================

    if (error.name === "AbortError") {
      return {
        success: false,

        error: "Timeout: Server tidak merespons dalam waktu yang ditentukan.",

        type: "timeout",
      };
    }

    // ==========================================================
    // NETWORK / CORS
    // ==========================================================

    if (
      error.message?.includes("Failed to fetch") ||
      error.message?.includes("CORS")
    ) {
      return {
        success: false,

        error:
          "⚠️ Tidak dapat terhubung ke server.\n\n" +
          "💡 Pastikan backend SERGAI sedang aktif dan endpoint pada config.js sudah benar.",

        type: "network",
      };
    }

    // ==========================================================
    // ERROR LAIN
    // ==========================================================

    return {
      success: false,

      error: CONFIG.fallback.apiError,

      type: "unknown",
    };
  }
}

/**
 * ==========================================================
 * FETCH LANGSUNG BPS WEBAPI
 * ==========================================================
 *
 * Fungsi opsional untuk testing/fallback.
 *
 * @param {Object} params
 * @param {string|number} params.variableId
 * @param {string|number} params.year
 * @param {string|number} params.domainId
 */
export async function fetchBPSData({
  variableId,
  year,
  domainId = CONFIG.api.bpsApi.domainId,
}) {
  // ==========================================================
  // VALIDASI API KEY
  // ==========================================================

  if (!CONFIG.api.bpsApi.apiKey || CONFIG.api.bpsApi.apiKey === "") {
    console.warn("⚠️ BPS API Key belum dikonfigurasi di config.js");

    return null;
  }

  // ==========================================================
  // BUILD URL
  // ==========================================================

  const url =
    `${CONFIG.api.bpsApi.baseUrl}` +
    `/list/model/data` +
    `/domain/${domainId}` +
    `/var/${variableId}` +
    `/th/${year}` +
    `/key/${CONFIG.api.bpsApi.apiKey}`;

  console.log(`🔍 Mengambil data BPS: ${url}`);

  try {
    // ==========================================================
    // FETCH
    // ==========================================================

    const res = await fetch(url);

    const json = await res.json();

    // ==========================================================
    // VALIDASI RESPONSE BPS
    // ==========================================================

    if (json.status !== "OK") {
      throw new Error(json.message || "BPS API error");
    }

    // ==========================================================
    // PARSE RESPONSE
    // ==========================================================

    const dataContent = json.data?.datacontent || {};

    const firstKey = Object.keys(dataContent)[0];

    const valueData = dataContent[firstKey];

    return {
      success: true,

      value: valueData?.value ?? "Data tidak ditemukan",

      unit: json.data?.var?.[0]?.unit || "",

      label: json.data?.var?.[0]?.label || "",

      year: json.data?.tahun?.[0]?.label || year,

      source: "BPS WebAPI",
    };
  } catch (error) {
    console.error("❌ Error fetch BPS:", error);

    return {
      success: false,

      error: error.message || "Gagal mengambil data dari BPS WebAPI",
    };
  }
}

/**
 * ==========================================================
 * FORMAT RESPONSE BOT
 * ==========================================================
 *
 * Sumber tidak ditambahkan lagi dari frontend.
 * Backend menjadi sumber utama untuk menentukan teks sumber.
 */
export function formatBotResponse(apiResult, originalQuestion) {
  if (!apiResult.success) {
    return {
      text: apiResult.error || CONFIG.fallback.noAnswer,

      isError: true,

      errorType: apiResult.type,
    };
  }

  // ==========================================================
  // FORMAT MARKDOWN SEDERHANA
  // ==========================================================

  const rawAnswer = String(apiResult.answer || "");

  const formattedAnswer = rawAnswer
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/^\*\s/gm, "• ")
    .replace(/\n/g, "<br>");

  return {
    text: formattedAnswer,

    meta: apiResult.meta || {},

    timestamp: apiResult.timestamp,

    isError: false,
  };
}

/**
 * ==========================================================
 * MODEL SELECTION
 * ==========================================================
 */

/**
 * Simpan pilihan model ke localStorage
 */
export function saveModelPreference(modelName) {
  if (
    modelName &&
    ["gemini", "openai", "rag", "rag_dynamic"].includes(modelName)
  ) {
    localStorage.setItem("sergai_model", modelName);
  }
}

/**
 * Load pilihan model dari localStorage
 */
export function loadModelPreference() {
  return localStorage.getItem("sergai_model") || "gemini";
}

/**
 * Inisialisasi dropdown model
 */
export function initModelSelector() {
  const select = document.getElementById("model-select");

  if (!select) {
    return;
  }

  const saved = loadModelPreference();

  select.value = saved;

  select.addEventListener("change", (event) => {
    saveModelPreference(event.target.value);

    console.log(`🔄 Model changed to: ${event.target.value}`);
  });

  console.log(`✅ Model selector initialized: ${saved}`);
}

// ==========================================================
// OPTIONAL GLOBAL ACCESS
// ==========================================================

window.fetchBPSData = fetchBPSData;
