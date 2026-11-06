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
            # P2-3: arrow keys cycle through the images and Escape closes the modal.
            page.keyboard.press("ArrowRight")
            expect(page.locator(".daily-lightbox-count")).to_have_text("2 / 2")
            page.keyboard.press("ArrowRight")
            expect(page.locator(".daily-lightbox-count")).to_have_text("1 / 2")
            page.keyboard.press("ArrowLeft")
            expect(page.locator(".daily-lightbox-count")).to_have_text("2 / 2")
            page.keyboard.press("Escape")
            expect(page.locator("#modal")).not_to_be_visible()
            print("PASS lightbox arrow-key cycling and Escape close", flush=True)

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
            page.set_viewport_size({"width": 1440, "height": 1000})

            # P1-F1: a converted suggestion must create an "active" task, matching
            # the front-end state machine and editor (active/done only, never "todo").
            page.evaluate("""() => {
              state.daily.entries.push({
                id: 'qa-suggestion-entry', spaceId: state.space.id, area: state.area,
                entryDate: dailyDateString(), title: 'QA 建议来源记录', summary: '', body: '',
                status: 'active',
                meta: {ai: {status: 'done', suggestions: ['QA 建议转待办']},
                       convertedSuggestions: [], source: 'daily'},
                createdBy: state.user.id,
                createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
                images: [],
              });
              render();
            }""")
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/items")
            ) as item_response:
                page.locator(".daily-suggestion", has_text="QA 建议转待办").click()
            payload = item_response.value.request.post_data_json
            assert payload["status"] == "active", payload
            assert payload["meta"]["source"] == "daily-suggestion"
            sid = page.evaluate("state.space.id")
            tasks = context.request.get(base + f"/api/spaces/{sid}/items").json()["items"]
            created = [task for task in tasks if task["title"] == "QA 建议转待办"]
            assert len(created) == 1 and created[0]["status"] == "active", created
            # 转换流程不本地插入任务，重新拉取后任务视图应展示这条 active 待办
            page.evaluate("loadItems()")
            page.locator('[data-view="tasks"]').click()
            expect(page.locator("#view-content")).to_contain_text("QA 建议转待办")
            print("PASS suggestion conversion posts and persists an active task", flush=True)

            # P2-2: a failed upload tile blocks submit and exposes retry/remove;
            # reusing the original File object recovers to ready and re-enables submit.
            page.locator('[data-view="daily"]').click()
            retry_image = upload_dir / "retry.png"
            remove_image = upload_dir / "remove.png"
            make_png(retry_image, (90, 120, 160))
            make_png(remove_image, (160, 110, 90))
            failed_uploads = {"count": 0}

            def fail_first_upload(route):
                if failed_uploads["count"] == 0:
                    failed_uploads["count"] += 1
                    route.fulfill(status=503, content_type="application/json",
                                  body='{"error":"QA 模拟上传暂时不可用"}')
                else:
                    route.continue_()

            page.route("**/daily/uploads", fail_first_upload)
            page.set_input_files("#daily-file-input", [str(retry_image)])
            error_tile = page.locator(".daily-upload.error")
            expect(error_tile).to_have_count(1)
            expect(error_tile.locator(".daily-upload-status")).to_contain_text("QA 模拟上传暂时不可用")
            expect(error_tile.get_by_role("button", name="重试", exact=True)).to_be_visible()
            expect(error_tile.get_by_role("button", name="移除", exact=True)).to_be_visible()
            submit_button = page.locator(".daily-composer .button.primary")
            expect(submit_button).to_be_disabled()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/daily/uploads")
            ) as retry_response:
                error_tile.get_by_role("button", name="重试", exact=True).click()
            assert retry_response.value.ok, retry_response.value.status
            expect(page.locator(".daily-upload.ready")).to_have_count(1)
            expect(submit_button).to_be_enabled()
            print("PASS failed upload tile offers retry/remove and retry restores ready state", flush=True)

            # Removing another failed tile unblocks the composer as well.
            failed_uploads["count"] = 0
            page.set_input_files("#daily-file-input", [str(remove_image)])
            second_error = page.locator(".daily-upload.error")
            expect(second_error).to_have_count(1)
            expect(submit_button).to_be_disabled()
            second_error.get_by_role("button", name="移除", exact=True).click()
            expect(page.locator(".daily-upload.error")).to_have_count(0)
            expect(submit_button).to_be_enabled()
            page.unroute("**/daily/uploads", fail_first_upload)
            print("PASS removing a failed upload tile clears the blocked submit state", flush=True)

            assert not errors, "Page errors: " + " | ".join(errors)
            print("PASS daily capture, staged upload, no-key degradation, editing and mobile layout", flush=True)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
