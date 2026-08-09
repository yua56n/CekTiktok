"""
TikTok Upload Monitor - GitHub Actions
========================================
Dipicu dari luar (cron eksternal kamu) lewat GitHub API (workflow_dispatch),
BUKAN lewat `schedule:` GitHub Actions.

Alur:
  1. Browser dibuka SEKALI di awal job (bukan reload tiap iterasi).
  2. Loop tick-aligned: cek tiap ~10 detik, sleep-nya dihitung dari sisa
     waktu (interval - waktu_proses), bukan sleep(10) flat -> tidak ngaret.
  3. Job GitHub Actions dibatasi ~5 jam 55 menit (di bawah limit keras 6 jam).
     Kalau window (misal 10 jam) belum selesai saat mendekati batas itu,
     job ini memicu dirinya sendiri lagi (repository_dispatch) untuk
     melanjutkan sisa window, lalu keluar.
  4. State (last_video_id, window_end) disimpan di state.json, di-commit
     balik ke repo agar tersambung antar run yang chaining.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ============== KONFIGURASI ==============
CHECK_INTERVAL_SEC = 10          # target interval tiap cek
JOB_SOFT_DEADLINE_MIN = 345      # 5 jam 55 menit -> harus di bawah timeout-minutes di YAML
CONSECUTIVE_FAIL_ALERT = 5       # kirim 1x alert kalau gagal beruntun sebanyak ini
STATE_FILE = Path("state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# ==========================================

TIKTOK_USERNAME = os.environ["TIKTOK_USERNAME"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]
EVENT_NAME = os.environ.get("EVENT_NAME", "workflow_dispatch")
INPUT_DURATION_HOURS = os.environ.get("INPUT_DURATION_HOURS") or "10"

TIKTOK_URL = f"https://www.tiktok.com/@{TIKTOK_USERNAME}"
VIDEO_ID_PATTERN = re.compile(rf"/@{re.escape(TIKTOK_USERNAME)}/video/(\d+)")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def git_commit_and_push(message: str):
    """Commit & push state.json langsung, dipakai saat ada notifikasi baru
    supaya state tidak hilang meski job berhenti di tengah jalan."""
    os.system('git config user.name "tiktok-monitor-bot"')
    os.system('git config user.email "actions@users.noreply.github.com"')
    os.system("git add state.json")
    os.system(f'git diff --cached --quiet || git commit -m "{message}"')
    os.system("git push")


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"Gagal kirim Telegram: {e}")


def trigger_continuation():
    """Picu run lanjutan lewat repository_dispatch, dipakai saat mendekati
    limit durasi job tapi window belum selesai."""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"event_type": "continue-monitor"}
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    print(f"Trigger continuation: {r.status_code}")


async def fetch_latest_video_id(page) -> tuple[str | None, dict]:
    """Fetch HTML halaman profil dari DALAM konteks browser yang sudah
    terbuka (pakai cookies & session yang sama), bukan reload/navigasi
    penuh. Lebih cepat dan lebih ringan daripada page.reload().

    Return (video_id_atau_None, debug_info) -- debug_info dipakai untuk
    diagnosis kalau video_id None (gagal parsing)."""
    result = await page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'include' });
            const text = await res.text();
            return { status: res.status, text: text };
        }""",
        TIKTOK_URL,
    )
    html = result["text"]
    debug_info = {
        "http_status": result["status"],
        "html_length": len(html),
        "snippet": html[:300].replace("\n", " "),
        "contains_captcha": "captcha" in html.lower(),
        "contains_verify": "verify" in html.lower(),
    }
    match = VIDEO_ID_PATTERN.search(html)
    return (match.group(1) if match else None), debug_info


async def run():
    state = load_state()
    job_start = time.monotonic()
    soft_deadline = job_start + JOB_SOFT_DEADLINE_MIN * 60

    if EVENT_NAME == "workflow_dispatch":
        # Window baru dimulai
        duration_hours = float(INPUT_DURATION_HOURS)
        window_end = now_utc() + timedelta(hours=duration_hours)
        state = {
            "window_end": window_end.isoformat(),
            "last_video_id": None,
            "started_at": now_utc().isoformat(),
        }
        save_state(state)
        print(f"Window baru dimulai, berakhir pada {window_end.isoformat()}")
    else:
        # Lanjutan dari run sebelumnya (repository_dispatch)
        if "window_end" not in state:
            print("Tidak ada window aktif di state.json, keluar.")
            return
        window_end = datetime.fromisoformat(state["window_end"])
        print(f"Melanjutkan window, berakhir pada {window_end.isoformat()}")

    if now_utc() >= window_end:
        print("Window sudah berakhir sebelum job ini mulai, keluar.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=30000)

        consecutive_fails = 0
        alert_sent = False
        next_tick = time.monotonic()

        while True:
            tick_start = time.monotonic()

            if tick_start >= soft_deadline and now_utc() < window_end:
                print("Mendekati batas durasi job, memicu lanjutan...")
                trigger_continuation()
                break

            if now_utc() >= window_end:
                print("Window selesai.")
                break

            try:
                video_id, debug_info = await fetch_latest_video_id(page)
                if video_id is None:
                    consecutive_fails += 1
                    print(f"[{datetime.now():%H:%M:%S}] Tidak bisa ambil ID video (gagal ke-{consecutive_fails}).")
                    print(f"    debug: status={debug_info['http_status']} "
                          f"panjang_html={debug_info['html_length']} "
                          f"captcha={debug_info['contains_captcha']} "
                          f"verify={debug_info['contains_verify']}")
                    print(f"    cuplikan: {debug_info['snippet']}")
                else:
                    consecutive_fails = 0
                    alert_sent = False
                    if state.get("last_video_id") is None:
                        print(f"[{datetime.now():%H:%M:%S}] Baseline diset: {video_id}")
                        state["last_video_id"] = video_id
                        save_state(state)
                    elif video_id != state["last_video_id"]:
                        video_url = f"https://www.tiktok.com/@{TIKTOK_USERNAME}/video/{video_id}"
                        print(f"[{datetime.now():%H:%M:%S}] VIDEO BARU: {video_url}")
                        send_telegram(f"Video baru dari @{TIKTOK_USERNAME}:\n{video_url}")
                        state["last_video_id"] = video_id
                        save_state(state)
                        git_commit_and_push("chore: update last_video_id [skip ci]")
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] Belum ada perubahan.")
            except Exception as e:
                consecutive_fails += 1
                print(f"[{datetime.now():%H:%M:%S}] Error saat cek (ke-{consecutive_fails}): {e}")

            if consecutive_fails >= CONSECUTIVE_FAIL_ALERT and not alert_sent:
                send_telegram(
                    f"⚠️ Gagal cek TikTok {consecutive_fails}x berturut-turut. "
                    f"Kemungkinan diblokir/berubah struktur halaman, cek manual."
                )
                alert_sent = True

            # ---- tick alignment: sleep = interval - waktu yang terpakai ----
            next_tick += CHECK_INTERVAL_SEC
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                # run overrun, jangan numpuk drift -> re-sync ke waktu sekarang
                next_tick = time.monotonic()

        await browser.close()

    save_state(state)


if __name__ == "__main__":
    asyncio.run(run())
