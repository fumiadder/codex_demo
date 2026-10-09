"""Life entry and safe bedtime sign-in return checks.

Runs against a temporary local server and isolated accounts. No provider calls.
"""
import os, sys, tempfile, threading, uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qs
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server import FolioServer

def register(page):
    page.locator('#auth-toggle').click()
    page.locator('#auth-name').fill('生活预览测试')
    email='life-'+uuid.uuid4().hex[:10]+'@example.com'
    page.locator('#auth-email').fill(email)
    page.locator('#auth-password').fill('Life-QA-only-2026')
    page.locator('#auth-submit').click()
    return email

def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

with tempfile.TemporaryDirectory(prefix='zhixu-life-qa-') as data:
    server=FolioServer(('127.0.0.1',0),data_dir=data,public_dir=ROOT/'public',registration_mode='open',storage_persistence='persistent')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('QA_CHROMIUM', '/usr/bin/chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':1440,'height':1000},locale='zh-CN',timezone_id='Asia/Shanghai')
            # Older Safari has URLSearchParams without its newer size property.
            context.add_init_script('delete URLSearchParams.prototype.size;')
            page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base,wait_until='networkidle');register(page)
            expect(page.locator('#view-content')).to_contain_text('添加第一件待办')
            expect(page.locator('#life-navigation')).not_to_be_visible()
            page.locator('[data-view="modules"]').click()
            assert page.locator('#view-content .life-bedtime-entry').count()==0
            page.locator('[data-area="life"]').click()
            expect(page.locator('#life-navigation')).to_be_visible()
            expect(page.locator('#view-content .module-category-heading')).to_contain_text('生活功能')
            expect(page.locator('#view-content .life-bedtime-entry')).to_be_visible()
            page.locator('[data-view="overview"]').click()
            expect(page.locator('#life-bedtime-title')).to_have_text('睡前小故事')
            uid=page.evaluate('state.user.id'); sid=page.evaluate('state.space.id')
            voice_link=page.get_by_role('link',name='上传我的声音',exact=True)
            assert parse_qs(urlsplit(voice_link.get_attribute('href')).query)=={'space':[sid],'panel':['voices']}
            assert page.get_by_role('link',name='看故事',exact=True).get_attribute('href')==f'/bedtime.html?space={sid}'
            no_overflow(page)
            (ROOT/'artifacts').mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT/'artifacts/workbench-life-desktop.png'),full_page=True)
            print('PASS real account work/life navigation, life-only module category and same-space story/upload links',flush=True)
            for width in [320,360,390,430]:
                page.set_viewport_size({'width':width,'height':844})
                no_overflow(page)
                expect(page.get_by_role('link',name='上传我的声音',exact=True)).to_be_visible()
            page.set_viewport_size({'width':1440,'height':1000})
            voice_link.click();page.wait_for_url('**/bedtime.html?space=*&panel=voices')
            assert parse_qs(urlsplit(page.url).query)=={'space':[sid],'panel':['voices']}
            expect(page.locator('#bedtime-app')).to_be_visible()
            # The app shell appears before loadSpace finishes. panel=voices
            # opens the dialog after that work, so wait for that actual state.
            expect(page.locator('#voice-dialog')).to_be_visible()
            page.locator('#close-voices').click()
            expect(page.locator('#voice-dialog')).not_to_be_visible()
            expect(page.locator('.back-link')).to_have_attribute('href','/?area=life')
            page.locator('.back-link').click()
            expect(page.locator('#life-navigation')).to_be_visible()
            expect(page.locator('#life-bedtime-title')).to_have_text('睡前小故事')
            for area in ['work','Life','life-extra']:
                page.goto(base+'/?area='+area,wait_until='networkidle')
                expect(page.locator('#life-navigation')).not_to_be_visible()
                assert page.evaluate('state.area')=='work'
            print('PASS four phone widths no overflow; actual bedtime backlink returns to life and other area values default work',flush=True)
            # An existing authenticated session consumes only the bedtime next.
            page.goto(base+'/?next='+quote('/bedtime.html?panel=voices&evil=https://evil.example',safe=''),wait_until='networkidle')
            page.wait_for_url('**/bedtime.html?panel=voices')
            assert parse_qs(urlsplit(page.url).query)=={'panel':['voices']}
            print('PASS existing login returns to canonical bedtime link and strips unknown parameters',flush=True)
            for target in ['https://evil.example/bedtime.html','//evil.example/bedtime.html','/\\evil.example/bedtime.html','/bedtime.html/../api/me','/bedtime.html.evil?panel=voices']:
                page.goto(base+'/?next='+quote(target,safe=''),wait_until='networkidle')
                expect(page.locator('#app-shell')).to_be_visible()
                expect(page.locator('#view-content')).to_contain_text('添加第一件待办')
                assert urlsplit(page.url).path=='/'
            print('PASS hostile external, slash/backslash and alternate-path next links never redirect',flush=True)
            fresh=browser.new_context(viewport={'width':1440,'height':1000})
            signup=fresh.new_page()
            signup.goto(base+'/?area=life&next='+quote('/bedtime.html?panel=voices',safe=''),wait_until='networkidle')
            expect(signup.locator('#auth-return-note')).to_contain_text('登录后继续上传')
            register(signup);signup.wait_for_url('**/bedtime.html?panel=voices')
            assert urlsplit(signup.url).netloc==urlsplit(base).netloc
            life_context=browser.new_context(viewport={'width':1440,'height':1000})
            life_signup=life_context.new_page()
            life_signup.goto(base+'/?area=life',wait_until='networkidle')
            email_b=register(life_signup)
            expect(life_signup.locator('#life-navigation')).to_be_visible()
            expect(life_signup.locator('#life-bedtime-title')).to_have_text('睡前小故事')
            uid_b=life_signup.evaluate('state.user.id');sid_b=life_signup.evaluate('state.space.id')
            assert life_signup.evaluate('(uid) => sessionStorage.getItem(`zhixu:space:${uid}`)',uid_b)==sid_b
            response=context.request.post(base+f'/api/spaces/{sid}/members',data={'email':email_b,'role':'viewer'},headers={'X-Requested-With':'Workspace','Origin':base})
            assert response.ok, response.text()
            life_signup.evaluate('(sid) => sessionStorage.setItem("zhixu-space",sid)',sid)
            life_signup.goto(base+'/?area=life',wait_until='networkidle')
            expect(life_signup.locator('#life-navigation')).to_be_visible()
            expect(life_signup.locator('#life-bedtime-title')).to_have_text('睡前小故事')
            assert life_signup.evaluate('state.space.id')==sid_b
            assert life_signup.evaluate('sessionStorage.getItem("zhixu-space")')==sid
            print('PASS registration retains life or prioritizes safe next; uid-scoped space ignores legacy A context even when B is a viewer there',flush=True)
            demo=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
            demo.goto(base,wait_until='networkidle')
            demo.locator('#demo-button').click();demo.locator('[data-area="life"]').click()
            demo.get_by_role('link',name='上传我的声音',exact=True).click()
            expect(demo.locator('#auth-screen')).to_be_visible()
            expect(demo.locator('#auth-return-note')).to_contain_text('登录后继续上传')
            assert demo.evaluate('state.user===null && state.demo===false')
            assert parse_qs(urlsplit(demo.url).query)['next']==['/bedtime.html?panel=voices']
            print('PASS demo upload transparently opens real sign-in with safe return intent',flush=True)
            assert not errors,errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
