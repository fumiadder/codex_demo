"""One-off visual check: work vs life sidebar/mobile labels and visibility."""
import sys, tempfile, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from server import FolioServer

data = Path(tempfile.mkdtemp())
server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=ROOT / "public",
                     registration_mode="open", storage_persistence="persistent")
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
out = ROOT / "artifacts"
out.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=__import__("os").environ.get("QA_CHROMIUM"))
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
    page = ctx.new_page()
    page.goto(base, wait_until="networkidle")
    page.locator("#auth-toggle").click()
    page.fill("#auth-name", "菜单核对")
    page.fill("#auth-email", "menu@example.com")
    page.fill("#auth-password", "menu-pass-123")
    page.click("#auth-submit")
    page.wait_for_selector("#app-shell:not([hidden])")
    def nav_text():
        return page.evaluate(
            "() => [...document.querySelectorAll('.main-nav .nav-item')].map(b => ({view:b.dataset.view, hidden:b.hidden, text:(b.querySelector('span[data-label-work],span:not(.nav-count):not(.tiny-dot):not(.nav-shortcut)')||{}).textContent}))")
    page.locator('[data-area="work"]').click()
    work = nav_text()
    page.screenshot(path=str(out / "menu-work-sidebar.png"))
    page.locator('[data-area="life"]').click()
    life = nav_text()
    page.screenshot(path=str(out / "menu-life-sidebar.png"))
    print("WORK", work)
    print("LIFE", life)
    # Switching to life while sitting on the work-only vault must fall back to overview.
    page.locator('[data-area="work"]').click()
    page.locator('[data-view="vault"]').click()
    page.locator('[data-area="life"]').click()
    assert page.evaluate("state.view") == "overview", "vault->life must reset view"
    print("PASS vault-to-life resets to overview")
    # Mobile
    m = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True)
    mp = m.new_page()
    mp.goto(base, wait_until="networkidle")
    mp.locator("#auth-toggle").click()
    mp.fill("#auth-name", "移动核对")
    mp.fill("#auth-email", "mobile@example.com")
    mp.fill("#auth-password", "menu-pass-123")
    mp.click("#auth-submit")
    mp.wait_for_selector("#app-shell:not([hidden])")
    mobile_labels = mp.evaluate(
        "() => [...document.querySelectorAll('[data-mobile-view] span[data-label-work]')].map(s=>s.textContent)")
    mp.locator('[data-area="life"]').click()
    mobile_labels_life = mp.evaluate(
        "() => [...document.querySelectorAll('[data-mobile-view] span[data-label-work]')].map(s=>s.textContent)")
    mp.locator("#mobile-more").click()
    mp.wait_for_selector("#sidebar:not([inert])")
    mp.screenshot(path=str(out / "menu-life-mobile-drawer.png"))
    print("MOBILE_WORK", mobile_labels)
    print("MOBILE_LIFE", mobile_labels_life)
    browser.close()
print("PASS visual menu check")
