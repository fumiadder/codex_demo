"""Real desktop, direct upload-link and local sample checks.

Uses disposable real accounts/HTTP and browser WAV decoding/native playback.
The generated sine-wave fixture tests file handling, not voice quality. Native
MediaRecorder checks use Chromium's fake microphone, never a person's recording.
No live cloud credentials or paid provider calls are used.
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
import time
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

# Enforce the relevant iOS policy while retaining actual browser WAV decoding
# and native play(). Chromium alone does not enforce Safari's per-element
# permission. This probe checks a delayed reply cannot rely on a new element.
MEDIA_GESTURE_PROBE = """
(() => {
  let inClick = false, nextId = 1;
  const unlocked = new WeakSet(), ids = new WeakMap();
  const id = audio => { if (!ids.has(audio)) ids.set(audio, nextId++); return ids.get(audio); };
  window.__qaMedia = {calls: [], elements: [], denied: 0, sources: [], revoked: [], gains: [], contexts: []};
  const now = Date.now.bind(Date);
  window.__qaNowOffset = 0;
  Date.now = () => now() + window.__qaNowOffset;
  document.addEventListener('click', () => {
    inClick = true;
    // Browser event dispatch can checkpoint microtasks between listeners.
    // A task boundary ends this synthetic gesture before the delayed reply.
    setTimeout(() => { inClick = false; }, 0);
  }, true);
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function(...args) {
    const gesture = inClick;
    window.__qaMedia.calls.push({id: id(this), gesture, src: this.getAttribute('src'), rate: this.playbackRate});
    if (!window.__qaMedia.elements.includes(this)) window.__qaMedia.elements.push(this);
    if (!gesture && !unlocked.has(this)) {
      window.__qaMedia.denied++;
      return Promise.reject(new DOMException('QA Safari per-element gesture required', 'NotAllowedError'));
    }
    const operation = Promise.resolve(play.apply(this, args)).then(value => {
      if (gesture) unlocked.add(this);
      return value;
    });
    if (window.__qaMedia.holdNext) {
      window.__qaMedia.holdNext = false;
      return operation.then(value => new Promise(resolve => { window.__qaMedia.releaseUnlock = () => resolve(value); }));
    }
    return operation;
  };
  const source = AudioContext.prototype.createMediaElementSource;
  AudioContext.prototype.createMediaElementSource = function(audio) {
    window.__qaMedia.sources.push(id(audio));
    if (!window.__qaMedia.contexts.includes(this)) window.__qaMedia.contexts.push(this);
    return source.call(this, audio);
  };
  const gain = AudioContext.prototype.createGain;
  AudioContext.prototype.createGain = function(...args) {
    const node = gain.apply(this, args);
    window.__qaMedia.gains.push(node);
    return node;
  };
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = url => { window.__qaMedia.revoked.push(url); revoke(url); };
  // Only system speech callbacks are a fixture; cloud WAV media plays natively.
  Object.defineProperty(window, 'speechSynthesis', {configurable: true, value: {
    getVoices: () => [], addEventListener: () => {}, cancel: () => {},
    speak: utterance => queueMicrotask(() => utterance.onstart?.()),
  }});
})();
"""

# Instrument the real stream and recorder instead of manufacturing a recording
# result. A held native getUserMedia reply also exercises permission races.
MICROPHONE_PROBE = """
(() => {
  const microphone = window.__qaMicrophone = {calls: 0, streams: [], recorders: [], errors: [], revoked: [], blobs: new Map()};
  const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async constraints => {
    microphone.calls++;
    try {
      const stream = await getUserMedia(constraints);
      microphone.streams.push(stream);
      if (microphone.holdNext) {
        microphone.holdNext = false;
        await new Promise(resolve => { microphone.releasePermission = resolve; });
      }
      return stream;
    } catch (error) { microphone.errors.push(error.name); throw error; }
  };
  const start = MediaRecorder.prototype.start;
  MediaRecorder.prototype.start = function(...args) {
    microphone.recorders.push(this);
    this.__qaStarted = performance.now();
    this.__qaChunks = [];
    this.addEventListener('dataavailable', event => this.__qaChunks.push({after:performance.now()-this.__qaStarted, size:event.data.size}));
    return start.apply(this, args);
  };
  const stop = MediaRecorder.prototype.stop;
  MediaRecorder.prototype.stop = function(...args) {
    this.__qaStopped = performance.now();
    return stop.apply(this, args);
  };
  const create = URL.createObjectURL.bind(URL);
  URL.createObjectURL = blob => { const url = create(blob); microphone.blobs.set(url, blob); return url; };
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = url => { microphone.revoked.push(url); microphone.blobs.delete(url); revoke(url); };
  const now = Date.now.bind(Date);
  window.__qaNowOffset = 0;
  Date.now = () => now() + window.__qaNowOffset;
})();
"""


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
        self.synthesis_gate = None
        self.synthesis_started = threading.Event()
        self.enrollment_failure = None

    def enroll(self, payload, preferred_name):
        if self.enrollment_failure is not None:
            message, self.enrollment_failure = self.enrollment_failure, None
            raise bedtime.ProviderFailure(message)
        self.enroll_calls.append((bedtime.pcm_sample(payload), preferred_name))
        return "QA-private-provider-voice", bedtime.VC_MODEL

    def synthesize(self, text, voice, model):
        self.synth_calls.append((text, voice, model))
        self.synthesis_started.set()
        if self.synthesis_gate is not None:
            assert self.synthesis_gate.wait(10), "QA synthesis gate was not released"
        # Allow the click's transient activation to expire before audio arrives.
        time.sleep(0.25)
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
        context.add_init_script(MEDIA_GESTURE_PROBE)
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

        # Device speech must begin while its optional media unlock promise is
        # still pending. It then switches to the first cloud voice through a
        # change event, without depending on a new click's media permission.
        page.locator("#voice-selector").select_option("system:default")
        before_device_synthesis = len(provider.synth_calls)
        before_unlock_revokes = page.evaluate("window.__qaMedia.revoked.length")
        page.evaluate("window.__qaMedia.holdNext=true")
        page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        page.wait_for_function("()=>typeof window.__qaMedia.releaseUnlock==='function'")
        assert len(provider.synth_calls) == before_device_synthesis, ("device speech requested cloud synthesis", page.locator("#voice-selector").input_value(), errors)
        assert page.evaluate("window.__qaMedia.revoked.length") == before_unlock_revokes
        page.evaluate("window.__qaMedia.releaseUnlock()")
        page.wait_for_function("before=>window.__qaMedia.revoked.length===before+1", arg=before_unlock_revokes)
        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET") as played:
            page.locator("#voice-selector").select_option(voice["id"])
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        assert played.value.status == 200
        assert played.value.headers["cache-control"] == "private, no-store"
        assert played.value.body() == provider.audio
        assert provider.synth_calls[before_device_synthesis][0] in original_text
        assert provider.synth_calls[before_device_synthesis][1:] == ("QA-private-provider-voice", bedtime.VC_MODEL)
        assert page.locator("#reader-text").inner_text() == original_text
        media = page.evaluate("({calls:window.__qaMedia.calls, sources:window.__qaMedia.sources, denied:window.__qaMedia.denied, revoked:window.__qaMedia.revoked})")
        assert len(media["calls"]) == 3, media
        assert media["calls"][0]["gesture"] and not any(call["gesture"] for call in media["calls"][1:]), media
        assert all(call["id"] == media["calls"][0]["id"] for call in media["calls"]), media
        assert all(call["src"] in media["revoked"] for call in media["calls"][:2]), media
        assert media["sources"] == [media["calls"][0]["id"]] and media["denied"] == 0, media
        first_audio_id = media["calls"][0]["id"]
        first_audio_url = media["calls"][-1]["src"]
        # Playback rate modifies the live media element without synthesizing or
        # reloading the text. GainNode still controls volume on iOS.
        calls_before_rate = len(provider.synth_calls)
        page.locator("#speech-rate").select_option("0.7")
        assert page.evaluate("window.__qaMedia.elements[0].playbackRate") == 0.7
        assert len(provider.synth_calls) == calls_before_rate
        page.locator("#play-toggle").click()
        assert page.evaluate("window.__qaMedia.elements.every(audio=>audio.paused && !audio.hasAttribute('src'))")
        assert page.evaluate("url=>window.__qaMedia.revoked.includes(url)", first_audio_url)

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
        # A real media ended event starts the next sentence without another
        # click; its private WAV must reuse the same already unlocked element.
        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET") as next_sentence:
            page.wait_for_function("()=>document.querySelector('#story-progress').value>0")
        assert next_sentence.value.status == 200
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        assert "月亮轻轻盖上云朵" in provider.synth_calls[-1][0]
        media = page.evaluate("({calls:window.__qaMedia.calls, sources:window.__qaMedia.sources, denied:window.__qaMedia.denied})")
        assert all(call["id"] == first_audio_id for call in media["calls"]), media
        assert media["sources"] == [first_audio_id] and media["denied"] == 0, media
        page.locator("#play-toggle").click()
        assert page.evaluate("window.__qaMedia.elements.every(audio=>audio.paused && !audio.hasAttribute('src'))")
        assert not page.evaluate("Object.keys(localStorage).some(key=>/audio/i.test(key))")

        # Pausing a request still in flight must not attach or play its audio.
        before_audio_gets = len([url for method,url in requests if method == "GET" and "/bedtime/audio/" in url])
        provider.synthesis_gate = gate = threading.Event()
        provider.synthesis_started.clear()
        page.locator("#play-toggle").click()
        assert provider.synthesis_started.wait(5)
        with page.expect_response(lambda response: response.url.endswith("/bedtime/synthesize") and response.request.method == "POST"):
            page.locator("#play-toggle").click()
            gate.set()
        provider.synthesis_gate = None
        page.wait_for_timeout(150)
        assert before_audio_gets == len([url for method,url in requests if method == "GET" and "/bedtime/audio/" in url])
        assert page.evaluate("window.__qaMedia.elements.every(audio=>audio.paused && !audio.hasAttribute('src'))")

        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET"):
            page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        page.locator("#sleep-timer").select_option("30")
        page.evaluate("window.__qaNowOffset=30*60000-30000")
        page.wait_for_function("()=>window.__qaMedia.gains.at(-1).gain.value<0.6")
        assert page.evaluate("window.__qaMedia.gains.at(-1).gain.value") > 0
        page.evaluate("window.__qaNowOffset=30*60000+1000")
        expect(page.locator("#sleep-overlay")).to_be_visible()
        page.wait_for_function("()=>window.__qaMedia.contexts.every(context=>context.state==='closed')")
        assert page.evaluate("window.__qaMedia.elements.every(audio=>audio.paused && !audio.hasAttribute('src'))")
        page.evaluate("window.__qaNowOffset=0")
        page.locator("#wake-sleep").click()
        with page.expect_response(lambda response: "/bedtime/audio/" in response.url and response.request.method == "GET"):
            page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        media = page.evaluate("({calls:window.__qaMedia.calls, sources:window.__qaMedia.sources, denied:window.__qaMedia.denied})")
        assert len(media["sources"]) == 2 and media["sources"][0] != media["sources"][1], media
        assert media["calls"][-2]["gesture"] and not media["calls"][-1]["gesture"], media
        assert media["calls"][-2]["id"] == media["calls"][-1]["id"] == media["sources"][-1], media
        assert media["denied"] == 0, media
        sibling = context.new_page()
        sibling.goto(origin, wait_until="networkidle")
        sibling.evaluate("uid=>localStorage.setItem('zhixu:session-event',JSON.stringify({kind:'logout',userId:uid,at:Date.now()}))", account["user"]["id"])
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        assert page.evaluate("window.__qaMedia.elements.every(audio=>audio.paused && !audio.hasAttribute('src'))")
        assert page.evaluate("window.__qaMedia.calls.every(call=>window.__qaMedia.revoked.includes(call.src))")
        assert not errors, errors
        browser.close()
        report("明确模拟供应商：真实上传/同意/克隆入库与自动选音色；文本不重载、私人音频真实播放、QA 联网文本沿用同一音色（不验证真人克隆质量）")
        report("模拟 Safari 每元素手势限制：点击本机静音解锁、延迟私有音频沿用同元素与唯一媒体源；语速不重合成、暂停/换故事/退出清源与 Blob，不缓存私有音频（真实 iPhone 待设备验收）")
        report("真实句末自动继续；合成等待期间暂停不取音频；GainNode 睡眠渐弱、定时关闭 Context，唤醒后新元素重新点击解锁与异步播放")
        report("设备语音事件为 QA stub：慢静音解锁不阻塞设备朗读，change 切首个云音色无需新点击手势，真实私人 WAV 继续播放且文本保持")


def recording_checks(origin, server, provider):
    """Native fake-microphone recording through the unchanged HTTP clone route."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--use-fake-device-for-media-stream"],
        )
        context = browser.new_context(viewport={"width":390, "height":844}, locale="zh-CN")
        context.grant_permissions(["microphone"], origin=origin)
        context.add_init_script(MICROPHONE_PROBE)
        account = register(context, origin, "QA 页面麦克风录音")
        created_space = context.request.post(origin + "/api/spaces", data={"name":"QA 录音隔离空间"}, headers=HEADERS)
        assert created_space.status == 201, created_space.text()
        second_space = created_space.json()["space"]
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.goto(origin + "/bedtime.html?space=" + account["space"]["id"], wait_until="networkidle")
        page.locator(".story-card").first.click()
        original_text = page.locator("#reader-text").inner_text()
        page.locator("#open-voices").click()
        no_overflow(page)
        assert not page.locator("#voice-file").evaluate("el=>el.required")
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        before_enroll = len(provider.enroll_calls)

        def start_recording():
            before = page.evaluate("window.__qaMicrophone.recorders.length")
            page.locator("#voice-record-start").click()
            page.wait_for_function("before=>window.__qaMicrophone.recorders.length===before+1 && window.__qaMicrophone.recorders.at(-1).state==='recording'", arg=before)
            expect(page.locator("#voice-record-status")).to_contain_text("录音中")
            expect(page.locator("#voice-record-stop")).to_be_enabled()
            expect(page.locator("#clone-submit")).to_be_disabled()
            expect(page.locator("#voice-save-sample")).to_be_disabled()

        def tracks_ended():
            page.wait_for_function("()=>window.__qaMicrophone.streams.length>0 && window.__qaMicrophone.streams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")

        # A real two-second Opus recording must pass through native decoding and
        # fail duration validation, rather than simply accepting a timer label.
        start_recording()
        page.wait_for_timeout(2200)
        page.locator("#voice-record-stop").click()
        tracks_ended()
        expect(page.locator("#clone-error")).to_contain_text("10–30 秒")
        expect(page.locator("#sample-preview")).not_to_be_visible()
        expect(page.locator("#clone-submit")).to_be_disabled()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        assert len(provider.enroll_calls) == before_enroll
        report("Chromium 原生假麦克风和 MediaRecorder：不足 10 秒拒绝、停止即释放音轨，录制阶段没有克隆请求")

        start_recording()
        # Fake Chromium capture can produce fewer samples than wall time under
        # concurrent browser load. Follow the UI's 15–20-second advice, while
        # keeping validation on the actual decoded PCM duration, never a label.
        page.wait_for_timeout(16000)
        page.locator("#voice-record-stop").click()
        tracks_ended()
        expect(page.locator("#sample-preview")).to_be_visible(timeout=10000)
        page.wait_for_function("()=>document.querySelector('#voice-sample-player').readyState>=1")
        preview_url = page.locator("#voice-sample-player").get_attribute("src")
        assert preview_url.startswith("blob:"), preview_url
        duration = page.locator("#voice-sample-player").evaluate("audio=>audio.duration")
        assert 10 <= duration <= 20, (duration, page.evaluate("window.__qaMicrophone.recorders.at(-1).__qaChunks"))
        recorder = page.evaluate("""()=>{
          const recorder=window.__qaMicrophone.recorders.at(-1);
          return {stopped:recorder.__qaStopped-recorder.__qaStarted, chunks:recorder.__qaChunks};
        }""")
        assert recorder["chunks"][-1]["after"] >= recorder["stopped"] and recorder["chunks"][-1]["size"] > 0, recorder
        header = page.evaluate("""async url => {
          // Product CSP intentionally allows blob media, not blob fetch. Read
          // the actual Blob captured by the QA createObjectURL probe directly.
          const bytes = await window.__qaMicrophone.blobs.get(url).arrayBuffer();
          const view = new DataView(bytes);
          let signal=false;
          for(let offset=44;offset<bytes.byteLength;offset+=2) { if(view.getInt16(offset,true)!==0) {signal=true;break;} }
          return {riff:String.fromCharCode(...new Uint8Array(bytes,0,4)), channels:view.getUint16(22,true), rate:view.getUint32(24,true), bits:view.getUint16(34,true), signal};
        }""", preview_url)
        assert header == {"riff":"RIFF", "channels":1, "rate":24000, "bits":16, "signal":True}, header
        assert page.locator("#voice-file").input_value() == ""
        assert not [(method,url) for method,url in requests if method == "POST" and url.endswith("/bedtime/voices")]
        page.locator("#voice-name").fill("QA 页面直接录音")
        expect(page.locator("#clone-submit")).to_be_disabled()
        page.locator("#voice-consent").check()
        expect(page.locator("#clone-submit")).to_be_enabled()
        expect(page.locator("#voice-save-sample")).to_be_enabled()
        no_overflow(page)
        with tempfile.TemporaryDirectory(prefix="zhixu-recording-download-qa-") as download_dir:
            with page.expect_download() as saved:
                page.locator("#voice-save-sample").click()
            downloaded = saved.value
            assert downloaded.failure() is None
            assert downloaded.suggested_filename.startswith("知序录音-") and downloaded.suggested_filename.endswith(".wav"), downloaded.suggested_filename
            assert "QA 页面直接录音" not in downloaded.suggested_filename
            recording_file = Path(download_dir) / downloaded.suggested_filename
            downloaded.save_as(recording_file)
            recorded_bytes = recording_file.read_bytes()
            with wave.open(io.BytesIO(recorded_bytes), "rb") as wav:
                assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) == (1, 24000, 2, "NONE")
                downloaded_duration = wav.getnframes() / wav.getframerate()
                assert 10 <= downloaded_duration <= 20, downloaded_duration
                assert any(value[0] != 0 for value in struct.iter_unpack("<h", wav.readframes(wav.getnframes()))), "saved native recording is silent"
            assert abs(downloaded_duration - duration) < 1 / 24000
            assert len(provider.enroll_calls) == before_enroll
            assert not [(method,url) for method,url in requests if method == "POST" and url.endswith("/bedtime/voices")]

            # A real HTTP 502 from the isolated fake must not destroy the local
            # sample. The downloaded copy is retained independently of refresh.
            provider.enrollment_failure = "QA 模拟供应商暂时拒绝创建"
            with page.expect_response(lambda response: response.url.endswith("/bedtime/voices") and response.request.method == "POST") as rejected:
                page.locator("#clone-submit").click()
            assert rejected.value.status == 502, rejected.value.text()
            expect(page.locator("#clone-error")).to_contain_text("QA 模拟供应商暂时拒绝创建")
            expect(page.locator("#voice-save-sample")).to_be_enabled()
            expect(page.locator("#clone-submit")).to_be_enabled()
            assert page.locator("#voice-sample-player").get_attribute("src") == preview_url
            with page.expect_download() as saved_again:
                page.locator("#voice-save-sample").click()
            retry_file = Path(download_dir) / "after-rejected.wav"
            saved_again.value.save_as(retry_file)
            assert retry_file.read_bytes() == recorded_bytes

            page.reload(wait_until="networkidle")
            page.locator(".story-card").first.click()
            original_text = page.locator("#reader-text").inner_text()
            page.locator("#open-voices").click()
            expect(page.locator("#sample-preview")).not_to_be_visible()
            expect(page.locator("#voice-save-sample")).to_be_disabled()
            assert page.evaluate("window.__qaMicrophone.calls") == 0
            page.locator("#voice-file").set_input_files(recording_file)
            expect(page.locator("#sample-preview")).to_be_visible()
            expect(page.locator("#voice-save-sample")).to_be_enabled()
            page.wait_for_function("()=>document.querySelector('#voice-sample-player').readyState>=1")
            preview_url = page.locator("#voice-sample-player").get_attribute("src")
            assert abs(page.locator("#voice-sample-player").evaluate("audio=>audio.duration") - downloaded_duration) < 1 / 24000
            assert page.evaluate("window.__qaMicrophone.calls") == 0
        page.locator("#voice-name").fill("QA 页面直接录音")
        page.locator("#voice-consent").check()
        with page.expect_response(lambda response: response.url.endswith("/bedtime/voices") and response.request.method == "POST") as created:
            page.locator("#clone-submit").click()
        assert created.value.status == 201, created.value.text()
        voice = created.value.json()["voice"]
        assert voice["state"] == "ready"
        assert len(provider.enroll_calls) == before_enroll + 1
        assert 10 <= provider.enroll_calls[-1][0] <= 20
        assert re.fullmatch(r"bedtime[0-9a-f]{8}", provider.enroll_calls[-1][1])
        assert voice["name"] == "QA 页面直接录音"
        assert page.evaluate("window.__qaMicrophone.calls") == 0
        expect(page.locator("#voice-selector")).to_have_value(voice["id"])
        assert page.locator("#reader-text").inner_text() == original_text
        assert page.evaluate("url=>window.__qaMicrophone.revoked.includes(url)", preview_url)
        with server.db() as conn:
            row = conn.execute("SELECT * FROM bedtime_voices WHERE id=?", (voice["id"],)).fetchone()
            assert row["user_id"] == account["user"]["id"] and row["space_id"] == account["space"]["id"] and row["state"] == "ready" and row["name"] == "QA 页面直接录音"
        report("原生麦克风录音可下载为非静音 24kHz/单声道/PCM16 WAV；模拟创建失败保留同一可下载素材，刷新后上传下载文件并授权创建，无需再次请求麦克风，当前故事不重载")

        # Keep the real 30-second boundary. Encoded tail padding must not cause
        # the recording's automatic stop to reject its own otherwise valid WAV.
        page.locator("#open-voices").click()
        start_recording()
        expect(page.locator("#sample-preview")).to_be_visible(timeout=35000)
        tracks_ended()
        page.wait_for_function("()=>document.querySelector('#voice-sample-player').readyState>=1")
        auto_duration = page.locator("#voice-sample-player").evaluate("audio=>audio.duration")
        assert 10 <= auto_duration <= 30, (auto_duration, page.evaluate("window.__qaMicrophone.recorders.at(-1).__qaChunks"))
        expect(page.locator("#voice-save-sample")).to_be_enabled()
        assert len(provider.enroll_calls) == before_enroll + 1
        old_preview = page.locator("#voice-sample-player").get_attribute("src")
        start_recording()
        assert page.evaluate("url=>window.__qaMicrophone.revoked.includes(url)", old_preview)
        page.locator("#close-voices").click()
        tracks_ended()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        report("原生录制达到 30 秒自动停止并释放麦克风，编码尾差裁切后仍可本机预览，未点击创建不提交")

        page.locator("#open-voices").click()
        start_recording()
        page.locator("#close-voices").click()
        tracks_ended()
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        expect(page.locator("#clone-submit")).to_be_disabled()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        page.evaluate("window.__qaMicrophone.holdNext=true")
        page.locator("#voice-record-start").click()
        page.wait_for_function("()=>typeof window.__qaMicrophone.releasePermission==='function'")
        page.locator("#close-voices").click()
        page.evaluate("window.__qaMicrophone.releasePermission()")
        tracks_ended()
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        expect(page.locator("#clone-submit")).to_be_disabled()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        page.locator("#voice-file").set_input_files(sample(12))
        expect(page.locator("#sample-preview")).to_be_visible()
        expect(page.locator("#voice-save-sample")).to_be_enabled()
        prepared_url = page.locator("#voice-sample-player").get_attribute("src")
        page.locator("#space-selector").select_option(second_space["id"])
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        assert page.evaluate("url=>window.__qaMicrophone.revoked.includes(url)", prepared_url)
        page.locator("#space-selector").select_option(account["space"]["id"])
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        start_recording()
        page.locator("#space-selector").select_option(second_space["id"])
        tracks_ended()
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        assert page.locator("#voice-name").input_value() == ""
        expect(page.locator("#clone-submit")).to_be_disabled()
        expect(page.locator("#voice-save-sample")).to_be_disabled()
        page.locator("#close-voices").click()
        report("关闭与切换空间释放真实音轨；迟到的 getUserMedia 回复也立即停止，旧录音不出现在重开弹窗或其他空间")

        if not page.locator("#sleep-controls").evaluate("details=>details.open"):
            page.locator("#sleep-controls > summary").click()
        page.locator("#sleep-timer").select_option("30")
        page.locator("#open-voices").click()
        start_recording()
        page.evaluate("window.__qaNowOffset=30*60000+1000")
        expect(page.locator("#sleep-overlay")).to_be_visible()
        tracks_ended()
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        page.evaluate("window.__qaNowOffset=0")
        page.locator("#wake-sleep").click()
        page.locator("#open-voices").click()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        start_recording()
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        tracks_ended()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        page.locator("#close-voices").click()
        report("定时睡眠及 pagehide 事件停止真实录音并释放音轨，未完成素材不保留为可提交预览")

        # Chromium's permission denial is real; the provider stays the explicit
        # isolated fake and the legacy WAV upload remains usable afterwards.
        cdp = context.new_cdp_session(page)
        target = cdp.send("Target.getTargetInfo")["targetInfo"]
        permissions = browser.new_browser_cdp_session()
        permissions.send("Browser.setPermission", {"permission":{"name":"microphone"}, "setting":"denied", "origin":origin, "browserContextId":target["browserContextId"]})
        page.locator("#open-voices").click()
        page.locator("#voice-record-start").click()
        page.wait_for_function("()=>window.__qaMicrophone.errors.includes('NotAllowedError')")
        expect(page.locator("#voice-record-status")).to_contain_text("麦克风未获授权")
        expect(page.locator("#voice-file")).to_be_enabled()
        page.locator("#voice-file").set_input_files(sample(12))
        expect(page.locator("#sample-preview")).to_be_visible()
        page.locator("#voice-name").fill("QA 拒绝麦克风后上传")
        page.locator("#voice-consent").check()
        with page.expect_response(lambda response: response.url.endswith("/bedtime/voices") and response.request.method == "POST") as uploaded:
            page.locator("#clone-submit").click()
        assert uploaded.value.status == 201, uploaded.value.text()
        assert provider.enroll_calls[-1][0] == 12
        assert uploaded.value.json()["voice"]["name"] == "QA 拒绝麦克风后上传"
        assert len(provider.enroll_calls) == before_enroll + 2
        permissions.send("Browser.setPermission", {"permission":{"name":"microphone"}, "setting":"granted", "origin":origin, "browserContextId":target["browserContextId"]})
        page.locator("#open-voices").click()
        start_recording()
        sibling = context.new_page()
        sibling.goto(origin, wait_until="networkidle")
        sibling.evaluate("uid=>localStorage.setItem('zhixu:session-event',JSON.stringify({kind:'logout',userId:uid,at:Date.now()}))", account["user"]["id"])
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        tracks_ended()
        expect(page.locator("#sample-preview")).not_to_be_visible()
        assert len(provider.enroll_calls) == before_enroll + 2
        assert not errors, errors
        browser.close()
        report("Chromium 真实拒绝麦克风权限有明确提示；既有文件上传/试听/授权/克隆路线仍可用，无真实百炼请求")
        report("跨标签退出停止进行中的真实录音，释放所有麦克风音轨，清理录音弹窗与未完成素材")


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
            recording_checks(f"http://127.0.0.1:{server.server_port}", server, provider)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    run()
