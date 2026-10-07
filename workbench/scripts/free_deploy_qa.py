"""Check test-mode disclosure and fail-safe behavior with a disposable server."""
import sys
import tempfile
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer


def run():
    with tempfile.TemporaryDirectory(prefix="zhixu-free-qa-") as data:
        server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=ROOT / "public")
        server.test_mode = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
                context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(origin, wait_until="networkidle")
                expect(page.locator("#test-environment-banner")).to_be_visible()
                expect(page.locator("#test-environment-banner")).to_contain_text("测试数据")
                page.locator("#auth-toggle").click()
                page.locator("#auth-name").fill("测试部署")
                page.locator("#auth-email").fill("free-deploy-qa@example.com")
                page.locator("#auth-password").fill("QA-only-password-2026")
                page.locator("#auth-submit").click()
                expect(page.locator("#app-shell")).to_be_visible()
                expect(page.locator("#test-environment-banner")).to_be_visible()
                page.locator('[data-view="vault"]').click()
                expect(page.locator("#vault-environment-note")).to_be_visible()
                expect(page.locator("#vault-environment-note")).to_contain_text("测试")
                assert page.locator("#sidebar").bounding_box()["y"] >= page.locator("#test-environment-banner").bounding_box()["height"] - 1
                print("PASS test-mode disclosure before/after login and in the vault", flush=True)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(ROOT / "artifacts" / "free-test-mobile.png"), full_page=True, animations="disabled")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                print("PASS mobile test banner without horizontal overflow", flush=True)
                context.close()

                server.test_mode = False
                normal = browser.new_page()
                normal.goto(origin, wait_until="networkidle")
                expect(normal.locator("#auth-screen")).to_be_visible()
                expect(normal.locator("#test-environment-banner")).to_be_hidden()
                print("PASS persistent deployment does not show test banner", flush=True)
                normal.close()
                fallback = browser.new_page()
                fallback.route("**/api/config", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"test failure"}'))
                fallback.goto(origin, wait_until="networkidle")
                expect(fallback.locator("#auth-screen")).to_be_visible()
                expect(fallback.locator("#test-environment-banner")).to_be_visible()
                expect(fallback.locator("#test-environment-banner")).to_contain_text("确认")
                print("PASS config outage discloses uncertainty without blocking login", flush=True)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    run()
