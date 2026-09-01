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
import asyncio
import time
import hashlib
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs, quote
from io import StringIO
import httpx
import pandas as pd

try:
    from lingua import Language, LanguageDetectorBuilder
    LINGUA_AVAILABLE = True
except ImportError:
    Language = None
    LanguageDetectorBuilder = None
    LINGUA_AVAILABLE = False

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
        # Detector bahasa khusus header tabel prioritas.
        self._header_language_detector = None

        if LINGUA_AVAILABLE:
            try:
                self._header_language_detector = (
                    LanguageDetectorBuilder
                    .from_languages(
                        Language.INDONESIAN,
                        Language.ENGLISH,
                    )
                    .build()
                )
            except Exception as e:
                print(
                    "⚠️ Lingua language detector tidak aktif:",
                    type(e).__name__,
                    e,
                )


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
                "structured_table_export",
                "publication_search"
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
    # ===== PUBLICATION SEARCH ===================================
    # ============================================================

    def _is_publication_intent(self, question: str) -> bool:
        """
        Jalur publikasi hanya aktif jika pengguna secara eksplisit
        menyebut 'publikasi' / 'publication'.

        Ini sengaja dibuat ketat agar pertanyaan data seperti
        'jumlah penduduk 2025' tetap masuk ke RAG data biasa.
        """
        q = self._normalize_search_text(question)

        return bool(
            re.search(
                r"\b(publikasi|publication|publications)\b",
                q,
                flags=re.IGNORECASE,
            )
        )

    def _extract_publication_keyword(self, question: str) -> str:
        """
        Ambil topik inti dari pertanyaan publikasi.

        Contoh:
        'tolong carikan publikasi tentang penduduk'
        -> 'penduduk'

        'ada publikasi kecamatan silinda?'
        -> 'kecamatan silinda'
        """
        q = self._normalize_search_text(question)

        # Kata yang hanya menunjukkan intent/permintaan,
        # bukan topik yang perlu dikirim ke WebAPI.
        stopwords = {
            "publikasi",
            "publication",
            "publications",
            "tolong",
            "carikan",
            "cari",
            "lihat",
            "tampilkan",
            "ada",
            "apakah",
            "tentang",
            "mengenai",
            "terkait",
            "untuk",
            "yang",
            "dari",
            "di",
            "bps",
            "serdang",
            "bedagai",
            "kabupaten",
            "pdf",
            "download",
            "unduh",
            "link",
        }

        tokens = []

        for token in q.split():
            if token in stopwords:
                continue

            if len(token) >= 2:
                tokens.append(token)

        return " ".join(tokens).strip()

    async def _fetch_publication_page(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        page: int,
    ) -> Tuple[Optional[Dict], List[Dict]]:
        """
        Ambil satu halaman publication WebAPI BPS.

        Response BPS:
        data[0] = metadata pagination
        data[1] = list publikasi
        """
        if not self.api_key:
            return None, []

        encoded_keyword = quote(
            keyword,
            safe="",
        )

        encoded_key = quote(
            self.api_key,
            safe="",
        )

        if page <= 1:
            url = (
                f"{self.base_url}/list/model/publication/"
                f"lang/ind/domain/{self.domain_id}/"
                f"keyword/{encoded_keyword}/"
                f"key/{encoded_key}/"
            )
        else:
            url = (
                f"{self.base_url}/list/model/publication/"
                f"lang/ind/domain/{self.domain_id}/"
                f"page/{page}/"
                f"keyword/{encoded_keyword}/"
                f"key/{encoded_key}/"
            )

        try:
            response = await client.get(url)
            response.raise_for_status()

            payload = response.json()

            if (
                not isinstance(payload, dict)
                or str(payload.get("status", "")).upper() != "OK"
            ):
                return None, []

            data = payload.get("data")

            if (
                not isinstance(data, list)
                or len(data) < 2
            ):
                return None, []

            pagination = (
                data[0]
                if isinstance(data[0], dict)
                else {}
            )

            publications = (
                data[1]
                if isinstance(data[1], list)
                else []
            )

            return pagination, publications

        except Exception as e:
            print(
                f"⚠️ Publication page {page} error:",
                type(e).__name__,
                e,
            )
            return None, []

    def _publication_year(self, publication: Dict) -> int:
        """
        Tahun untuk tie-break ranking.
        Prioritas title -> rl_date.
        """
        title = str(
            publication.get("title", "")
        )

        years = re.findall(
            r"\b(20\d{2})\b",
            title,
        )

        if years:
            try:
                return max(
                    int(year)
                    for year in years
                )
            except Exception:
                pass

        release_date = str(
            publication.get("rl_date", "")
        )

        match = re.match(
            r"^(20\d{2})",
            release_date,
        )

        if match:
            try:
                return int(
                    match.group(1)
                )
            except Exception:
                pass

        return 0

    def _publication_score(
        self,
        publication: Dict,
        keyword: str,
    ) -> float:
        """
        Ranking publikasi:
        - judul mendapat bobot terbesar
        - abstrak sebagai pendukung
        - publikasi terbaru sebagai tie-break ringan
        """
        keyword_norm = self._normalize_search_text(
            keyword
        )

        title_norm = self._normalize_search_text(
            publication.get("title", "")
        )

        abstract_norm = self._normalize_search_text(
            publication.get("abstract", "")
        )

        query_tokens = [
            token
            for token in keyword_norm.split()
            if len(token) >= 2
        ]

        if not query_tokens:
            return 0.0

        score = 0.0

        # Phrase lengkap pada judul adalah sinyal terkuat.
        if (
            keyword_norm
            and keyword_norm == title_norm
        ):
            score += 140

        elif (
            keyword_norm
            and keyword_norm in title_norm
        ):
            score += 90

        elif (
            keyword_norm
            and keyword_norm in abstract_norm
        ):
            score += 20

        matched_title = 0
        matched_abstract = 0

        for token in query_tokens:
            if token in title_norm:
                score += 24
                matched_title += 1

            elif token in abstract_norm:
                score += 6
                matched_abstract += 1

        title_coverage = (
            matched_title / len(query_tokens)
        )

        all_coverage = (
            (matched_title + matched_abstract)
            / len(query_tokens)
        )

        if title_coverage == 1:
            score += 45
        elif title_coverage >= 0.75:
            score += 25
        elif title_coverage >= 0.5:
            score += 12

        if all_coverage == 1:
            score += 10

        # Tahun hanya bonus kecil; relevansi tetap utama.
        year = self._publication_year(
            publication
        )

        if year:
            score += max(
                0,
                year - 2020
            ) * 0.4

        return round(
            score,
            3,
        )

    def _clean_publication_item(
        self,
        publication: Dict,
    ) -> Optional[Dict]:
        """
        Hanya field yang dibutuhkan frontend.
        """
        title = re.sub(
            r"\s+",
            " ",
            str(
                publication.get(
                    "title",
                    ""
                )
            )
        ).strip()

        abstract = re.sub(
            r"\s+",
            " ",
            str(
                publication.get(
                    "abstract",
                    ""
                )
            )
        ).strip()

        pdf = str(
            publication.get(
                "pdf",
                ""
            )
        ).strip()

        cover = str(
            publication.get(
                "cover",
                ""
            )
        ).strip()

        if not title:
            return None

        return {
            "pub_id": str(
                publication.get(
                    "pub_id",
                    ""
                )
            ).strip(),
            "title": title,
            "abstract": abstract,
            "pdf": pdf,
            "cover": cover,
        }

    async def _search_publications(
        self,
        keyword: str,
        limit: int = 5,
    ) -> Dict:
        """
        Ambil page 1, baca jumlah pages, lalu ambil semua page sisanya.
        Setelah digabung, deduplicate + ranking + ambil top N.
        """
        if not self.api_key:
            return {
                "ok": False,
                "reason": "missing_api_key",
                "keyword": keyword,
                "total": 0,
                "publications": [],
            }

        timeout = httpx.Timeout(
            25.0,
            connect=10.0,
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
        ) as client:

            pagination, first_items = (
                await self._fetch_publication_page(
                    client,
                    keyword,
                    1,
                )
            )

            if pagination is None:
                return {
                    "ok": False,
                    "reason": "api_error",
                    "keyword": keyword,
                    "total": 0,
                    "publications": [],
                }

            try:
                pages = max(
                    1,
                    int(
                        pagination.get(
                            "pages",
                            1
                        )
                    ),
                )
            except Exception:
                pages = 1

            all_items = list(
                first_items
            )

            # Fetch page 2..N secara paralel.
            if pages > 1:
                tasks = [
                    self._fetch_publication_page(
                        client,
                        keyword,
                        page,
                    )
                    for page in range(
                        2,
                        pages + 1
                    )
                ]

                results = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )

                for result in results:
                    if isinstance(
                        result,
                        Exception
                    ):
                        continue

                    _, page_items = result

                    if page_items:
                        all_items.extend(
                            page_items
                        )

        # ==========================================
        # DEDUPLICATE
        # ==========================================
        unique = {}

        for item in all_items:
            if not isinstance(
                item,
                dict
            ):
                continue

            cleaned = (
                self._clean_publication_item(
                    item
                )
            )

            if not cleaned:
                continue

            dedupe_key = (
                cleaned.get("pub_id")
                or cleaned.get("pdf")
                or self._normalize_search_text(
                    cleaned.get(
                        "title",
                        ""
                    )
                )
            )

            if not dedupe_key:
                continue

            if dedupe_key not in unique:
                cleaned["_score"] = (
                    self._publication_score(
                        item,
                        keyword,
                    )
                )

                cleaned["_year"] = (
                    self._publication_year(
                        item
                    )
                )

                unique[
                    dedupe_key
                ] = cleaned

        ranked = sorted(
            unique.values(),
            key=lambda item: (
                item.get(
                    "_score",
                    0
                ),
                item.get(
                    "_year",
                    0
                ),
                item.get(
                    "title",
                    ""
                ),
            ),
            reverse=True,
        )

        selected = []

        for item in ranked[:limit]:
            item = dict(item)

            item.pop(
                "_score",
                None,
            )

            item.pop(
                "_year",
                None,
            )

            selected.append(
                item
            )

        try:
            total = int(
                pagination.get(
                    "total",
                    len(unique)
                )
            )
        except Exception:
            total = len(unique)

        return {
            "ok": True,
            "keyword": keyword,
            "pages": pages,
            "total": total,
            "matched_total": len(unique),
            "publications": selected,
        }

    async def _handle_publication_search(
        self,
        question: str,
    ) -> ModelResponse:
        """
        Response khusus pencarian publikasi.
        Tidak melewati LLM agar judul/link tidak terarang.
        """
        keyword = (
            self._extract_publication_keyword(
                question
            )
        )

        if not keyword:
            return ModelResponse(
                answer=(
                    "Silakan sebutkan topik publikasi yang ingin dicari, "
                    "misalnya **publikasi penduduk**, "
                    "**publikasi kemiskinan**, atau "
                    "**publikasi Kecamatan Silinda**."
                ),
                sources=[],
                meta={
                    "provider": "rag_unified",
                    "type": "publication_keyword_required",
                    "model_used": "none",
                },
                success=True,
            )

        result = await self._search_publications(
            keyword=keyword,
            limit=5,
        )

        if not result.get("ok"):
            if result.get("reason") == "missing_api_key":
                message = (
                    "Pencarian publikasi belum dapat digunakan karena "
                    "BPS_API_KEY belum dikonfigurasi."
                )
            else:
                message = (
                    "Maaf, pencarian publikasi sedang tidak dapat diakses. "
                    "Silakan coba kembali beberapa saat lagi."
                )

            return ModelResponse(
                answer=message,
                sources=[],
                meta={
                    "provider": "rag_unified",
                    "type": "publication_error",
                    "model_used": "none",
                },
                success=True,
            )

        publications = result.get(
            "publications",
            []
        )

        if not publications:
            return ModelResponse(
                answer=(
                    f"Saya belum menemukan publikasi yang sesuai dengan "
                    f"kata kunci **{keyword}**."
                ),
                sources=[],
                meta={
                    "provider": "rag_unified",
                    "type": "publication_not_found",
                    "keyword": keyword,
                    "model_used": "none",
                },
                success=True,
            )

        shown = len(
            publications
        )

        total = result.get(
            "matched_total",
            result.get(
                "total",
                shown
            )
        )

        answer = (
            f"Saya menemukan beberapa publikasi yang relevan untuk "
            f"kata kunci **{keyword}**. "
            f"Berikut {shown} publikasi yang paling sesuai."
        )

        return ModelResponse(
            answer=answer,
            sources=[],
            table=None,
            meta={
                "provider": "rag_unified",
                "type": "publication_results",
                "model_used": "bps_webapi",
                "keyword": keyword,
                "publication_count": shown,
                "publication_total": total,
                "publication_pages": result.get(
                    "pages",
                    1
                ),
                "publications": publications,
            },
            success=True,
        )


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


            # ===== 1. PUBLICATION INTENT =====
            # Hanya aktif jika pengguna secara eksplisit mengetik
            # "publikasi" / "publication".
            if self._is_publication_intent(question):
                return await self._handle_publication_search(
                    question
                )
            
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

        years.extend(self._extract_years_from_text(title))
        years.extend(self._extract_years_from_text(sheet_name))
        years.extend(
            self._extract_years_from_text(
                df.attrs.get("sheet_title", "")
            )
        )
        years.extend(
            self._extract_years_from_text(
                df.attrs.get("source_note", "")
            )
        )

        for year in df.attrs.get("raw_years", []):
            try:
                years.append(int(year))
            except (TypeError, ValueError):
                pass

        for column in df.columns:
            years.extend(self._extract_years_from_text(str(column)))

        sample = df.head(200)

        for column in sample.columns:
            for value in sample[column].dropna().astype(str).tolist():
                years.extend(self._extract_years_from_text(value))

        return str(max(years)) if years else None

    def _df_contains_year(
        self,
        df: pd.DataFrame,
        year: str,
    ) -> bool:
        if not year:
            return True

        year = str(year)

        metadata_text = " ".join([
            str(df.attrs.get("sheet_title", "")),
            str(df.attrs.get("source_note", "")),
            " ".join(str(y) for y in df.attrs.get("raw_years", [])),
        ])

        if year in metadata_text:
            return True

        for column in df.columns:
            if year in str(column):
                return True

        sample = df.head(500)

        for column in sample.columns:
            series = sample[column].dropna().astype(str)

            if series.str.contains(year, regex=False).any():
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
        """
        Ambil tahun referensi data dari pertanyaan pengguna.

        Penting:
        - Tahun pada frasa seperti "harga konstan 2010" atau
          "tahun dasar 2010" adalah tahun dasar/metodologis,
          BUKAN otomatis tahun data.
        - Jika pengguna menyebut "tahun 2025", "pada tahun 2025",
          atau "periode 2025", tahun tersebut diprioritaskan.
        """
        text = str(text or "").strip()

        if not text:
            return None

        # 1. Prioritaskan penyebutan tahun data secara eksplisit.
        explicit_patterns = [
            r"\bpada\s+tahun\s+(20\d{2})\b",
            r"\btahun\s+(20\d{2})\b",
            r"\bperiode\s+(20\d{2})\b",
        ]

        for pattern in explicit_patterns:
            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1)

        # 2. Ambil seluruh tahun 20xx yang muncul.
        years = re.findall(
            r"\b(20\d{2})\b",
            text,
        )

        if not years:
            return None

        # 3. Tandai tahun dasar/metodologis agar tidak dipakai
        #    sebagai tahun referensi data.
        base_year_patterns = [
            r"\bharga\s+konstan(?:\s+tahun\s+dasar)?\s+(20\d{2})\b",
            r"\btahun\s+dasar\s+(20\d{2})\b",
            r"\bdasar\s+harg[ai]\s+(20\d{2})\b",
        ]

        base_years = set()

        for pattern in base_year_patterns:
            base_years.update(
                re.findall(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                )
            )

        valid_years = [
            year
            for year in years
            if year not in base_years
        ]

        # 4. Jika masih ada tahun non-dasar, pakai tahun terakhir.
        #    Ini juga menangani judul/rentang seperti 2021-2025.
        if valid_years:
            return valid_years[-1]

        # 5. Bila yang ada hanya tahun dasar, berarti pengguna
        #    tidak menyebut tahun referensi data.
        return None
    
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
    
    def _parse_bps_json(
        self,
        json_data: Dict,
        used_year: str
    ) -> Tuple[str, Optional[Dict]]:
        """
        Parse JSON BPS menjadi:
        - teks konteks untuk LLM
        - payload tabel untuk frontend

        Struktur key BPS:
        {vervar}{var}{turvar}{tahun}{turtahun}

        Prinsip:
        1. Nilai yang dikirim WebAPI dipertahankan apa adanya.
        2. Tidak dilakukan pembulatan atau format ulang angka.
        3. Jika kombinasi sel tabel tersedia secara struktural
        tetapi key-nya tidak ada di datacontent, tampilkan "-".
        4. Simbol BPS dipertahankan agar dapat dideteksi
        oleh _detect_bps_symbol_notes().
        """

        try:
            # =====================================================
            # INFORMASI VARIABEL
            # =====================================================

            var_info = json_data.get(
                "var",
                [{}],
            )[0]

            var_val = str(
                var_info.get("val")
            )

            unit = (
                var_info.get(
                    "unit",
                    "",
                )
                or ""
            )

            row_header = (
                json_data.get(
                    "labelvervar",
                    "",
                )
                or "Kategori"
            )

            # =====================================================
            # STRUKTUR DATA BPS
            # =====================================================

            vervars = json_data.get(
                "vervar",
                [],
            )

            turvars = json_data.get(
                "turvar",
                [],
            )

            tahuns = json_data.get(
                "tahun",
                [],
            )

            turtahuns = json_data.get(
                "turtahun",
                [],
            )

            datacontent = json_data.get(
                "datacontent",
                {},
            ) or {}

            # =====================================================
            # CARI TAHUN YANG DIGUNAKAN
            # =====================================================

            tahun_obj = next(
                (
                    t
                    for t in tahuns
                    if str(
                        t.get("label")
                    )
                    == str(used_year)
                ),
                None,
            )

            if not tahun_obj:
                return (
                    "⚠️ Format data tidak dikenali.",
                    None,
                )

            tahun_val = str(
                tahun_obj.get("val")
            )

            # =====================================================
            # FORMAT NILAI
            # =====================================================

            def fmt_raw_value(value):
                """
                Pertahankan nilai yang diberikan WebAPI.

                Tidak:
                - membulatkan;
                - memberi separator ribuan;
                - menghapus simbol;
                - menerjemahkan simbol.
                """

                if value is None:
                    return ""

                return str(value)

            # =====================================================
            # TENTUKAN STRUKTUR KOLOM
            # =====================================================

            if len(turvars) > 1:
                # Contoh:
                # Triwulan I | Triwulan II | ...
                col_mode = "turvar"

                col_list = turvars

            elif len(turtahuns) > 1:
                # Beberapa kategori turunan tahun
                col_mode = "turtahun"

                col_list = turtahuns

            else:
                # Hanya satu nilai
                col_mode = "single"

                col_list = []

            # =====================================================
            # PENAMPUNG HASIL
            # =====================================================

            lines = [
                (
                    f"📊 "
                    f"{var_info.get('label', 'Data')} "
                    f"(Tahun {used_year})"
                )
            ]

            priority_rows = []

            other_rows = []

            table_rows = []

            # =====================================================
            # LOOP BARIS
            # =====================================================

            for v in vervars:
                v_val = str(
                    v.get("val")
                )

                row_label = str(
                    v.get(
                        "label",
                        f"Kode {v_val}",
                    )
                )

                is_priority = any(
                    keyword
                    in row_label.upper()
                    for keyword in [
                        "TOTAL",
                        "JUMLAH",
                        "SERDANG",
                        "KAB",
                    ]
                )

                parts = [
                    row_label
                ]

                cells = [
                    row_label
                ]

                # =================================================
                # MODE 1 KOLOM
                # =================================================

                if col_mode == "single":
                    t_val = (
                        str(
                            turvars[0]["val"]
                        )
                        if turvars
                        else ""
                    )

                    tt_val = (
                        str(
                            turtahuns[0][
                                "val"
                            ]
                        )
                        if turtahuns
                        else "0"
                    )

                    key = (
                        f"{v_val}"
                        f"{var_val}"
                        f"{t_val}"
                        f"{tahun_val}"
                        f"{tt_val}"
                    )

                    # =============================================
                    # PENTING:
                    # Jika key memang dikirim BPS, ambil nilai
                    # apa adanya.
                    #
                    # Jika kombinasi tabel ada tetapi key tidak
                    # dikirim, tampilkan "-" seperti tabel BPS.
                    # =============================================

                    if key in datacontent:
                        value = (
                            datacontent[
                                key
                            ]
                        )

                        val_str = (
                            fmt_raw_value(
                                value
                            )
                        )

                    else:
                        val_str = "-"

                    parts.append(
                        (
                            f"{val_str} "
                            f"{unit}"
                        ).strip()
                    )

                    cells.append(
                        val_str
                    )

                # =================================================
                # MODE MULTI KOLOM
                # =================================================

                else:
                    for c in col_list:
                        c_val = str(
                            c.get("val")
                        )

                        c_label = str(
                            c.get(
                                "label",
                                "",
                            )
                        )

                        # =========================================
                        # KOLOM BERASAL DARI TURVAR
                        # =========================================

                        if (
                            col_mode
                            == "turvar"
                        ):
                            tt_val = (
                                str(
                                    turtahuns[
                                        0
                                    ][
                                        "val"
                                    ]
                                )
                                if turtahuns
                                else "0"
                            )

                            key = (
                                f"{v_val}"
                                f"{var_val}"
                                f"{c_val}"
                                f"{tahun_val}"
                                f"{tt_val}"
                            )

                        # =========================================
                        # KOLOM BERASAL DARI TURTAHUN
                        # =========================================

                        else:
                            t_val = (
                                str(
                                    turvars[
                                        0
                                    ][
                                        "val"
                                    ]
                                )
                                if turvars
                                else ""
                            )

                            key = (
                                f"{v_val}"
                                f"{var_val}"
                                f"{t_val}"
                                f"{tahun_val}"
                                f"{c_val}"
                            )

                        # =========================================
                        # NILAI SEL
                        # =========================================

                        if key in datacontent:
                            value = (
                                datacontent[
                                    key
                                ]
                            )

                            val_str = (
                                fmt_raw_value(
                                    value
                                )
                            )

                        else:
                            val_str = "-"

                        parts.append(
                            (
                                f"{c_label}: "
                                f"{val_str} "
                                f"{unit}"
                            ).strip()
                        )

                        cells.append(
                            val_str
                        )

                # =================================================
                # SIMPAN BARIS
                # =================================================

                row_text = (
                    " | ".join(
                        parts
                    )
                )

                if is_priority:
                    priority_rows.append(
                        "🔹 "
                        + row_text
                    )

                else:
                    other_rows.append(
                        "• "
                        + row_text
                    )

                table_rows.append(
                    cells
                )

            # =====================================================
            # CONTEXT UNTUK LLM
            # =====================================================

            lines.extend(
                priority_rows
            )

            lines.extend(
                other_rows
            )

            # =====================================================
            # HEADER TABEL
            # =====================================================

            if col_mode == "single":
                headers = [
                    (
                        f"Nilai ({unit})"
                        if unit
                        else "Nilai"
                    )
                ]

            else:
                headers = [
                    str(
                        c.get(
                            "label",
                            "",
                        )
                    )
                    for c in col_list
                ]

            # =====================================================
            # PAYLOAD TABEL
            # =====================================================

            payload = {
                "title": (
                    var_info.get(
                        "label",
                        "Data",
                    )
                ),

                "source":
                    "Tabel Dinamis BPS",

                "columns":
                    [row_header]
                    + headers,

                "rows":
                    table_rows,

                "total_rows":
                    len(
                        table_rows
                    ),
            }

            # =====================================================
            # DETEKSI SIMBOL YANG MUNCUL
            # =====================================================

            symbol_notes = (
                self
                ._detect_bps_symbol_notes(
                    payload
                )
            )

            if symbol_notes:
                payload[
                    "symbol_notes"
                ] = symbol_notes

            # =====================================================
            # RETURN
            # =====================================================

            return (
                "\n".join(
                    lines
                ),
                payload,
            )

        except Exception as e:
            print(
                f"❌ BPS parse error: {e}"
            )

            return (
                "⚠️ Gagal memproses format data BPS.",
                None,
            )
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
    
    # ============================================================
    # ===== NORMALISASI SHEET PRIORITAS ==========================
    # ============================================================

    def _priority_cell_text(
        self,
        value,
        preserve_lines: bool = False,
    ) -> str:
        """Bersihkan isi sel; line break dapat dipertahankan untuk header bilingual."""
        if value is None:
            return ""

        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass

        value = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()

        if value.lower() in {"nan", "none", "null"}:
            return ""

        if preserve_lines:
            lines = []
            for line in value.split("\n"):
                line = re.sub(r"[ \t]+", " ", line).strip()
                if line:
                    lines.append(line)
            return "\n".join(lines)

        return re.sub(r"\s+", " ", value).strip()

    def _is_priority_numbering_cell(self, value: str) -> bool:
        """
        Deteksi nomor kolom tabel BPS seperti (1), (2), (3).

        PENTING:
        angka data biasa seperti 6, 11, 85 TIDAK boleh dianggap
        sebagai nomor kolom.
        """
        value = self._priority_cell_text(value)

        return bool(
            value
            and re.fullmatch(
                r"\(\s*\d{1,3}\s*\)",
                value
            )
        )

    def _is_priority_numbering_row(self, row_values: List[str]) -> bool:
        values = [self._priority_cell_text(v) for v in row_values]
        nonempty = [v for v in values if v]

        return bool(
            nonempty
            and all(self._is_priority_numbering_cell(v) for v in nonempty)
        )

    def _is_priority_footer_row(self, row_values: List[str]) -> bool:
        values = [self._priority_cell_text(v) for v in row_values]
        nonempty = [v for v in values if v]

        if not nonempty:
            return False

        return bool(
            re.match(
                r"^(sumber|source|catatan|note|keterangan)\s*:",
                nonempty[0].lower()
            )
        )

    def _priority_numeric_like(self, value: str) -> bool:
        value = self._priority_cell_text(value)

        if not value or self._is_priority_numbering_cell(value):
            return False

        cleaned = value.replace(" ", "").replace("%", "")

        return bool(
            re.fullmatch(
                r"[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?",
                cleaned
            )
        )

    def _looks_like_priority_data_row(self, row_values: List[str]) -> bool:
        values = [self._priority_cell_text(v) for v in row_values]
        nonempty = [v for v in values if v]

        if len(nonempty) < 2:
            return False

        if self._is_priority_numbering_row(values):
            return False

        if self._is_priority_footer_row(values):
            return False

        numeric_after_first = sum(
            1
            for v in values[1:]
            if self._priority_numeric_like(v)
        )

        return numeric_after_first >= 1

    def _header_language_confidence(
        self,
        text: str,
    ) -> Dict[str, float]:
        """Confidence Bahasa Indonesia vs Inggris untuk potongan header."""
        text = self._priority_cell_text(text)
        result = {"id": 0.0, "en": 0.0}

        if not text or self._header_language_detector is None:
            return result

        try:
            confidence_values = self._header_language_detector.compute_language_confidence_values(text)
            for item in confidence_values:
                if item.language == Language.INDONESIAN:
                    result["id"] = float(item.value)
                elif item.language == Language.ENGLISH:
                    result["en"] = float(item.value)
        except Exception:
            pass

        return result

    def _looks_like_english_header_piece(
        self,
        text: str,
        min_confidence: float = 0.72,
    ) -> bool:
        conf = self._header_language_confidence(text)
        return conf["en"] >= min_confidence and conf["en"] > conf["id"]

    def _looks_like_indonesian_header_piece(
        self,
        text: str,
        min_confidence: float = 0.58,
    ) -> bool:
        conf = self._header_language_confidence(text)
        return conf["id"] >= min_confidence and conf["id"] >= conf["en"]

    def _split_bilingual_header_by_separator(
        self,
        value: str,
    ) -> Optional[str]:
        """Tangani line break dan slash bila kanan terdeteksi sebagai Inggris."""
        raw = self._priority_cell_text(value, preserve_lines=True)
        if not raw:
            return None

        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if len(lines) >= 2:
            left = lines[0]
            right = " ".join(lines[1:])
            if (
                self._looks_like_indonesian_header_piece(left)
                and self._looks_like_english_header_piece(right)
            ):
                return left

        slash_match = re.match(r"^\s*(.+?)\s*/\s*(.+?)\s*$", raw, flags=re.DOTALL)
        if slash_match:
            left = self._priority_cell_text(slash_match.group(1))
            right = self._priority_cell_text(slash_match.group(2))
            if (
                left
                and right
                and self._looks_like_indonesian_header_piece(left)
                and self._looks_like_english_header_piece(right)
            ):
                return left

        return None

    def _split_bilingual_header_without_separator(
        self,
        value: str,
    ) -> Optional[str]:
        """Cari titik split ID|EN terbaik pada header tanpa separator."""
        normalized = self._priority_cell_text(value)
        tokens = normalized.split()

        if len(tokens) < 2:
            return None

        best = None

        for split_index in range(1, len(tokens)):
            left = " ".join(tokens[:split_index])
            right = " ".join(tokens[split_index:])
            left_conf = self._header_language_confidence(left)
            right_conf = self._header_language_confidence(right)

            if not (
                left_conf["id"] >= 0.58
                and left_conf["id"] >= left_conf["en"]
                and right_conf["en"] >= 0.76
                and right_conf["en"] > right_conf["id"]
            ):
                continue

            score = (
                left_conf["id"]
                + right_conf["en"]
                - left_conf["en"]
                - right_conf["id"]
            )

            if best is None or score > best["score"]:
                best = {"left": left, "score": score}

        return best["left"] if best else None

    def _clean_bilingual_parentheses(
        self,
        value: str,
    ) -> str:
        """Hapus (English translation), tetapi pertahankan satuan seperti (jiwa), (%), (km²)."""
        value = self._priority_cell_text(value)
        match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", value)
        if not match:
            return value

        outside = match.group(1).strip()
        inside = match.group(2).strip()

        if (
            outside
            and inside
            and self._looks_like_indonesian_header_piece(outside)
            and self._looks_like_english_header_piece(inside, min_confidence=0.78)
        ):
            return outside

        return value

    def _clean_priority_header_label(
        self,
        value: str,
        column_index: int,
    ) -> str:
        """
        Bersihkan Bahasa Inggris dari HEADER tabel prioritas secara adaptif.
        Jika tidak yakin, teks asli dipertahankan.
        """
        raw = self._priority_cell_text(value, preserve_lines=True)
        if not raw:
            return f"Kolom {column_index + 1}"

        separated = self._split_bilingual_header_by_separator(raw)

        if separated:
            cleaned = separated
        else:
            cleaned = self._priority_cell_text(raw)
            cleaned = self._clean_bilingual_parentheses(cleaned)
            split_result = self._split_bilingual_header_without_separator(cleaned)
            if split_result:
                cleaned = split_result

        # Safety-net konservatif bila Lingua belum tersedia atau tidak yakin.
        # Tidak menjadi mekanisme utama.
        conservative_patterns = [
            r"\s*/\s*Teachers?$",
            r"\s*/\s*Schools?$",
            r"\s*/\s*Students?$",
            r"\s*/\s*Subdistrict$",
            r"\s*/\s*District$",
            r"\s*/\s*Sex$",
            r"\s*/\s*Gender$",
            r"\s*/\s*Year$",
            r"\s*/\s*Public$",
            r"\s*/\s*Private$",
            r"\s*/\s*Male$",
            r"\s*/\s*Female$",
        ]

        for pattern in conservative_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return cleaned or f"Kolom {column_index + 1}"

    def _expand_merged_priority_header_row(
        self,
        row_values: List[str],
    ) -> List[str]:
        """
        Google Sheets mengekspor merged cells hanya pada sel pertama.

        Contoh:
        ["Kecamatan", "Sekolah", "", "", "Guru", "", "", "Murid", "", ""]

        diubah menjadi:
        ["Kecamatan", "Sekolah", "Sekolah", "Sekolah",
         "Guru", "Guru", "Guru", "Murid", "Murid", "Murid"]
        """
        values = [
            self._priority_cell_text(v)
            for v in row_values
        ]

        expanded = []
        current = ""

        for value in values:
            if value:
                current = value
                expanded.append(value)
            else:
                expanded.append(current)

        return expanded

    def _build_priority_headers(
        self,
        header_rows: List[List[str]],
        column_count: int,
    ) -> List[str]:
        """
        Bentuk header akhir dari header bertingkat.

        Contoh:
        Sekolah / Schools
            Negeri / Public
            Swasta / Private
            Jumlah / Total

        menjadi:
        Sekolah - Negeri
        Sekolah - Swasta
        Sekolah - Jumlah
        """
        if not header_rows:
            return [
                f"Kolom {i + 1}"
                for i in range(column_count)
            ]

        # Parent/group header di-expand mengikuti merged cell.
        expanded_rows = [
            self._expand_merged_priority_header_row(row)
            for row in header_rows
        ]

        headers = []

        for column_index in range(column_count):
            parts = []

            for row_values in expanded_rows:
                if column_index >= len(row_values):
                    continue

                value = self._priority_cell_text(
                    row_values[column_index]
                )

                if not value:
                    continue

                cleaned = self._clean_priority_header_label(
                    value,
                    column_index
                )

                # Hindari label sama berulang.
                if (
                    cleaned
                    and cleaned not in parts
                    and not cleaned.startswith("Kolom ")
                ):
                    parts.append(cleaned)

            if not parts:
                header = f"Kolom {column_index + 1}"
            elif len(parts) == 1:
                header = parts[0]
            else:
                # Untuk kolom pertama biasanya parent dan child sama/bermakna sama.
                # Untuk kolom lain, gabungkan group + subheader.
                header = " - ".join(parts)

            headers.append(header)

        return self._make_unique_priority_headers(
            headers
        )

    def _make_unique_priority_headers(
        self,
        headers: List[str],
    ) -> List[str]:
        result = []
        counts = {}

        for index, header in enumerate(headers):
            base = self._clean_priority_header_label(header, index)
            count = counts.get(base, 0) + 1
            counts[base] = count

            result.append(
                base if count == 1 else f"{base} ({count})"
            )

        return result

    def _build_priority_header_structure(
        self,
        header_rows: List[List[str]],
        column_count: int,
    ) -> List[List[Dict]]:
        """Bentuk header bertingkat dengan colspan/rowspan untuk frontend."""
        if not header_rows:
            return []

        raw_matrix = []

        for row in header_rows:
            raw_matrix.append([
                self._priority_cell_text(
                    row[i] if i < len(row) else ""
                )
                for i in range(column_count)
            ])

        expanded_matrix = [
            self._expand_merged_priority_header_row(row)
            for row in raw_matrix
        ]

        cleaned_matrix = []

        for row in expanded_matrix:
            cleaned_row = []

            for column_index, value in enumerate(row):
                cleaned_row.append(
                    self._clean_priority_header_label(
                        value,
                        column_index
                    )
                    if value
                    else ""
                )

            cleaned_matrix.append(cleaned_row)

        structure = []
        row_count = len(raw_matrix)
        covered_until = [-1] * column_count

        for row_index in range(row_count):
            cells = []
            column_index = 0

            while column_index < column_count:
                if covered_until[column_index] >= row_index:
                    column_index += 1
                    continue

                label = cleaned_matrix[row_index][column_index]

                if not label:
                    column_index += 1
                    continue

                colspan = 1

                while (
                    column_index + colspan < column_count
                    and cleaned_matrix[row_index][column_index + colspan] == label
                    and raw_matrix[row_index][column_index + colspan] == ""
                    and covered_until[column_index + colspan] < row_index
                ):
                    colspan += 1

                rowspan = 1

                for next_row in range(row_index + 1, row_count):
                    if all(
                        self._priority_cell_text(
                            raw_matrix[next_row][c]
                        ) == ""
                        for c in range(
                            column_index,
                            column_index + colspan
                        )
                    ):
                        rowspan += 1
                    else:
                        break

                if rowspan > 1:
                    for c in range(
                        column_index,
                        column_index + colspan
                    ):
                        covered_until[c] = (
                            row_index + rowspan - 1
                        )

                cells.append({
                    "label": label,
                    "colspan": colspan,
                    "rowspan": rowspan,
                })

                column_index += colspan

            if cells:
                structure.append(cells)

        return structure

    def _build_priority_header_matrix(
        self,
        header_rows: List[List[str]],
        column_count: int,
    ) -> List[List[str]]:
        """Matrix header bersih untuk export Excel."""
        matrix = []

        for row in header_rows:
            expanded = self._expand_merged_priority_header_row([
                self._priority_cell_text(
                    row[i] if i < len(row) else ""
                )
                for i in range(column_count)
            ])

            matrix.append([
                self._clean_priority_header_label(
                    value,
                    column_index
                )
                if value
                else ""
                for column_index, value in enumerate(expanded)
            ])

        return matrix

    def _detect_priority_label_columns(
        self,
        df: pd.DataFrame,
    ) -> List[int]:
        """
        Deteksi kolom kategori/label di sisi kiri tabel.

        Contoh:
        - Jenis Kelamin
        - Kelompok Umur
        - Kecamatan

        Kolom nilai numerik tidak ikut dianggap label.
        """
        if df is None or df.empty:
            return []

        label_columns = []

        for column_index in range(len(df.columns)):
            series = [
                self._priority_cell_text(v)
                for v in df.iloc[:, column_index].tolist()
            ]

            nonempty = [
                v for v in series
                if v
            ]

            if not nonempty:
                break

            numeric_count = sum(
                1
                for v in nonempty
                if self._priority_numeric_like(v)
            )

            numeric_ratio = (
                numeric_count / len(nonempty)
            )

            # Jika mayoritas isi kolom berupa angka,
            # berarti sudah masuk area nilai.
            if numeric_ratio >= 0.60:
                break

            label_columns.append(
                column_index
            )

        return label_columns

    def _build_priority_body_rowspans(
        self,
        df: pd.DataFrame,
    ) -> List[Dict]:
        """
        Deteksi merged cell vertikal pada isi tabel.

        Contoh CSV hasil export Google Sheet:

        Laki-laki | 7-12
                  | 13-15
                  | 16-18

        akan dikirim ke frontend sebagai rowspan=3.
        """
        if df is None or df.empty:
            return []

        label_columns = (
            self._detect_priority_label_columns(
                df
            )
        )

        if not label_columns:
            return []

        spans = []

        for column_index in label_columns:
            row_index = 0

            while row_index < len(df):
                value = (
                    self._priority_cell_text(
                        df.iat[
                            row_index,
                            column_index
                        ]
                    )
                )

                if not value:
                    row_index += 1
                    continue

                span = 1
                next_row = row_index + 1

                while next_row < len(df):
                    next_value = (
                        self._priority_cell_text(
                            df.iat[
                                next_row,
                                column_index
                            ]
                        )
                    )

                    # Kategori baru ditemukan.
                    if next_value:
                        break

                    # Jangan merge apabila baris benar-benar kosong.
                    row_values = [
                        self._priority_cell_text(v)
                        for v in df.iloc[next_row].tolist()
                    ]

                    if not any(row_values):
                        break

                    span += 1
                    next_row += 1

                if span > 1:
                    spans.append({
                        "row": row_index,
                        "col": column_index,
                        "rowspan": span,
                        "value": value,
                    })

                row_index = next_row

        return spans

    def _normalize_priority_sheet(
        self,
        raw_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Normalisasi sheet prioritas:
        judul -> header bertingkat -> nomor kolom -> data -> sumber/catatan.
        """
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        work = raw_df.copy()

        def clean_raw_priority_cell(value):
            return self._priority_cell_text(
                value,
                preserve_lines=True,
            )

        try:
            work = work.map(clean_raw_priority_cell)
        except AttributeError:
            work = work.applymap(clean_raw_priority_cell)

        # buang baris kosong
        work = work.loc[
            ~work.apply(
                lambda row: all(
                    self._priority_cell_text(v) == ""
                    for v in row
                ),
                axis=1
            )
        ]

        if work.empty:
            return pd.DataFrame()

        # buang kolom kosong
        work = work.loc[
            :,
            ~work.apply(
                lambda col: all(
                    self._priority_cell_text(v) == ""
                    for v in col
                ),
                axis=0
            )
        ].reset_index(drop=True)

        raw_values = [
            self._priority_cell_text(v)
            for v in work.to_numpy().flatten().tolist()
        ]
        raw_text = " ".join(v for v in raw_values if v)
        raw_years = sorted(
            set(self._extract_years_from_text(raw_text))
        )

        # judul
        sheet_title = ""
        first_row = [
            self._priority_cell_text(v)
            for v in work.iloc[0].tolist()
        ]
        first_nonempty = [v for v in first_row if v]

        if (
            len(first_nonempty) == 1
            and not self._looks_like_priority_data_row(first_row)
        ):
            sheet_title = first_nonempty[0]
            work = work.iloc[1:].reset_index(drop=True)

        if work.empty:
            result = pd.DataFrame()
            result.attrs["sheet_title"] = sheet_title
            result.attrs["raw_years"] = raw_years
            return result

        # footer
        source_notes = []
        keep_rows = []

        for index, row in work.iterrows():
            row_values = [
                self._priority_cell_text(v)
                for v in row.tolist()
            ]

            if self._is_priority_footer_row(row_values):
                note = " ".join(v for v in row_values if v).strip()
                if note:
                    source_notes.append(note)
                continue

            keep_rows.append(index)

        work = work.loc[keep_rows].reset_index(drop=True)
        source_note = " | ".join(dict.fromkeys(source_notes))

        if work.empty:
            result = pd.DataFrame()
            result.attrs["sheet_title"] = sheet_title
            result.attrs["source_note"] = source_note
            result.attrs["raw_years"] = raw_years
            return result

        # cari awal data
        data_start = None

        for index in range(min(len(work), 15)):
            row_values = [
                self._priority_cell_text(v)
                for v in work.iloc[index].tolist()
            ]

            if self._looks_like_priority_data_row(row_values):
                data_start = index
                break

        if data_start is None:
            data_start = 1 if len(work) > 1 else 0

        header_area = work.iloc[:data_start].copy()
        data_area = work.iloc[data_start:].copy()

        # header
        header_rows = []

        for _, row in header_area.iterrows():
            row_values = [
                self._priority_cell_text(v)
                for v in row.tolist()
            ]

            if not self._is_priority_numbering_row(row_values):
                header_rows.append(row_values)

        headers = self._build_priority_headers(
            header_rows=header_rows,
            column_count=work.shape[1],
        )

        header_structure = self._build_priority_header_structure(
            header_rows=header_rows,
            column_count=work.shape[1],
        )

        header_matrix = self._build_priority_header_matrix(
            header_rows=header_rows,
            column_count=work.shape[1],
        )

        # data
        cleaned_rows = []

        for _, row in data_area.iterrows():
            row_values = [
                self._priority_cell_text(v)
                for v in row.tolist()
            ]

            if not any(row_values):
                continue

            if self._is_priority_numbering_row(row_values):
                continue

            if self._is_priority_footer_row(row_values):
                note = " ".join(v for v in row_values if v).strip()
                if note:
                    source_notes.append(note)
                continue

            cleaned_rows.append(row_values)

        source_note = " | ".join(dict.fromkeys(source_notes))

        result = pd.DataFrame(cleaned_rows, columns=headers)

        if not result.empty:
            result = result.loc[
                :,
                ~result.apply(
                    lambda col: all(
                        self._priority_cell_text(v) == ""
                        for v in col
                    ),
                    axis=0
                )
            ]

        result = result.reset_index(drop=True)

        result.attrs["sheet_title"] = sheet_title
        result.attrs["source_note"] = source_note
        result.attrs["raw_years"] = raw_years
        result.attrs["header_rows"] = header_structure
        result.attrs["header_matrix"] = header_matrix
        result.attrs["body_rowspans"] = (
            self._build_priority_body_rowspans(
                result
            )
        )

        print(
            "🧹 Priority sheet normalized:",
            f"title={sheet_title!r}",
            f"headers={list(result.columns)}",
            f"rows={len(result)}",
            f"source={source_note!r}",
        )

        return result

    async def _fetch_sheet_csv(self, link: str) -> Optional[pd.DataFrame]:
        if not link:
            return None

        now = time.time()

        if (
            link in self._csv_data_cache
            and now < self._csv_data_cache[link]["expiry"]
        ):
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

            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True
            ) as client:
                res = await client.get(csv_url)
                res.raise_for_status()

                raw_df = pd.read_csv(
                    StringIO(res.text),
                    header=None,
                    dtype=str,
                    keep_default_na=False,
                )

                df = self._normalize_priority_sheet(raw_df)

                if df.empty:
                    print("⚠️ Priority sheet kosong setelah normalisasi")
                    return None

                self._csv_data_cache[link] = {
                    "df": df,
                    "expiry": now + self.cache_ttl,
                }

                print(
                    f"💾 Fetched priority sheet: "
                    f"{len(df)} rows, {len(df.columns)} columns"
                )

                return df

        except Exception as e:
            print(
                f"❌ Failed to fetch CSV from {csv_url}: "
                f"{type(e).__name__}: {e}"
            )
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
    

    # ============================================================
    # ===== SIMBOL / KETERANGAN DATA BPS ========================
    # ============================================================

    def _detect_bps_symbol_notes(self, table: Dict) -> List[Dict[str, str]]:
        """
        Deteksi HANYA simbol BPS yang benar-benar muncul pada tabel.
        Fungsi ini tidak mengubah isi tabel.
        """

        meanings = {
            "...": "Data tidak tersedia",
            "-": "Tidak ada atau nol",
            "NA": "Data tidak dapat ditampilkan",
            "e": "Angka estimasi",
            "r": "Angka diperbaiki",
            "~0": "Data dapat diabaikan",
            "*": "Angka sementara",
            "**": "Angka sangat sementara",
            "***": "Angka sangat sangat sementara",
        }

        found = set()

        def inspect(value):
            if value is None:
                return

            text = str(value).strip()
            if not text:
                return

            # Simbol yang berdiri sendiri.
            if text in {"...", "-", "NA", "~0"}:
                found.add(text)

            # e/r sebagai anotasi angka, bukan huruf biasa pada label.
            if re.search(r"[-+]?\d[\d.,\s]*e$", text, flags=re.IGNORECASE):
                found.add("e")

            if re.search(r"[-+]?\d[\d.,\s]*r$", text, flags=re.IGNORECASE):
                found.add("r")

            # Bintang dibaca sesuai jumlah yang benar-benar muncul.
            for star_run in re.findall(r"\*{1,3}", text):
                if star_run in {"*", "**", "***"}:
                    found.add(star_run)

        # Header biasa.
        for column in table.get("columns", []) or []:
            inspect(column)

        # Header bertingkat.
        for header_row in table.get("header_rows", []) or []:
            for cell in header_row or []:
                if isinstance(cell, dict):
                    inspect(cell.get("label", ""))
                else:
                    inspect(cell)

        # Isi tabel.
        for row in table.get("rows", []) or []:
            for value in row or []:
                inspect(value)

        order = ["...", "-", "NA", "e", "r", "~0", "*", "**", "***"]

        return [
            {
                "symbol": symbol,
                "meaning": meanings[symbol],
            }
            for symbol in order
            if symbol in found
        ]

    def _df_to_table_payload(
        self,
        df: pd.DataFrame,
        title: str,
        source: str,
        max_rows: int = 100
    ) -> Dict:
        """Ubah DataFrame bersih menjadi payload tabel frontend."""

        def fmt(value):
            """
            Pertahankan nilai sumber apa adanya.
            Hanya missing value nyata dari pandas yang menjadi sel kosong.
            """
            if value is None:
                return ""

            try:
                if pd.isna(value):
                    return ""
            except Exception:
                pass

            return str(value)

        head = df.head(max_rows)

        rows = [
            [fmt(value) for value in row]
            for row in head.values.tolist()
        ]

        payload = {
            "title": title,
            "source": source,
            "columns": [str(column) for column in df.columns],
            "rows": rows,
            "total_rows": int(len(df))
        }

        source_note = str(df.attrs.get("source_note", "")).strip()

        if source_note:
            payload["source_note"] = source_note

        header_rows = df.attrs.get("header_rows", [])
        if header_rows:
            payload["header_rows"] = header_rows

        header_matrix = df.attrs.get("header_matrix", [])
        if header_matrix:
            payload["header_matrix"] = header_matrix

        body_rowspans = df.attrs.get(
            "body_rowspans",
            []
        )

        if body_rowspans:
            payload["body_rowspans"] = (
                body_rowspans
            )

        symbol_notes = self._detect_bps_symbol_notes(payload)
        if symbol_notes:
            payload["symbol_notes"] = symbol_notes

        return payload

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
    
    def _ensure_statistical_source_in_answer(
        self,
        answer: str,
        sources: List[Source],
        meta: Dict,
        table: Optional[Dict] = None,
    ) -> str:
        """
        Pastikan jawaban data statistik tetap menampilkan satu sumber.

        Aturan:
        1. Sheet prioritas dengan source_note -> sumber sudah ditampilkan
           frontend dari source_note, jadi tidak ditambahkan lagi.
        2. Tabel Dinamis/WebAPI -> tambahkan sumber ke jawaban karena
           tabel dinamis tidak mempunyai source_note.
        3. Jawaban data tanpa tabel -> sumber tetap ditambahkan.
        4. Hanya berlaku untuk response data statistik.
        """
        answer = str(answer or "").strip()
        statistical_source = str(meta.get("source", "")).strip()

        if statistical_source not in {"database_bps", "rekap_sheet"}:
            return answer

        if table and str(table.get("source_note", "")).strip():
            return answer

        if re.search(r"(?im)^\s*(?:📖\s*)?sumber\s*:", answer):
            return answer

        primary_source = None

        for source in sources or []:
            source_type = str(getattr(source, "type", "") or "").strip().lower()
            source_name = str(getattr(source, "name", "") or "").strip()

            if not source_name:
                continue
            if source_type == "definition":
                continue
            if source_name.lower().startswith("sumber definisi"):
                continue

            primary_source = source_name
            break

        if not primary_source:
            if statistical_source == "database_bps":
                primary_source = "Tabel Dinamis BPS"
            else:
                primary_source = "BPS Kabupaten Serdang Bedagai"

        source_line = f"📖 Sumber: {primary_source}"

        if not answer:
            return source_line

        return f"{answer}\n\n{source_line}"

    # async def _generate_with_fallback(
    #     self,
    #     question: str,
    #     context_text: str,
    #     sources: List[Source],
    #     meta: Dict,
    #     chat_history: Optional[List[Dict]],
    #     table: Optional[Dict] = None
    # ) -> ModelResponse:
    #     # ===== Coba Gemini =====
    #     gemini = self._get_gemini()
    #     if gemini:
    #         try:
    #             response = await gemini.generate_response(
    #                 question=question,
    #                 chat_history=chat_history,
    #                 context=context_text
    #             )
    #             if response.success:
    #                 response.sources = sources
    #                 response.table = table
    #                 response.meta = {
    #                     **meta,
    #                     "model_used": "gemini"
    #                 }
    #                 response.answer = (
    #                     self._ensure_statistical_source_in_answer(
    #                         answer=response.answer,
    #                         sources=sources,
    #                         meta=meta,
    #                         table=table,
    #                     )
    #                 )
    #                 return response
    #             else:
    #                 print(f"⚠️ Gemini gagal: {response.error}")
    #         except Exception as e:
    #             print(f"⚠️ Gemini exception: {type(e).__name__}: {e}")
        
    #     # ===== Fallback OpenAI =====
    #     openai = self._get_openai()
    #     if openai:
    #         try:
    #             response = await openai.generate_response(
    #                 question=question,
    #                 chat_history=chat_history,
    #                 context=context_text
    #             )
    #             if response.success:
    #                 response.sources = sources
    #                 response.table = table
    #                 response.meta = {
    #                     **meta,
    #                     "model_used": "openai",
    #                     "fallback": True
    #                 }
    #                 response.answer = (
    #                     self._ensure_statistical_source_in_answer(
    #                         answer=response.answer,
    #                         sources=sources,
    #                         meta=meta,
    #                         table=table,
    #                     )
    #                 )
    #                 return response
    #             else:
    #                 print(f"⚠️ OpenAI gagal: {response.error}")
    #         except Exception as e:
    #             print(f"⚠️ OpenAI exception: {type(e).__name__}: {e}")
        
    #     # ===== Keduanya gagal =====
    #     return ModelResponse(
    #         answer=(
    #             "Maaf, saat ini sistem sedang mengalami kendala dalam menghasilkan "
    #             "jawaban. Silakan coba beberapa saat lagi."
    #         ),
    #         sources=sources,
    #         table=table,
    #         meta={**meta, "fallback_failed": True, "model_used": "failed"},
    #         success=False
    #     )
    async def _generate_with_fallback(
        self,
        question: str,
        context_text: str,
        sources: List[Source],
        meta: Dict,
        chat_history: Optional[List[Dict]],
        table: Optional[Dict] = None
    ) -> ModelResponse:

        # ============================================================
        # TEST OPENAI DULU
        # ============================================================

        openai = self._get_openai()

        if openai:
            try:
                print("🧪 TEST MODE: mencoba OpenAI...")

                response = await openai.generate_response(
                    question=question,
                    chat_history=chat_history,
                    context=context_text
                )

                if response.success:
                    print("✅ OpenAI berhasil")

                    response.sources = sources
                    response.table = table

                    response.meta = {
                        **meta,
                        "model_used": "openai",
                        "test_mode": True,
                    }

                    return response

                else:
                    print(
                        f"⚠️ OpenAI gagal: {response.error}"
                    )

            except Exception as e:
                print(
                    f"⚠️ OpenAI exception: "
                    f"{type(e).__name__}: {e}"
                )

        else:
            print(
                "⚠️ OpenAI tidak aktif / OPENAI_API_KEY tidak ditemukan"
            )

        # ============================================================
        # FALLBACK KE GEMINI SAAT TEST OPENAI GAGAL
        # ============================================================

        gemini = self._get_gemini()

        if gemini:
            try:
                print(
                    "🔄 OpenAI gagal/tidak aktif, mencoba Gemini..."
                )

                response = await gemini.generate_response(
                    question=question,
                    chat_history=chat_history,
                    context=context_text
                )

                if response.success:
                    print("✅ Gemini berhasil")

                    response.sources = sources
                    response.table = table

                    response.meta = {
                        **meta,
                        "model_used": "gemini",
                        "fallback": True,
                    }

                    return response

                else:
                    print(
                        f"⚠️ Gemini gagal: {response.error}"
                    )

            except Exception as e:
                print(
                    f"⚠️ Gemini exception: "
                    f"{type(e).__name__}: {e}"
                )

        # ============================================================
        # KEDUANYA GAGAL
        # ============================================================

        return ModelResponse(
            answer=(
                "Maaf, saat ini sistem sedang mengalami kendala "
                "dalam menghasilkan jawaban. "
                "Silakan coba beberapa saat lagi."
            ),
            sources=sources,
            table=table,
            meta={
                **meta,
                "fallback_failed": True,
                "model_used": "failed",
            },
            success=False
        )
