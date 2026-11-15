"""Disposable browser integration checks for the warm bedtime story module.

All accounts, permissions, HTTP requests, generated-story persistence and
WebAudio nodes are real. AI generation uses an explicitly fake QA provider on
a second isolated server; only device speech callbacks and elapsed wall time
are controlled for deterministic headless testing. No paid provider, live key,
microphone or personal recording is used. Preview screenshots are taken only
from the disconnected server and original stories, never from mock AI output.
These checks do not assess children's sleep, voice quality or mobile lockscreen
background execution. Run: python scripts/warm_story_qa.py
"""
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer
import bedtime

PASSWORD = "QA-only-warm-story-2026"
HEADERS = {"X-Requested-With": "Workspace"}
CATEGORIES = ["儿童睡前", "治愈温柔", "小动物", "公主冒险", "成长勇气", "亲情暖心"]
PARAMETERS = {"ageGroup": "3-6", "category": "儿童睡前", "durationMinutes": 1,
              "style": "healing", "moral": True, "blockScary": True,
              "protagonist": "cat", "interactive": True}

# Speech events are the only substitute for a device's unavailable Linux TTS.
# The AudioContext wrappers observe real source nodes without replacing them.
EVENT_PROBES = r"""(() => {
  const now = Date.now.bind(Date);
  window.__qaClock = {offset:0};
  Date.now = () => now() + window.__qaClock.offset;
  localStorage.setItem('zhixu:bedtime:theme', 'day');
  window.__qaSpeech = {calls:[],current:null,canceled:0};
  window.SpeechSynthesisUtterance = class {
    constructor(text) { this.text=text; this.voice=null; this.rate=1; this.volume=1; }
  };
  const voices=[{name:'QA event voice A',lang:'zh-CN',localService:true},
                {name:'QA event voice B',lang:'zh-CN',localService:true}];
  Object.defineProperty(window,'speechSynthesis',{configurable:true,value:{
    getVoices:()=>voices,addEventListener:()=>{},
    cancel:()=>{window.__qaSpeech.canceled++;window.__qaSpeech.current=null;},
    speak:utterance=>{
      window.__qaSpeech.current=utterance;
      window.__qaSpeech.calls.push({text:utterance.text,voice:utterance.voice?.name||'default',rate:utterance.rate,volume:utterance.volume});
      queueMicrotask(()=>utterance.onstart?.({}));
    }
  }});
  window.__qaSpeech.end=()=>{
    const utterance=window.__qaSpeech.current;
    if(!utterance)return false;
    window.__qaSpeech.current=null;
    utterance.onend?.({});return true;
  };
  window.__qaAudio={starts:0,stops:0,disconnects:0,kinds:[],contexts:[]};
  for(const method of ['createBufferSource','createOscillator']){
    const create=AudioContext.prototype[method];
    AudioContext.prototype[method]=function(...args){
      if(!window.__qaAudio.contexts.includes(this))window.__qaAudio.contexts.push(this);
      const source=create.apply(this,args);
      const start=source.start.bind(source),stop=source.stop.bind(source),disconnect=source.disconnect.bind(source);
      source.start=(...values)=>{window.__qaAudio.starts++;window.__qaAudio.kinds.push(method);return start(...values);};
      source.stop=(...values)=>{window.__qaAudio.stops++;return stop(...values);};
      source.disconnect=(...values)=>{window.__qaAudio.disconnects++;return disconnect(...values);};
      return source;
    };
  }
})();
"""


def report(message):
    print("PASS", message, flush=True)


def register(context, origin, name):
    email = f"warm-story-{uuid.uuid4().hex[:10]}@example.com"
    response = context.request.post(origin + "/api/auth/register", data={"name": name, "email": email, "password": PASSWORD}, headers=HEADERS)
    assert response.status == 201, response.text()
    spaces = context.request.get(origin + "/api/spaces").json()["spaces"]
    return {"user": response.json()["user"], "email": email, "space": spaces[0]}


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll('*')].map(el=>({tag:el.tagName,id:el.id,right:el.getBoundingClientRect().right})).filter(el=>el.right>innerWidth+1)})")


def original_story(page):
    expect(page.locator(".story-card").first).to_be_visible()
    page.locator(".story-card").first.click()
    expect(page.locator("#reader")).to_be_visible()
    return page.locator("#reader-text").text_content()


def text_reads(requests):
    return len([url for method, url in requests if method == "GET" and ("/bedtime/search?" in url or "/bedtime/stories/" in url)])


def ready_page(context, origin, space=None, errors=None, requests=None):
    page = context.new_page()
    if errors is not None:
        page.on("pageerror", lambda error: errors.append(str(error)))
    if requests is not None:
        page.on("request", lambda request: requests.append((request.method, request.url)))
    page.goto(origin + "/bedtime.html" + ("?space=" + space if space else ""), wait_until="networkidle")
    expect(page.locator("#bedtime-app")).to_be_visible()
    expect(page.locator(".story-card").first).to_be_visible()
    return page


def set_preferences(page, parameters):
    page.locator("#generate-age").select_option(parameters["ageGroup"])
    page.locator("#generate-category").select_option(parameters["category"])
    page.locator("#generate-duration").select_option(str(parameters["durationMinutes"]))
    if not page.locator(".generator-preferences").evaluate("el=>el.open"):
        page.locator(".generator-preferences summary").click()
    page.locator("#generate-style").select_option(parameters["style"])
    page.locator("#generate-protagonist").select_option(parameters["protagonist"])
    for key, selector in [("moral", "#generate-moral"), ("blockScary", "#generate-block-scary"), ("interactive", "#generate-interactive")]:
        page.locator(selector).set_checked(parameters[key])


def generate(page, parameters):
    set_preferences(page, parameters)
    with page.expect_response(lambda response: response.url.endswith("/bedtime/generate") and response.request.method == "POST") as generated:
        page.locator("#generate-submit").click()
    assert generated.value.status == 200, generated.value.text()
    story = generated.value.json()["story"]
    expect(page.locator("#reader-title")).to_have_text(story["title"])
    return story


class FakeQAGenerationProvider:
    """Clearly fake AI text fixtures; never calls an external service."""
    generation_enabled = True
    voice_enabled = False
    search_enabled = False
    search_provider = "local"

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_next = False

    def generate(self, parameters):
        self.calls.append(dict(parameters))
        if self.block_next:
            self.block_next = False
            self.started.set()
            assert self.release.wait(15), "QA generation boundary was not released"
        count, total = (3, 200) if parameters["durationMinutes"] == 1 else (9, 1100)
        body_size = total - len(bedtime.SLEEP_ENDING) - 2 * count
        base, remainder = divmod(body_size, count)
        paragraphs = []
        for index in range(count):
            size = base + (1 if index < remainder else 0)
            fixture = f"QA 模拟第{index + 1}段。" + "云朵轻轻停在床边，灯光柔柔地陪着小兔子。" * 12
            paragraphs.append(fixture[:size - 1] + "。")
        return {"title": f"QA 模拟故事 {len(self.calls)}", "paragraphs": paragraphs,
                "questions": [{"afterParagraph": 0, "question": "QA 温和问题：你想给这朵云送一点什么温暖？可以在心里想一想。"}] if parameters["interactive"] else []}

    def hold_next(self):
        self.started.clear()
        self.release.clear()
        self.block_next = True


def baseline_checks(origin):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width":1440,"height":1080}, locale="zh-CN", timezone_id="Asia/Shanghai")
        context.add_init_script(EVENT_PROBES)
        account = register(context, origin, "暖光原创预览")
        errors, requests = [], []
        page = ready_page(context, origin, account["space"]["id"], errors, requests)
        expect(page.locator("#generate-submit")).to_be_disabled()
        expect(page.locator("#generation-status")).to_contain_text("待连接")
        disabled = context.request.post(origin + f'/api/spaces/{account["space"]["id"]}/bedtime/generate', data=PARAMETERS, headers=HEADERS)
        assert disabled.status == 503, disabled.text()
        assert page.locator("#generate-category option").all_text_contents() == CATEGORIES
        assert page.locator("#generate-duration option").evaluate_all("els=>els.map(el=>el.value)") == ["1", "5"]
        original_story(page)
        expect(page.locator("#reader-source")).to_contain_text("知序原创")
        home = context.new_page()
        home.goto(origin + "/?area=life", wait_until="networkidle")
        expect(home.get_by_role("heading", name="睡前小故事", exact=True)).to_be_visible()
        home.close()
        report("1/11 无密钥时真实生成 API 返回 503、界面明确待连接；生活卡片及六类/1与5分钟入口可用，原创可阅读")

        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for width, height in [(320,760),(390,844),(430,932),(844,390),(1440,1080)]:
            page.set_viewport_size({"width":width,"height":height})
            no_overflow(page)
            page.locator("#player-voices").click()
            no_overflow(page)
            dialog = page.locator("#voice-dialog").bounding_box()
            assert dialog["width"] <= width and dialog["height"] <= height
            page.locator("#close-voices").click()
        page.set_viewport_size({"width":1440,"height":1080})
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(artifacts / "bedtime-warm-desktop.png"), full_page=False, animations="disabled")
        page.set_viewport_size({"width":390,"height":844})
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(artifacts / "bedtime-warm-mobile.png"), full_page=False, animations="disabled")
        report("2/11 原创真实页面 320/390/430 手机、横屏及 PC1440无横向溢出，上传弹窗均在视口内；无模拟AI截图")

        style = page.locator("#reader-text").evaluate("el=>{const s=getComputedStyle(el);return {size:parseFloat(s.fontSize),weight:parseInt(s.fontWeight),height:parseFloat(s.lineHeight),spacing:parseFloat(s.letterSpacing),color:s.color,font:s.fontFamily}}")
        assert style["weight"] >= 400 and style["height"] / style["size"] >= 1.8 and style["spacing"] >= 1.9, style
        assert page.locator("html").evaluate("el=>getComputedStyle(el).getPropertyValue('--cream').trim().toLowerCase()") == "#fff6e6"
        assert style["color"] == "rgb(74, 63, 53)", style
        loaded_fonts = page.evaluate("async()=>{const faces=await document.fonts.load('400 20px \"Zhixu Sleep Sans\"','小猫暖光');return faces.map(face=>({status:face.status,weight:face.weight}));}")
        assert loaded_fonts == [{"status":"loaded","weight":"400 500"}], loaded_fonts
        font_response = context.request.get(origin + "/fonts/zhixu-sleep-sans-sc-v1.woff2")
        assert font_response.status == 200 and len(font_response.body()) > 1_000_000
        assert any(url.endswith("/fonts/zhixu-sleep-sans-sc-v1.woff2") for _,url in requests)
        page.locator("#theme-toggle").click()
        page.locator("#theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "night")
        night_size = page.locator("#reader-text").evaluate("el=>parseFloat(getComputedStyle(el).fontSize)")
        assert abs(night_size / style["size"] - 1.1) <= 0.005, (style, night_size)
        page.screenshot(path=str(artifacts / "bedtime-warm-night.png"), full_page=False, animations="disabled")
        assert not [url for _,url in requests if not url.startswith(origin)], requests
        report("3/11 奶黄与棕灰正文、正文400以上字重/1.8行高/2px字距、夜间字号+10%；页面字体资源同站加载")

        page.locator("#player-voices").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#voice-dialog")).to_be_visible()
        for _ in range(16):
            page.keyboard.press("Tab")
            # Chromium may tab into browser chrome and report body as the
            # activeElement. The modal must still exclude background controls.
            assert page.evaluate("document.activeElement===document.body || document.querySelector('#voice-dialog').contains(document.activeElement)"), "modal allowed a background control to receive focus"
        page.keyboard.press("Escape")
        expect(page.locator("#voice-dialog")).not_to_be_visible()
        assert page.evaluate("document.activeElement.id") == "player-voices"
        page.emulate_media(reduced_motion="reduce")
        page.locator("#play-toggle").click()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        animation = page.evaluate("[...document.querySelectorAll('body,.reader,.moon-scene')].flatMap(el=>[getComputedStyle(el).animationName,getComputedStyle(el,'::before').animationName])")
        assert all(value == "none" for value in animation), animation
        page.locator("#play-toggle").click()
        assert not errors, errors
        report("4/11 键盘进入/Tab限制/Escape返回上传按钮；reduce-motion关闭呼吸与装饰动效，朗读仍可控制（设备语音事件为QA stub）")
        browser.close()


def generation_checks(origin, provider):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width":1440,"height":1080}, locale="zh-CN", timezone_id="Asia/Shanghai")
        context.add_init_script(EVENT_PROBES)
        account = register(context, origin, "QA 模拟生成流程")
        page = ready_page(context, origin, account["space"]["id"])
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append((request.method, request.url)))
        first_params = {**PARAMETERS, "ageGroup":"7-10", "category":"公主冒险", "style":"fantasy", "moral":False, "protagonist":"rabbit"}
        reads = text_reads(requests)
        story = generate(page, first_params)
        assert provider.calls[-1] == first_params, provider.calls[-1]
        assert story["generationParameters"] == first_params
        assert 150 <= len(story["text"]) <= 250 and story["readMinutes"] == 1
        assert story["text"].endswith(bedtime.SLEEP_ENDING) and story["text"].count(bedtime.SLEEP_ENDING) == 1
        assert story["source"]["kind"] == "generated"
        assert text_reads(requests) == reads, requests
        assert page.locator("#reader-text").text_content().count(bedtime.SLEEP_ENDING) == 1
        report("5/11 明确模拟AI供应商：真实POST传完整年龄/类型/时长/风格/道理/恐怖屏蔽/主角/互动参数；1分钟文本独立显示，固定结尾仅一次且不重搜")

        expect(page.locator("#paragraph-position")).to_contain_text("第 1 / 4 段")
        first = page.locator("#reader-text .reading-paragraph:visible").inner_text()
        page.locator("#next-paragraph").click()
        expect(page.locator("#paragraph-position")).to_contain_text("第 2 / 4 段")
        assert page.locator("#reader-text .reading-paragraph:visible").inner_text() != first
        page.locator("#prev-paragraph").click()
        expect(page.locator("#paragraph-position")).to_contain_text("第 1 / 4 段")
        page.locator("#speech-rate").select_option("0.7")
        page.locator("#play-toggle").click()
        expect(page.locator("body")).to_have_class(re.compile(r".*\bis-reading\b.*"))
        assert page.evaluate("document.body.classList.contains('warm-light')")
        assert page.evaluate("window.__qaSpeech.calls.at(-1).rate") == 0.7
        question_waited = False
        for _ in range(12):
            if page.locator("#story-question").is_visible():
                question_waited = True
                break
            assert page.evaluate("window.__qaSpeech.end()")
            page.wait_for_timeout(40)
        assert question_waited, "gentle question did not pause at the first paragraph"
        expect(page.locator("#story-question-text")).to_contain_text("QA 温和问题")
        before_continue = page.evaluate("window.__qaSpeech.calls.length")
        page.wait_for_timeout(100)
        assert page.evaluate("window.__qaSpeech.calls.length") == before_continue
        page.locator("#continue-question").click()
        expect(page.locator("#story-question")).not_to_be_visible()
        page.wait_for_function("value=>window.__qaSpeech.calls.length>value", arg=before_continue)
        expect(page.locator("#paragraph-position")).to_contain_text("第 2 / 4 段")
        body = page.locator("#reader-text").text_content()
        reads = text_reads(requests)
        page.locator("#voice-selector").select_option("system:1")
        assert page.locator("#reader-text").text_content() == body and text_reads(requests) == reads
        assert page.evaluate("window.__qaSpeech.calls.at(-1).voice") == "QA event voice B"
        page.locator("#read-aloud-switch").uncheck()
        expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "开始朗读")
        page.locator("#read-aloud-switch").check()
        report("6/11 实际逐段上/下与逐句事件推进；温和问题暂停等继续，慢速0.7倍/朗读开关/暖灯有效；切音色保留全文且不重搜（设备语音事件QA stub）")

        page.locator("#favorite-story").click()
        expect(page.locator("#favorite-story")).to_contain_text("已收藏")
        favorite = context.request.get(origin + f'/api/spaces/{account["space"]["id"]}/bedtime/favorites').json()["favorites"][0]
        assert favorite["text"] == story["text"] and favorite["questions"] == story["questions"]
        page.reload(wait_until="networkidle")
        expect(page.locator("#resume-last-story")).to_be_visible()
        page.locator("#resume-last-story").click()
        expect(page.locator("#reader-title")).to_have_text(story["title"])
        assert page.locator("#reader-text").text_content().count(bedtime.SLEEP_ENDING) == 1
        report("7/11 生成故事真实收藏保留参数/问题与完整结尾；真实播放历史刷新后可接着上次故事")

        second_params = {**PARAMETERS, "category":"亲情暖心", "durationMinutes":5, "style":"realistic", "protagonist":"child", "blockScary":False, "interactive":False}
        long_story = generate(page, second_params)
        assert provider.calls[-1] == second_params, provider.calls[-1]
        assert long_story["generationParameters"] == second_params
        assert 1001 <= len(long_story["text"]) <= 1250 and long_story["readMinutes"] == 5
        assert long_story["questions"] == []
        assert page.locator("#reader-text").text_content().count(bedtime.SLEEP_ENDING) == 1
        expect(page.locator("#paragraph-position")).to_contain_text("第 1 / 10 段")
        report("8/11 明确模拟AI：5分钟分级与另一组写实/小朋友/无互动参数真实POST，长篇正文与结尾保持分离")

        readonly = browser.new_context(viewport={"width":390,"height":844})
        readonly.add_init_script(EVENT_PROBES)
        viewer = register(readonly, origin, "QA 只读访问者")
        shared = context.request.post(origin + f'/api/spaces/{account["space"]["id"]}/members', data={"email":viewer["email"],"role":"viewer"}, headers=HEADERS)
        assert shared.ok, shared.text()
        viewer_page = ready_page(readonly, origin, account["space"]["id"])
        expect(viewer_page.locator("#generate-submit")).to_be_disabled()
        denied = readonly.request.post(origin + f'/api/spaces/{account["space"]["id"]}/bedtime/generate', data=PARAMETERS, headers=HEADERS)
        assert denied.status == 403, denied.text()
        original_story(viewer_page)
        viewer_page.locator("#play-toggle").click()
        expect(viewer_page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
        readonly.close()
        report("9/11 真实viewer共享访问可读原创/设备朗读，生成按钮禁用且服务端POST强制403")

        page.locator("#noise-kind").select_option("piano")
        for minutes in (30,60,90):
            page.locator("#sleep-timer").select_option(str(minutes))
            starts = page.evaluate("window.__qaAudio.starts")
            page.locator("#play-toggle").click()
            expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "暂停朗读")
            page.wait_for_function("value=>window.__qaAudio.starts>value", arg=starts)
            stops = page.evaluate("window.__qaAudio.stops")
            page.evaluate("minutes=>{window.__qaClock.offset+=(minutes*60+1)*1000;document.dispatchEvent(new Event('visibilitychange'));}", minutes)
            expect(page.locator("#sleep-overlay")).to_be_visible()
            expect(page.locator("#play-toggle")).to_have_attribute("aria-label", "开始朗读")
            assert page.evaluate("window.__qaAudio.stops") > stops
            page.wait_for_function("() => window.__qaAudio.contexts.every(context=>context.state==='closed')")
            assert page.evaluate("!navigator.mediaSession || navigator.mediaSession.metadata === null")
            assert page.evaluate("document.activeElement.id") == "wake-sleep"
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "wake-sleep"
            page.locator("#wake-sleep").click()
            expect(page.locator("#sleep-overlay")).not_to_be_visible()
            assert page.evaluate("document.activeElement.id") == "play-toggle"
        assert page.evaluate("window.__qaAudio.kinds.length>0 && window.__qaAudio.disconnects>0")
        report("10/11 真实 WebAudio 钢琴节点启动/停止清理；仅Date.now快进，30/60/90分钟真实截止处理停止朗读/背景音、休眠变暗与唤醒焦点")

        second_space = context.request.post(origin + "/api/spaces", data={"name":"QA 独立第二空间"}, headers=HEADERS)
        assert second_space.status == 201, second_space.text()
        sid2 = second_space.json()["space"]["id"]
        # New load obtains the real authorized space list; fake AI request blocks
        # in the provider while the real browser changes space or logs out.
        page.reload(wait_until="networkidle")
        provider.hold_next()
        page.locator("#generate-submit").click()
        deadline = time.monotonic() + 10
        while not provider.started.is_set() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert provider.started.is_set(), "generation never reached the real HTTP provider adapter"
        page.locator("#space-selector").select_option(sid2)
        expect(page.locator("#reader")).not_to_be_visible()
        provider.release.set()
        page.wait_for_timeout(300)
        expect(page.locator("#reader")).not_to_be_visible()
        assert page.locator("#reader-text").text_content() == ""
        expect(page.locator("#generate-submit")).to_be_enabled()

        original_story(page)
        page.locator("#interactive-questions").check()
        page.locator("#play-toggle").click()
        for _ in range(30):
            if page.locator("#story-question").is_visible():
                break
            assert page.evaluate("window.__qaSpeech.end()")
            page.wait_for_timeout(40)
        expect(page.locator("#story-question")).to_be_visible()
        assert page.locator("#reader-title").text_content().strip()
        assert page.locator("#story-question-text").text_content().strip()
        assert page.locator("#voice-selector option").count() > 1
        page.locator("#read-aloud-switch").uncheck()
        page.locator("#read-aloud-switch").check()
        page.locator("#sleep-timer").select_option("30")
        provider.hold_next()
        page.locator("#generate-submit").click()
        deadline = time.monotonic() + 10
        while not provider.started.is_set() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert provider.started.is_set()
        sibling = context.new_page()
        sibling.goto(origin, wait_until="networkidle")
        logged_out = context.request.post(origin + "/api/auth/logout", headers=HEADERS)
        assert logged_out.ok
        sibling.evaluate("uid=>localStorage.setItem('zhixu:session-event',JSON.stringify({kind:'logout',userId:uid,at:Date.now()}))", account["user"]["id"])
        expect(page.locator("#bedtime-app")).not_to_be_visible()
        provider.release.set()
        page.evaluate("window.__qaClock.offset+=31*60*1000")
        page.wait_for_timeout(600)
        assert page.locator("#reader-text").text_content() == ""
        assert page.locator("#reader-title").text_content() == ""
        assert page.locator("#story-question-text").text_content() == ""
        assert page.locator("#space-selector option").count() == 0
        assert page.locator("#voice-selector option").count() == 1
        expect(page.locator("#sleep-overlay")).not_to_be_visible()
        assert not page.locator("#bedtime-app").evaluate("el=>el.inert")
        assert not errors, errors
        report("11/11 明确模拟AI延迟：真实生成HTTP进行时切空间/退出，旧回包不写DOM；退出清旧标题/问题/选择项，旧计时不能再弹休眠，服务端复核会话")
        browser.close()


def run():
    for key in ("DASHSCOPE_API_KEY", "OPENSEARCH_API_KEY", "TAVILY_API_KEY"):
        os.environ.pop(key, None)
    for prefix, provider, checks in [("zhixu-warm-original-qa-", None, baseline_checks),
                                     ("zhixu-warm-fake-generation-qa-", FakeQAGenerationProvider(), generation_checks)]:
        with tempfile.TemporaryDirectory(prefix=prefix) as data:
            server = FolioServer(("127.0.0.1",0), data_dir=data, public_dir=ROOT / "public", registration_mode="open", storage_persistence="persistent")
            if provider is not None:
                server.bedtime_provider = provider
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                checks(origin) if provider is None else checks(origin, provider)
            finally:
                if provider is not None:
                    provider.release.set()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    run()
