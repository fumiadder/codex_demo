"""Real-browser collaboration and recording retry checks with disposable data.

Run: python scripts/collaboration_qa.py
Requires Playwright and Chromium (QA_CHROMIUM defaults to /usr/bin/chromium).
The script starts and stops its own server on a random localhost port. It does
not use a deployed server or save accounts, recordings, or database files.
"""
import os
import re
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer

PASSWORD = "QA-collaboration-password-2026"
SHARED_NAME = "浏览器协作验证空间"
OWNER_NOTE = "所有者共享给成员的记录"
EDITOR_NOTE = "编辑者新增的协作记录"


def report(message):
    print("PASS", message, flush=True)


def register(page, base, name, email):
    page.goto(base, wait_until="networkidle")
    page.locator("#auth-toggle").click()
    page.locator("#auth-name").fill(name)
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill(PASSWORD)
    page.locator("#auth-submit").click()
    expect(page.locator("#view-content")).to_contain_text("添加第一件待办")


def new_note(page, title, body):
    page.locator("#new-button").click()
    page.locator("#modal").get_by_role("button", name=re.compile("^灵感笔记")).click()
    page.get_by_label("标题", exact=True).fill(title)
    page.get_by_label("正文", exact=True).fill(body)
    page.locator("#modal").get_by_role("button", name="保存记录", exact=True).click()
    expect(page.locator("#modal")).not_to_be_visible()
    expect(page.locator("#view-content")).to_contain_text(title)


def manage_members(page):
    page.locator('[data-view="spaces"]').click()
    page.locator(".space-card").filter(has_text=SHARED_NAME).get_by_role(
        "button", name="管理成员", exact=True
    ).click()
    expect(page.get_by_label("成员邮箱", exact=True)).to_be_visible()


def assign_role(page, email, role, display_name):
    page.get_by_label("成员邮箱", exact=True).fill(email)
    page.get_by_label("访问权限").select_option(role)
    page.get_by_role("button", name="保存权限", exact=True).click()
    row = page.locator(".member-row").filter(has_text=email)
    expect(row.locator(".tag")).to_have_text(display_name)


def check_collaboration(owner, member, base, member_email):
    owner.locator('[data-view="spaces"]').click()
    owner.get_by_role("button", name="创建共享空间", exact=True).click()
    owner.get_by_label("空间名称", exact=True).fill(SHARED_NAME)
    owner.locator("#modal").get_by_role("button", name="创建空间", exact=True).click()
    expect(owner.locator("#modal")).not_to_be_visible()
    expect(owner.locator("#current-space-name")).to_have_text(SHARED_NAME)
    shared_id = owner.evaluate("state.space.id")
    new_note(owner, OWNER_NOTE, "共享记录可以查看，只读成员不能编辑。")
    manage_members(owner)
    assign_role(owner, member_email, "viewer", "只读成员")
    report("所有者通过界面创建协作空间，并按已注册邮箱授予只读权限")

    member.reload(wait_until="networkidle")
    member.locator("#space-switcher").click()
    member.locator(".space-choice").filter(has_text=SHARED_NAME).click()
    expect(member.locator("#current-space-name")).to_have_text(SHARED_NAME)
    expect(member.locator("#view-content")).to_contain_text(OWNER_NOTE)
    expect(member.locator("#new-button")).to_be_disabled()
    member.locator('[data-view="notes"]').click()
    member.locator(".note-card").filter(has_text=OWNER_NOTE).click()
    expect(member.get_by_label("正文", exact=True)).to_have_value(
        "共享记录可以查看，只读成员不能编辑。"
    )
    expect(member.get_by_label("正文", exact=True)).to_be_disabled()
    member.locator("#modal-close").click()
    report("只读成员切入共享空间可以阅读记录，新建及正文编辑被禁用")

    assign_role(owner, member_email, "editor", "编辑者")
    member.reload(wait_until="networkidle")
    expect(member.locator("#current-space-name")).to_have_text(SHARED_NAME)
    expect(member.locator("#new-button")).to_be_enabled()
    new_note(member, EDITOR_NOTE, "从只读升级后，通过真实界面写入。")
    owner.locator("#modal-close").click()
    owner.reload(wait_until="networkidle")
    expect(owner.locator("#view-content")).to_contain_text(EDITOR_NOTE)
    report("所有者升级编辑权限后，成员写入成功且所有者刷新可以看到")

    manage_members(owner)
    owner.locator(".member-row").filter(has_text=member_email).get_by_role(
        "button", name="移除成员", exact=True
    ).click()
    owner.locator("#modal").get_by_role("button", name="移除成员", exact=True).click()
    expect(owner.locator(".member-row").filter(has_text=member_email)).to_have_count(0)
    owner.locator("#modal-close").click()
    member.reload(wait_until="networkidle")
    expect(member.locator("#current-space-name")).to_have_text("我的空间")
    expect(member.locator("#view-content")).not_to_contain_text(OWNER_NOTE)
    expect(member.locator("#view-content")).not_to_contain_text(EDITOR_NOTE)
    member.locator("#space-switcher").click()
    expect(member.locator(".space-choice").filter(has_text=SHARED_NAME)).to_have_count(0)
    member.locator("#modal-close").click()
    response = member.request.get(f"{base}/api/spaces/{shared_id}/items")
    assert response.status == 404, response.text()
    report("所有者移除成员后，刷新不再显示共享空间，原空间 API 返回 404")


def check_recording_retry(page):
    page.locator('[data-view="media"]').click()
    page.locator("#new-button").click()
    page.locator("#modal").get_by_role("button", name=re.compile("^语音录入")).click()
    page.get_by_role("button", name="开始录音", exact=True).click()
    expect(page.get_by_role("button", name="停止录音", exact=True)).to_be_enabled()
    # Give the real MediaRecorder time to produce audio from Chromium's fake mic.
    page.wait_for_timeout(1300)
    page.get_by_role("button", name="停止录音", exact=True).click()
    save = page.get_by_role("button", name="保存录音", exact=True)
    expect(save).to_be_enabled()
    page.wait_for_function("() => document.querySelector('#modal audio')?.readyState >= 1")
    preview = page.locator("#modal audio")
    original_url = preview.get_attribute("src")
    assert original_url and original_url.startswith("blob:")
    rejected_uploads = []

    def reject_upload(route):
        assert route.request.method == "POST"
        rejected_uploads.append(route.request.url)
        route.fulfill(status=503, content_type="application/json", body='{"error":"QA 上传暂时不可用"}')

    page.route("**/uploads", reject_upload)
    try:
        save.click()
        expect(page.locator("#modal .form-error")).to_contain_text("录音没有上传成功")
        expect(page.locator("#modal")).to_be_visible()
        expect(save).to_be_enabled()
        expect(preview).to_have_attribute("src", original_url)
        assert preview.evaluate("node => node.readyState >= 1")
        assert len(rejected_uploads) == 1
        expect(page.locator(".media-tile").filter(has_text="语音记录-")).to_have_count(0)
        report("真实录音上传收到 503 后保留弹窗和原音频预览，保存按钮重新启用")
    finally:
        page.unroute("**/uploads", reject_upload)

    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/uploads")) as uploaded:
        save.click()
    assert uploaded.value.status == 201, uploaded.value.text()
    expect(page.locator("#modal")).not_to_be_visible()
    recording = page.locator(".media-tile").filter(has_text="语音记录-")
    expect(recording).to_have_count(1)
    recording.click()
    expect(page.locator("#modal audio")).to_be_visible()
    page.wait_for_function("() => document.querySelector('#modal audio')?.readyState >= 1")
    page.locator("#modal-close").click()
    report("解除失败拦截后直接重试，录音上传 201，仅保存一份且可以解码预览")


def run():
    with tempfile.TemporaryDirectory(prefix="zhixu-collaboration-qa-") as data_dir:
        server = FolioServer(("127.0.0.1", 0), data_dir=data_dir, public_dir=ROOT / "public")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
                )
                try:
                    options = dict(viewport={"width": 1440, "height": 1024}, locale="zh-CN", timezone_id="Asia/Shanghai", permissions=["microphone"])
                    owner = browser.new_context(**options).new_page()
                    member = browser.new_context(**options).new_page()
                    errors = []
                    for page in (owner, member):
                        page.on("pageerror", lambda error: errors.append(str(error)))
                    suffix = uuid.uuid4().hex[:10]
                    member_email = f"qa-member-{suffix}@example.com"
                    register(owner, base, "协作所有者", f"qa-owner-{suffix}@example.com")
                    register(member, base, "协作成员", member_email)
                    check_collaboration(owner, member, base, member_email)
                    check_recording_retry(owner)
                    assert not errors, errors
                    report("全部检查完成，两个真实浏览器会话均没有脚本异常")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive(), "Disposable server did not stop"
            print("Disposable server stopped; temporary data removed on exit.", flush=True)


if __name__ == "__main__":
    run()
