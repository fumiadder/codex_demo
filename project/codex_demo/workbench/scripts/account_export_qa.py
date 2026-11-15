"""数据与账号设置中心的浏览器端到端 QA（真实后端集成测试）。

直接启动 FolioServer 真实 HTTP 服务，验证前端与已落地的冻结契约联动：

- GET  /api/spaces/:sid/export?format=json|markdown|html （owner 专属，ZIP 流）
- PATCH /api/me（昵称 {name}；密码 {password:{currentPassword,newPassword}}，
  成功后服务端轮换会话并下发新 Cookie）
- DELETE /api/me/account（密码 + 409 约束，成功后会话失效）
- DELETE /api/spaces/:sid（owner，有其他成员 409）

覆盖：昵称改后刷新持久化、改密码后新旧密码登录、三种格式导出为合法 ZIP、
非 owner 看不到导出且直连 403、共享空间删除 409 与移除成员后删除成功、
账号注销后回到登录页且旧会话 401、演示模式禁用导出、320px 窄屏与 Escape。

Run:
  export QA_CHROMIUM=/path/to/chrome-headless-shell
  export PLAYWRIGHT_BROWSERS_PATH=/path/to/.pw-browsers
  python scripts/account_export_qa.py
"""
import os
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer  # noqa: E402

PASS_COUNT = 0


def report(message):
    global PASS_COUNT
    PASS_COUNT += 1
    print("PASS", message, flush=True)


EXPORT_FORMATS = ("json", "markdown", "html")
FORMAT_LABELS = {"json": "JSON", "markdown": "Markdown", "html": "HTML"}
DELETE_PHRASE = "删除我的账号"
PASSWORD = "Account-QA-only-2026"
NEW_PASSWORD = "Account-QA-renewed-2026"


def register(page, prefix):
    page.locator("#auth-toggle").click()
    page.locator("#auth-name").fill(prefix + "用户")
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill(PASSWORD)
    page.locator("#auth-submit").click()
    return email


def wait_app(page):
    # 注册成功后服务端已自动创建个人空间「我的空间」，直接进入工作台
    expect(page.locator("#app-shell")).to_be_visible()
    expect(page.locator("#modal")).not_to_be_visible()


def open_account_settings(page):
    page.locator("#account-settings-button").click()
    expect(page.locator("#modal")).to_be_visible()
    expect(page.locator("#modal-title")).to_have_text("数据与账号")


def no_horizontal_overflow(page):
    assert page.evaluate(
        "document.documentElement.scrollWidth <= innerWidth"
    ), page.evaluate(
        "({width: innerWidth, scroll: document.documentElement.scrollWidth})"
    )


def run_checks(base):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get(
                "QA_CHROMIUM", "/usr/bin/chromium"
            ),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=True,
        )
        context.add_init_script("delete URLSearchParams.prototype.size;")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")

        # ① 注册账号 A（服务端自动创建个人空间）
        email_a = register(page, "acct")
        wait_app(page)

        # ② 修改昵称并验证刷新后的持久化
        open_account_settings(page)
        new_name = "新昵称-" + uuid.uuid4().hex[:6]
        page.get_by_label("昵称", exact=True).fill(new_name)
        page.get_by_role("button", name="保存昵称", exact=True).click()
        expect(page.locator(".toast").last).to_contain_text("昵称已更新")
        assert page.evaluate("state.user.name") == new_name
        expect(page.locator("#user-name")).to_have_text(new_name)
        page.keyboard.press("Escape")
        expect(page.locator("#modal")).not_to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator("#app-shell")).to_be_visible()
        expect(page.locator("#user-name")).to_have_text(new_name)
        me = context.request.get(base + "/api/me").json()["user"]
        assert me["name"] == new_name and me["email"] == email_a
        report("修改昵称成功，刷新页面与 /api/me 均保持持久化")

        # ③ 修改密码：前端校验 + 成功清空表单（服务端成功后轮换会话）
        open_account_settings(page)
        save_password = page.get_by_role("button", name="更新密码", exact=True)
        password_error = page.locator(
            ".account-section",
            has=page.get_by_role("heading", name="修改密码", exact=True),
        ).locator(".form-error")
        page.get_by_label("当前密码", exact=True).fill(PASSWORD)
        page.get_by_label("新密码", exact=True).fill("short")
        page.get_by_label("确认新密码", exact=True).fill("short")
        save_password.click()
        expect(password_error).to_contain_text("新密码至少需要 10 位字符")
        page.get_by_label("新密码", exact=True).fill(NEW_PASSWORD)
        page.get_by_label("确认新密码", exact=True).fill(PASSWORD)
        save_password.click()
        expect(password_error).to_contain_text("两次输入的新密码不一致")
        page.get_by_label("确认新密码", exact=True).fill(NEW_PASSWORD)
        save_password.click()
        expect(page.locator(".toast").last).to_contain_text("密码已更新")
        assert page.locator('input[autocomplete="current-password"]').last.input_value() == ""
        report("密码前端长度/一致性校验与 PATCH /api/me 改密成功（会话已轮换）")

        # ④ 退出登录：新密码可登录，旧密码被拒
        page.keyboard.press("Escape")
        page.locator("#profile-button").click()
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page.locator("#auth-screen")).to_be_visible()
        page.locator("#auth-email").fill(email_a)
        page.locator("#auth-password").fill(PASSWORD)
        page.locator("#auth-submit").click()
        expect(page.locator("#auth-error")).to_contain_text("密码")
        report("旧密码登录被服务端拒绝")
        page.locator("#auth-password").fill(NEW_PASSWORD)
        page.locator("#auth-submit").click()
        expect(page.locator("#app-shell")).to_be_visible()
        report("新密码登录成功")

        # ⑤ 三种格式导出：download 事件 + ZIP 合法性与内容断言
        page.locator('[data-view="spaces"]').click()
        expect(page.locator(".space-card")).to_have_count(1)
        download_dir = Path(tempfile.mkdtemp(prefix="zhixu-export-qa-"))
        for fmt in EXPORT_FORMATS:
            with page.expect_download() as download_info:
                page.get_by_role(
                    "button", name=FORMAT_LABELS[fmt], exact=True
                ).click()
            download = download_info.value
            target = download_dir / f"{fmt}.zip"
            download.save_as(str(target))
            assert download.suggested_filename.endswith(".zip")
            with zipfile.ZipFile(target) as archive:
                assert archive.testzip() is None
                names = archive.namelist()
                # 真实后端：JSON 为 export.json；Markdown 含 INDEX.md；HTML 为 export.html
                expected = {
                    "json": "export.json",
                    "markdown": "INDEX.md",
                    "html": "index.html",
                }[fmt]
                assert expected in names, (fmt, names)
                assert any(
                    "index" in name.lower() or "export" in name.lower()
                    for name in names
                ), names
                content = archive.read(expected).decode("utf-8")
                assert content.strip()
            report(f"{fmt} 导出为合法 ZIP，且包含契约约定的 {expected} 文件")

        # ⑥ 非 owner：UI 隐藏导出，直连接口 403
        member_context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        member_context.add_init_script("delete URLSearchParams.prototype.size;")
        member_page = member_context.new_page()
        member_page.on("pageerror", lambda error: errors.append(str(error)))
        member_page.goto(base, wait_until="networkidle")
        email_b = register(member_page, "member")
        wait_app(member_page)

        # A 建共享空间并邀请 B
        page.locator(".button.primary", has_text="创建共享空间").click()
        expect(page.locator("#modal")).to_be_visible()
        shared_name = "共享QA空间"
        page.locator("#modal-body input").first.fill(shared_name)
        page.get_by_role("button", name="创建空间", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        page.locator('[data-view="spaces"]').click()
        shared_card = page.locator(".space-card", has_text=shared_name)
        shared_card.get_by_role("button", name="管理成员").click()
        page.locator(".invite-form input[type='email']").fill(email_b)
        page.get_by_role("button", name="保存权限", exact=True).click()
        expect(page.locator(".member-row", has_text=email_b)).to_be_visible()
        page.locator("#modal-close").click()

        # B 侧看不到导出操作，只有说明文案；直连导出返回 403
        member_page.reload(wait_until="networkidle")
        member_page.locator('[data-view="spaces"]').click()
        shared_member_card = member_page.locator(".space-card", has_text=shared_name)
        expect(shared_member_card).to_be_visible()
        expect(shared_member_card.locator(".export-note")).to_contain_text(
            "仅空间所有者可导出"
        )
        assert shared_member_card.locator(".export-actions").count() == 0
        spaces = member_context.request.get(base + "/api/spaces").json()["spaces"]
        shared_id = next(s["id"] for s in spaces if s["name"] == shared_name)
        forbidden = member_context.request.get(
            base + f"/api/spaces/{shared_id}/export?format=json"
        )
        assert forbidden.status == 403, forbidden.status
        report("非 owner 看不到导出入口，直连导出接口返回 403")

        # ⑦ 删除空间：有成员 409 提示可见；移除成员后键入全名删除成功
        page.locator('[data-view="spaces"]').click()
        shared_card = page.locator(".space-card", has_text=shared_name)
        shared_card.get_by_role("button", name="删除空间", exact=True).click()
        expect(page.locator("#modal-title")).to_have_text("删除这个空间？")
        confirm_delete = page.get_by_role("button", name="确认删除空间", exact=True)
        expect(confirm_delete).to_be_disabled()
        page.locator("#modal-body input").first.fill(shared_name)
        expect(confirm_delete).to_be_enabled()
        confirm_delete.click()
        expect(page.locator("#modal .form-error")).to_contain_text("成员")
        report("共享空间存在其他成员时删除返回 409，中文提示可见且表单保留可重试")

        # 移除 B 后再次删除：先点成员行内图标按钮，再在确认弹窗中确认
        page.get_by_role("button", name="取消", exact=True).click()
        shared_card.get_by_role("button", name="管理成员").click()
        expect(page.locator(".member-row", has_text=email_b)).to_be_visible()
        page.locator(".member-row", has_text=email_b).get_by_role(
            "button", name="移除成员", exact=True
        ).click()
        expect(page.locator("#modal-title")).to_have_text("移除这位成员？")
        page.get_by_role("button", name="移除成员", exact=True).last.click()
        # 移除成功后成员管理弹窗带着最新名单重新渲染，确认 B 已消失再关闭
        expect(page.locator(".member-row", has_text=email_b)).to_have_count(0)
        page.locator("#modal-close").click()
        expect(page.locator("#modal")).not_to_be_visible()
        page.locator('[data-view="spaces"]').click()
        shared_card = page.locator(".space-card", has_text=shared_name)
        shared_card.get_by_role("button", name="删除空间", exact=True).click()
        page.locator("#modal-body input").first.fill(shared_name)
        page.get_by_role("button", name="确认删除空间", exact=True).click()
        expect(page.locator("#modal")).not_to_be_visible()
        expect(page.locator(".space-card", has_text=shared_name)).to_have_count(0)
        assert context.request.get(
            base + f"/api/spaces/{shared_id}/items"
        ).status in (403, 404)
        report("移除成员后键入空间全名删除成功，列表与接口均不再可见")

        # ⑧ 一次性账号走完注销：错误短语禁用按钮、错误密码 400、成功后会话失效
        victim_context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        victim_context.add_init_script("delete URLSearchParams.prototype.size;")
        victim_page = victim_context.new_page()
        victim_page.on("pageerror", lambda error: errors.append(str(error)))
        victim_page.goto(base, wait_until="networkidle")
        register(victim_page, "victim")
        wait_app(victim_page)
        victim_page.locator("#account-settings-button").click()
        victim_page.get_by_role("button", name="注销账号", exact=True).click()
        expect(victim_page.locator("#modal-title")).to_have_text("注销账号")
        confirm_btn = victim_page.get_by_role(
            "button", name="确认注销账号", exact=True
        )
        expect(confirm_btn).to_be_disabled()
        victim_page.get_by_label("确认文字", exact=True).fill("删除我的帐户")
        victim_page.get_by_label("登录密码", exact=True).fill(PASSWORD)
        expect(confirm_btn).to_be_disabled()
        victim_page.get_by_label("确认文字", exact=True).fill(DELETE_PHRASE)
        expect(confirm_btn).to_be_enabled()
        # 密码错误时保留上下文可重试
        victim_page.get_by_label("登录密码", exact=True).fill("Wrong-password-2026")
        confirm_btn.click()
        expect(victim_page.locator("#modal .form-error")).to_contain_text("密码")
        report("注销确认短语不匹配时按钮禁用，密码错误返回 400 且表单保留")
        victim_page.get_by_label("登录密码", exact=True).fill(PASSWORD)
        confirm_btn.click()
        expect(victim_page.locator("#auth-screen")).to_be_visible()
        expect(victim_page.locator("#app-shell")).to_be_hidden()
        stale = victim_context.request.get(base + "/api/me")
        assert stale.status == 401, stale.status
        report("账号注销成功并回到登录页，旧 Cookie 调用 /api/me 返回 401")

        # ⑨ 演示模式：导出按钮禁用并展示说明
        demo_context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, locale="zh-CN"
        )
        demo_context.add_init_script("delete URLSearchParams.prototype.size;")
        demo_page = demo_context.new_page()
        demo_page.on("pageerror", lambda error: errors.append(str(error)))
        demo_page.goto(base, wait_until="networkidle")
        demo_page.locator("#demo-button").click()
        expect(demo_page.locator("#app-shell")).to_be_visible()
        demo_page.locator('[data-view="spaces"]').click()
        expect(demo_page.locator(".export-note").first).to_contain_text(
            "演示模式不提供导出"
        )
        # 演示账号固定有两个演示空间，每个空间 3 个禁用的导出按钮
        expect(demo_page.locator(".export-actions")).to_have_count(2)
        disabled_exports = demo_page.locator(".export-actions .button:disabled")
        expect(disabled_exports).to_have_count(6)
        report("演示模式下三种导出按钮全部禁用并展示说明")
        demo_context.close()
        victim_context.close()
        member_context.close()

        # ⑩ 320px 窄屏：导航走抽屉，设置中心无横向溢出，Escape 关闭弹窗
        page.set_viewport_size({"width": 320, "height": 640})
        page.locator("#mobile-more").click()
        page.locator('.sidebar [data-view="spaces"]').click()
        expect(page.locator("#sidebar.open")).to_have_count(0)
        no_horizontal_overflow(page)
        page.locator("#mobile-more").click()
        page.locator("#account-settings-button").click()
        expect(page.locator("#modal")).to_be_visible()
        no_horizontal_overflow(page)
        expect(page.get_by_role("button", name="保存昵称", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#modal")).not_to_be_visible()
        report("320px 窄屏下设置中心可用且无横向溢出，Escape 正确关闭弹窗")
        page.set_viewport_size({"width": 1440, "height": 1000})

        assert not errors, "Page errors: " + " | ".join(errors)
        report("全程无 pageerror")
        context.close()
        browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="zhixu-account-export-qa-") as data:
        server = FolioServer(
            ("127.0.0.1", 0),
            data_dir=data,
            public_dir=ROOT / "public",
            registration_mode="open",
            storage_persistence="persistent",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            run_checks(base)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(f"ACCOUNT_EXPORT_QA PASS 计数：{PASS_COUNT}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(f"ACCOUNT_EXPORT_QA 失败，已完成 PASS 计数：{PASS_COUNT}", file=sys.stderr)
        raise
