"""Real desktop, direct upload-link and local sample checks.

Uses disposable real accounts/HTTP and browser WAV decoding/native playback.
The generated sine-wave fixture tests file handling, not voice quality. No live
cloud credentials, paid provider calls, microphone or personal recording is used.
Group eight injects an explicitly fake provider into a second isolated server;
HTTP authorization, cloning DB writes and private audio playback remain real.
Run: python scripts/desktop_story_qa.py
"""
import io
import math
import os
import re
import struct
import sys
import tempfile
import threading
import uuid
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer
import bedtime

PASSWORD = "QA-only-desktop-2026"
HEADERS = {"X-Requested-With": "Workspace"}


def report(message):
    print("PASS", message, flush=True)


def register(context, origin, name):
    email = f"desktop-{uuid.uuid4().hex[:10]}@example.com"
    response = context.request.post(origin + "/api/auth/register", data={"name": name, "email": email, "password": PASSWORD}, headers=HEADERS)
    assert response.status == 201, response.text()
    result = response.json()
    spaces = context.request.get(origin + "/api/spaces").json()["spaces"]
    return {"user": result["user"], "email": email, "space": spaces[0]}


def sample(seconds):
    result = io.BytesIO()
    with wave.open(result, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 180 * i / 24000))) for i in range(int(seconds * 24000))))
    return {"name": "QA-generated-sample.wav", "mimeType": "audio/wav", "buffer": result.getvalue()}


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")


class FakeQAProviders:
    """No external requests: generated test WAV and clearly labeled story."""
    voice_enabled = True
    search_enabled = True
    search_provider = "QA fake provider"

    def __init__(self):
        self.enroll_calls = []
        self.synth_calls = []
        self.search_calls = []
        self.audio = sample(3)["buffer"]

    def enroll(self, payload, preferred_name):
        self.enroll_calls.append((bedtime.pcm_sample(payload), preferred_name))
        return "QA-private-provider-voice", bedtime.VC_MODEL

    def synthesize(self, text, voice, model):
        self.synth_calls.append((text, voice, model))
        return self.audio

    def search(self, keyword):
        self.search_calls.append(keyword)
        return [{"id":"qa-web-moon","title":"QA 模拟联网月亮故事","text":"这是明确标注的 QA 模拟检索文本，只用于验证搜索与克隆音色的独立播放流程。月亮轻轻盖上云朵，测试故事在这里结束。","category":"测试","readMinutes":1,"isExcerpt":True,"source":{"kind":"web","label":"QA 模拟检索来源"}}]

    def delete_voice(self, voice):
        pass


def cloud_checks(origin, server, provider):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width":1440,"height":1050}, locale="zh-CN")
        account = register(context, origin, "QA 云接口流程测试")
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.goto(origin + "/bedtime.html", wait_until="networkidle")
        expect(page.locator(".story-card").first).to_be_visible()
        page.locator(".story-card").first.click()
        original_text = page.locator("#reader-text").inner_text()
        before_text_requests = len([url for _,url in requests if "/bedtime/search?" in url or "/bedtime/stories/" in url])
        page.locator("#open-voices").click()
        page.locator("#voice-name").fill("QA 模拟克隆声音")
        page.locator("#voice-file").set_input_files(sample(10))
        expect(page.locator("#sample-preview")).to_be_visible()
        expect(page.locator("#clone-submit")).to_be_disabled()
        page.locator("#voice-consent").check()
        expect(page.locator("#clone-submit")).to_be_enabled()
        with page.expect_response(lambda response: response.url.endswith("/bedtime/voices") and response.request.method == "POST") as created:
            page.locator("#clone-submit").click()
        assert created.value.status == 201, created.value.text()
        voice = created.value.json()["voice"]
        assert voice["state"] == "ready"
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        expect(page.locator("#voice-selector")).to_have_value(voice["id"])
        assert page.locator("#reader-text").inner_text() == original_text
        assert before_text_requests == len([url for _,url in requests if "/bedtime/search?" in url or "/bedtime/stories/" in url])
        assert provider.enroll_calls and provider.enroll_calls[0][0] == 10
        with server.db() as conn:
            row = conn.execute("SELECT * FROM bedtime_voices WHERE id=?", (voice["id"],)).fetchone()
            assert row["state"] == "ready" and row["user_id"] == account["user"]["id"] and row["space_id"] == account["space"]["id"]

        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET") as played:
            page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        assert played.value.status == 200
        assert played.value.headers["cache-control"] == "private, no-store"
        assert played.value.body() == provider.audio
        assert provider.synth_calls[0][0] in original_text
        assert provider.synth_calls[0][1:] == ("QA-private-provider-voice", bedtime.VC_MODEL)
        assert page.locator("#reader-text").inner_text() == original_text
        page.locator("#play-toggle").click()

        page.locator("#search-input").fill("QA 月亮")
        page.locator("#web-search").check()
        expect(page.locator(".story-card")).to_have_count(1)
        expect(page.locator(".story-card").first).to_contain_text("QA 模拟联网月亮故事")
        page.locator(".story-card").first.click()
        expect(page.locator("#reader-source")).to_contain_text("QA 模拟检索来源")
        assert provider.search_calls == ["QA 月亮"], provider.search_calls
        expect(page.locator("#voice-selector")).to_have_value(voice["id"])
        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET") as web_played:
            page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        assert web_played.value.status == 200
        assert "QA 模拟检索文本" in provider.synth_calls[-1][0]
        page.locator("#play-toggle").click()
        assert not errors, errors
        browser.close()
        report("明确模拟供应商：真实上传/同意/克隆入库与自动选音色；文本不重载、私人音频真实播放、QA 联网文本沿用同一音色（不验证真人克隆质量）")


def checks(origin):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width":1440,"height":1050}, locale="zh-CN", timezone_id="Asia/Shanghai")
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        context.add_init_script("localStorage.setItem('zhixu:bedtime:theme','day'); window.__qaRevoked=[]; const revoke=URL.revokeObjectURL.bind(URL); URL.revokeObjectURL=(url)=>{window.__qaRevoked.push(url);revoke(url);};")
        account_a = register(context, origin, "桌面测试 A")
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.goto(origin + "/bedtime.html?panel=voices", wait_until="networkidle")
        expect(page.locator("#voice-dialog")).to_be_visible()
        assert page.locator("#space-selector").input_value() == account_a["space"]["id"]
        page.locator("#close-voices").click()
        other = browser.new_context()
        account_b = register(other, origin, "桌面测试 B")
        other.close()
        member = context.request.post(origin + "/api/spaces/" + account_a["space"]["id"] + "/members", data={"email":account_b["email"],"role":"viewer"}, headers=HEADERS)
        assert member.ok, member.text()
        page.evaluate("data=>{sessionStorage.setItem('zhixu-space',data.space);sessionStorage.setItem('zhixu:space:'+data.uid,data.space);}", {"space":account_a["space"]["id"],"uid":account_b["user"]["id"]})
        context.request.post(origin + "/api/auth/logout", headers=HEADERS)

        # Both legacy and UID-scoped hints name a shared space where B is only
        # a viewer. Generic upload links choose B's own personal writable space.
        page.goto(origin + "/bedtime.html?panel=voices", wait_until="networkidle")
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        login_link = page.locator("#loading-screen a")
        parsed = parse_qs(urlparse(login_link.get_attribute("href")).query)
        assert parsed["next"] == ["/bedtime.html?panel=voices"], parsed
        login_link.click()
        expect(page.locator("#auth-email")).to_be_visible()
        page.locator("#auth-email").fill(account_b["email"])
        page.locator("#auth-password").fill(PASSWORD)
        page.locator("#auth-submit").click()
        expect(page).to_have_url(re.compile(r"/bedtime\.html\?panel=voices$"))
        expect(page.locator("#voice-dialog")).to_be_visible()
        assert page.locator("#space-selector").input_value() == account_b["space"]["id"]
        report("真实登录保留上传入口；旧账号空间提示不会阻止新账号打开自己的空间")

        page.locator("#dialog-share-link").click()
        shared = page.evaluate("navigator.clipboard.readText()")
        assert shared == origin + "/bedtime.html?panel=voices", shared
        expect(page.locator("#voice-file")).to_be_enabled()
        expect(page.locator("#clone-submit")).to_be_disabled()
        expect(page.locator("#clone-status")).to_contain_text("尚未连接")
        report("共享链接没有账号或空间标识；未配置云服务时仅允许本机选择录音")

        page.locator("#voice-file").set_input_files(sample(12))
        expect(page.locator("#sample-preview")).to_be_visible()
        expect(page.locator("#audio-duration-note")).to_contain_text("12.0 秒")
        page.wait_for_function("()=>document.querySelector('#voice-sample-player').readyState >= 1")
        sample_url = page.locator("#voice-sample-player").get_attribute("src")
        assert sample_url.startswith("blob:"), sample_url
        duration = page.locator("#voice-sample-player").evaluate("el=>el.duration")
        assert 11.9 <= duration <= 12.1, duration
        page.locator("#voice-sample-player").click(position={"x":25,"y":21})
        page.wait_for_function("()=>!document.querySelector('#voice-sample-player').paused")
        expect(page.locator("#clone-submit")).to_be_disabled()
        assert not [(method,url) for method,url in requests if method == "POST" and "/bedtime/voices" in url]
        (ROOT / "artifacts").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "artifacts" / "voice-upload-desktop.png"), full_page=False, animations="disabled")
        report("真实 12 秒 WAV 在浏览器解码并试听；无密钥不上传、不伪造克隆档案")

        page.locator("#close-voices").click()
        assert page.locator("#voice-sample-player").get_attribute("src") is None
        assert page.evaluate("document.querySelector('#voice-sample-player').paused")
        assert page.evaluate("url=>window.__qaRevoked.includes(url)", sample_url)
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        page.locator("#voice-file").set_input_files(sample(3))
        expect(page.locator("#clone-error")).to_contain_text("10–30 秒")
        expect(page.locator("#sample-preview")).not_to_be_visible()
        # Exercise the actual browser decoder and rendered WAV at both exact
        # boundaries, including 44.1-kHz decoder rounding of 24-kHz source PCM.
        for seconds in (10,30):
            page.locator("#voice-file").set_input_files(sample(seconds))
            expect(page.locator("#sample-preview")).to_be_visible()
            expect(page.locator("#audio-duration-note")).to_contain_text(f"{seconds}.0 秒")
            page.wait_for_function("()=>document.querySelector('#voice-sample-player').readyState >= 1")
            actual_duration = page.locator("#voice-sample-player").evaluate("el=>el.duration")
            assert abs(actual_duration - seconds) < 1 / 24000, actual_duration
            expect(page.locator("#clone-submit")).to_be_disabled()
        page.locator("#voice-file").set_input_files(sample(12))
        page.keyboard.press("Escape")
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        page.wait_for_timeout(150)
        assert page.locator("#voice-sample-player").get_attribute("src") is None
        report("关闭/Escape/异步解码清理试听；过短录音拒绝，真实解码的准确 10 秒与 30 秒均可预览")

        page.locator(".story-card").first.click()
        expect(page.locator("#reader")).to_be_visible()
        original_text = page.locator("#reader-text").inner_text()
        assert original_text.strip()
        before_search = len([url for _,url in requests if "/bedtime/search?" in url])
        page.locator("#open-voices").click()
        page.locator("#voice-file").set_input_files(sample(12))
        expect(page.locator("#sample-preview")).to_be_visible()
        page.locator("#close-voices").click()
        assert page.locator("#reader-text").inner_text() == original_text
        assert before_search == len([url for _,url in requests if "/bedtime/search?" in url])
        report("选择、试听人声保留当前故事，文本检索与声音入口互相独立")

        for width, height in [(1024,900),(1280,960),(1440,1050),(1920,1080),(320,720),(360,780),(390,844),(430,932),(844,390)]:
            page.set_viewport_size({"width":width,"height":height})
            no_overflow(page)
            if width >= 1024:
                player = page.locator("#player").bounding_box()
                main = page.locator("main").bounding_box()
                reader = page.locator("#reader").bounding_box()
                library = page.locator(".story-library").bounding_box()
                assert player["x"] >= main["x"] + main["width"] - 1
                assert reader["x"] > library["x"] + library["width"]
                assert page.locator("#player").evaluate("el=>getComputedStyle(el).position") == "sticky"
            else:
                assert page.locator("#player").evaluate("el=>getComputedStyle(el).position") == "fixed"
            page.locator("#player-voices").click()
            no_overflow(page)
            rect = page.locator("#voice-dialog").bounding_box()
            assert rect["width"] <= width and rect["height"] <= height
            page.locator("#close-voices").click()
        page.set_viewport_size({"width":1440,"height":1050})
        page.evaluate("scrollTo(0,0)")
        expect(page.locator("#toast")).not_to_be_visible(timeout=6000)
        (ROOT / "artifacts").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "artifacts" / "bedtime-desktop.png"), full_page=False, animations="disabled")
        report("1024–1920px PC 分列阅读与固定右侧播放器；320–430px 手机及横屏无溢出")

        page.locator("#open-voices").click()
        page.locator("#voice-file").set_input_files(sample(12))
        expect(page.locator("#sample-preview")).to_be_visible()
        revoke_url = page.locator("#voice-sample-player").get_attribute("src")
        sibling = context.new_page()
        sibling.goto(origin, wait_until="networkidle")
        sibling.evaluate("data=>localStorage.setItem('zhixu:session-event',JSON.stringify(data))", {"kind":"logout","userId":account_b["user"]["id"],"at":1})
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        assert page.locator("#voice-sample-player").get_attribute("src") is None
        assert page.evaluate("url=>window.__qaRevoked.includes(url)", revoke_url)
        assert page.locator("#reader-text").inner_text() == ""
        report("跨标签退出立即停止试听、释放 Blob 地址并清理当前故事")
        assert not errors, errors
        browser.close()


def run():
    # Never make real provider requests: disconnected first, injected fake next.
    for key in ("DASHSCOPE_API_KEY", "OPENSEARCH_API_KEY", "TAVILY_API_KEY"):
        os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="zhixu-desktop-story-qa-") as data:
        server = FolioServer(("127.0.0.1",0), data_dir=data, public_dir=ROOT / "public", registration_mode="open", storage_persistence="persistent")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            checks(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    with tempfile.TemporaryDirectory(prefix="zhixu-desktop-fake-provider-qa-") as data:
        server = FolioServer(("127.0.0.1",0), data_dir=data, public_dir=ROOT / "public", registration_mode="open", storage_persistence="persistent")
        server.bedtime_provider = provider = FakeQAProviders()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cloud_checks(f"http://127.0.0.1:{server.server_port}", server, provider)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    run()
