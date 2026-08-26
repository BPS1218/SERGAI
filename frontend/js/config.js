/**
 * sergAI
 * BPS Kabupaten Serdang Bedagai
 */

export const CONFIG = {
  googleClientId:
    "445651010935-rerc0ceit8mom8n413fjo72cv0d0b7dj.apps.googleusercontent.com",
  googleScriptUrl:
    "https://script.google.com/macros/s/AKfycbzRt6Z5S8i9bFv2kuom4J9cWsRqSs4u3iDv13DmLsL2rWvvyuQuXYoxlPnAhgkhbptVLg/exec",
  // 🎨 Branding
  branding: {
    name: "SERGAI",
    fullName:
      "Smart Engagement for Responsive Government Assistant Intelligence",
    tagline: "Tanya Data, sergAI Jawab!",
    developer: "Elgresia -  CPNS 2026",
    // ✅ FIX: Hardcode nama langsung agar tidak error "Cannot access before initialization"
    welcomeMessage: `Halo! 👋 Saya sergAI, asisten virtual BPS Kabupaten Serdang Bedagai.

Saya membantu Anda mengakses data statistik resmi secara cepat dan akurat.`,
  },

  // 🔌 API Configuration (Ganti dengan endpoint backend Anda nanti)
  api: {
    // Endpoint backend orchestrator (Vercel/Netlify)
    endpoint: "/api/chat",

    // Fallback: Direct BPS WebAPI (untuk testing saja)
    bpsApi: {
      baseUrl: "https://webapi.bps.go.id/v1/api",
      domainId: "1218", // ⚠️ Pastikan ini domain_id Sergai
    },

    // Timeout & retry
    timeout: 45000, // 45 detik
    maxRetries: 2,
  },

  //  Quick Replies (tombol saran)
  quickReplies: [
    {
      icon: "",
      text: "PDRB Sergai",
      query: "Berapa PDRB Kabupaten Serdang Bedagai tahun 2024?",
    },
    {
      icon: "👥",
      text: "Jumlah Penduduk",
      query: "Berapa jumlah penduduk Sergai terbaru?",
    },
    {
      icon: "📉",
      text: "Data Kemiskinan",
      query: "Berapa persentase penduduk miskin di Sergai?",
    },
    {
      icon: "📖",
      text: "Unduh Publikasi",
      query: "Di mana saya bisa download Sergai Dalam Angka?",
    },
    { icon: "❓", text: "Bantuan", query: "Fitur apa saja yang kamu miliki?" },
  ],

  // 🛡️ Fallback Messages
  fallback: {
    noAnswer: `Maaf, saya belum menemukan data yang Anda cari. \n\nSilakan:\n• Periksa kembali kata kunci pertanyaan\n• Gunakan quick reply di bawah\n• Hubungi PST BPS Sergai: (0621) XXX-XXX`,
    apiError:
      "Terjadi kendala koneksi ke server data. Silakan coba beberapa saat lagi.",
    offline: "Anda sedang offline. Beberapa fitur mungkin tidak tersedia.",
  },
};

// Helper: Get API headers
export const getApiHeaders = () => ({
  "Content-Type": "application/json",
  // Tambahkan auth header jika backend membutuhkan
  // "Authorization": `Bearer ${process.env.API_TOKEN}`
});
