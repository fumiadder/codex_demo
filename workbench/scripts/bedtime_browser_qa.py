"""Disposable real-browser checks for stories, account boundaries and sleep UI.

Run: python scripts/bedtime_browser_qa.py
The local HTTP server, accounts, story search, favorites and history are real.
Only device speech events are a QA stub: headless Linux has no guaranteed TTS
engine. These checks do not validate voice quality or a live cloud provider.
No provider key is used and no recording is read from the microphone.
"""
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer

PASSWORD = "QA-only-bedtime-2026"
HEADERS = {"X-Requested-With": "Workspace"}
SPEECH_EVENTS = r"""(() => {
  window.__qaSpeech = {calls: [], canceled: 0, timer: null};
  window.SpeechSynthesisUtterance = class {
    constructor(text) { this.text = text; this.voice = null; this.rate = 1; this.volume = 1; }
  };
  const voices = [{name:'QA device voice A',lang:'zh-CN',localService:true},{name:'QA device voice B',lang:'zh-CN',localService:true}];
  Object.defineProperty(window, 'speechSynthesis', {value: {
    getVoices: () => voices, addEventListener: () => {},
    cancel: () => { window.__qaSpeech.canceled++; clearInterval(window.__qaSpeech.timer); },
    speak: (utterance) => {
      window.__qaSpeech.calls.push({text:utterance.text,voice:utterance.voice?.name || 'default',volume:utterance.volume});
      let char = 0;
      setTimeout(() => {
        utterance.onstart?.({});
        window.__qaSpeech.timer = setInterval(() => {
          char = Math.min(char + 1, Math.max(0,utterance.text.length - 2));
          utterance.onboundary?.({charIndex:char});
        }, 300);
      }, 20);
    }
  }, configurable:true});
  window.__qaAudio = {starts:0,stops:0};
  const original = AudioContext.prototype.createBufferSource;
  AudioContext.prototype.createBufferSource = function(...args) {
    const source = original.apply(this,args), start = source.start.bind(source), stop = source.stop.bind(source);
    source.start = (...values) => { window.__qaAudio.starts++; return start(...values); };
    source.stop = (...values) => { window.__qaAudio.stops++; return stop(...values); };
    return source;
  };
})();
"""


def report(message):
    print("PASS", message, flush=True)


def register(context, origin, name):
    email = f"bedtime-{uuid.uuid4().hex[:10]}@example.com"
    response = context.request.post(origin + "/api/auth/register", data={"name": name, "email": email, "password": PASSWORD}, headers=HEADERS)
    assert response.status == 201, response.text()
    user = response.json()["user"]
    spaces = context.request.get(origin + "/api/spaces").json()["spaces"]
    return {"user": user, "space": spaces[0], "email": email}


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll('*')].map(el=>({tag:el.tagName,class:el.className,right:el.getBoundingClientRect().right})).filter(el=>el.right>innerWidth+1)})")


def open_first(page):
    expect(page.locator(".story-card").first).to_be_visible()
    page.locator(".story-card").first.click()
    expect(page.locator("#reader")).to_be_visible()
    return page.locator("#reader-text").inner_text()


def offline_keys(page):
    return page.evaluate("Object.keys(localStorage).filter(key=>key.startsWith('zhixu:bedtime:stories:'))")


def checks(origin):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        anonymous = browser.new_page()
        anonymous.goto(origin + "/bedtime.html")
        expect(anonymous.locator("#loading-screen")).to_contain_text("登录")
        expect(anonymous.locator("#bedtime-app")).not_to_be_visible()
        anonymous.close()
        report("未登录无法进入晚安故事，页面沿用知序账号")

        context = browser.new_context(viewport={"width":390,"height":844}, locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=True)
        context.add_init_script(SPEECH_EVENTS)
        context.add_init_script("if (!localStorage.getItem('zhixu:bedtime:theme')) localStorage.setItem('zhixu:bedtime:theme','day');")
        user_a = register(context, origin, "故事测试 A")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        requests = []
        page.on("request", lambda request: requests.append((request.method, request.url, request.headers.get("x-expected-user"))))
        page.goto(origin + "/bedtime.html?space=" + user_a["space"]["id"], wait_until="networkidle")
        expect(page.locator("#bedtime-app")).to_be_visible()
        expect(page.locator(".story-card").first).to_be_visible()
        assert page.locator("#space-selector").input_value() == user_a["space"]["id"]
        no_overflow(page)
        page.locator("#open-voices").click()
        expect(page.locator("#clone-status")).to_contain_text("尚未连接")
        expect(page.locator("#voice-file")).to_be_enabled()
        expect(page.locator("#clone-submit")).to_be_disabled()
        page.locator("#close-voices").click()
        expect(page.locator("#web-search")).to_be_disabled()
        report("无密钥时联网检索和声音克隆明确待连接，没有伪造进度或音色")

        for width, height in [(320,720),(360,780),(390,844),(430,932),(844,390),(1440,1000)]:
            page.set_viewport_size({"width":width,"height":height})
            no_overflow(page)
            page.locator("#open-voices").click()
            rect = page.locator("#voice-dialog").bounding_box()
            assert rect["width"] <= width and rect["height"] <= height
            page.locator("#close-voices").click()
        page.set_viewport_size({"width":390,"height":844})
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(350)
        (ROOT / "artifacts").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "artifacts" / "bedtime-mobile.png"), full_page=False, animations="disabled")
        page.locator("#theme-toggle").click()
        page.locator("#theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme","night")
        page.wait_for_timeout(350)
        card_color = page.locator(".story-card").first.evaluate("el => getComputedStyle(el).backgroundColor")
        assert card_color.startswith("rgba(44, 61, 71,"), card_color
        page.screenshot(path=str(ROOT / "artifacts" / "bedtime-night.png"), full_page=False, animations="disabled")
        report("320–430px 手机、横屏与桌面无溢出，柔和日间/夜间界面实际截图")

        page.locator("#search-input").fill("月")
        expect(page.locator("#list-status")).to_contain_text("原创故事")
        page.wait_for_timeout(400)
        body = open_first(page)
        assert body.strip()
        expect(page.locator("#reader-source")).to_contain_text("知序原创")
        page.wait_for_timeout(200)
        page.screenshot(path=str(ROOT / "artifacts" / "bedtime-reader.png"), full_page=False, animations="disabled")
        page.locator("#favorite-story").click()
        expect(page.locator("#favorite-story")).to_contain_text("已收藏")
        page.locator('[data-library="favorites"]').click()
        expect(page.locator(".story-card")).to_have_count(1)
        report("真实原创文本搜索、打开与个人收藏成功")

        before = sum(1 for method,url,_ in requests if "/bedtime/search?" in url or "/bedtime/stories/" in url)
        page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label","暂停朗读")
        page.locator("#voice-selector").select_option("system:1")
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label","暂停朗读")
        assert page.locator("#reader-text").inner_text() == body
        after = sum(1 for method,url,_ in requests if "/bedtime/search?" in url or "/bedtime/stories/" in url)
        assert before == after
        assert page.evaluate("window.__qaSpeech.calls.at(-1).voice") == "QA device voice B"
        assert any(expected == user_a["user"]["id"] for _,url,expected in requests if "/bedtime/" in url)
        page.locator('[data-library="history"]').click()
        expect(page.locator(".story-card")).to_have_count(1)
        report("音色切换保留文本且不重新检索；系统语音事件仅 QA stub，真实历史写入成功")

        page.locator("#sleep-controls summary").click()
        page.locator("#noise-kind").select_option("rain")
        page.wait_for_function("window.__qaAudio.starts > 0")
        page.clock.install()
        page.locator("#sleep-timer").select_option("5")
        page.clock.fast_forward(5 * 60 * 1000 + 1000)
        expect(page.locator("#play-status")).to_contain_text("定时已停止")
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label","开始朗读")
        assert page.evaluate("window.__qaAudio.stops > 0")
        assert page.evaluate("!navigator.mediaSession || navigator.mediaSession.metadata === null")
        page.locator("#sleep-controls summary").click()
        report("真实 WebAudio 白噪音启动/停止；截止时间到达后朗读、噪音与锁屏元信息清理")

        page.locator("#cache-story").click()
        assert offline_keys(page)
        context.set_offline(True)
        page.locator('[data-library="offline"]').click()
        expect(page.locator(".story-card")).to_have_count(1)
        page.locator(".story-card").first.click()
        assert page.locator("#reader-text").inner_text() == body
        expect(page.locator("#list-status")).to_contain_text("已打开")
        with page.expect_download() as downloaded:
            page.locator("#download-story").click()
        assert downloaded.value.suggested_filename.endswith(".txt")
        context.set_offline(False)
        page.locator("#clear-list").click()
        assert not offline_keys(page)
        report("主动保存的故事在已打开页面断网可读，TXT 可下载，删除离线缓存有效")

        # Login broadcasts reject delayed responses at the account boundary.
        other_context = browser.new_context()
        user_b = register(other_context, origin, "故事测试 B")
        add = context.request.post(origin + "/api/spaces/" + user_a["space"]["id"] + "/members", data={"email":user_b["email"],"role":"editor"}, headers=HEADERS)
        assert add.ok, add.text()
        pending = []
        def hold_search(route):
            if "q=delayed" in route.request.url:
                pending.append(route)
            else:
                route.continue_()
        page.route("**/bedtime/search?*", hold_search)
        page.locator("#cache-story").click()
        assert offline_keys(page)
        page.locator("#search-input").fill("delayed")
        deadline = time.monotonic() + 10
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert pending, "搜索请求未在 10 秒内发出"
        workbench = context.new_page()
        workbench.set_viewport_size({"width":1440,"height":1000})
        # A different account is registered in a separate context, then the
        # actual workbench login form changes this shared-cookie context.
        context.request.post(origin + "/api/auth/logout", headers=HEADERS)
        workbench.goto(origin, wait_until="networkidle")
        workbench.locator("#auth-email").fill(user_b["email"])
        workbench.locator("#auth-password").fill(PASSWORD)
        workbench.locator("#auth-submit").click()
        expect(workbench.locator("#app-shell")).to_be_visible()
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        pending.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"stories":[{"id":"delayed-private","title":"B_PRIVATE_SHOULD_NOT_RENDER","text":"B_PRIVATE_SHOULD_NOT_CACHE","category":"晚安","source":{"label":"QA delayed response"}}]}))
        page.wait_for_timeout(100)
        assert page.locator("#story-list").inner_text() == ""
        assert page.locator("#reader-text").inner_text() == ""
        assert not offline_keys(page)
        report("真实跨标签登录广播清旧账号 DOM/缓存；仅时序 stub 的延迟回包在边界后丢弃")

        # Cookie switches without a broadcast are caught by the real header.
        context.request.post(origin + "/api/auth/login", data={"email":user_a["email"],"password":PASSWORD}, headers=HEADERS)
        page.unroute("**/bedtime/search?*", hold_search)
        page.goto(origin + "/bedtime.html?space=" + user_a["space"]["id"], wait_until="networkidle")
        open_first(page)
        page.locator("#cache-story").click()
        context.request.post(origin + "/api/auth/login", data={"email":user_b["email"],"password":PASSWORD}, headers=HEADERS)
        page.locator("#search-input").fill("账号边界")
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        assert not offline_keys(page)
        report("未广播的共享 Cookie 账号切换被 X-Expected-User 真实拒绝，不读入 B 的私密数据")

        # Membership revocation returns 404; the current reader must fail shut.
        member = other_context.new_page()
        other_context.add_init_script(SPEECH_EVENTS)
        member.goto(origin + "/bedtime.html?space=" + user_a["space"]["id"], wait_until="networkidle")
        open_first(member)
        member.locator("#cache-story").click()
        assert offline_keys(member)
        member.locator("#play-toggle").click()
        expect(member.locator("#play-toggle")).to_have_attribute("aria-label","暂停朗读")
        owner_context = browser.new_context()
        login = owner_context.request.post(origin + "/api/auth/login", data={"email":user_a["email"],"password":PASSWORD}, headers=HEADERS)
        assert login.ok
        removed = owner_context.request.delete(origin + "/api/spaces/" + user_a["space"]["id"] + "/members/" + user_b["user"]["id"], headers=HEADERS)
        assert removed.ok, removed.text()
        member.locator("#search-input").fill("权限已撤销")
        expect(member.locator("#bedtime-app")).not_to_be_visible()
        assert member.locator("#reader-text").inner_text() == ""
        assert not offline_keys(member)
        assert member.evaluate("window.__qaSpeech.canceled > 0")
        report("空间成员撤销后的真实 404 清理读者正文、缓存、播放与音色列表")

        # A real workbench logout also clears an open bedtime page.
        logout_context = browser.new_context(viewport={"width":1440,"height":1000})
        logout_context.add_init_script(SPEECH_EVENTS)
        logout_user = register(logout_context, origin, "退出清理测试")
        bedtime_page = logout_context.new_page()
        bedtime_page.goto(origin + "/bedtime.html?space=" + logout_user["space"]["id"], wait_until="networkidle")
        open_first(bedtime_page)
        bedtime_page.locator("#cache-story").click()
        bedtime_page.locator("#play-toggle").click()
        expect(bedtime_page.locator("#play-toggle")).to_have_attribute("aria-label","暂停朗读")
        logout_page = logout_context.new_page()
        logout_page.goto(origin, wait_until="networkidle")
        logout_page.locator("#profile-button").click()
        logout_page.get_by_role("button", name="退出登录", exact=True).click()
        expect(bedtime_page.locator("#bedtime-app")).not_to_be_visible()
        assert not offline_keys(bedtime_page)
        assert bedtime_page.locator("#reader-text").inner_text() == ""
        report("工作台真实退出广播停止晚安故事，并清除私人设备缓存")
        assert not errors, errors
        browser.close()


def run():
    with tempfile.TemporaryDirectory(prefix="zhixu-bedtime-browser-qa-") as data:
        server = FolioServer(("127.0.0.1",0), data_dir=data, public_dir=ROOT / "public", registration_mode="open", storage_persistence="persistent")
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
