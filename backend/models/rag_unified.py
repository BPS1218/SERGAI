"""
backend/models/rag_unified.py
Modul RAG Terpadu untuk sergAI

Alur pencarian:
1. Pre-check conversational (jawab langsung)
2. Pre-check layanan/fitur (jawab dari prompt)
3. Cari di DATABASE SPREADSHEET → fetch BPS WebAPI
   - Kalau OK → generate jawaban
   - Kalau kosong → LANJUT cek rekap (jangan menyerah!)
4. Cari di REKAP SPREADSHEET → fetch CSV dari sheet
5. Kalau keduanya gagal → arahkan ke PST BPS
6. Generate jawaban: Gemini → fallback OpenAI
7. Jika user minta tabel → kirim data terstruktur untuk download Excel
"""
import re
import time
import hashlib
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs
from io import StringIO
import httpx
import pandas as pd

from config import settings
from .base import BaseModel, ModelResponse, Source
from .gemini import GeminiModel
from .openai import OpenAIModel


class RAGUnifiedModel(BaseModel):
    """RAG terpadu: 2 sumber spreadsheet + fallback LLM + tabel download"""
    
    def __init__(self):
        # ===== Cache =====
        self._db_df_cache = None
        self._db_cache_expiry = 0
        self._rekap_df_cache = None
        self._rekap_cache_expiry = 0
        self._csv_data_cache = {}
        self.cache_ttl = settings.cache_ttl
        
        # ===== LLM instances (lazy init) =====
        self._gemini = None
        self._openai = None
        
        # ===== BPS WebAPI =====
        self.api_key = settings.bps_api_key
        self.domain_id = settings.bps_domain_id
        self.base_url = settings.bps_base_url
        
        # ===== Spreadsheet IDs =====
        self.db_sheet_id = settings.database_spreadsheet_id
        self.rekap_sheet_id = settings.rekap_spreadsheet_id
      # ===== Candidate matching =====
        self.max_candidate_choices = 10

        # Jika selisih skor kandidat pertama dan kedua
        # lebih kecil dari ini → anggap ambigu
        self.candidate_score_gap = 15

        # Minimum coverage keyword agar kandidat dianggap kuat
        self.candidate_min_coverage = 0.75

        # Similarity judul untuk dianggap data yang sama / sangat mirip
        self.title_similarity_threshold = 0.88 
    
    def get_model_info(self) -> Dict:
        return {
            "name": "RAG Unified",
            "model": "rag-unified-v1",
            "provider": "Multi-source RAG + Gemini/OpenAI",
            "capabilities": [
                "database_spreadsheet_mapping",
                "bps_webapi_dynamic",
                "sheet_csv_fetch",
                "gemini_to_openai_fallback",
                "year_fallback",
                "definisi_interpretasi",
                "rekap_fallback",
                "structured_table_export"
            ]
        }

    # ============================================================
# ===== SEARCH / CANDIDATE HELPERS ===========================
# ============================================================

    def _normalize_search_text(self, text: str) -> str:
        """
        Normalisasi text untuk matching.
        """
        text = str(text or "").lower().strip()

        text = re.sub(r"[^a-z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text)

        return text.strip()


    def _search_keywords(self, question: str) -> List[str]:
        """
        Ambil kata penting dari pertanyaan.
        """

        q = self._normalize_search_text(question)

        stopwords = {
            "berapa",
            "bagaimana",
            "adalah",
            "tentang",
            "untuk",
            "dengan",
            "yang",
            "dari",
            "pada",
            "data",
            "tabel",
            "tampilkan",
            "tolong",
            "minta",
            "cari",
            "lihat",
            "sergai",
            "kabupaten",
            "serdang",
            "bedagai",
            "tahun",
        }

        keywords = []

        for word in q.split():
            if word in stopwords:
                continue

            # tetap izinkan singkatan:
            # IPM, TPT, PDRB, dll
            if len(word) >= 2:
                keywords.append(word)

        return keywords


    def _candidate_id(self, title: str) -> str:
        """
        Buat ID kandidat stabil berdasarkan judul.
        """

        normalized = self._normalize_search_text(title)

        digest = hashlib.sha1(
            normalized.encode("utf-8")
        ).hexdigest()[:12]

        return f"candidate:{digest}"


    def _title_similarity(
        self,
        title_a: str,
        title_b: str
    ) -> float:

        a = self._normalize_search_text(title_a)
        b = self._normalize_search_text(title_b)

        if not a or not b:
            return 0.0

        if a == b:
            return 1.0

        return SequenceMatcher(
            None,
            a,
            b
        ).ratio()

    def _calculate_candidate_score(
        self,
        question: str,
        title: str,
        short_title: str = ""
    ) -> Dict:

        query_normalized = self._normalize_search_text(
            question
        )

        title_normalized = self._normalize_search_text(
            title
        )

        short_normalized = self._normalize_search_text(
            short_title
        )

        keywords = self._search_keywords(question)

        # tahun jangan dijadikan keyword biasa
        keywords = [
            k for k in keywords
            if not re.fullmatch(r"20\d{2}", k)
        ]

        if not keywords:
            return {
                "score": 0,
                "coverage": 0,
                "matched_keywords": [],
                "exact": False,
            }

        matched_keywords = []

        for keyword in keywords:

            if (
                keyword in title_normalized
                or (
                    short_normalized
                    and keyword in short_normalized
                )
            ):
                matched_keywords.append(keyword)

        coverage = (
            len(matched_keywords)
            / len(keywords)
        )

        score = 0.0

        # ==========================================
        # Exact match
        # ==========================================

        exact = (
            query_normalized == title_normalized
            or (
                short_normalized
                and query_normalized == short_normalized
            )
        )

        if exact:
            score += 100

        # ==========================================
        # Seluruh query muncul sebagai phrase
        # ==========================================

        if (
            query_normalized
            and query_normalized in title_normalized
        ):
            score += 35

        if (
            short_normalized
            and query_normalized
            and query_normalized in short_normalized
        ):
            score += 35

        # ==========================================
        # Keyword match
        # ==========================================

        score += len(matched_keywords) * 10

        if coverage == 1:
            score += 25

        elif coverage >= 0.75:
            score += 15

        elif coverage >= 0.5:
            score += 5

        # sedikit preferensi judul yang lebih ringkas
        title_word_count = len(
            title_normalized.split()
        )

        if (
            coverage == 1
            and title_word_count <= 12
        ):
            score += 2

        return {
            "score": round(score, 3),
            "coverage": round(coverage, 3),
            "matched_keywords": matched_keywords,
            "exact": exact,
        }

# ============================================================
# ===== MAIN ENTRY POINT =====
# ============================================================
    
    async def generate_response(
        self,
        question: str,
        chat_history: Optional[List[Dict]] = None,
        context: Optional[Dict] = None
    ) -> ModelResponse:
        try:
            q_lower = question.lower().strip()
            
            # ✅ Deteksi intent "mau lihat tabel / unduh"
            table_keywords = [
                "tabel", "tampilkan", "rinci", "rincian", "lengkap",
                "semua data", "daftar", "excel", "unduh", "download", "csv"
            ]
            include_table = any(kw in q_lower for kw in table_keywords)
            
            # ===== 1a. PRE-CHECK: Conversational =====
            conversational_keywords = [
                "halo", "hi", "hello", "siapa kamu", "apa itu sergai",
                "kamu siapa", "bisa bantu", "test", "apa kabar",
                "terima kasih", "makasih", "thanks"
            ]
            if len(q_lower) < 60 and any(kw in q_lower for kw in conversational_keywords):
                return ModelResponse(
                    answer=(
                        "Halo! 👋 Saya adalah **sergAI** (Smart Engagement for "
                        "Responsive Government Assistant Intelligence), asisten "
                        "virtual resmi BPS Kabupaten Serdang Bedagai.\n\n"
                        "Saya siap membantu Anda mengakses data statistik seperti:\n"
                        "• 📊 Ekonomi\n"
                        "• 👥 Penduduk\n"
                        "• 📉 Kemisikinan\n"
                        "• 🌾 Produksi pertanian\n\n"
                        "Silakan tanyakan data spesifik yang Anda butuhkan!"
                    ),
                    sources=[Source(
                        name="BPS Kabupaten Serdang Bedagai",
                        url="https://serdangbedagaikab.bps.go.id"
                    )],
                    meta={"provider": "rag_unified", "type": "conversational", "model_used": "none"},
                    success=True
                )

            # ===== 1b. PRE-CHECK: Pertanyaan layanan / fitur / bantuan =====
            info_keywords = [
                "layanan", "fitur", "bisa apa", "kamu bisa", "kemampuan",
                "bantuan", "help", "cara pakai", "cara menggunakan",
                "alamat", "jam layanan", "jam buka", "kontak", "email",
                "telepon", "media sosial", "unduh", "download",
                "pst", "pelayanan statistik",
                "link", "url", "website", "situs",
                "tabel statistik", "publikasi", "buku", "download publikasi",
                "brs", "berita resmi", "siaran pers", "berita",
                "metadata", "sirusa", "indikator", "definisi lengkap"
            ]
            if len(q_lower) < 120 and any(kw in q_lower for kw in info_keywords):
                return await self._generate_with_fallback(
                    question=question,
                    context_text=(
                        "(Tidak ada konteks data statistik. Jawab pertanyaan ini "
                        "hanya berdasarkan bagian PENGETAHUAN LAYANAN & FITUR "
                        "di system prompt. Jika user bertanya link/URL, WAJIB "
                        "berikan link yang relevan dari daftar di prompt.)"
                    ),
                    sources=[Source(
                        name="BPS Kabupaten Serdang Bedagai",
                        url="https://serdangbedagaikab.bps.go.id"
                    )],
                    meta={"provider": "rag_unified", "type": "info_layanan"},
                    chat_history=chat_history,
                    table=None
                )
            
            # ===== 2. SEARCH DI DATABASE SPREADSHEET =====
            # ============================================================
    # ===== 2. SEARCH CANDIDATES ================================
    # ============================================================

            selected_candidate_id = None

            if context:
                selected_candidate_id = (
                    context.get(
                        "selected_candidate_id"
                    )
                )

            candidate_groups = (
                await self._find_candidate_groups(
                    question
                )
            )

            if not candidate_groups:

                return ModelResponse(
                    answer=(
                        "Maaf, data yang Anda tanyakan belum tersedia di sistem kami.\n\n"
                        "Sistem sergAI masih dalam tahap pengembangan. Untuk sementara, "
                        "silakan kunjungi langsung **Pelayanan Statistik Terpadu (PST) "
                        "BPS Kabupaten Serdang Bedagai** untuk mendapatkan data tersebut.\n\n"
                        "atau mengunjungi Website: "
                        "https://serdangbedagaikab.bps.go.id"
                    ),
                    sources=[
                        Source(
                            name="BPS Kabupaten Serdang Bedagai",
                            url="https://serdangbedagaikab.bps.go.id",
                        )
                    ],
                    meta={
                        "provider": "rag_unified",
                        "type": "not_found",
                        "model_used": "none",
                    },
                    success=True,
                )


            # ============================================================
            # USER BELUM MEMILIH KANDIDAT
            # ============================================================

            selected_group = None

            if not selected_candidate_id:

                if self._should_show_candidate_selection(
                    question,
                    candidate_groups,
                ):

                    public_candidates = (
                        self._candidate_public_view(
                            candidate_groups
                        )
                    )

                    total = len(
                        candidate_groups
                    )

                    if total > len(
                        public_candidates
                    ):

                        answer = (
                            "Saya menemukan beberapa data yang sesuai "
                            "dengan pertanyaan Anda. Silakan pilih data "
                            "yang paling sesuai.\n\n"
                            f"Ditampilkan {len(public_candidates)} dari "
                            f"{total} data yang ditemukan."
                        )

                    else:

                        answer = (
                            "Saya menemukan beberapa data yang sesuai "
                            "dengan pertanyaan Anda. Silakan pilih data "
                            "yang paling sesuai."
                        )

                    return ModelResponse(
                        answer=answer,
                        sources=[],
                        table=None,
                        meta={
                            "provider": "rag_unified",

                            "type": "candidate_selection",

                            "model_used": "none",

                            "candidates": public_candidates,

                            "candidate_count": total,

                            "original_question": question,
                        },
                        success=True,
                    )

                # kandidat #1 sangat kuat
                selected_group = (
                    candidate_groups[0]
                )


            # ============================================================
            # USER SUDAH MEMILIH
            # ============================================================

            else:

                selected_group = next(
                    (
                        group
                        for group
                        in candidate_groups
                        if group[
                            "candidate_id"
                        ] == selected_candidate_id
                    ),
                    None,
                )

                if selected_group is None:

                    return ModelResponse(
                        answer=(
                            "Pilihan data tersebut tidak lagi ditemukan. "
                            "Silakan ketik kembali data yang ingin Anda cari."
                        ),
                        sources=[],
                        meta={
                            "provider": "rag_unified",
                            "type": "candidate_expired",
                            "model_used": "none",
                        },
                        success=True,
                    )


            # ============================================================
            # ===== 3. RESOLVE SOURCE TERBAIK ===========================
            # ============================================================

            resolved = (
                await self._resolve_best_variant(
                    selected_group,
                    question,
                )
            )

            if not resolved:

                return ModelResponse(
                    answer=(
                        f"Maaf, data **{selected_group['title']}** "
                        "belum tersedia pada sumber data yang dapat diakses saat ini.\n\n"
                        "Silakan kunjungi Pelayanan Statistik Terpadu "
                        "(PST) BPS Kabupaten Serdang Bedagai."
                    ),
                    sources=[
                        Source(
                            name="BPS Kabupaten Serdang Bedagai",
                            url="https://serdangbedagaikab.bps.go.id",
                        )
                    ],
                    meta={
                        "provider": "rag_unified",
                        "type": "data_unavailable",
                        "model_used": "none",
                    },
                    success=True,
                )


            # ============================================================
            # ===== 3a. SOURCE = BPS WEBAPI =============================
            # ============================================================

            if (
                resolved["source_type"]
                == "database_bps"
            ):

                variant = resolved[
                    "variant"
                ]

                used_year = resolved[
                    "year"
                ]

                context_text = (
                    self._build_context_bps(
                        title=variant[
                            "title"
                        ],

                        year=used_year,

                        data=resolved[
                            "table_context"
                        ],

                        definisi=variant.get(
                            "definisi",
                            ""
                        ),

                        interpretasi=variant.get(
                            "interpretasi",
                            ""
                        ),
                    )
                )

                sources = [
                    Source(
                        name="Tabel Dinamis BPS",

                        url=(
                            "https://serdangbedagaikab.bps.go.id"
                        ),

                        page=variant[
                            "title"
                        ],
                    )
                ]

                if variant.get(
                    "link_sumber"
                ):

                    sources.append(
                        Source(
                            name="Sumber Definisi",

                            url=variant[
                                "link_sumber"
                            ],

                            type="definition",
                        )
                    )

                return await self._generate_with_fallback(

                    question=question,

                    context_text=context_text,

                    sources=sources,

                    meta={
                        "provider": "rag_unified",

                        "source": "database_bps",

                        "var_id": variant[
                            "var_id"
                        ],

                        "selected_candidate_id": (
                            selected_group[
                                "candidate_id"
                            ]
                        ),

                        "selected_title": (
                            selected_group[
                                "title"
                            ]
                        ),

                        "used_year": used_year,
                    },

                    chat_history=chat_history,

                    table=(
                        resolved[
                            "table_payload"
                        ]
                        if include_table
                        else None
                    ),
                )


            # ============================================================
            # ===== 3b. SOURCE = DATA PRIORITAS =========================
            # ============================================================

            variant = resolved[
                "variant"
            ]

            csv_df = resolved[
                "df"
            ]

            judul = variant[
                "title"
            ]

            sumber_tabel = (
                variant.get(
                    "sumber_tabel"
                )
                or "BPS Kabupaten Serdang Bedagai"
            )

            table_payload = (
                self._df_to_table_payload(
                    csv_df,
                    judul,
                    sumber_tabel,
                )
            )

            context_text = (
                self._build_context_sheet(

                    judul=judul,

                    df=csv_df,

                    definisi=variant.get(
                        "definisi",
                        ""
                    ),

                    interpretasi=variant.get(
                        "interpretasi",
                        ""
                    ),

                    sheet_name=variant.get(
                        "sheet",
                        ""
                    ),

                    sumber=sumber_tabel,
                )
            )

            sources = [
                Source(
                    name=sumber_tabel,

                    url=variant.get(
                        "link",
                        ""
                    ),

                    page=judul,
                )
            ]

            if variant.get(
                "link_sumber"
            ):

                sources.append(
                    Source(
                        name="Sumber Definisi",

                        url=variant[
                            "link_sumber"
                        ],

                        type="definition",
                    )
                )

            return await self._generate_with_fallback(

                question=question,

                context_text=context_text,

                sources=sources,

                meta={
                    "provider": "rag_unified",

                    "source": "rekap_sheet",

                    "selected_candidate_id": (
                        selected_group[
                            "candidate_id"
                        ]
                    ),

                    "selected_title": judul,

                    "used_year": resolved.get(
                        "year"
                    ),
                },

                chat_history=chat_history,

                table=table_payload,
            )
            
            # ===== 4. TIDAK ADA YANG MATCH / DATA TIDAK TERSEDIA =====
            
            if bps_failed_info:
                year_msg = bps_failed_info["user_year"] or "2020-2025"
                return ModelResponse(
                    answer=(
                        f"Maaf, data '{bps_failed_info['title']}' untuk tahun "
                        f"{year_msg} belum tersedia di sistem BPS maupun di sumber prioritas kami.\n\n"
                        "Silakan kunjungi langsung **Pelayanan Statistik Terpadu "
                        "(PST) BPS Kabupaten Serdang Bedagai** untuk mendapatkan "
                        "data tersebut.\n\n"
                        "atau mengunjungi Website: https://serdangbedagaikab.bps.go.id"
                    ),
                    sources=[Source(
                        name="BPS Kabupaten Serdang Bedagai",
                        url="https://serdangbedagaikab.bps.go.id"
                    )],
                    meta={"provider": "rag_unified", "type": "bps_empty_rekap_miss", "model_used": "none"},
                    success=True
                )
            
            return ModelResponse(
                answer=(
                    "Maaf, data yang Anda tanyakan belum tersedia di sistem kami.\n\n"
                    "Sistem sergAI masih dalam tahap pengembangan. Untuk sementara, "
                    "silakan kunjungi langsung **Pelayanan Statistik Terpadu (PST) "
                    "BPS Kabupaten Serdang Bedagai** untuk mendapatkan data tersebut.\n\n"
                    "atau mengunjungi Website: https://serdangbedagaikab.bps.go.id"
                ),
                sources=[Source(
                    name="BPS Kabupaten Serdang Bedagai",
                    url="https://serdangbedagaikab.bps.go.id"
                )],
                meta={"provider": "rag_unified", "type": "not_found", "model_used": "none"},
                success=True
            )
            
        except Exception as e:
            print(f"❌ RAG Unified Error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return ModelResponse(
                answer="Maaf, terjadi kendala teknis. Silakan coba lagi.",
                error=str(e),
                meta={"model_used": "error"},
                success=False
            )
    
    # ============================================================
    # ===== DATABASE SPREADSHEET (BPS WebAPI) =====
    # ============================================================
    
    async def _load_database_df(self) -> pd.DataFrame:
        now = time.time()
        if self._db_df_cache is not None and now < self._db_cache_expiry:
            return self._db_df_cache
        
        url = f"https://docs.google.com/spreadsheets/d/{self.db_sheet_id}/export?format=csv"
        
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(url)
                res.raise_for_status()
                df = pd.read_csv(StringIO(res.text))
                df.columns = [str(c).strip().lower() for c in df.columns]
                self._db_df_cache = df
                self._db_cache_expiry = now + self.cache_ttl
                print(f"💾 Loaded database spreadsheet: {len(df)} rows")
                return df
        except Exception as e:
            print(f"❌ Failed to load database spreadsheet: {e}")
            return pd.DataFrame()

    async def _get_database_candidates(
        self,
        question: str
    ) -> List[Dict]:

        df = await self._load_database_df()

        if df.empty:
            return []

        candidates = []

        for _, row in df.iterrows():

            title = str(
                row.get("title", "")
            ).strip()

            var_id = str(
                row.get("var_id", "")
            ).strip()

            if (
                not title
                or not var_id
                or title.lower() == "nan"
                or var_id.lower() == "nan"
            ):
                continue

            scoring = self._calculate_candidate_score(
                question=question,
                title=title,
            )

            if scoring["score"] <= 0:
                continue

            candidates.append({
                "source_type": "database_bps",

                "title": title,
                "short_title": "",

                "score": scoring["score"],
                "coverage": scoring["coverage"],
                "matched_keywords": scoring[
                    "matched_keywords"
                ],
                "exact": scoring["exact"],

                "var_id": var_id,

                "definisi": (
                    str(row.get("definisi", ""))
                    if pd.notna(
                        row.get("definisi")
                    )
                    else ""
                ),

                "interpretasi": (
                    str(
                        row.get(
                            "interpretasi",
                            ""
                        )
                    )
                    if pd.notna(
                        row.get("interpretasi")
                    )
                    else ""
                ),

                "link_sumber": (
                    str(
                        row.get(
                            "link sumber definisi+interpretasi",
                            ""
                        )
                    )
                    if pd.notna(
                        row.get(
                            "link sumber definisi+interpretasi"
                        )
                    )
                    else ""
                ),
            })

        return candidates   

    async def _get_rekap_candidates(
        self,
        question: str
    ) -> List[Dict]:

        df = await self._load_rekap_df()

        if df.empty:
            return []

        col_map = {
            c.lower().replace(" ", "_"): c
            for c in df.columns
        }

        def get_col(row, *candidates):

            for c in candidates:

                key = (
                    c.lower()
                    .replace(" ", "_")
                )

                if key in col_map:

                    val = row.get(
                        col_map[key]
                    )

                    if pd.notna(val):
                        return str(val).strip()

            return ""

        candidates = []

        for _, row in df.iterrows():

            title = get_col(
                row,
                "Judul Tabel",
                "judul_tabel",
                "Judul",
            )

            if not title:
                continue

            short_title = get_col(
                row,
                "Judul SIngkat",
                "Judul Singkat",
                "judul_singkat",
                "Judul singkat",
            )

            scoring = self._calculate_candidate_score(
                question=question,
                title=title,
                short_title=short_title,
            )

            if scoring["score"] <= 0:
                continue

            candidates.append({

                "source_type": "rekap",

                "title": title,
                "short_title": short_title,

                "score": scoring["score"],
                "coverage": scoring["coverage"],
                "matched_keywords": scoring[
                    "matched_keywords"
                ],
                "exact": scoring["exact"],

                "judul_tabel": title,

                "judul_singkat": short_title,

                "definisi": get_col(
                    row,
                    "definisi",
                    "Definisi",
                ),

                "interpretasi": get_col(
                    row,
                    "interpretasi",
                    "Interpretasi",
                ),

                "link_sumber": get_col(
                    row,
                    "Link sumber definisi+interpretasi",
                    "link_sumber",
                ),

                "sumber_tabel": get_col(
                    row,
                    "Sumber Tabel",
                    "sumber_tabel",
                    "sumber tabel",
                    "Sumber",
                    "Sumber Data",
                ),

                "link": get_col(
                    row,
                    "Link",
                    "link",
                ),

                "sheet": get_col(
                    row,
                    "Sheet",
                    "sheet",
                    "Sheet Name",
                ),
            })

        return candidates

    async def _find_candidate_groups(
        self,
        question: str
    ) -> List[Dict]:

        database_candidates = (
            await self._get_database_candidates(
                question
            )
        )

        rekap_candidates = (
            await self._get_rekap_candidates(
                question
            )
        )

        all_candidates = (
            database_candidates
            + rekap_candidates
        )

        # skor tertinggi dulu
        all_candidates.sort(
            key=lambda x: (
                x["score"],
                x["coverage"]
            ),
            reverse=True
        )

        groups = []

        for candidate in all_candidates:

            matched_group = None

            for group in groups:

                similarity = self._title_similarity(
                    candidate["title"],
                    group["title"],
                )

                if (
                    similarity
                    >= self.title_similarity_threshold
                ):
                    matched_group = group
                    break

            # ======================================
            # Belum ada grup → buat grup baru
            # ======================================

            if matched_group is None:

                groups.append({
                    "candidate_id": self._candidate_id(
                        candidate["title"]
                    ),

                    "title": candidate["title"],

                    "score": candidate["score"],

                    "coverage": candidate["coverage"],

                    "exact": candidate["exact"],

                    "variants": [
                        candidate
                    ],
                })

                continue

            # ======================================
            # Kandidat merupakan sumber alternatif
            # dari judul yang sama / sangat mirip
            # ======================================

            matched_group[
                "variants"
            ].append(candidate)

            # gunakan skor tertinggi sebagai skor grup
            if (
                candidate["score"]
                > matched_group["score"]
            ):

                matched_group["score"] = (
                    candidate["score"]
                )

                matched_group["coverage"] = (
                    candidate["coverage"]
                )

                matched_group["exact"] = (
                    candidate["exact"]
                )

                matched_group["title"] = (
                    candidate["title"]
                )

        # Urutkan lagi setelah grouping
        groups.sort(
            key=lambda x: (
                x["score"],
                x["coverage"]
            ),
            reverse=True
        )

        return groups

    def _should_show_candidate_selection(
        self,
        question: str,
        groups: List[Dict],
    ) -> bool:

        if len(groups) <= 1:
            return False

        keywords = self._search_keywords(
            question
        )

        keywords = [
            k for k in keywords
            if not re.fullmatch(
                r"20\d{2}",
                k
            )
        ]

        # ==========================================
        # QUERY SANGAT SINGKAT
        # contoh:
        # penduduk
        # kemiskinan
        # pdrb
        # ==========================================

        if len(keywords) == 1:

            keyword = keywords[0]

            literal_groups = [
                group
                for group in groups
                if keyword
                in self._normalize_search_text(
                    group["title"]
                )
            ]

            if len(literal_groups) > 1:
                return True

        top = groups[0]
        second = groups[1]

        # ==========================================
        # Exact match boleh langsung
        # ==========================================

        if top.get("exact"):
            return False

        score_gap = (
            top["score"]
            - second["score"]
        )

        # ==========================================
        # kandidat pertama sangat dominan
        # ==========================================

        if (
            top["coverage"] >= 0.90
            and score_gap
            >= self.candidate_score_gap
        ):
            return False

        # ==========================================
        # kandidat berdekatan → ambigu
        # ==========================================

        if (
            score_gap
            < self.candidate_score_gap
        ):
            return True

        if (
            top["coverage"]
            < self.candidate_min_coverage
        ):
            return True

        return False

    def _candidate_public_view(
        self,
        groups: List[Dict],
    ) -> List[Dict]:

        output = []

        for group in groups[
            :self.max_candidate_choices
        ]:

            sources = []

            for variant in group[
                "variants"
            ]:

                if (
                    variant["source_type"]
                    == "database_bps"
                ):
                    label = "Tabel Dinamis BPS"

                else:
                    label = (
                        variant.get(
                            "sumber_tabel"
                        )
                        or "Data Prioritas"
                    )

                if label not in sources:
                    sources.append(label)

            output.append({

                "id": group[
                    "candidate_id"
                ],

                "title": group[
                    "title"
                ],

                "sources": sources,
            })

        return output

    def _extract_years_from_text(
        self,
        text: str
    ) -> List[int]:

        years = re.findall(
            r"\b(20\d{2})\b",
            str(text or "")
        )

        result = []

        for year in years:

            y = int(year)

            if 2000 <= y <= 2100:
                result.append(y)

        return result

    def _detect_latest_year_from_df(
        self,
        df: pd.DataFrame,
        title: str = "",
        sheet_name: str = "",
    ) -> Optional[str]:

        years = []

        # ==========================================
        # Judul
        # ==========================================

        years.extend(
            self._extract_years_from_text(
                title
            )
        )

        # ==========================================
        # Nama sheet
        # ==========================================

        years.extend(
            self._extract_years_from_text(
                sheet_name
            )
        )

        # ==========================================
        # Nama kolom
        # ==========================================

        for column in df.columns:

            years.extend(
                self._extract_years_from_text(
                    str(column)
                )
            )

        # ==========================================
        # Isi data
        # Batasi agar tidak scan berlebihan
        # ==========================================

        sample = df.head(200)

        for column in sample.columns:

            values = (
                sample[column]
                .dropna()
                .astype(str)
                .tolist()
            )

            for value in values:

                years.extend(
                    self._extract_years_from_text(
                        value
                    )
                )

        if not years:
            return None

        return str(max(years))

    def _df_contains_year(
        self,
        df: pd.DataFrame,
        year: str,
    ) -> bool:

        if not year:
            return True

        # kolom
        for column in df.columns:

            if (
                year
                in str(column)
            ):
                return True

        # data
        sample = df.head(500)

        for column in sample.columns:

            series = (
                sample[column]
                .dropna()
                .astype(str)
            )

            if series.str.contains(
                year,
                regex=False
            ).any():
                return True

        return False

    async def _resolve_best_variant(
        self,
        group: Dict,
        question: str,
    ) -> Optional[Dict]:

        user_year = self._extract_year(
            question
        )

        available = []

        for variant in group[
            "variants"
        ]:

            # ======================================
            # BPS WEBAPI
            # ======================================

            if (
                variant["source_type"]
                == "database_bps"
            ):

                table_context, used_year, table_payload = (
                    await self._fetch_bps_with_fallback(
                        variant["var_id"],
                        user_year,
                    )
                )

                if not table_context:
                    continue

                available.append({

                    "source_type": "database_bps",

                    "year": used_year,

                    "numeric_year": (
                        int(used_year)
                        if used_year
                        else 0
                    ),

                    "variant": variant,

                    "table_context": table_context,

                    "table_payload": table_payload,
                })

            # ======================================
            # SHEET PRIORITAS
            # ======================================

            elif (
                variant["source_type"]
                == "rekap"
            ):

                link = variant.get(
                    "link",
                    ""
                )

                csv_df = (
                    await self._fetch_sheet_csv(
                        link
                    )
                )

                if (
                    csv_df is None
                    or csv_df.empty
                ):
                    continue

                # Kalau user meminta tahun tertentu,
                # jangan anggap sheet cocok kalau tahun
                # tersebut tidak ditemukan
                if (
                    user_year
                    and not self._df_contains_year(
                        csv_df,
                        user_year,
                    )
                ):
                    continue

                detected_year = (
                    user_year
                    or self._detect_latest_year_from_df(
                        csv_df,
                        title=variant.get(
                            "title",
                            ""
                        ),
                        sheet_name=variant.get(
                            "sheet",
                            ""
                        ),
                    )
                )

                available.append({

                    "source_type": "rekap",

                    "year": detected_year,

                    "numeric_year": (
                        int(detected_year)
                        if detected_year
                        else 0
                    ),

                    "variant": variant,

                    "df": csv_df,
                })

        if not available:
            return None

        # ==================================================
        # USER MEMINTA TAHUN TERTENTU
        # ==================================================

        if user_year:

            exact_year_sources = [
                item
                for item in available
                if item.get("year")
                == user_year
            ]

            if exact_year_sources:

                available = exact_year_sources

        # ==================================================
        # PRIORITAS:
        #
        # 1. tahun terbaru
        # 2. jika tahun sama:
        #    data prioritas/sheet sedikit diprioritaskan
        # ==================================================

        available.sort(
            key=lambda item: (
                item.get(
                    "numeric_year",
                    0
                ),

                1
                if item[
                    "source_type"
                ] == "rekap"
                else 0
            ),
            reverse=True
        )

        selected = available[0]

        print(
            "✅ Selected source:",
            selected["source_type"],
            "| year:",
            selected.get("year"),
            "| title:",
            group["title"]
        )

        return selected

    async def _match_database_spreadsheet(self, question: str) -> Optional[Dict]:
        df = await self._load_database_df()
        if df.empty:
            return None
        
        q = question.lower()
        keywords = [k for k in re.findall(r'\b\w+\b', q) if len(k) > 3]
        
        best_match = None
        best_score = 0
        
        for _, row in df.iterrows():
            title = str(row.get("title", "")).lower()
            var_id = str(row.get("var_id", "")).strip()
            
            if not var_id or var_id == "nan":
                continue
            
            score = sum(1 for kw in keywords if kw in title)
            
            user_year = self._extract_year(question)
            if user_year and user_year in title:
                score += 2
            
            if score > best_score:
                best_score = score
                best_match = {
                    "var_id": var_id,
                    "title": str(row.get("title", "")),
                    "definisi": str(row.get("definisi", "")) if pd.notna(row.get("definisi")) else "",
                    "interpretasi": str(row.get("interpretasi", "")) if pd.notna(row.get("interpretasi")) else "",
                    "link_sumber": str(row.get("link sumber definisi+interpretasi", "")) if pd.notna(row.get("link sumber definisi+interpretasi")) else ""
                }
        
        if best_score > 0:
            return best_match
        return None
    
    # ============================================================
    # ===== BPS WebAPI Fetch =====
    # ============================================================
    
    def _extract_year(self, text: str) -> Optional[str]:
        match = re.search(r'\b(20\d{2})\b', text)
        return match.group(1) if match else None
    
    async def _fetch_bps_with_fallback(
        self, var_id: str, user_year: Optional[str]
    ) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
        if not self.api_key:
            return None, None, None
        
        if user_year:
            try:
                y = int(user_year)
                if 2010 <= y <= 2100:
                    years_to_check = [str(y - 2000 + 100)]
                else:
                    return None, None, None
            except:
                return None, None, None
    
        else:
            current_year = time.localtime().tm_year

            # cek dari tahun sekarang mundur 6 tahun
            years_to_check = [
                str(year - 2000 + 100)
                for year in range(
                    current_year,
                    current_year - 6,
                    -1
                )
            ]
        for year_code in years_to_check:
            json_data = await self._fetch_bps_json(var_id, year_code)
            if json_data and json_data.get("datacontent"):
                actual_year = str(int(year_code) - 120 + 2020)
                context, payload = self._parse_bps_json(json_data, actual_year)
                print(f"   ✅ BPS OK: var_id={var_id} th={actual_year} (kode {year_code})")
                return context, actual_year, payload
            else:
                print(f"   ⚠️ BPS kosong: var_id={var_id} th_kode={year_code}")
        
        return None, None, None
    
    async def _fetch_bps_json(self, var_id: str, year_code: str) -> Optional[Dict]:
        url = f"{self.base_url}/list/model/data/lang/ind/domain/{self.domain_id}/var/{var_id}/th/{year_code}/key/{self.api_key}/"
        print(f"   🔗 BPS fetch: .../var/{var_id}/th/{year_code}/")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(url)
                res.raise_for_status()
                data = res.json()
                if data.get("status") == "OK":
                    return data
        except Exception as e:
            print(f"   ⚠️ BPS fetch error var={var_id} th={year_code}: {e}")
        return None
    
    def _parse_bps_json(self, json_data: Dict, used_year: str) -> Tuple[str, Optional[Dict]]:
        """Parse JSON BPS → (teks konteks, payload tabel) dengan struktur key:
        {vervar}{var}{turvar}{tahun}{turtahun}"""
        try:
            var_info = json_data.get("var", [{}])[0]
            var_val = str(var_info.get("val"))
            unit = var_info.get("unit", "")
            row_header = json_data.get("labelvervar", "") or "Kategori"
            
            vervars = json_data.get("vervar", [])
            turvars = json_data.get("turvar", [])
            tahuns = json_data.get("tahun", [])
            turtahuns = json_data.get("turtahun", [])
            datacontent = json_data.get("datacontent", {})
            
            tahun_obj = next((t for t in tahuns if str(t.get("label")) == used_year), None)
            if not tahun_obj:
                return "⚠️ Format data tidak dikenali.", None
            tahun_val = str(tahun_obj.get("val"))
            
            def fmt_num(v):
                if isinstance(v, float) and v.is_integer():
                    return f"{int(v):,}"
                if isinstance(v, float):
                    return f"{v:,.2f}"
                return f"{v:,}" if isinstance(v, int) else str(v)
            
            # ✅ Kolom nilai DINAMIS: turvar kalau >1, sonst turtahun, sonst 1 kolom
            if len(turvars) > 1:
                col_mode = "turvar"
                col_list = turvars
            elif len(turtahuns) > 1:
                col_mode = "turtahun"
                col_list = turtahuns
            else:
                col_mode = "single"
                col_list = []
            
            lines = [f"📊 {var_info.get('label', 'Data')} (Tahun {used_year})"]
            priority_rows = []
            other_rows = []
            table_rows = []
            
            for v in vervars:
                v_val = str(v.get("val"))
                row_label = str(v.get("label", f"Kode {v_val}"))
                is_priority = any(k in row_label.upper() for k in ["TOTAL", "JUMLAH", "SERDANG", "KAB"])
                
                parts = [row_label]
                cells = [row_label]
                
                if col_mode == "single":
                    t_val = str(turvars[0]["val"]) if turvars else ""
                    tt_val = str(turtahuns[0]["val"]) if turtahuns else "0"
                    key = f"{v_val}{var_val}{t_val}{tahun_val}{tt_val}"
                    value = datacontent.get(key)
                    val_str = fmt_num(value) if value is not None else ""
                    parts.append(f"{val_str} {unit}".strip())
                    cells.append(val_str)
                else:
                    for c in col_list:
                        c_val = str(c.get("val"))
                        c_label = str(c.get("label", ""))
                        if col_mode == "turvar":
                            tt_val = str(turtahuns[0]["val"]) if turtahuns else "0"
                            key = f"{v_val}{var_val}{c_val}{tahun_val}{tt_val}"
                        else:
                            t_val = str(turvars[0]["val"]) if turvars else ""
                            key = f"{v_val}{var_val}{t_val}{tahun_val}{c_val}"
                        value = datacontent.get(key)
                        val_str = fmt_num(value) if value is not None else ""
                        parts.append(f"{c_label}: {val_str} {unit}".strip())
                        cells.append(val_str)
                
                row_text = " | ".join(parts)
                (priority_rows if is_priority else other_rows).append(
                    ("🔹 " if is_priority else "• ") + row_text
                )
                table_rows.append(cells)
            
            lines.extend(priority_rows)
            lines.extend(other_rows)
            
            if col_mode == "single":
                headers = [f"Nilai ({unit})" if unit else "Nilai"]
            else:
                headers = [str(c.get("label", "")) for c in col_list]
            
            payload = {
                "title": var_info.get("label", "Data"),
                "source": "Tabel Dinamis BPS",
                "columns": [row_header] + headers,
                "rows": table_rows,
                "total_rows": len(table_rows)
            }
            return "\n".join(lines), payload
            
        except Exception as e:
            print(f"❌ BPS parse error: {e}")
            return "⚠️ Gagal memproses format data BPS.", None
        
    # ============================================================
    # ===== REKAP SPREADSHEET =====
    # ============================================================
    
    async def _load_rekap_df(self) -> pd.DataFrame:
        now = time.time()
        if self._rekap_df_cache is not None and now < self._rekap_cache_expiry:
            return self._rekap_df_cache
        
        if settings.rekap_sheet_name:
            url = (
                f"https://docs.google.com/spreadsheets/d/{self.rekap_sheet_id}"
                f"/gviz/tq?tqx=out:csv&sheet={settings.rekap_sheet_name}"
            )
        else:
            url = f"https://docs.google.com/spreadsheets/d/{self.rekap_sheet_id}/export?format=csv"
        
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(url)
                res.raise_for_status()
                df = pd.read_csv(StringIO(res.text))
                df.columns = [str(c).strip() for c in df.columns]
                self._rekap_df_cache = df
                self._rekap_cache_expiry = now + self.cache_ttl
                print(f"💾 Loaded rekap spreadsheet: {len(df)} rows")
                print(f"   Kolom terdeteksi: {list(df.columns)}")
                return df
        except Exception as e:
            print(f"❌ Failed to load rekap spreadsheet: {e}")
            return pd.DataFrame()
    
    async def _match_rekap_spreadsheet(self, question: str) -> Optional[Dict]:
        df = await self._load_rekap_df()
        if df.empty:
            return None
        
        q = question.lower()
        keywords = [k for k in re.findall(r'\b\w+\b', q) if len(k) > 3]
        
        col_map = {c.lower().replace(" ", "_"): c for c in df.columns}
        
        def get_col(row, *candidates):
            for c in candidates:
                key = c.lower().replace(" ", "_")
                if key in col_map:
                    val = row.get(col_map[key])
                    if pd.notna(val):
                        return str(val)
            return ""
        
        best_match = None
        best_score = 0
        
        for _, row in df.iterrows():
            judul = get_col(row, "Judul Tabel", "judul_tabel", "Judul").lower()
            if not judul:
                continue
            
            score = sum(1 for kw in keywords if kw in judul)
            
            judul_singkat = get_col(
                row, "Judul SIngkat", "Judul Singkat", "judul_singkat", "Judul singkat"
            ).lower()
            score += sum(1 for kw in keywords if kw in judul_singkat)
            
            if score > best_score:
                best_score = score
                best_match = {
                    "judul_tabel": get_col(row, "Judul Tabel", "judul_tabel", "Judul"),
                    "judul_singkat": get_col(
                        row, "Judul SIngkat", "Judul Singkat", "judul_singkat", "Judul singkat"
                    ),
                    "definisi": get_col(row, "definisi", "Definisi"),
                    "interpretasi": get_col(row, "interpretasi", "Interpretasi"),
                    "link_sumber": get_col(row, "Link sumber definisi+interpretasi", "link_sumber"),
                    "sumber_tabel": get_col(
                        row, "Sumber Tabel", "sumber_tabel", "sumber tabel", "Sumber", "Sumber Data"
                    ),
                    "link": get_col(row, "Link", "link"),
                    "sheet": get_col(row, "Sheet", "sheet", "Sheet Name")
                }
        
        if best_score > 0:
            return best_match
        return None
    
    async def _fetch_sheet_csv(self, link: str) -> Optional[pd.DataFrame]:
        if not link:
            return None
        
        now = time.time()
        if link in self._csv_data_cache and now < self._csv_data_cache[link]["expiry"]:
            return self._csv_data_cache[link]["df"]
        
        gid = self._extract_gid_from_url(link)
        spreadsheet_id = self._extract_spreadsheet_id_from_url(link)
        
        if not spreadsheet_id:
            print(f"⚠️ Cannot extract spreadsheet ID from: {link}")
            return None
        
        if gid:
            csv_url = (
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
                f"/export?format=csv&gid={gid}"
            )
        else:
            csv_url = (
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
                f"/export?format=csv"
            )
        
        try:
            print(f"🔗 Fetching CSV: {csv_url}")
            print(f"   From link: {link}")
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                res = await client.get(csv_url)
                res.raise_for_status()
                df = pd.read_csv(StringIO(res.text))
                df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                self._csv_data_cache[link] = {"df": df, "expiry": now + self.cache_ttl}
                print(f"💾 Fetched CSV from sheet: {len(df)} rows")
                return df
        except Exception as e:
            print(f"❌ Failed to fetch CSV from {csv_url}: {type(e).__name__}: {e}")
            return None
    
    def _extract_gid_from_url(self, url: str) -> Optional[str]:
        if "#" in url:
            fragment = url.split("#")[1]
            match = re.search(r'gid=(\d+)', fragment)
            if match:
                return match.group(1)
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "gid" in params:
                return params["gid"][0]
        except:
            pass
        return None
    
    def _extract_spreadsheet_id_from_url(self, url: str) -> Optional[str]:
        match = re.search(r'/d/([a-zA-Z0-9_-]+)/', url)
        return match.group(1) if match else None
    
    # ============================================================
    # ===== PAYLOAD TABEL =====
    # ============================================================
    
    def _df_to_table_payload(self, df: pd.DataFrame, title: str, source: str, max_rows: int = 100) -> Dict:
        """Ubah DataFrame → payload tabel untuk frontend"""
        def fmt(v):
            if isinstance(v, float) and v.is_integer():
                return f"{int(v):,}"
            if isinstance(v, float):
                return f"{v:,.2f}"
            return str(v)
        
        head = df.head(max_rows)
        rows = [[fmt(v) for v in r] for r in head.values.tolist()]
        return {
            "title": title,
            "source": source,
            "columns": [str(c) for c in df.columns],
            "rows": rows,
            "total_rows": int(len(df))
        }
    
    # ============================================================
    # ===== BUILD CONTEXT =====
    # ============================================================
    
    def _build_context_bps(
        self, title: str, year: str, data: str, definisi: str, interpretasi: str
    ) -> str:
        parts = [
            f"📋 JUDUL DATA: {title}",
            f"📅 TAHUN REFERENSI: {year}",
            "📖 SUMBER UNTUK JAWABAN: Tabel Dinamis BPS",
            "",
            "📖 DEFINISI:",
            definisi if definisi else "(Tidak tersedia)",
            "",
            "💡 INTERPRETASI:",
            interpretasi if interpretasi else (
                "(Tidak tersedia — berikan interpretasi Anda sendiri seperti "
                "tren naik/turun, nilai tertinggi/terendah, perbandingan)"
            ),
            "",
            "📊 DATA LENGKAP (hanya untuk referensi interpretasi Anda — JANGAN dituliskan kembali di jawaban, karena tabel lengkap sudah ditampilkan otomatis oleh sistem):",
            data
        ]
        return "\n".join(parts)
    
    def _build_context_sheet(
        self, judul: str, df: pd.DataFrame,
        definisi: str, interpretasi: str, sheet_name: str, sumber: str
    ) -> str:
        lines = []
        cols = list(df.columns)
        lines.append("Kolom: " + " | ".join([str(c) for c in cols]))
        lines.append("")
        
        for _, row in df.head(20).iterrows():
            parts = [
                f"{col}: {row[col]}"
                for col in cols
                if pd.notna(row[col]) and str(row[col]).strip() != ""
            ]
            if parts:
                lines.append("• " + " | ".join(parts))
        
        if len(df) > 20:
            lines.append(f"... dan {len(df) - 20} baris lainnya")
        
        auto_interpret = ""
        if not interpretasi:
            numeric_cols = df.select_dtypes(include="number").columns
            for col in numeric_cols[:2]:
                try:
                    max_val = df[col].max()
                    min_val = df[col].min()
                    if pd.notna(max_val) and pd.notna(min_val):
                        auto_interpret += (
                            f"\n• Kolom '{col}': nilai tertinggi {max_val}, "
                            f"terendah {min_val}"
                        )
                except:
                    pass
        
        sheet_label = f" (Sheet: {sheet_name})" if sheet_name else ""
        
        parts = [
            f"📋 JUDUL TABEL: {judul}{sheet_label}",
            f"📖 SUMBER UNTUK JAWABAN: {sumber}",
            "",
            "📖 DEFINISI:",
            definisi if definisi else "(Tidak tersedia)",
            "",
            "💡 INTERPRETASI:",
            (interpretasi if interpretasi else
             "(Tidak tersedia — gunakan auto-interpretasi di bawah jika relevan)") +
            auto_interpret,
            "",
            "📊 DATA TABEL:",
            "\n".join(lines)
        ]
        return "\n".join(parts)
    
    # ============================================================
    # ===== LLM FALLBACK (Gemini → OpenAI) =====
    # ============================================================
    
    def _get_gemini(self) -> Optional[GeminiModel]:
        if self._gemini is None and settings.gemini_api_key:
            try:
                self._gemini = GeminiModel()
            except Exception as e:
                print(f"⚠️ Gemini init failed: {e}")
        return self._gemini
    
    def _get_openai(self) -> Optional[OpenAIModel]:
        if self._openai is None and settings.openai_api_key:
            try:
                self._openai = OpenAIModel()
            except Exception as e:
                print(f"⚠️ OpenAI init failed: {e}")
        return self._openai
    
    async def _generate_with_fallback(
        self,
        question: str,
        context_text: str,
        sources: List[Source],
        meta: Dict,
        chat_history: Optional[List[Dict]],
        table: Optional[Dict] = None
    ) -> ModelResponse:
        # ===== Coba Gemini =====
        gemini = self._get_gemini()
        if gemini:
            try:
                response = await gemini.generate_response(
                    question=question,
                    chat_history=chat_history,
                    context=context_text
                )
                if response.success:
                    response.sources = sources
                    response.table = table
                    response.meta = {**meta, "model_used": "gemini"}
                    return response
                else:
                    print(f"⚠️ Gemini gagal: {response.error}")
            except Exception as e:
                print(f"⚠️ Gemini exception: {type(e).__name__}: {e}")
        
        # ===== Fallback OpenAI =====
        openai = self._get_openai()
        if openai:
            try:
                response = await openai.generate_response(
                    question=question,
                    chat_history=chat_history,
                    context=context_text
                )
                if response.success:
                    response.sources = sources
                    response.table = table
                    response.meta = {**meta, "model_used": "openai", "fallback": True}
                    return response
                else:
                    print(f"⚠️ OpenAI gagal: {response.error}")
            except Exception as e:
                print(f"⚠️ OpenAI exception: {type(e).__name__}: {e}")
        
        # ===== Keduanya gagal =====
        return ModelResponse(
            answer=(
                "Maaf, saat ini sistem sedang mengalami kendala dalam menghasilkan "
                "jawaban. Silakan coba beberapa saat lagi."
            ),
            sources=sources,
            table=table,
            meta={**meta, "fallback_failed": True, "model_used": "failed"},
            success=False
        )
