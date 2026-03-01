---
name: threads-research-repurpose
description: End-to-end workflow untuk scrape, analisis, dan repurpose konten Threads dari sebuah handle (default: @hanifmuh_). Self-evolving — makin pintar setiap sesi. Gunakan saat user bilang "riset Threads", "analisis konten Threads", "repurpose konten Threads", "buat draft Threads mirip gaya akun ini", atau ingin workflow scrape→analisis→draft dalam satu prompt. Mendukung fallback ke browser automation jika web_fetch gagal karena JS-heavy/anti-bot.
compatibility: Membutuhkan tool web_fetch. Jika Threads sulit di-scrape, butuh Browser Automation (Managed/Relay/Extension) yang tersedia di environment OpenClaw.
metadata:
  author: akbar
  version: 2.1.0
  category: content
  tags: [threads, research, analysis, repurpose, content-strategy, self-evolving]
---

# Threads Research + Repurpose (OpenClaw Skill)

Skill ini menjalankan hingga 15 tahap dalam 1 alur:
0) **Load Knowledge Base** → baca accumulated learnings
1) **Scrape** post Threads dari akun target → simpan Markdown (+ update scrape learnings)
2) **Normalisasi Dump** → format data konsisten
3) **Analisis Pilar Topik** (Deep Analysis Session 1)
4) **Analisis Pain Points** (Deep Analysis Session 2)
5) **Analisis Emotional Triggers & Beliefs** (Deep Analysis Session 3)
6) **Analisis Hook Library** (Deep Analysis Session 4)
7) **Analisis Structure DNA** (Deep Analysis Session 5)
8) **Analisis Tone & Diction Map** (Deep Analysis Session 6)
9) **Final Merge Report** → gabungan semua analisis (+ update analysis learnings)
10) **Brave Topic Brief (Opsional)** → perkaya topik dengan web search
11) **Generate Hooks** (Repurpose Pass 1) → 20 hook spesifik topik
12) **Select & Outline** (Repurpose Pass 2) → pilih hook terbaik & buat outline
13) **Write Final Drafts** (Repurpose Pass 3) → 5 draft Threads final
14) **User Feedback & Self-Evolution** → minta feedback, simpan learnings

## Knowledge Base (WAJIB DIBACA SEBELUM MULAI)

Sebelum memulai workflow apapun, **WAJIB** baca knowledge base di:
`{baseDir}/research-knowledge.md`

File ini berisi accumulated learnings dari semua sesi sebelumnya. Terapkan semua pelajaran yang ada:
- Scrape: gunakan metode yang sudah terbukti berhasil untuk handle serupa
- Analysis: perhatikan pattern yang berulang lintas akun
- Drafts: ikuti format/hook yang mendapat skor tinggi dari user sebelumnya

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

# ATURAN TOKEN SAFETY (WAJIB DIPATUHI)

> **PRIORITAS #1: FILE WRITE > CHAT OUTPUT**
>
> Di setiap step yang menghasilkan output panjang:
> 1. **TULIS KE FILE DULU** — selesaikan semua file write sebelum output ke chat
> 2. **Knowledge update WAJIB DULUAN** — append ke research-knowledge.md SEBELUM output apapun ke chat
> 3. **Chat output TERAKHIR** — hanya setelah semua file write sukses
> 4. **Chat output RINGKAS** — maksimal 15 baris per step, jangan dump seluruh analisis ke chat
>
> **ALASAN:** LLM punya batas output token. Jika chat output ditulis duluan dan terlalu panjang,
> file write di akhir akan terpotong/gagal. Ini menyebabkan knowledge base tidak terupdate.
>
> **CHECKPOINT:** Setelah setiap step, konfirmasi singkat ke user:
> `[✓ Step N selesai — file: {nama_file}]`
> Jika step gagal di tengah jalan, user bisa minta lanjut dari step terakhir yang sukses.

# Workflow Utama (Wajib Ikuti Urutan)

## STEP 0 — Load Knowledge Base

1) Baca `{baseDir}/research-knowledge.md`
2) Terapkan semua learnings yang relevan untuk sesi ini:
   - **Scrape**: cek apakah ada catatan untuk handle yang sama/serupa. Jika ya, langsung pakai metode yang terbukti berhasil.
   - **Analysis**: cek pattern yang berulang lintas akun. Gunakan sebagai hipotesis awal saat analisis.
   - **Drafts**: cek hook/format yang mendapat feedback positif dari user. Prioritaskan gaya tersebut.
3) Jika knowledge base kosong (sesi pertama), lanjut ke Step 1 dengan foundational knowledge saja.

Output ke chat:
- Sebutkan berapa session learnings yang ditemukan
- Sebutkan 1–2 learnings paling relevan untuk sesi ini (jika ada)

---

## STEP 1 — Resolve URL + Scrape

1) Jika target_url kosong:
   - Pastikan target_handle diawali "@"
   - Bentuk URL target: https://www.threads.net/{target_handle}

2) **Cek knowledge base**: apakah ada scrape learning untuk handle ini?
   - Jika ya: langsung pakai metode yang terbukti berhasil (skip trial-error)
   - Jika tidak: mulai dengan web_fetch

3) Jalankan web_fetch pada URL target dengan extractMode: markdown.
4) Simpan hasil ke file: threads_raw.md

5) Validasi hasil:
   Anggap gagal jika:
   - kosong / error
   - hanya "enable JavaScript"
   - tidak ada teks post/caption yang jelas

Jika gagal:
6) Fallback ke Browser Automation:
   - buka URL target
   - scroll sampai minimal post_limit post terbaru (atau semampunya)
   - ekstrak untuk tiap post: text/caption + (date/metrics/url jika ada)
   - susun jadi Markdown
   - simpan overwrite ke threads_raw.md

### 🧠 Knowledge Update (Scrape)
Setelah scrape selesai (berhasil atau fallback), **WAJIB append** ke `{baseDir}/research-knowledge.md`:

```
### Scrape — @{handle} — {YYYY-MM-DD}
- **Method**: web_fetch / browser_automation / fallback
- **Success**: ya/tidak
- **Posts scraped**: {N}
- **Gotcha**: {masalah yang ditemui, "none" jika lancar}
- **Tip**: {apa yang akhirnya berhasil}
---
```

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

## STEP 3–8 — DEEP ANALYSIS (6 Sesi, Simpan ke File)

> **TOKEN SAFETY:** Semua hasil analisis Step 3–8 WAJIB ditulis ke file `analysis_notes.md`
> (append per section). Chat output cukup konfirmasi singkat per step.
> Ini mencegah output terpotong karena token limit.

### STEP 3 — Pilar Topik (Session 1)

Baca `threads_dump.md`.

Tugas:
- Kelompokkan 5–10 tema/pilar konten yang paling sering muncul.
- Untuk tiap tema:
  1) Ringkasan 1–2 kalimat
  2) 3 kutipan singkat (<= 20 kata) dari post sebagai bukti
  3) Catatan: "kenapa tema ini penting" (1 kalimat)

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Pilar Topik`
**Output ke chat:** `[✓ Step 3 selesai]` + sebutkan 3 pilar teratas (1 baris per pilar)

---

### STEP 4 — Pain Points / Tension (Session 2)

Baca `threads_dump.md`.

Tugas:
- Infer pain points / tension / problem (tersurat atau tersirat).
- Buat 6–10 "problem statements" untuk positioning.
- Untuk tiap problem statement:
  - Bukti: 2 kutipan singkat (<= 20 kata)
  - Persona: siapa yang paling relate
  - Dampak: "kalau ini tidak selesai, apa risikonya" (1 kalimat)

Jangan bikin asumsi di luar isi dump. Kalau bukti lemah, tulis "low confidence".

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Pain Points`
**Output ke chat:** `[✓ Step 4 selesai]` + sebutkan 3 pain point teratas (1 baris per item)

---

### STEP 5 — Emotional Triggers & Beliefs (Session 3)

Baca `threads_dump.md`.

Tugas:
- Identifikasi emosi dominan (min 6 emosi).
- Identifikasi belief/opini khas (min 8) — termasuk "kontra arus".
- Untuk tiap emosi: 2 kutipan bukti + trigger (1 kalimat)
- Untuk tiap belief: 1 kutipan bukti + "implikasi ke konten" (1 kalimat)

Fokus pada "apa yang dia percaya" dan "apa yang dia lawan".

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Emotional Triggers & Beliefs`
**Output ke chat:** `[✓ Step 5 selesai]` + sebutkan 3 emosi/belief terkuat

---

### STEP 6 — Hook Library Builder (Session 4)

Baca `threads_dump.md`.

Tugas:
- Ambil 10 pola hook pembuka paling khas.
- Untuk tiap pola hook:
  1) Template umum (format: "Kalau kamu ___, berhenti ___")
  2) 1 contoh pembuka asli (kutipan <= 20 kata)
  3) Kapan dipakai (edukasi, kontroversi, curhat, tutorial, dsb.)

Jangan copy isi full post. Tujuan: bikin "hook patterns" replicable.

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Hook Library`
**Output ke chat:** `[✓ Step 6 selesai]` + tampilkan 3 hook template terbaik

---

### STEP 7 — Structure DNA (Session 5)

Baca `threads_dump.md`.

Tugas:
- Petakan format: list/bullet, storytelling, opini pendek, Q&A, myth-busting, how-to
- Untuk tiap format:
  - ciri struktur (jumlah baris, panjang, ritme)
  - sinyal CTA (jenis penutup)
  - 2 contoh post (kutipan singkat)
- Jika metrics tersedia: sebutkan format yang perform lebih tinggi

Output harus actionable: "DNA" yang bisa ditiru.

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Structure DNA`
**Output ke chat:** `[✓ Step 7 selesai]` + sebutkan 2 format dominan

---

### STEP 8 — Tone & Diction Map (Session 6)

Baca `threads_dump.md`.

Tugas:
- Profil tone: formal/santai, tegas/reflektif, humor/sarkas, optimis/skeptis.
- Diction: pilihan kata khas, campur Indo/English, "signature phrases".
- Tanda baca & emoji: pola penggunaan.
- "Do & Don't" (min 10 poin) untuk meniru gaya TANPA meniru isi.
- "Anti-plagiarism rules" singkat.

**Simpan hasil ke file:** Append ke `analysis_notes.md` dengan heading `## Tone & Diction Map`
**Output ke chat:** `[✓ Step 8 selesai]` + sebutkan 4–5 frasa khas

---

## STEP 9 — FINAL MERGE (Comprehensive Report)

> **TOKEN SAFETY — URUTAN WAJIB:**
> 1. Tulis knowledge update ke research-knowledge.md **PERTAMA**
> 2. Tulis report.md **KEDUA**
> 3. Output ke chat **TERAKHIR** (ringkas saja)

### 9A — Knowledge Update DULU (WAJIB SEBELUM REPORT)

Berdasarkan hasil analisis di `analysis_notes.md`, **LANGSUNG append** ke `{baseDir}/research-knowledge.md`:

```
### Analysis — @{handle} — {YYYY-MM-DD}
- **Niche**: {kategori akun: tech/lifestyle/business/education/dll}
- **Top Pattern**: {pola paling menarik yang ditemukan}
- **Unique Insight**: {insight yang belum pernah ditemukan sebelumnya}
- **Hook Style Dominan**: {jenis hook yang paling sering: provokasi/pertanyaan/statement/dll}
- **Avg Post Length**: {pendek <5 baris / sedang 5-10 / panjang >10}
- **Cross-Account Pattern**: {jika ada kesamaan dengan akun yang pernah diriset sebelumnya, sebutkan}
---
```

### 9B — Buat Report

Baca `analysis_notes.md` dan gabungkan menjadi 1 laporan komprehensif.

Struktur report wajib:
1) Executive Summary (5 bullet)
2) Content Pillars (tema utama + bukti kutipan)
3) Deepest Tensions (Top 3 pain points/tension + trigger emosinya)
4) Hook Library (10 template + contoh)
5) Structure DNA (format terbaik + rekomendasi panjang/ritme/CTA)
6) Tone & Style Guide (do/don't + diction map)
7) Repurpose Rules (5–8 aturan praktis)

Simpan sebagai file: `report.md`

### 9C — Output ke Chat (TERAKHIR, RINGKAS)

- `[✓ Step 9 selesai — knowledge updated + report.md dibuat]`
- Tampilkan 10 bullet "language bank" (frasa yang bisa dipakai untuk hook/copy)
- **Jangan dump seluruh report ke chat** — user bisa baca di file

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

Berdasarkan `report.md` (+ knowledge base learnings tentang hook yang pernah disukai user), buat 20 hook untuk topik: `{topic_repurpose}`

Syarat:
- gaya mengikuti `{target_handle}` (tone + diction + structure DNA)
- variasikan: edukasi, kontroversi, curhat, myth-busting, how-to
- **PRIORITASKAN** jenis hook yang mendapat feedback positif di knowledge base (jika ada)
- 1 hook = 1 baris
- jangan copy kalimat asli dari dump

Output: daftar bernomor 1–20

---

## STEP 12 — REPURPOSE (Pass 2: Select & Outline)
Pilih 5 hook terbaik dari STEP 11.
Untuk tiap hook, buat outline super singkat (3 bullet points) alur ceritanya.

Jika knowledge base punya info tentang format yang disukai user, **prioritaskan format tersebut**.

---

## STEP 13 — REPURPOSE (Pass 3: Write Final Drafts)

Gunakan outline yang sudah dipilih (dan *Topic Brief* jika ada) untuk menulis `draft_count` (default: 5) draft final.

Syarat per draft:
- 6–14 baris
- 1 CTA ringan di akhir
- bahasa: `{language}` (sesuai input)
- jangan klaim faktual spesifik yang tidak bisa diverifikasi
- jangan menyebut "meniru akun X"
- **Terapkan** learnings dari knowledge base tentang format/hook/tone yang disukai

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

## STEP 14 — User Feedback & Self-Evolution

Setelah semua output ditampilkan, **WAJIB** minta feedback dari user:

```
📝 Feedback Time!
Dari {draft_count} draft, jawab singkat:
1. Draft mana yang paling kamu suka? (nomor)
2. Draft mana yang kurang oke? (nomor + alasan singkat)
3. Ada gaya/angle yang mau lebih banyak next time?

Balas singkat aja, misal: "suka 1,3 — kurang suka 4 terlalu panjang — next time lebih banyak myth-busting"
```

Berdasarkan feedback user:

1) **Beri skor internal** 1–10 per draft:
   - Draft yang dipuji user: 8–10
   - Draft yang tidak dikomentari: 6
   - Draft yang dikritik: 3–5

2) **Identifikasi pola**:
   - Format apa yang disukai? (list/story/opini/dll)
   - Hook type apa yang menarik?
   - Tone apa yang cocok?
   - Panjang yang ideal?

3) **WAJIB append** ke `{baseDir}/research-knowledge.md`:

```
### Draft Feedback — {topic} — {YYYY-MM-DD}
- **Handle Ref**: @{handle}
- **Topic**: {topic_repurpose}
- **Drafts Generated**: {N}
- **User Favorites**: Draft #{list nomor}
- **User Dislikes**: Draft #{list nomor}
- **What Worked**: {hook type, format, tone yang disukai}
- **What Failed**: {elemen yang ditolak / diminta revisi}
- **Key Learning**: {1 kalimat insight actionable untuk sesi berikutnya}
---
```

4) **Jika user minta revisi**:
   - Revisi maksimal 2 pass (total 3 versi termasuk original)
   - Setiap revisi, simpan learning tambahan
   - Setelah revisi selesai, update skor di knowledge base

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
- Cek knowledge base untuk metode scrape yang terbukti berhasil
- Pakai Browser Automation untuk render halaman
- Jika tetap diblok:
  - sarankan integrasi Decodo/anti-bot (jika tersedia di environment user)

## Output terpotong / knowledge base tidak terupdate
Solusi:
- Ini terjadi karena LLM kehabisan output token sebelum sempat write ke file
- Pastikan ikuti aturan TOKEN SAFETY: file write DULU, chat output TERAKHIR
- Step 3–8 menyimpan analisis ke `analysis_notes.md` (bukan ke chat)
- Step 9 menulis knowledge update SEBELUM generate report
- Jika masih terpotong: minta user lanjutkan dari step terakhir yang sukses

## Output terlalu panjang / boros token
Solusi:
- Turunkan limit ke 50–80 post
- Fokus ke post dengan sinyal engagement tertinggi (jika metrics tersedia)

## Metrics tidak tersedia
Solusi:
- Gunakan heuristik (panjang, struktur, CTA, format) dan beri catatan "metrics not available"

## Knowledge base hilang / corrupt
Solusi:
- Buat ulang `{baseDir}/research-knowledge.md` dengan foundational knowledge dari SKILL.md
- Semua session learnings sebelumnya akan hilang — mulai dari nol
