"""Daily image log browser checks against a temporary local server.

Covers the no-key degradation path: capture, two-stage private upload, timeline
rendering, manual summary and the mobile layout. No provider is configured, so
the AI surfaces must explicitly show the disconnected state and never fake it.
"""
import io
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer


def register(page):
    page.locator("#auth-toggle").click()
    page.locator("#auth-name").fill("今日记录测试")
    email = "daily-" + uuid.uuid4().hex[:10] + "@example.com"
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill("Daily-QA-only-2026")
    page.locator("#auth-submit").click()
    return email


def make_png(path, color):
    Image.new("RGB", (640, 480), color).save(path, format="PNG")


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


with tempfile.TemporaryDirectory(prefix="zhixu-daily-qa-") as data:
    upload_dir = Path(data) / "uploads"
    upload_dir.mkdir()
    first = upload_dir / "whiteboard.png"
    second = upload_dir / "notes.png"
    make_png(first, (110, 150, 120))
    make_png(second, (190, 150, 110))

    server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=ROOT / "public",
                         registration_mode="open", storage_persistence="persistent")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                          locale="zh-CN", timezone_id="Asia/Shanghai")
            context.add_init_script("delete URLSearchParams.prototype.size;")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")
            register(page)
            expect(page.locator("#view-content")).to_be_visible()

            # The module library lists the daily log as an available module.
            page.locator('[data-view="modules"]').click()
            expect(page.locator(".module-card", has_text="今日记录")).to_contain_text("已可使用")

            page.locator('[data-view="daily"]').click()
            expect(page.locator(".daily-composer")).to_be_visible()
            expect(page.locator(".daily-timeline .empty-state")).to_be_visible()
            # No DASHSCOPE_API_KEY: the AI capability is honestly disconnected.
            expect(page.locator(".daily-composer .capture-hint")).to_contain_text("待连接")

            # Staged private uploads render thumbnails and become ready.
            page.set_input_files("#daily-file-input", [str(first), str(second)])
            expect(page.locator(".daily-upload.ready")).to_have_count(2)
            expect(page.locator(".daily-counter")).to_have_text("2/9 张")

            page.locator(".daily-note").fill("周会白板：确认上线节奏")
            page.locator(".daily-composer .button.primary").click()
            expect(page.locator(".daily-entry")).to_have_count(1)
            entry = page.locator(".daily-entry").first
            expect(entry).to_contain_text("周会白板：确认上线节奏")
            expect(entry.locator(".daily-image-grid .daily-image-tile")).to_have_count(2)
            # Vision is disconnected, so the card says so instead of inventing a result.
            expect(entry.locator(".daily-ai.disabled")).to_contain_text("待连接")
            expect(page.locator("#nav-daily-count")).to_have_text("1")

            # The lightbox opens the original private image.
            entry.locator(".daily-image-tile").first.click()
            expect(page.locator("#modal .daily-lightbox img")).to_be_visible()
            expect(page.locator(".daily-lightbox-count")).to_have_text("1 / 2")
            page.locator("#modal-close").click()

            # Manual editing keeps working without a model.
            entry.get_by_role("button", name="编辑").click()
            page.locator("#modal textarea").first.fill("手动整理的摘要内容")
            page.locator("#modal .button.primary").click()
            expect(entry.locator(".daily-summary")).to_contain_text("手动整理的摘要内容")

            # The overview surfaces today's capture and its entry point.
            page.locator('[data-view="overview"]').click()
            expect(page.locator(".overview-daily")).to_be_visible()
            expect(page.locator(".overview-daily")).to_contain_text("今天已记录 1 条")

            # Narrow phones must not overflow, including the composer and grid.
            for width in (320, 390):
                page.set_viewport_size({"width": width, "height": 844})
                no_overflow(page)
            page.set_viewport_size({"width": 390, "height": 844})
            # The sidebar becomes a drawer on phones; open it before navigating.
            page.locator("#mobile-more").click()
            expect(page.locator("#sidebar.open")).to_be_visible()
            page.locator('[data-view="daily"]').click()
            no_overflow(page)
            expect(page.locator(".daily-composer")).to_be_visible()
            (ROOT / "artifacts").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "artifacts/workbench-daily-mobile.png"), full_page=True)

            assert not errors, "Page errors: " + " | ".join(errors)
            print("PASS daily capture, staged upload, no-key degradation, editing and mobile layout", flush=True)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
