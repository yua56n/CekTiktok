# TikTok Upload Monitor — GitHub Actions + Cron Eksternal

Memantau akun TikTok, cek tiap ~10 detik (tick-aligned, tidak ngaret),
kirim notifikasi Telegram saat ada video baru. Trigger AWAL dipicu presisi
dari luar GitHub (bukan `schedule:` bawaan GitHub yang tidak presisi).
Otomatis melanjutkan diri sendiri kalau window (misal 10 jam) melebihi
limit durasi 1 job GitHub Actions (6 jam).

## Struktur file

```
.github/workflows/monitor.yml   # definisi job + trigger
scripts/monitor.py              # logic utama: loop, cek, notif, chaining
scripts/requirements.txt
state.json                      # jangan diedit manual, dikelola otomatis
trigger-start.ps1               # dipanggil Windows Task Scheduler untuk mulai window
```

## 1. Buat repo GitHub

Buat repo baru, **public** (supaya menit Actions gratis unlimited), lalu
upload semua file di atas ke branch `main`.

## 2. Buat bot Telegram

1. Chat `@BotFather` di Telegram → `/newbot` → ikuti instruksi → dapat `BOT_TOKEN`.
2. Cari tahu `CHAT_ID` kamu: chat `@userinfobot`, dia akan balas ID kamu.
   (Atau kirim pesan apa saja ke bot kamu, lalu buka
   `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` di browser, cari
   `"chat":{"id":...}`.)

## 3. Set Secrets & Variables di repo GitHub

Buka repo → **Settings → Secrets and variables → Actions**:

**Secrets** (tab "Secrets"):
| Nama | Isi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token dari BotFather |
| `TELEGRAM_CHAT_ID` | chat ID kamu |

**Variables** (tab "Variables"):
| Nama | Isi |
|---|---|
| `TIKTOK_USERNAME` | username TikTok yang mau dipantau, tanpa `@` |

## 4. Buat Personal Access Token (PAT) — untuk memicu dari luar

Ini token yang dipakai untuk **menyalakan** window monitoring dari luar
GitHub, jadi bukan disimpan di repo, tapi disimpan lokal di laptop kamu.

1. GitHub → Settings (akun, bukan repo) → **Developer settings → Personal
   access tokens → Fine-grained tokens → Generate new token**.
2. Scope: pilih repo target ini saja, izin **Actions: Read and write**.
3. Copy token-nya, simpan baik-baik (nggak akan ditampilkan lagi).

## 5. Setup trigger presisi via Windows Task Scheduler

Ini pengganti `schedule:` GitHub yang kamu hindari karena tidak presisi.
Laptop kamu **hanya perlu nyala di momen trigger**, bukan selama 10 jam
— begitu terpicu, semua proses berikutnya jalan di server GitHub.

1. Edit `trigger-start.ps1`, isi `GITHUB_PAT`, `GITHUB_OWNER`, `GITHUB_REPO`.
2. Buka **Task Scheduler** di Windows → **Create Task**:
   - General: beri nama, centang "Run whether user is logged on or not" kalau perlu.
   - Triggers: New → set jam presisi yang kamu mau (misal tiap hari jam 08:00:00).
   - Actions: New → Program/script: `powershell.exe`,
     Argument: `-ExecutionPolicy Bypass -File "C:\path\ke\trigger-start.ps1"`
3. Test dulu manual (klik kanan task → Run), cek tab **Actions** di repo
   GitHub — harus muncul run baru dalam beberapa detik.

> Alternatif kalau nggak mau bergantung laptop nyala: pakai layanan cron
> eksternal gratis (misal cron-job.org) yang hit endpoint yang sama.
> Tapi karena itu perlu expose PAT ke pihak ketiga, kalau mau lebih aman
> sebaiknya taruh dulu di depan Cloudflare Worker/Netlify Function kecil
> yang menyimpan PAT sebagai secret di sana, bukan di layanan cron-nya
> langsung. Kalau kamu mau opsi ini, saya bisa bantu buatkan.

## 6. Jalankan

Trigger `trigger-start.ps1` (manual dulu untuk uji coba). Ini akan:
1. Memulai job GitHub Actions, set window 10 jam ke depan di `state.json`.
2. Run pertama cuma set **baseline** (video terbaru saat ini), belum kirim notif.
3. Tiap ~10 detik dicek, kalau ID video berubah → notif Telegram.
4. Kalau job mendekati ~5 jam 55 menit dan window belum selesai, job ini
   otomatis memicu run lanjutan (`continue-monitor`) untuk melanjutkan
   sisa waktu — kamu tidak perlu trigger manual lagi untuk ini.
5. Setelah 10 jam window selesai, job berhenti dengan sendirinya.

Pantau progress lewat tab **Actions** di repo → klik run yang sedang
jalan → lihat log real-time.

## Batasan & risiko yang perlu kamu tahu

- **Anti-bot TikTok**: browser jalan headless dari IP datacenter GitHub.
  Ada kemungkinan kena challenge/captcha terutama di awal sesi, dan tidak
  ada manusia untuk menyelesaikannya di tengah run otomatis. Script akan
  kirim **1x alert Telegram** kalau gagal cek 5x berturut-turut, supaya
  kamu tahu perlu intervensi manual (misalnya restart window baru setelah
  cek manual dari browser kamu sendiri).
- **Selector/parsing HTML**: script mengandalkan pola URL
  `/@username/video/<id>` yang muncul di HTML halaman profil. Kalau TikTok
  mengubah struktur halaman secara signifikan, ini bisa berhenti berfungsi
  dan butuh disesuaikan.
- **Commit state.json**: tiap kali ada video baru terdeteksi, script commit
  `state.json` balik ke repo. Ini normal dan diharapkan (bukan bug) — cara
  ini yang membuat state tersambung antar run yang chaining.
