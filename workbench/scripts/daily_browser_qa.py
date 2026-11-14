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

            # 侧边栏不再有「今日记录」菜单，改为通过视图切换进入
            page.evaluate("navigate('daily')")
            expect(page.locator(".daily-composer")).to_be_visible()
            expect(page.locator(".daily-timeline .empty-state")).to_be_visible()
            # No DASHSCOPE_API_KEY: the AI capability is honestly disconnected.
            expect(page.locator(".daily-composer .capture-hint")).to_contain_text("待连接")

            # Staged private uploads render thumbnails and become ready.
            page.set_input_files("#daily-file-input", [str(first), str(second)])
            expect(page.locator(".daily-upload.ready")).to_have_count(2)
            expect(page.locator(".daily-counter")).to_have_text("2/9 张")

            # Manual tags are edited in the quick composer and sent with creation.
            tag_input = page.locator(".daily-composer .tag-editor input[aria-label='添加标签']")
            tag_input.fill("学习")
            tag_input.press("Enter")
            expect(page.locator(".daily-composer .tag-chip-name")).to_have_text("学习")
            page.locator(".daily-note").fill("周会白板：确认上线节奏")
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/daily/entries")
            ) as create_response:
                page.locator(".daily-composer .button.primary").click()
            create_payload = create_response.value.request.post_data_json
            assert create_payload["tags"] == ["学习"], create_payload
            expect(page.locator(".daily-entry")).to_have_count(1)
            entry = page.locator(".daily-entry").first
            expect(entry).to_contain_text("周会白板：确认上线节奏")
            expect(entry.locator(".entry-tags .tag", has_text="学习")).to_have_count(1)
            expect(entry.locator(".daily-image-grid .daily-image-tile")).to_have_count(2)
            # Vision is disconnected, so the card says so instead of inventing a result.
            expect(entry.locator(".daily-ai.disabled")).to_contain_text("待连接")
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

            # Seed a same-day task with another tag as a filtering control.
            control_title = "QA 工作对照待办"
            control_item = page.evaluate("""async (title) => {
              const item = await saveItem({
                kind: 'task', area: state.area, title, body: '', status: 'active',
                meta: {due: dailyDateString(), priority: 'normal', tags: ['工作']},
              });
              await loadSpaceTags();
              return item;
            }""", control_title)
            assert control_item["meta"]["due"] == page.evaluate("dailyDateString()")
            assert control_item["meta"]["tags"] == ["工作"], control_item

            # Today's capture appears only inside this month's schedule.
            page.evaluate("navigate('overview')")
            page.wait_for_timeout(1200)
            expect(page.locator(".month-agenda-item")).not_to_have_count(0)
            expect(page.locator(".today-items, .overview-daily")).to_have_count(0)
            today_agenda = page.locator(".month-agenda-day", has_text="今天")
            expect(today_agenda).to_have_count(1)
            expect(today_agenda.locator(".month-agenda-daily")).to_have_count(1)
            expect(today_agenda.locator(".month-agenda-daily")).to_contain_text("周会白板：确认上线节奏")
            before_url = page.url
            today_agenda.locator(".month-agenda-daily").click()
            expect(page.locator("#modal")).to_be_visible()
            expect(page.locator("#modal-title")).to_have_text("今天的记录")
            expect(page.locator("#modal .daily-detail-body")).to_have_text("周会白板：确认上线节奏")
            expect(page.locator("#modal .daily-detail-summary")).to_have_text("手动整理的摘要内容")
            expect(page.locator("#modal .daily-image-tile")).to_have_count(2)
            assert page.url == before_url
            for width, height in ((320, 844), (390, 844), (844, 390)):
                page.set_viewport_size({"width": width, "height": height})
                no_overflow(page)
                bounds = page.locator("#modal").bounding_box()
                assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
                expect(page.locator("#modal-close")).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(ROOT / "artifacts/workbench-agenda-dialog-mobile.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.keyboard.press("Escape")
            expect(page.locator("#modal")).not_to_be_visible()
            expect(page.locator('[data-view="overview"].active')).to_have_count(1)
            (ROOT / "artifacts").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "artifacts/workbench-month-agenda.png"), full_page=True)
            page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
            page.evaluate("navigate('calendar')")
            page.get_by_role("button", name="日程", exact=True).click()
            agenda_record = page.locator(".agenda-group", has_text="今天").locator(".agenda-daily")
            expect(agenda_record).to_contain_text("周会白板：确认上线节奏")
            agenda_record.click()
            expect(page.locator("#modal .daily-detail-body")).to_have_text("周会白板：确认上线节奏")
            assert page.url == before_url
            page.locator("#modal-close").click()
            agenda_record.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#modal .daily-detail-body")).to_have_text("周会白板：确认上线节奏")
            page.keyboard.press("Escape")

            # Month cells expose the same record's first image, count and manual tag.
            mode_switch = page.locator(".calendar-mode-switch")
            mode_switch.get_by_role("button", name="日历", exact=True).click()
            today_cell = page.locator(".calendar-cell.today")
            expect(today_cell).to_have_count(1)
            calendar_record = today_cell.locator(
                ".calendar-entry.agenda-daily", has_text="周会白板：确认上线节奏")
            calendar_control = today_cell.locator(".calendar-entry", has_text=control_title)
            expect(calendar_record).to_have_count(1)
            expect(calendar_record.locator(".entry-thumb.compact")).to_be_visible()
            expect(calendar_record.locator(".entry-thumb.compact img")).to_be_visible()
            expect(calendar_record.locator(".entry-thumb-count")).to_have_text("2 张")
            expect(calendar_record.locator(".entry-tags .tag", has_text="学习")).to_have_count(1)
            expect(calendar_control).to_have_count(1)
            expect(calendar_control.locator(".entry-tags .tag", has_text="工作")).to_have_count(1)

            # Pick today so filtering can also prove month/selection stability.
            today_cell.locator(".calendar-day").click()
            expect(page.locator(".calendar-cell.today.picked")).to_have_count(1)
            month_before = page.evaluate("state.calendar.month")
            selected_before = page.evaluate("state.calendar.filter")
            assert selected_before == page.evaluate("dailyDateString()")
            study_filter = page.locator(".tag-filter-bar .tag-filter-chip", has_text="学习")
            expect(study_filter).to_have_count(1)
            study_filter.click()
            expect(study_filter).to_have_attribute("aria-pressed", "true")
            expect(calendar_record).to_have_count(1)
            expect(calendar_control).to_have_count(0)
            expect(today_cell.locator(".calendar-entry")).to_have_count(1)

            # Agenda uses the same filter; its thumbnail opens the common viewer.
            mode_switch.get_by_role("button", name="日程", exact=True).click()
            filtered_agenda = page.locator(".agenda-group", has_text="今天")
            filtered_record = filtered_agenda.locator(
                ".agenda-item.agenda-daily", has_text="周会白板：确认上线节奏")
            expect(filtered_record).to_have_count(1)
            expect(filtered_agenda.locator(".agenda-item", has_text=control_title)).to_have_count(0)
            expect(filtered_agenda.locator(".agenda-item")).to_have_count(1)
            filtered_record.locator(".entry-thumb").click()
            expect(page.locator("#modal .daily-lightbox img")).to_be_visible()
            expect(page.locator("#modal .daily-lightbox-count")).to_have_text("1 / 2")
            page.keyboard.press("ArrowRight")
            expect(page.locator("#modal .daily-lightbox-count")).to_have_text("2 / 2")
            page.keyboard.press("Escape")
            expect(page.locator("#modal")).not_to_be_visible()

            # Clearing the tag restores both views without changing month/date.
            mode_switch.get_by_role("button", name="日历", exact=True).click()
            expect(calendar_control).to_have_count(0)
            study_filter.click()
            expect(study_filter).to_have_attribute("aria-pressed", "false")
            expect(calendar_record).to_have_count(1)
            expect(calendar_control).to_have_count(1)
            expect(today_cell.locator(".calendar-entry")).to_have_count(2)
            assert page.evaluate("state.calendar.month") == month_before
            assert page.evaluate("state.calendar.filter") == selected_before
            for width, height in ((375, 844), (1920, 1080)):
                page.set_viewport_size({"width": width, "height": height})
                expect(page.locator(".calendar-grid")).to_be_visible()
                expect(calendar_record.locator(".calendar-entry-title")).to_be_visible()
                expect(calendar_record.locator(
                    ".entry-tags .tag", has_text="学习")).to_be_visible()
                thumbnail_box = calendar_record.locator(".entry-thumb.compact").bounding_box()
                assert thumbnail_box is not None
                assert abs(thumbnail_box["width"] - 40) <= 0.5, (width, thumbnail_box)
                assert abs(thumbnail_box["height"] - 40) <= 0.5, (width, thumbnail_box)
                no_overflow(page)
            page.set_viewport_size({"width": 1440, "height": 1000})
            mode_switch.get_by_role("button", name="日程", exact=True).click()
            restored_agenda = page.locator(".agenda-group", has_text="今天")
            expect(restored_agenda.locator(
                ".agenda-item.agenda-daily", has_text="周会白板：确认上线节奏")).to_have_count(1)
            expect(restored_agenda.locator(".agenda-item", has_text=control_title)).to_have_count(1)
            assert page.evaluate("state.calendar.month") == month_before
            assert page.evaluate("state.calendar.filter") == selected_before
            print("PASS calendar tags, filtering, common lightbox and responsive overflow", flush=True)

            # Deleting the active unique tag must clear filtering and preserve the selected date.
            page.locator(".calendar-mode-btn", has_text="日历").click()
            temporary_entry = page.evaluate("""async () => {
              const result = await api(
                `/api/spaces/${state.space.id}/daily/entries`, 'POST',
                {body: 'QA 待删除标签记录', imageIds: [], area: state.area,
                 tags: ['临时标签']},
              );
              upsertDailyEntry(result.entry);
              state.tagsLoadedSpace = null;
              await loadSpaceTags();
              render();
              return result.entry;
            }""")
            temporary_tag_filter = page.locator(
                ".tag-filter-bar .tag-filter-chip", has_text="临时标签")
            expect(temporary_tag_filter).to_have_count(1)
            temporary_tag_filter.click()
            expect(calendar_record).to_have_count(0)
            page.evaluate("(entry) => deleteDailyEntry(entry)", temporary_entry)
            page.locator("#modal").get_by_role(
                "button", name="确认删除", exact=True).click()
            expect(page.locator("#modal")).not_to_be_visible()
            assert not page.evaluate(
                "(id) => state.daily.entries.some((entry) => entry.id === id)",
                arg=temporary_entry["id"],
            )
            expect(temporary_tag_filter).to_have_count(0)
            expect(calendar_record).to_have_count(1)
            assert page.evaluate("state.tagFilter") is None
            assert page.evaluate("state.calendar.filter") == selected_before
            print("PASS deleting the active unique tag clears filtering and its chip", flush=True)

            page.locator('[data-view="overview"]').click()
            print("PASS today records merge into month agenda and open as non-navigating dialogs", flush=True)

            # Narrow phones must not overflow, including the composer and grid.
            for width in (320, 390):
                page.set_viewport_size({"width": width, "height": 844})
                no_overflow(page)
            page.set_viewport_size({"width": 390, "height": 844})
            # The sidebar becomes a drawer on phones; open it before navigating.
            page.evaluate("navigate('daily')")
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
            page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
            page.evaluate("navigate('tasks')")
            expect(page.locator("#view-content")).to_contain_text("QA 建议转待办")
            print("PASS suggestion conversion posts and persists an active task", flush=True)

            # P2-2: a failed upload tile blocks submit and exposes retry/remove;
            # reusing the original File object recovers to ready and re-enables submit.
            page.evaluate("navigate('daily')")
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

            # Demo entries keep using their local object URLs after saving. They
            # must remain usable in both the daily grid and the calendar lightbox.
            demo_context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN", timezone_id="Asia/Shanghai",
            )
            demo_page = demo_context.new_page()
            demo_errors = []
            demo_page.on("pageerror", lambda error: demo_errors.append(str(error)))
            demo_page.goto(base, wait_until="networkidle")
            demo_page.locator("#demo-button").click()
            expect(demo_page.locator("#demo-banner")).to_be_visible()
            demo_page.get_by_role("button", name="今日记录", exact=True).click()
            demo_page.set_input_files("#daily-file-input", [str(first), str(second)])
            expect(demo_page.locator(".daily-upload.ready")).to_have_count(2)
            demo_page.locator(".daily-note").fill("演示双图保存后仍可查看")
            demo_page.locator(".daily-composer .button.primary").click()
            demo_entry = demo_page.locator(".daily-entry", has_text="演示双图保存后仍可查看")
            demo_images = demo_entry.locator(".daily-image-grid img")
            expect(demo_images).to_have_count(2)
            expect(demo_images.nth(0)).to_have_js_property("naturalWidth", 640)
            expect(demo_images.nth(1)).to_have_js_property("naturalWidth", 640)

            demo_page.locator('[data-view="overview"]').click()
            demo_page.get_by_role("button", name="日历日程", exact=True).click()
            demo_record = demo_page.locator(
                ".calendar-cell.today .calendar-entry.agenda-daily",
                has_text="演示双图保存后仍可查看",
            )
            demo_thumb = demo_record.locator(".entry-thumb.compact")
            expect(demo_thumb).to_be_visible()
            expect(demo_record.locator(".entry-thumb-count")).to_have_text("2 张")
            expect(demo_thumb.locator("img")).to_be_visible()
            expect(demo_thumb.locator("img")).to_have_js_property("naturalWidth", 640)
            demo_thumb.click()
            expect(demo_page.locator("#modal .daily-lightbox img")).to_be_visible()
            expect(demo_page.locator(".daily-lightbox-count")).to_have_text("1 / 2")
            demo_page.keyboard.press("ArrowRight")
            expect(demo_page.locator(".daily-lightbox-count")).to_have_text("2 / 2")
            expect(demo_page.locator("#modal .daily-lightbox img")).to_have_js_property("naturalWidth", 640)
            assert not demo_errors, "Demo page errors: " + " | ".join(demo_errors)
            print("PASS demo saved images remain visible in daily, calendar and lightbox", flush=True)
            demo_context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
