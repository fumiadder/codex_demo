"""Real mobile workbench checks against a disposable running server.

QA_BASE_URL=http://127.0.0.1:8135 python scripts/mobile_qa.py
Uses a new test account; never run against a production data directory.
"""
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("QA_BASE_URL")
sys.path.insert(0, str(ROOT))
from server import FolioServer


def report(message):
    print("PASS", message, flush=True)


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")


def login_form(page, email):
    if page.locator("#name-label").is_visible():
        page.locator("#auth-toggle").click()
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill("QA-only-mobile-2026")
    page.locator("#auth-submit").click()


def account_boundaries(page, context, base, email_a):
    user_a = context.request.get(base + "/api/me").json()["user"]
    page.locator('[data-view="vault"]').click()
    page.get_by_label("设置独立密码", exact=True).fill("QA-vault-mobile-2026")
    page.get_by_label("确认密码", exact=True).fill("QA-vault-mobile-2026")
    page.get_by_role("button", name="创建密码箱", exact=True).click()
    page.get_by_role("button", name="添加私密记录", exact=True).first.click()
    page.get_by_label("名称", exact=True).fill("A 私密浏览器测试")
    page.get_by_label("私密内容", exact=True).fill("QA_A_VAULT_PRIVATE_TEXT")
    page.get_by_role("button", name="加密保存", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    page.wait_for_function("() => state.vault.key !== null && state.vault.entries.length === 1")
    page.evaluate("""() => {
      window.__qaRevoked = [];
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = url => { window.__qaRevoked.push(url); revoke(url); };
      window.__qaOldURL = URL.createObjectURL(new Blob(['private preview'], {type:'text/plain'}));
      state.objectURLs.add(window.__qaOldURL);
    }""")
    other = context.new_page()
    other.set_viewport_size({"width":1440,"height":1000})
    other.goto(base, wait_until="networkidle")
    expect(other.locator("#view-content")).to_contain_text("手机上留下的一句话")
    other.locator("#profile-button").click()
    other.get_by_role("button", name="退出登录", exact=True).click()
    expect(page.locator("#auth-screen")).to_be_visible()
    assert page.locator("#view-content").inner_text() == ""
    assert page.locator("#modal-body").inner_text() == ""
    assert page.evaluate("state.vault.key === null && state.vault.entries.length === 0 && state.objectURLs.size === 0")
    assert page.evaluate("window.__qaRevoked.includes(window.__qaOldURL)")
    report("另一标签真实退出立即清旧账号正文、解锁密钥、密码箱条目与媒体 URL")

    email_b = f"mobile-b-{uuid.uuid4().hex[:10]}@example.com"
    other.locator("#auth-toggle").click()
    other.locator("#auth-name").fill("账号 B")
    other.locator("#auth-email").fill(email_b)
    other.locator("#auth-password").fill("QA-only-mobile-2026")
    other.locator("#auth-submit").click()
    expect(other.locator("#app-shell")).to_be_visible()
    expect(other.locator("#view-content")).to_contain_text("添加第一件待办")
    other.locator("#new-button").click()
    other.locator("#modal").get_by_role("button", name="灵感笔记", exact=False).click()
    other.get_by_label("标题", exact=True).fill("B_PRIVATE_NOTE")
    other.get_by_label("正文", exact=True).fill("仅 B 的空间内容")
    other.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
    expect(other.locator("#modal")).not_to_be_visible()
    expect(other.locator("#view-content")).to_contain_text("B_PRIVATE_NOTE")
    assert page.locator("#view-content").inner_text() == ""
    space_b = context.request.get(base + "/api/spaces").json()["spaces"][0]["id"]
    response = context.request.get(base + "/api/spaces/" + space_b + "/items", headers={"X-Expected-User":user_a["id"]})
    assert response.status == 401
    report("真实登录 B 后旧 A 页面仍为空，A 身份请求不能读取 B 空间")

    held = []
    def hold_a_items(route):
        if route.request.headers.get("x-expected-user") == user_a["id"]:
            held.append(route)
        else:
            route.continue_()
    page.route("**/api/spaces/*/items", hold_a_items)
    login_form(page, email_a)
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert held, "A 的待完成请求未在 10 秒内发出"
    expect(other.locator("#auth-screen")).to_be_visible()
    login_form(other, email_b)
    expect(other.locator("#app-shell")).to_be_visible()
    expect(other.locator("#view-content")).to_contain_text("B_PRIVATE_NOTE")
    expect(page.locator("#auth-screen")).to_be_visible()
    login_form(page, email_b)
    expect(page.locator("#view-content")).to_contain_text("B_PRIVATE_NOTE")
    held.pop().fulfill(status=401, content_type="application/json", body='{"error":"QA delayed old A response"}')
    page.wait_for_timeout(150)
    expect(page.locator("#app-shell")).to_be_visible()
    expect(page.locator("#view-content")).to_contain_text("B_PRIVATE_NOTE")
    assert page.evaluate("state.user.id") != user_a["id"]
    page.unroute("**/api/spaces/*/items", hold_a_items)
    other.close()
    report("账号切换后，旧 A 请求延迟返回 401 不会清除新 B 会话（仅回包时序 stub）")


def checks(base):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 320, "height": 720}, is_mobile=True, has_touch=True, locale="zh-CN", timezone_id="Asia/Shanghai")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        expect(page.locator("#auth-screen")).to_be_visible()
        no_overflow(page)
        page.locator("#auth-toggle").click()
        page.locator("#auth-name").fill("手机测试")
        email_a = f"mobile-{uuid.uuid4().hex[:10]}@example.com"
        page.locator("#auth-email").fill(email_a)
        page.locator("#auth-password").fill("QA-only-mobile-2026")
        assert float(page.locator("#auth-email").evaluate("el => parseFloat(getComputedStyle(el).fontSize)")) >= 16
        slow_spaces_checked = []
        def pause_initial_spaces(route):
            # Keep the real request paused while observing the intermediate
            # user-visible state, then release it to the actual HTTP server.
            expect(page.locator("#app-shell")).to_be_visible()
            expect(page.locator("#new-button")).to_be_disabled()
            expect(page.locator("#mobile-new")).to_be_disabled()
            slow_spaces_checked.append(True)
            route.continue_()
        page.route("**/api/spaces", pause_initial_spaces)
        page.locator("#auth-submit").click()
        expect(page.locator("#app-shell")).to_be_visible()
        expect(page.locator("#view-content")).to_contain_text("添加第一件待办")
        expect(page.locator("#new-button")).to_be_enabled()
        expect(page.locator("#mobile-new")).to_be_enabled()
        assert slow_spaces_checked == [True]
        page.unroute("**/api/spaces", pause_initial_spaces)
        report("空间列表真实请求暂缓时两个新建按钮禁用，加载完成后才可新建")
        expect(page.locator("#mobile-navigation")).to_be_visible()
        no_overflow(page)
        report("320px 窄屏真实注册，输入字体与底部导航可用")

        page.locator("#mobile-more").click()
        expect(page.locator("#sidebar")).to_have_attribute("aria-modal", "true")
        assert page.locator(".workspace-body").evaluate("el => el.inert")
        page.keyboard.press("Shift+Tab")
        assert page.locator("#sidebar").evaluate("el => el.contains(document.activeElement)")
        page.keyboard.press("Escape")
        expect(page.locator("#mobile-more")).to_be_focused()
        assert not page.locator(".workspace-body").evaluate("el => el.inert")
        report("导航抽屉焦点约束、遮罩、Escape 与焦点恢复")

        page.locator('[data-mobile-view="notes"]').click()
        page.locator("#mobile-new").click()
        page.locator("#modal").get_by_role("button", name="灵感笔记", exact=False).click()
        page.get_by_label("标题", exact=True).fill("手机上留下的一句话")
        page.get_by_label("正文", exact=True).fill("从窄屏也能把日常记录保存下来。")
        no_overflow(page)
        assert page.locator("#modal").bounding_box()["width"] <= 320
        assert float(page.get_by_label("标题", exact=True).evaluate("el => parseFloat(getComputedStyle(el).fontSize)")) >= 16
        page.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
        expect(page.locator("#view-content")).to_contain_text("手机上留下的一句话")
        report("手机弹窗录入并真实保存笔记")

        for width, height in [(320, 720), (360, 780), (390, 844), (430, 932), (844, 390)]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(280)
            no_overflow(page)
            expect(page.locator("#mobile-navigation")).to_be_visible()
            page.locator("#mobile-more").click()
            page.wait_for_timeout(250)
            rect = page.locator("#sidebar").bounding_box()
            assert rect["x"] >= -1 and rect["x"] + rect["width"] <= width + 1
            page.locator("#drawer-close").click()
        report("320/360/390/430px 竖屏与 844×390 横屏无横向溢出")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("""() => {
          const drawer = document.querySelector('#sidebar');
          const transform = getComputedStyle(drawer).transform;
          const x = transform === 'none' ? 0 : new DOMMatrix(transform).m41;
          return !drawer.classList.contains('open') && drawer.inert &&
            x <= -drawer.getBoundingClientRect().width + 1;
        }""")
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(100)
        (ROOT / "artifacts").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "artifacts" / "mobile-workbench.png"), full_page=False, animations="disabled")
        page.set_viewport_size({"width": 1440, "height": 1000})
        expect(page.locator("#mobile-navigation")).not_to_be_visible()
        page.wait_for_function(
            "() => !document.querySelector('#sidebar').inert",
            timeout=5000,
        )
        no_overflow(page)
        account_boundaries(page, context, base, email_a)
        page.locator('[data-area="life"]').click()
        expect(page.locator("#life-navigation")).to_be_visible()
        page.locator('[data-view="bedtime"]').click()
        expect(page).to_have_url(re.compile(r"/bedtime\.html\?space="))
        expect(page.locator("#bedtime-app")).to_be_visible()
        expect(page.locator(".story-card").first).to_be_visible()
        report("桌面导航保留，晚安故事从同一账号空间打开")
        assert not errors, errors
        browser.close()


def run():
    if BASE:
        checks(BASE)
        return
    with tempfile.TemporaryDirectory(prefix="zhixu-mobile-qa-") as data:
        server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=ROOT / "public", registration_mode="open", storage_persistence="persistent")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            checks(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    run()
