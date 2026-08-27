/**
 * SERGAI - Smart Engagement for Responsive Government Assistant Intelligence
 * Menangani komunikasi dengan backend & BPS WebAPI
 * ✅ UPDATED: Added mock response for local testing
 */

import { CONFIG, getApiHeaders } from "./config.js";

/**
 * Kirim pertanyaan ke backend orchestrator
 * @param {string} question - Pertanyaan pengguna
 * @param {string} userId - ID pengguna (opsional)
 * @returns {Promise<Object>} Respons terformat
 */
export async function asksergAI(question, userId = "web-user") {
  try {
    // // 🧪 MOCK MODE: Jika endpoint masih placeholder, return mock response
    // if (CONFIG.api.endpoint.includes("your-backend-url")) {
    //   console.log("🧪 Mock mode active - using fake response for testing");
    //   await new Promise((resolve) => setTimeout(resolve, 1000)); // Simulasi delay

    //   const mockAnswers = {
    //     pdrb: "📊 **PDRB Sergai 2024**: Rp 45,2 Triliun (atas dasar harga berlaku).\n\nSumber: *BPS Sergai, PDRB Menurut Lapangan Usaha 2024*",
    //     penduduk:
    //       "👥 **Jumlah Penduduk Sergai**: 685.432 jiwa (proyeksi 2024).\n\nSumber: *BPS Sergai, Statistik Daerah 2024*",
    //     kemiskinan:
    //       "📉 **Tingkat Kemiskinan Sergai**: 5,87% (Maret 2024).\n\nSumber: *BPS Sergai, Berita Resmi Statistik*",
    //     download:
    //       "📖 Publikasi resmi BPS Sergai dapat diunduh di:\n• https://serdangbedagaikab.bps.go.id/id/publication\n• Pilih menu 'Publikasi' → 'Sergai Dalam Angka'",
    //     default: `Terima kasih atas pertanyaan Anda: "*${question}*" 🙏\n\n💡 *Ini adalah mode demo*. Untuk jawaban data real-time, silakan:\n• Hubungi PST BPS Sergai: (0621) 441805\n• Kunjungi: https://serdangbedagaikab.bps.go.id\n• Email: bps1218@bps.go.id`,
    //   };

    //   // Simple keyword matching untuk mock
    //   const lowerQ = question.toLowerCase();
    //   let answerKey = "default";
    //   if (lowerQ.includes("pdrb")) answerKey = "pdrb";
    //   else if (lowerQ.includes("penduduk") || lowerQ.includes("jiwa"))
    //     answerKey = "penduduk";
    //   else if (lowerQ.includes("miskin") || lowerQ.includes("kemiskinan"))
    //     answerKey = "kemiskinan";
    //   else if (
    //     lowerQ.includes("download") ||
    //     lowerQ.includes("unduh") ||
    //     lowerQ.includes("publikasi")
    //   )
    //     answerKey = "download";

    //   return {
    //     success: true,
    //     answer: mockAnswers[answerKey],
    //     meta: { mock: true },
    //     sources:
    //       answerKey !== "default"
    //         ? [
    //             {
    //               name: "BPS Kabupaten Serdang Bedagai",
    //               url: "https://serdangbedagaikab.bps.go.id/id/publication",
    //               page: "Publikasi Resmi",
    //             },
    //           ]
    //         : [],
    //     timestamp: new Date().toLocaleTimeString("id-ID"),
    //   };
    // }

    // 🌐 REAL API CALL (jika endpoint sudah diganti dengan backend sungguhan)
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), CONFIG.api.timeout);

    const response = await fetch(CONFIG.api.endpoint, {
      method: "POST",
      headers: getApiHeaders(),
      body: JSON.stringify({
        question: question.trim(),
        userId,
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
      table: data.table,
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
          "⚠️ Tidak dapat terhubung ke server.\n\n💡 *Tips*: Untuk testing lokal, pastikan endpoint di config.js sudah diganti dengan backend yang aktif, atau biarkan placeholder untuk menggunakan mode mock.",
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
 * [OPSIONAL] Fetch langsung dari BPS WebAPI (untuk testing/fallback)
 * @param {Object} params - { variableId, year, domainId }
 */
/**
 * [OPSIONAL] Fetch langsung dari BPS WebAPI
 * @param {Object} params - { variableId, year }
 */
export async function fetchBPSData({
  variableId,
  year,
  domainId = CONFIG.api.bpsApi.domainId,
}) {
  // Validasi Key
  if (!CONFIG.api.bpsApi.apiKey || CONFIG.api.bpsApi.apiKey === "") {
    console.warn("⚠️ BPS API Key belum dikonfigurasi di config.js");
    return null;
  }

  // 1. Susun URL
  const url = `${CONFIG.api.bpsApi.baseUrl}/list/model/data/domain/${domainId}/var/${variableId}/th/${year}/key/${CONFIG.api.bpsApi.apiKey}`;

  console.log(`🔍 Mengambil data BPS: ${url}`);

  try {
    // 2. Fetch data
    const res = await fetch(url);
    const json = await res.json();

    // 3. Validasi Status
    if (json.status !== "OK") {
      throw new Error(json.message || "BPS API error");
    }

    // 4. Parse Response (Sesuai format BPS)
    const dataContent = json.data?.datacontent || {};
    const firstKey = Object.keys(dataContent)[0]; // Ambil key pertama yang ditemukan
    const valueData = dataContent[firstKey];

    return {
      success: true,
      value: valueData?.value || "Data tidak ditemukan",
      unit: json.data?.var?.[0]?.unit || "",
      label: json.data?.var?.[0]?.label || "",
      year: json.data?.tahun?.[0]?.label || year,
      source: "BPS WebAPI",
    };
  } catch (e) {
    console.error("❌ Error fetch BPS:", e);
    return {
      success: false,
      error: e.message,
    };
  }
}

// /**
//  * Format respons untuk ditampilkan di UI
//  */
// export function formatBotResponse(apiResult, originalQuestion) {
//   if (!apiResult.success) {
//     return {
//       text: apiResult.error || CONFIG.fallback.noAnswer,
//       isError: true,
//       errorType: apiResult.type,
//     };
//   }

//   // Build citation section if sources exist
//   let citationHtml = "";
//   if (apiResult.sources?.length > 0) {
//     citationHtml = `
//       <div class="source-citation">
//         📖 Sumber: ${apiResult.sources
//           .map(
//             (s) =>
//               `<a href="${s.url || "#"}" target="_blank">${s.name}${s.page ? `, hlm. ${s.page}` : ""}</a>`,
//           )
//           .join("; ")}
//       </div>
//     `;
//   }

//   // Format answer with markdown-like syntax
//   let formattedAnswer = apiResult.answer
//     .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>") // Bold
//     .replace(/^\*\s/gm, "• ") // Bullet list
//     .replace(/\n/g, "<br>"); // New line

//   return {
//     text: formattedAnswer + citationHtml,
//     meta: apiResult.meta,
//     timestamp: apiResult.timestamp,
//     isError: false,
//   };
// }

// /**
//  * Format respons untuk ditampilkan di UI
//  * ✅ Sekarang source 100% dari backend
//  */
// export function formatBotResponse(apiResult, originalQuestion) {
//   if (!apiResult.success) {
//     return {
//       text: apiResult.error || CONFIG.fallback.noAnswer,
//       isError: true,
//       errorType: apiResult.type,
//     };
//   }

//   // ✅ Build citation HANYA jika backend kirim sources
//   let citationHtml = "";
//   if (apiResult.sources?.length > 0) {
//     citationHtml = `
//       <div class="source-citation">
//         📖 Sumber: ${apiResult.sources
//           .map((s) => {
//             const url = s.url || "https://serdangbedagaikab.bps.go.id/";
//             const name = s.name || "BPS Kabupaten Serdang Bedagai";
//             const page = s.page ? `, hlm. ${s.page}` : "";
//             return `<a href="${url}" target="_blank" rel="noopener noreferrer">${name}${page}</a>`;
//           })
//           .join("; ")}
//       </div>
//     `;
//   }

//   // Format answer dengan markdown-like syntax
//   let formattedAnswer = apiResult.answer
//     .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>") // Bold
//     .replace(/^\*\s/gm, "• ") // Bullet list
//     .replace(/\n/g, "<br>"); // New line

//   return {
//     text: formattedAnswer + citationHtml,
//     meta: apiResult.meta,
//     timestamp: apiResult.timestamp,
//     isError: false,
//   };
// }

export function formatBotResponse(apiResult, originalQuestion) {
  if (!apiResult.success) {
    return {
      text: apiResult.error || CONFIG.fallback.noAnswer,
      isError: true,
      errorType: apiResult.type,
    };
  }

  // ✅ Langsung gunakan text dari Gemini, tanpa tambah blok sumber otomatis
  let formattedAnswer = apiResult.answer
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/^\*\s/gm, "• ")
    .replace(/\n/g, "<br>");

  return {
    text: formattedAnswer,
    meta: apiResult.meta,
    timestamp: apiResult.timestamp,
    isError: false,
  };
}

// ========== UTILITIES: Model Selection Persistence ==========

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
 * Inisialisasi dropdown model saat page load
 * (Panggil di js/app.js saat DOM ready)
 */
export function initModelSelector() {
  const select = document.getElementById("model-select");
  if (select) {
    const saved = loadModelPreference();
    select.value = saved;

    // Simpan saat user ganti pilihan
    select.addEventListener("change", (e) => {
      saveModelPreference(e.target.value);
      console.log(`🔄 Model changed to: ${e.target.value}`);
    });

    console.log(`✅ Model selector initialized: ${saved}`);
  }
}

// ... kode existing di api.js ...

window.fetchBPSData = fetchBPSData;
