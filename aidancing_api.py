"""Aidancing.net HTTP helpers — gọi API qua session Chrome (fetch in-page).

Cần Playwright BrowserContext đã login (CDP hoặc persistent profile).
Không dùng requests thuần — Cloudflare chặn 503.
"""

import base64
import json
import os
import time

AIDANCING_ORIGIN = os.environ.get("AIDANCING_ORIGIN", "https://aidancing.net")
DASHBOARD_URL = f"{AIDANCING_ORIGIN}/dashboard"


class AidancingApiClient:
    """Tab nền cố định + fetch API — không reload dashboard mỗi lần poll."""

    def __init__(self, context, warmup=True, persistent=False):
        self.context = context
        self.persistent = persistent
        self._page = None
        self._warmed = False
        if warmup:
            self.warmup(force=True)

    def _page_alive(self):
        return self._page is not None and not self._page.is_closed()

    def _on_aidancing(self):
        if not self._page_alive():
            return False
        return "aidancing.net" in (self._page.url or "")

    def warmup(self, force=False):
        """Mở dashboard 1 lần; lần sau chỉ dùng fetch (không reload trang)."""
        if self.persistent and self._warmed and self._page_alive() and not force:
            if self._on_aidancing():
                return self._page
        page = self._page if self._page_alive() else self.context.new_page()
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(800)
        self._page = page
        self._warmed = True
        return page

    def close(self):
        if self.persistent:
            return
        if self._page_alive():
            try:
                self._page.close()
            except Exception:
                pass
        self._page = None
        self._warmed = False

    def shutdown(self):
        if self._page_alive():
            try:
                self._page.close()
            except Exception:
                pass
        self._page = None
        self._warmed = False

    def _fetch_json(self, path):
        page = self.warmup(force=False)
        result = page.evaluate(
            """async (path) => {
                const r = await fetch(path, { credentials: 'include' });
                const text = await r.text();
                return { ok: r.ok, status: r.status, text };
            }""",
            path,
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"Aidancing API {path} → HTTP {result.get('status')}: {result.get('text', '')[:200]}"
            )
        return json.loads(result["text"])

    def list_jobs(self, page=0, size=50):
        return self._fetch_json(f"/api/proxy/jobs?page={page}&size={size}")

    def find_job(self, job_id):
        found = self.find_jobs_by_ids([job_id])
        return found.get(int(job_id))

    def find_jobs_by_ids(self, job_ids):
        """Quét tối đa 3 trang API — 1 lần fetch/page cho nhiều job."""
        wanted = {int(j) for j in job_ids if j}
        found = {}
        if not wanted:
            return found
        for p in range(3):
            data = self.list_jobs(page=p, size=50)
            for item in data.get("items", []):
                jid = int(item.get("id", 0))
                if jid in wanted:
                    found[jid] = item
            if len(found) == len(wanted):
                break
        return found

    def create_job(self, model_id, image_path, video_path, quality_mode="2", aspect_ratio="9:16"):
        """Upload qua form create — tab create tạm, tab nền giữ nguyên."""
        bg = self.warmup(force=False)
        page = self.context.new_page()
        submit_timeout = int(os.environ.get("BOT_CREATE_SUBMIT_TIMEOUT_MS", "300000"))
        upload_timeout = int(os.environ.get("BOT_CREATE_UPLOAD_TIMEOUT_MS", "600000"))
        try:
            img_mb = os.path.getsize(image_path) / (1024 * 1024)
            vid_mb = os.path.getsize(video_path) / (1024 * 1024)
            print(f"📎 Nạp form: ảnh {img_mb:.1f}MB, video {vid_mb:.1f}MB")
            create_url = f"{AIDANCING_ORIGIN}/create/general?id={model_id}"
            page.goto(create_url, wait_until="domcontentloaded", timeout=90000)
            page.bring_to_front()
            page.set_input_files('input[name="image"]', image_path)
            page.wait_for_timeout(1500)
            print(f"📤 Gán file video vào form ({video_path})...")
            page.set_input_files('input[name="video"]', video_path)
            page.bring_to_front()
            self._wait_create_uploads(page, vid_mb, upload_timeout)
            page.evaluate(
                """({qualityMode, aspectRatio}) => {
                    const q = document.querySelector('[name=qualityMode]');
                    const a = document.querySelector('[name=aspectRatio]');
                    if (q) q.value = qualityMode;
                    if (a) a.value = aspectRatio;
                }""",
                {"qualityMode": str(quality_mode), "aspectRatio": aspect_ratio},
            )
            before_ids = {j["id"] for j in self.list_jobs(page=0, size=30).get("items", [])}
            page.locator("button.neon-ai-2").first.click()
            print(f"⏳ Đã bấm submit — chờ về dashboard (tối đa {submit_timeout // 1000}s)...")
            page.wait_for_url("**/dashboard**", timeout=submit_timeout)
            page.wait_for_timeout(2000)
            for _ in range(10):
                data = self.list_jobs(page=0, size=30)
                for item in data.get("items", []):
                    if item["id"] not in before_ids:
                        return str(item["id"])
                page.wait_for_timeout(2000)
            raise RuntimeError("Đã submit nhưng không thấy job mới trên API")
        finally:
            try:
                page.close()
            except Exception:
                pass
            if bg and not bg.is_closed():
                self._page = bg

    def _wait_create_uploads(self, page, video_mb, timeout_ms):
        """Aidancing phải upload xong video lên form trước khi bấm submit."""
        per_mb_ms = int(os.environ.get("BOT_CREATE_WAIT_PER_MB_MS", "12000"))
        min_wait = int(os.environ.get("BOT_CREATE_MIN_WAIT_MS", "8000"))
        target_ms = min(timeout_ms, max(min_wait, int(video_mb * per_mb_ms)))
        print(
            f"⏳ Chờ Aidancing upload video ({video_mb:.1f} MB, ~{target_ms // 1000}s) — "
            "đừng đóng tab create..."
        )
        page.bring_to_front()
        deadline = time.time() + (target_ms / 1000)
        last_log = 0
        while time.time() < deadline:
            try:
                ready = page.evaluate(
                    """() => {
                        const spinners = document.querySelectorAll(
                            '[class*="loading"], [class*="uploading"], [class*="spinner"], [class*="progress"]'
                        );
                        for (const el of spinners) {
                            const s = getComputedStyle(el);
                            if (s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null)
                                return { ok: false, why: 'spinner' };
                        }
                        const btn = document.querySelector('button.neon-ai-2');
                        if (btn && btn.disabled) return { ok: false, why: 'btn-disabled' };
                        const vid = document.querySelector('video');
                        if (vid && vid.readyState >= 2 && vid.duration > 0)
                            return { ok: true, why: 'video-preview' };
                        return { ok: false, why: 'waiting' };
                    }"""
                )
                if ready.get("ok"):
                    print(f"✅ Video sẵn sàng submit ({ready.get('why')})")
                    return
            except Exception:
                pass
            now = time.time()
            if now - last_log >= 15:
                left = int(deadline - now)
                print(f"   ... vẫn đang upload (~{left}s còn lại)")
                last_log = now
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            page.wait_for_timeout(2000)
        print(f"⚠️ Hết thời gian chờ upload — thử submit anyway (video {video_mb:.1f} MB)")

    def download_file(self, file_id, dest_path):
        """Tải /api/proxy/files/{id} qua fetch — không reload tab."""
        page = self.warmup(force=False)
        file_id = str(file_id).split("/")[-1]
        result = page.evaluate(
            """async (fileId) => {
                const r = await fetch('/api/proxy/files/' + fileId, { credentials: 'include' });
                if (!r.ok) return { ok: false, status: r.status };
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return { ok: true, b64: btoa(binary), size: bytes.length };
            }""",
            file_id,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Download file {file_id} failed: HTTP {result.get('status')}")
        with open(dest_path, "wb") as f:
            f.write(base64.b64decode(result["b64"]))
        return os.path.abspath(dest_path)
