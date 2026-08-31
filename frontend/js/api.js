/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * Menangani komunikasi dengan backend & BPS WebAPI
 */

import { CONFIG, getApiHeaders } from "./config.js";

/**
 * Kirim pertanyaan ke backend orchestrator.
 *
 * options.selectedCandidateId:
 * dipakai ketika user memilih salah satu kandidat data.
 */
export async function asksergAI(question, userId = "web-user", options = {}) {
  try {
    const controller = new AbortController();

    const timeoutId = setTimeout(() => controller.abort(), CONFIG.api.timeout);

    const response = await fetch(CONFIG.api.endpoint, {
      method: "POST",

      headers: getApiHeaders(),

      body: JSON.stringify({
        question: question.trim(),

        // Backend FastAPI menggunakan snake_case.
        user_id: userId,

        // Normal question → null.
        // Candidate dipilih → berisi ID kandidat.
        selected_candidate_id: options.selectedCandidateId || null,

        timestamp: new Date().toISOString(),
      }),

      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const data = await response.json();

    if (!data.answer) {
      throw new Error("Respons tidak valid: tidak ada jawaban");
    }

    return {
      success: true,

      answer: data.answer,

      table: data.table || null,

      meta: data.meta || {},

      sources: data.sources || [],

      timestamp: new Date().toLocaleTimeString("id-ID"),
    };
  } catch (error) {
    console.error("API Error:", error);

    if (error.name === "AbortError") {
      return {
        success: false,

        error: "Timeout: Server tidak merespons dalam waktu yang ditentukan.",

        type: "timeout",
      };
    }

    if (
      error.message.includes("Failed to fetch") ||
      error.message.includes("CORS")
    ) {
      return {
        success: false,

        error:
          "⚠️ Tidak dapat terhubung ke server. Pastikan backend SERGAI sedang aktif.",

        type: "network",
      };
    }

    return {
      success: false,

      error: CONFIG.fallback.apiError,

      type: "unknown",
    };
  }
}

/**
 * Fetch langsung dari BPS WebAPI.
 * Fungsi ini tetap dipertahankan untuk testing/fallback.
 */
export async function fetchBPSData({
  variableId,
  year,
  domainId = CONFIG.api.bpsApi.domainId,
}) {
  if (!CONFIG.api.bpsApi.apiKey || CONFIG.api.bpsApi.apiKey === "") {
    console.warn("⚠️ BPS API Key belum dikonfigurasi di config.js");

    return null;
  }

  const url =
    `${CONFIG.api.bpsApi.baseUrl}` +
    `/list/model/data/domain/${domainId}` +
    `/var/${variableId}` +
    `/th/${year}` +
    `/key/${CONFIG.api.bpsApi.apiKey}`;

  console.log(`🔍 Mengambil data BPS: ${url}`);

  try {
    const res = await fetch(url);

    const json = await res.json();

    if (json.status !== "OK") {
      throw new Error(json.message || "BPS API error");
    }

    const dataContent = json.data?.datacontent || {};

    const firstKey = Object.keys(dataContent)[0];

    const valueData = dataContent[firstKey];

    return {
      success: true,

      value: valueData?.value || "Data tidak ditemukan",

      unit: json.data?.var?.[0]?.unit || "",

      label: json.data?.var?.[0]?.label || "",

      year: json.data?.tahun?.[0]?.label || year,

      source: "BPS WebAPI",
    };
  } catch (error) {
    console.error("❌ Error fetch BPS:", error);

    return {
      success: false,

      error: error.message,
    };
  }
}

/**
 * Format response backend untuk ditampilkan
 * pada bubble chat.
 */
export function formatBotResponse(apiResult, originalQuestion) {
  if (!apiResult.success) {
    return {
      text: apiResult.error || CONFIG.fallback.noAnswer,

      isError: true,

      errorType: apiResult.type,
    };
  }

  const formattedAnswer = String(apiResult.answer || "")
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

// Tetap tersedia untuk testing dari console browser.
window.fetchBPSData = fetchBPSData;
