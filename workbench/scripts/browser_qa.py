"""Optional real-browser checks; requires Playwright and a running local server.

Run: QA_BASE_URL=http://127.0.0.1:8123 python scripts/browser_qa.py
QA data is written only to that server. Use a disposable DATA_DIR.
"""
import base64
import os
import re
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("QA_BASE_URL", "http://127.0.0.1:8123")
EMAIL = f"qa-{uuid.uuid4().hex[:8]}@example.com"
PASSWORD = "QA-only-password-2026"
VAULT_PASSWORD = "QA-vault-password-2026"
SECRET = "仅密码箱可见的私密内容"


def report(message):
    print("PASS", message, flush=True)


def open_new(page, label):
    page.locator("#new-button").click()
    page.locator("#modal").get_by_role("button", name=re.compile("^" + re.escape(label))).click()


def run():
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1024}, locale="zh-CN", timezone_id="Asia/Shanghai", permissions=["microphone"])
        page = context.new_page()
        errors = []
        vault_requests = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: vault_requests.append(r.post_data or "") if r.method == "PUT" and r.url.endswith("/vault") else None)
        page.goto(BASE, wait_until="networkidle")
        page.locator("#auth-toggle").click()
        page.locator("#auth-name").fill("测试用户")
        page.locator("#auth-email").fill(EMAIL)
        page.locator("#auth-password").fill(PASSWORD)
        page.locator("#auth-submit").click()
        expect(page.locator("#app-shell")).to_be_visible()
        expect(page.locator("#view-content")).to_contain_text("添加第一件待办")
        expect(page.locator(".task-row")).to_have_count(0)
        report("注册后的私人空间初始为空")

        open_new(page, "灵感笔记")
        page.get_by_label("标题", exact=True).fill("工作记录测试")
        page.get_by_label("正文", exact=True).fill('<img src=x onerror="window.__xss=1">这段内容只按文字展示。')
        page.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        expect(page.locator("#view-content")).to_contain_text("工作记录测试")
        assert page.evaluate("window.__xss === undefined")
        report("笔记真实保存，用户文本不会执行 HTML")

        open_new(page, "待办事项")
        page.get_by_label("标题", exact=True).fill("完成一件待办")
        page.get_by_label("补充说明", exact=True).fill("浏览器端到端验证")
        page.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        page.get_by_role("button", name="完成「完成一件待办」", exact=True).click()
        expect(page.get_by_role("button", name="将「完成一件待办」标为未完成", exact=True)).to_be_visible()
        report("任务创建并标记完成")

        page.locator('[data-area="life"]').click()
        expect(page.locator("#view-content")).not_to_contain_text("工作记录测试")
        open_new(page, "灵感笔记")
        page.get_by_label("标题", exact=True).fill("生活记录测试")
        page.get_by_label("正文", exact=True).fill("工作与生活分别整理")
        page.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        page.locator('[data-area="work"]').click()
        expect(page.locator("#view-content")).not_to_contain_text("生活记录测试")
        page.reload(wait_until="networkidle")
        expect(page.locator("#view-content")).to_contain_text("工作记录测试")
        report("工作生活分区及刷新后持久保存")

        page.locator('[data-view="media"]').click()
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=")
        page.locator("#file-input").set_input_files({"name": "测试图片.png", "mimeType": "image/png", "buffer": image})
        expect(page.locator(".media-tile").filter(has_text="测试图片.png")).to_be_visible()
        page.locator(".media-tile").filter(has_text="测试图片.png").click()
        expect(page.locator("#modal img")).to_be_visible()
        page.wait_for_function("document.querySelector('#modal img')?.naturalWidth > 0")
        page.locator("#modal-close").click()
        report("媒体上传及图片实际解码展示")

        page.locator('[data-view="vault"]').click()
        page.get_by_label("设置独立密码", exact=True).fill(VAULT_PASSWORD)
        page.get_by_label("确认密码", exact=True).fill(VAULT_PASSWORD)
        page.get_by_role("button", name="创建密码箱", exact=True).click()
        page.get_by_role("button", name="添加私密记录", exact=True).first.click()
        page.get_by_label("名称", exact=True).fill("私密测试")
        page.get_by_label("私密内容", exact=True).fill(SECRET)
        page.get_by_role("button", name="加密保存", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        expect(page.locator("#view-content")).to_contain_text("私密测试")
        assert vault_requests and all(SECRET not in body and VAULT_PASSWORD not in body for body in vault_requests)
        page.get_by_role("button", name="立即锁定", exact=True).click()
        expect(page.locator("#view-content")).not_to_contain_text("私密测试")
        page.get_by_label("密码箱密码", exact=True).fill("incorrect-password")
        page.get_by_role("button", name="解锁密码箱", exact=True).click()
        expect(page.locator(".vault-form .form-error")).not_to_be_empty()
        page.get_by_label("密码箱密码", exact=True).fill(VAULT_PASSWORD)
        page.get_by_role("button", name="解锁密码箱", exact=True).click()
        expect(page.locator("#view-content")).to_contain_text("私密测试")
        report("密码箱加密保存、错误密码拒绝、锁定后清除和正确解锁")

        # Headless Chromium does not reliably hide background tabs. Verify the
        # actual reload boundary here; the visibility handler is reviewed apart.
        page.reload(wait_until="networkidle")
        page.locator('[data-view="vault"]').click()
        expect(page.get_by_role("button", name="解锁密码箱", exact=True)).to_be_visible()
        report("页面刷新后密码箱保持锁定")

        page.locator('[data-view="media"]').click()
        open_new(page, "语音录入")
        page.get_by_role("button", name="开始录音", exact=True).click()
        expect(page.get_by_role("button", name="停止录音", exact=True)).to_be_enabled()
        page.wait_for_timeout(1200)
        page.get_by_role("button", name="停止录音", exact=True).click()
        expect(page.get_by_role("button", name="保存录音", exact=True)).to_be_enabled()
        page.get_by_role("button", name="保存录音", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        expect(page.locator(".media-tile").filter(has_text="语音记录-")).to_be_visible()
        page.locator(".media-tile").filter(has_text="语音记录-").click()
        expect(page.locator("#modal audio")).to_be_visible()
        page.wait_for_function("document.querySelector('#modal audio')?.readyState >= 1")
        page.locator("#modal-close").click()
        report("真实 MediaRecorder 录制、上传及音频预览")

        # Test cleanup at account boundary without creating additional accounts.
        page.locator("#profile-button").click()
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page.locator("#auth-screen")).to_be_visible()
        assert page.locator("#view-content").inner_text() == ""
        assert page.locator("#modal-body").inner_text() == ""
        report("退出清除旧账号页面与私密内容")
        assert not errors, errors
        browser.close()
        print("Browser checks completed without script errors.", flush=True)


if __name__ == "__main__":
    run()
