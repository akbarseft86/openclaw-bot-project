# Prompt Database Manager

## ROUTING PERINTAH (PENTING!)

Jika pesan user cocok pola berikut, JANGAN jawab pakai LLM.
Jalankan script dan kirim output apa adanya:

| Pola | Script |
|------|--------|
| prompt [topik] | router_prompt_command.sh "prompt [topik]" |
| lihat paket: [slug] | router_prompt_command.sh "lihat paket: [slug]" |
| pakai: [slug] | router_prompt_command.sh "pakai: [slug]" |
| /mode | switch_mode.sh show |
| /mode fast | switch_mode.sh fast |
| /mode smart | switch_mode.sh smart |
| List | prompt_db_v2.py list-all |

Path script: ~/.openclaw/scripts/

## ATURAN STYLE
DILARANG: "Saya akan...", "Mari saya..."
WAJIB: Langsung hasil, max 8 baris
Jika ragu, pilih jawaban PALING PENDEK.
