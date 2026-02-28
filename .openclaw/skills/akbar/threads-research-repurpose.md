---
name: threads-research-repurpose
description: End-to-end workflow untuk scrape, analisis, dan repurpose konten Threads dari sebuah handle (default: @hanifmuh_). Gunakan saat user bilang "riset Threads", "analisis konten Threads", "repurpose konten Threads", "buat draft Threads mirip gaya akun ini", atau ingin workflow scrape→analisis→draft dalam satu prompt. Mendukung fallback ke browser automation jika web_fetch gagal karena JS-heavy/anti-bot.
compatibility: Membutuhkan tool web_fetch. Jika Threads sulit di-scrape, butuh Browser Automation (Managed/Relay/Extension) yang tersedia di environment OpenClaw.
metadata:
  author: akbar
  version: 1.0.1
  category: content
  tags: [threads, research, analysis, repurpose, content-strategy]
---

# Threads Research + Repurpose (OpenClaw Skill)

Skill ini menjalankan hingga 13 tahap dalam 1 alur:
1) **Scrape** post Threads dari akun target → simpan Markdown  
2) **Analisis Pilar Topik** (Deep Analysis Session 1)
3) **Analisis Pain Points** (Deep Analysis Session 2)
4) **Analisis Emotional Triggers & Beliefs** (Deep Analysis Session 3)
5) **Analisis Hook Library** (Deep Analysis Session 4)
6) **Analisis Structure DNA** (Deep Analysis Session 5)
7) **Analisis Tone & Diction Map** (Deep Analysis Session 6)
8) **Final Merge Report** → gabungan semua analisis jadi 1 laporan utuh
9) **Brave Topic Brief (Opsional)** → perkaya topik dengan web search
10) **Generate Hooks** (Repurpose Pass 1) → 20 hook spesifik topik
11) **Select & Outline** (Repurpose Pass 2) → pilih hook terbaik & buat outline
12) **Write Final Drafts** (Repurpose Pass 3) → 5 draft Threads final

## Kapan Skill Ini Dipakai
Gunakan skill ini jika user:
- minta "scrape Threads @…"
- minta "analisis gaya konten Threads akun …"
- minta "buat draft Threads dengan gaya serupa akun …"
- minta workflow "scrape → analisis → repurpose" dalam satu sesi.

## Input yang Harus Ditanyakan (kalau user belum kasih)
Tanyakan **singkat** dan **satu kali** (jangan bolak-balik):
- Handle target (default `hanifmuh_`)
- Batas jumlah post terbaru (default 80; saran 50–100)
- Topik repurpose (wajib; contoh: "AI workflow untuk content creator")
- Jumlah draft (default 5)
- Bahasa output (default Indonesia)

Jika user tidak jawab, pakai default aman:
- handle: `hanifmuh_`
- limit: `80`
- draft: `5`
- bahasa: `id`
- topik: minta user isi (minimal 1 topik)

---

# Workflow Utama (Wajib Ikuti Urutan)

## STEP 1 — Resolve URL + Scrape

1) Jika target_url kosong:
   - Pastikan target_handle diawali "@"
   - Bentuk URL target: https://www.threads.net/{target_handle}

2) Jalankan web_fetch pada URL target dengan extractMode: markdown.
3) Simpan hasil ke file: threads_raw.md

4) Validasi hasil:
   Anggap gagal jika:
   - kosong / error
   - hanya "enable JavaScript"
   - tidak ada teks post/caption yang jelas

Jika gagal:
5) Fallback ke Browser Automation:
   - buka URL target
   - scroll sampai minimal post_limit post terbaru (atau semampunya)
   - ekstrak untuk tiap post: text/caption + (date/metrics/url jika ada)
   - susun jadi Markdown
   - simpan overwrite ke threads_raw.md

Output ke chat:
- Konfirmasi file threads_raw.md dibuat
- Tuliskan 3–5 contoh potongan post (singkat) untuk bukti scrape berhasil

---

## STEP 2 — Normalisasi Dump

Baca threads_raw.md, lalu normalisasi jadi format ini:

# Threads Dump — {target_handle} (latest)
## Post 1
- date: YYYY-MM-DD or unknown
- url: ... or unknown
- metrics: likes X, replies Y, reposts Z (atau unknown)
- text:
  (isi post)

## Post 2
...

Aturan:
- Maksimal post_limit post terbaru
- Hapus duplikat
- Jika date/metrics/url tidak ada, isi unknown
- Pertahankan urutan: terbaru → lama

Simpan hasil ke file: threads_dump.md

Output ke chat:
- Konfirmasi threads_dump.md
- Statistik cepat: jumlah post final, ada/tidak metrics, rata-rata panjang post (perkiraan)

---

## STEP 3 — DEEP ANALYSIS (Session 1: Pilar Topik)

Baca `threads_dump.md`.

Tugas:
- Kelompokkan 5–10 tema/pilar konten yang paling sering muncul.
- Untuk tiap tema:
  1) Ringkasan 1–2 kalimat
  2) 3 kutipan singkat (<= 20 kata) yang diambil dari post sebagai bukti
  3) Catatan: "kenapa tema ini penting" (1 kalimat)

Output dalam Markdown dengan heading yang rapi.
Jangan bahas hook/tone dulu—fokus tema saja.

---

## STEP 4 — DEEP ANALYSIS (Session 2: Pain Points / Tension Audience)

Baca `threads_dump.md`.

Tugas:
- Infer pain points / tension / problem yang sering disinggung (tersurat atau tersirat).
- Buat 6–10 "problem statements" yang bisa dipakai untuk positioning.
- Untuk tiap problem statement:
  - Bukti: 2 kutipan singkat dari dump (<= 20 kata)
  - Catatan: siapa yang paling relate (persona singkat)
  - Dampak: "kalau ini tidak selesai, apa risikonya" (1 kalimat)

Jangan bikin asumsi di luar isi dump. Kalau bukti lemah, tulis "low confidence".

---

## STEP 5 — DEEP ANALYSIS (Session 3: Emotional Triggers & Beliefs)

Baca `threads_dump.md`.

Tugas:
- Identifikasi emosi dominan yang muncul (min 6 emosi).
- Identifikasi belief/opini khas (min 8) — termasuk yang "kontra arus" jika ada.
- Untuk tiap emosi:
  - 2 kutipan bukti (<= 20 kata)
  - "trigger" yang memicunya (1 kalimat)
- Untuk tiap belief:
  - 1 kutipan bukti
  - "implikasi ke konten": angle apa yang cocok (1 kalimat)

Fokus pada "apa yang dia percaya" dan "apa yang dia lawan".

---

## STEP 6 — DEEP ANALYSIS (Session 4: Hook Library Builder)

Baca `threads_dump.md`.

Tugas:
- Ambil 10 pola hook pembuka paling khas.
- Untuk tiap pola hook:
  1) Template umum (format: "Kalau kamu ___, berhenti ___")
  2) 1 contoh pembuka asli (kutipan <= 20 kata)
  3) Kapan dipakai (use-case: edukasi, kontroversi, curhat, tutorial, dsb.)

Catatan:
- Jangan copy isi full post.
- Tujuan: bikin "hook patterns" yang bisa direplikasi untuk topik lain.

---

## STEP 7 — DEEP ANALYSIS (Session 5: Structure DNA)

Baca `threads_dump.md`.

Tugas:
- Petakan format yang sering dipakai:
  - list/bullet
  - storytelling
  - opini pendek
  - Q&A
  - myth-busting
  - how-to
- Untuk tiap format:
  - ciri struktur (jumlah baris tipikal, panjang baris, ritme)
  - sinyal CTA (jenis penutup)
  - 2 contoh post yang merepresentasikan (kutipan singkat, bukan full)
- Jika metrics tersedia:
  - sebutkan format yang tampak perform lebih tinggi (berdasarkan contoh top posts)

Output harus actionable: "DNA" yang bisa ditiru.

---

## STEP 8 — DEEP ANALYSIS (Session 6: Tone & Diction Map)

Baca `threads_dump.md`.

Tugas:
- Profil tone: formal/santai, tegas/reflektif, humor/sarkas, optimis/skeptis.
- Diction: pilihan kata khas, campur Indo/English, "signature phrases".
- Tanda baca & emoji: pola penggunaan (kalau ada).
- Buat "Do & Don't" (min 10 poin) untuk meniru gaya TANPA meniru isi.
- Tambahkan "anti-plagiarism rules" singkat.

Sertakan 8–12 contoh kata/frasa khas (bukan kalimat panjang).

---

## STEP 9 — FINAL MERGE (Comprehensive Report)

Gabungkan hasil dari STEP 3 sampai STEP 8 menjadi 1 laporan komprehensif yang rapi.

Struktur report wajib:
1) Executive Summary (5 bullet)
2) Content Pillars (tema utama + bukti kutipan)
3) Deepest Tensions (Top 3 pain points/tension + trigger emosinya)
4) Hook Library (10 template + contoh)
5) Structure DNA (format terbaik + rekomendasi panjang/ritme/CTA)
6) Tone & Style Guide (do/don't + diction map)
7) Repurpose Rules (5–8 aturan praktis)

Simpan sebagai file: `report.md`

Output ke chat:
- Konfirmasi `report.md` dibuat
- Tampilkan 10 bullet "language bank" (frasa yang bisa dipakai untuk hook/copy)

---

## STEP 10 — OPTIONAL (Brave Topic Brief)
**Jika user mengaktifkan mode=brave_search atau meminta riset web:**

Gunakan `web_search` (Brave) untuk topik: `{topic_repurpose}`

Kumpulkan:
- 10 insight terbaru / angle unik
- 10 myth / kontroversi yang sering diperdebatkan

Ringkas jadi "Topic Brief" (bullet) + cantumkan sumber (judul + domain saja).
*Gunakan Topic Brief ini untuk memperkaya hook dan draft di tahap selanjutnya.*

---

## STEP 11 — REPURPOSE (Pass 1: Generate Hooks)

Berdasarkan `report.md`, buat 20 hook untuk topik: `{topic_repurpose}`

Syarat:
- gaya mengikuti `{target_handle}` (tone + diction + structure DNA)
- variasikan: edukasi, kontroversi, curhat, myth-busting, how-to
- 1 hook = 1 baris
- jangan copy kalimat asli dari dump

Output: daftar bernomor 1–20

---

## STEP 12 — REPURPOSE (Pass 2: Select & Outline)
Pilih 5 hook terbaik dari STEP 10.
Untuk tiap hook, buat outline super singkat (3 bullet points) alur ceritanya.

---

## STEP 13 — REPURPOSE (Pass 3: Write Final Drafts)

Gunakan outline yang sudah dipilih (dan *Topic Brief* jika ada) untuk menulis `draft_count` (default: 5) draft final.

Syarat per draft:
- 6–14 baris
- 1 CTA ringan di akhir
- bahasa: `{language}` (sesuai input)
- jangan klaim faktual spesifik yang tidak bisa diverifikasi
- jangan menyebut "meniru akun X"

Simpan semua draft ke file: `drafts.md`

Format file:
# Drafts — {target_handle} style, topic: {topic_repurpose}
## Draft 1 (judul internal)
...
## Draft 5
...

Output ke chat:
- Konfirmasi `drafts.md` dibuat
- Tampilkan draft #1 dan #2 saja (biar ringkas)

---

# Output Akhir (Wajib Ditampilkan ke User)
Tampilkan ringkasan + lokasi file:
- `threads_raw.md` (raw scrape)
- `threads_dump.md` (dump final)
- `report.md` (Laporan Analisis Komprehensif)
- `drafts.md` (File Draft Repurpose)

Sertakan juga:
- 5 bullet "temuan paling penting" dari target akun
- 3 ide konten masa depan (judul + angle)

---

# Troubleshooting
## web_fetch gagal (JS-heavy / anti-bot)
Solusi:
- Pakai Browser Automation untuk render halaman
- Jika tetap diblok:
  - sarankan integrasi Decodo/anti-bot (jika tersedia di environment user)

## Output terlalu panjang / boros token
Solusi:
- Turunkan limit ke 50–80 post
- Fokus ke post dengan sinyal engagement tertinggi (jika metrics tersedia)

## Metrics tidak tersedia
Solusi:
- Gunakan heuristik (panjang, struktur, CTA, format) dan beri catatan "metrics not available"
