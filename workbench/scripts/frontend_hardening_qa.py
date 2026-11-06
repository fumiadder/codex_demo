"""Front-end hardening browser checks against a temporary local server.

Covers:
- P1-F2: vault creation keeps its derived key and form context when the first
  PUT fails with 503; the user can retry without retyping the password, and a
  successful retry persists exactly like a normal creation.
- P2-5: api() maps 429 (with Retry-After) and empty 503 bodies to dedicated
  Chinese copy, while specific back-end Chinese messages are never replaced.
- P2-4: the PDF preview iframe carries a minimal sandbox attribute and the
  preview modal still opens after a real same-origin upload.

Run:
  export QA_CHROMIUM=/path/to/chrome-headless-shell
  python scripts/frontend_hardening_qa.py
"""
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer

# Smallest valid one-page PDF, generated locally so the QA needs no fixture file.
PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000098 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n"
)


def register(page):
    page.locator("#auth-toggle").click()
    page.locator("#auth-name").fill("前端加固测试")
    email = "hardening-" + uuid.uuid4().hex[:10] + "@example.com"
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill("Hardening-QA-only-2026")
    page.locator("#auth-submit").click()
    return email


def check_vault_retry(page):
    page.locator('[data-view="vault"]').click()
    expect(page.locator(".vault-form")).to_be_visible()
    password = "Hardening-vault-2026"
    page.get_by_label("设置独立密码", exact=True).fill(password)
    page.get_by_label("确认密码", exact=True).fill(password)

    put_attempts = {"count": 0}

    def fail_first_vault_put(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        if put_attempts["count"] == 0:
            put_attempts["count"] += 1
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"error":"QA 模拟密码箱服务暂不可用"}',
            )
        else:
            route.continue_()

    page.route("**/vault", fail_first_vault_put)
    page.get_by_role("button", name="创建密码箱", exact=True).click()

    # ① 失败后不跳回丢密码态：错误提示可见、密钥与表单上下文仍在、可再次提交
    form_error = page.locator(".vault-form .form-error")
    expect(form_error).to_be_visible()
    expect(form_error).to_contain_text("QA 模拟密码箱服务暂不可用")
    retry_button = page.get_by_role("button", name="重试创建密码箱", exact=True)
    expect(retry_button).to_be_visible()
    expect(page.locator(".vault-form")).to_be_visible()
    assert page.evaluate("state.vault.key !== null"), "失败后派生密钥被清空"
    assert page.evaluate("Array.isArray(state.vault.entries)"), "entries 上下文丢失"
    assert page.evaluate("state.vault.payload && state.vault.payload.salt"), "salt 上下文丢失"

    # ② 放行第二次后保存成功，流程与正常创建一致
    with page.expect_response(
        lambda response: response.request.method == "PUT"
        and response.url.endswith("/vault")
    ) as put_response:
        retry_button.click()
    assert put_response.value.ok, put_response.value.status
    # 工具栏与空状态各有一个同名入口，取首个即可证明已进入解锁视图
    expect(page.get_by_role("button", name="添加私密记录", exact=True).first).to_be_visible()
    assert page.evaluate("state.vault.key !== null && state.vault.entries.length === 0")
    page.unroute("**/vault", fail_first_vault_put)
    print("PASS vault PUT 503 keeps key/form context and retry persists normally", flush=True)


def check_status_mapping(page):
    async_probe = """
      async (body) => {
        try { await api('/api/spaces'); return ''; }
        catch (error) { return error.message; }
      }
    """
    page.route(
        "**/api/spaces",
        lambda route: route.fulfill(
            status=429,
            headers={"Retry-After": "12"},
            content_type="application/json",
            body='{"error":""}',
        ),
    )
    message = page.evaluate(async_probe)
    assert message == "操作太频繁，请 12 秒后再试", message
    page.unroute("**/api/spaces")

    page.route(
        "**/api/spaces",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":""}',
        ),
    )
    message = page.evaluate(async_probe)
    assert message == "服务暂未连接，请稍后再试。", message
    page.unroute("**/api/spaces")

    # 后端给出具体中文 message 时必须原样保留，不被通用 503 文案吞掉
    page.route(
        "**/api/spaces",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body='{"error":"媒体存储暂时不可用，请稍后重试"}',
        ),
    )
    message = page.evaluate(async_probe)
    assert message == "媒体存储暂时不可用，请稍后重试", message
    page.unroute("**/api/spaces")
    print("PASS 429 Retry-After and 503 fallback/specific message mapping", flush=True)


def check_pdf_sandbox(page, upload_dir):
    pdf_path = upload_dir / "hardening-preview.pdf"
    pdf_path.write_bytes(PDF_BYTES)
    page.locator('[data-view="media"]').click()
    page.set_input_files("#file-input", [str(pdf_path)])
    tile = page.locator(".media-tile", has_text="hardening-preview.pdf")
    expect(tile).to_have_count(1)
    tile.click()
    expect(page.locator("#modal")).to_be_visible()
    frame = page.locator("#modal iframe")
    expect(frame).to_have_count(1)
    sandbox = frame.get_attribute("sandbox")
    assert sandbox is not None and "allow-scripts" not in sandbox, sandbox
    assert frame.get_attribute("src").startswith("/api/files/"), frame.get_attribute("src")
    print("PASS PDF preview iframe uses a scripts-forbidden sandbox and still opens", flush=True)
    page.locator("#modal-close").click()
    expect(page.locator("#modal")).not_to_be_visible()


with tempfile.TemporaryDirectory(prefix="zhixu-hardening-qa-") as data:
    upload_dir = Path(data) / "uploads"
    upload_dir.mkdir()
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            context.add_init_script("delete URLSearchParams.prototype.size;")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")
            register(page)
            expect(page.locator("#app-shell")).to_be_visible()

            check_vault_retry(page)
            check_status_mapping(page)
            check_pdf_sandbox(page, upload_dir)

            assert not errors, "Page errors: " + " | ".join(errors)
            print("PASS frontend hardening suite finished without page errors", flush=True)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
