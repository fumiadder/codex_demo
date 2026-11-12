"""重复任务 / 子任务 / 多标签 / 跨空间搜索 / 月历 / Markdown 浏览器 QA。

参照 frontend_hardening_qa.py：临时 FolioServer + 真实注册 + QA_CHROMIUM
无头浏览器，所有请求走真实 HTTP（含服务端 meta 校验与重复滚动）。
演示模式（state.demo，本地 demoStore）使用独立浏览器上下文验证一次。

运行：
  export QA_CHROMIUM=/path/to/chrome-headless-shell
  export PLAYWRIGHT_BROWSERS_PATH=/path/to/.pw-browsers
  python scripts/recur_calendar_qa.py

任一断言失败以非 0 退出码结束；全程监听 pageerror 与 dialog。
"""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import FolioServer  # noqa: E402

PASS_COUNT = 0
FAILURES = []


def report(message):
    global PASS_COUNT
    PASS_COUNT += 1
    print("PASS", message, flush=True)


def check(label, fn):
    try:
        fn()
        report(label)
    except Exception as exc:  # noqa: BLE001 - QA 脚本需要收集全部失败
        FAILURES.append(f"{label}: {exc}")
        print("FAIL", label, "->", repr(exc), flush=True)
        traceback.print_exc()


def register(page):
    # 调用方负责先 page.goto(base)；这里只操作登录/注册表单
    page.locator("#auth-toggle").click()
    page.locator("#auth-name").fill("重复月历测试")
    email = "recur-" + uuid.uuid4().hex[:10] + "@example.com"
    page.locator("#auth-email").fill(email)
    page.locator("#auth-password").fill("Recur-QA-only-2026")
    page.locator("#auth-submit").click()
    expect(page.locator("#app-shell")).to_be_visible()
    return email


def today_str():
    return date.today().isoformat()


def shift_day(days):
    return (date.today() + timedelta(days=days)).isoformat()


def open_editor_for_new_task(page):
    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
    page.evaluate("navigate('tasks')")
    page.get_by_role("button", name="添加待办").first.click()
    expect(page.locator("#modal")).to_be_visible()


def fill_task_title(page, title):
    page.locator("#modal .editor-main input").first.fill(title)


def set_due(page, value):
    page.locator("#modal input[type='date']").first.fill(value)


def select_freq(page, label):
    page.locator("#modal .recur-editor select").select_option(label=label)


def choose_end_mode(page, text):
    page.locator("#modal .recur-radio", has_text=text).locator("input").check()


def save_modal(page):
    page.locator("#modal button[type='submit']").click()
    expect(page.locator("#modal")).not_to_be_visible()


def item_by_title(page, title):
    return page.evaluate(
        "(title) => state.items.find((item) => item.title === title)",
        title,
    )


# 命名谓词在页面内分支判断，避免在严格 CSP 页面内使用 eval
def wait_item(page, title, predicate_key):
    page.wait_for_function(
        """([title, key]) => {
            const item = state.items.find((entry) => entry.title === title);
            if (!item) return false;
            if (key === 'daily_completed_1')
                return item.status === 'active' && item.meta.recurrence.completed === 1;
            if (key === 'daily_completed_2')
                return item.status === 'active' && item.meta.recurrence.completed === 2;
            if (key === 'exhausted')
                return item.status === 'done' && item.meta.recurrence.exhausted === true;
            if (key === 'subtask_done')
                return Array.isArray(item.meta.subtasks) && item.meta.subtasks[0].done === true;
            return false;
        }""",
        arg=[title, predicate_key],
    )


def complete_task_once(page, title):
    page.locator(".task-row", has_text=title).first.locator(".task-check").click()


# ---------------------------------------------------------------------------
# 功能检查
# ---------------------------------------------------------------------------
def check_recurrence_real(page):
    """每天 count=3：连续完成，due 前进、status 回 active，耗尽 done+exhausted。"""
    title = "QA 每日站会 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, title)
    set_due(page, today_str())
    select_freq(page, "每天")
    choose_end_mode(page, "固定次数")
    page.locator("#modal input[aria-label='重复总次数']").fill("3")
    save_modal(page)

    row = page.locator(".task-row", has_text=title).first
    expect(row).to_be_visible()
    expect(row.locator("small")).to_contain_text("每天")
    expect(row.locator("small")).to_contain_text("共 3 次")
    item = item_by_title(page, title)
    assert item["meta"]["recurrence"]["freq"] == "daily"
    assert item["meta"]["recurrence"]["count"] == 3
    assert item["meta"]["due"] == today_str()

    complete_task_once(page, title)
    wait_item(page, title, "daily_completed_1")
    assert item_by_title(page, title)["meta"]["due"] == shift_day(1)

    complete_task_once(page, title)
    wait_item(page, title, "daily_completed_2")
    assert item_by_title(page, title)["meta"]["due"] == shift_day(2)

    complete_task_once(page, title)
    wait_item(page, title, "exhausted")
    expect(
        page.locator(".task-row", has_text=title).first.locator("small"),
    ).to_contain_text("重复已结束")
    report("真实模式：每天重复滚动、count 耗尽与中文描述正确")


def check_weekly_monthly_real(page):
    """每周 byday 与每月规则：下一次到期日的星期/月份正确。"""
    weekly_title = "QA 周例会 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, weekly_title)
    set_due(page, today_str())
    select_freq(page, "每周")
    page.locator("#modal .weekday-toggle[aria-label='周一']").click()
    page.locator("#modal .weekday-toggle[aria-label='周三']").click()
    save_modal(page)
    complete_task_once(page, weekly_title)
    wait_item(page, weekly_title, "daily_completed_1")
    next_due = item_by_title(page, weekly_title)["meta"]["due"]
    weekday = datetime.strptime(next_due, "%Y-%m-%d").weekday()
    assert weekday in (0, 2), next_due
    assert next_due > today_str()
    expect(
        page.locator(".task-row", has_text=weekly_title).first.locator("small"),
    ).to_contain_text("每周的周一、周三")

    monthly_title = "QA 月度复盘 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, monthly_title)
    set_due(page, today_str())
    select_freq(page, "每月")
    save_modal(page)
    complete_task_once(page, monthly_title)
    wait_item(page, monthly_title, "daily_completed_1")
    next_due = item_by_title(page, monthly_title)["meta"]["due"]
    expected_month = date.today().month % 12 + 1
    actual = datetime.strptime(next_due, "%Y-%m-%d").date()
    assert actual.month == expected_month, next_due
    report("真实模式：每周 byday 与每月到期推进正确")


def check_subtasks_real(page):
    """子任务增删、勾选持久化、进度 x/y、非法 meta 被服务端 400。"""
    title = "QA 子任务清单 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, title)
    add_btn = page.locator("#modal .subtask-editor button", has_text="添加子任务")
    add_btn.click()
    page.locator("#modal .subtask-edit-row input[type='text']").first.fill("第一步")
    add_btn.click()
    page.locator("#modal .subtask-edit-row input[type='text']").nth(1).fill("第二步")
    # 删除第二项再补一项，验证删除按钮
    page.locator("#modal .subtask-edit-row").nth(1).locator(".icon-button").click()
    add_btn.click()
    page.locator("#modal .subtask-edit-row input[type='text']").nth(1).fill("替代第二步")
    save_modal(page)

    item = item_by_title(page, title)
    assert len(item["meta"]["subtasks"]) == 2
    row = page.locator(".task-row", has_text=title).first
    expect(row.locator(".subtask-mini-progress")).to_contain_text("0/2")
    row.locator(".subtask-mini-row input").first.check()
    wait_item(page, title, "subtask_done")
    expect(row.locator(".subtask-mini-progress")).to_contain_text("1/2")

    # 刷新后仍持久化
    page.reload()
    expect(page.locator("#app-shell")).to_be_visible()
    page.wait_for_function(
        "(title) => state.items.some((item) => item.title === title)",
        arg=title,
    )
    item = item_by_title(page, title)
    assert item["meta"]["subtasks"][0]["done"] is True
    expect(
        page.locator(".task-row", has_text=title).first.locator(".subtask-mini-progress"),
    ).to_contain_text("1/2")

    # 非法 subtask id 必须被服务端 400 拒绝
    status = page.evaluate(
        """async (item) => {
            try {
                await api(`/api/items/${item.id}`, 'PATCH', {
                    kind: item.kind, area: item.area, title: item.title,
                    body: item.body || '', status: item.status,
                    meta: { ...item.meta, subtasks: [
                        { id: 'not-a-valid-hex-id', title: '坏标识', done: false },
                    ] },
                });
                return 0;
            } catch (error) {
                return error.status || 0;
            }
        }""",
        item,
    )
    assert status == 400, status
    report("真实模式：子任务增删/勾选持久化/进度 x/y/400 校验通过")


def check_tags_real(page):
    """多标签添加/删除/8 个上限、筛选条、旧单标签兼容。"""
    title = "QA 标签笔记 " + uuid.uuid4().hex[:6]
    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
    page.evaluate("navigate('notes')")
    page.get_by_role("button", name="写一条笔记").first.click()
    expect(page.locator("#modal")).to_be_visible()
    page.locator("#modal .editor-main input").first.fill(title)
    tag_input = page.locator("#modal .tag-editor input")
    for name in ["项目", "阅读", "旅行灵感"]:
        tag_input.fill(name)
        tag_input.press("Enter")
    expect(page.locator("#modal .tag-chip")).to_have_count(3)
    # 逗号也能添加
    tag_input.fill("逗号标签,")
    tag_input.press("Enter")
    expect(page.locator("#modal .tag-chip")).to_have_count(4)
    # 删除一个
    page.locator("#modal .tag-chip-remove").first.click()
    expect(page.locator("#modal .tag-chip")).to_have_count(3)
    save_modal(page)
    item = item_by_title(page, title)
    assert item["meta"]["tags"] == ["阅读", "旅行灵感", "逗号标签"], item["meta"]["tags"]

    # 8 个上限
    page.locator(".note-card", has_text=title).first.click()
    tag_input = page.locator("#modal .tag-editor input")
    for index in range(8):
        tag_input.fill(f"上限{index}")
        tag_input.press("Enter")
    expect(page.locator("#modal .tag-chip")).to_have_count(8)
    tag_input.fill("第九个标签")
    tag_input.press("Enter")
    expect(page.locator("#modal .tag-chip")).to_have_count(8)
    expect(page.locator(".toast").last).to_contain_text("最多只能添加 8 个标签")
    page.locator("#modal-close").click()

    # 筛选条：数据来自 GET /tags，点击筛选、再点取消
    filter_bar = page.locator(".tag-filter-bar")
    expect(filter_bar).to_be_visible()
    chip = filter_bar.locator(".tag-filter-chip", has_text="旅行灵感")
    expect(chip).to_be_visible()
    chip.click()
    expect(page.locator(".note-card", has_text=title)).to_have_count(1)
    filter_bar.locator(".tag-filter-chip", has_text="全部").click()

    # 旧单值 meta.tag 兼容：直接走 API 播种后强制重新聚合
    sid = page.evaluate("state.space.id")
    legacy_title = "QA 旧标签 " + uuid.uuid4().hex[:6]
    page.evaluate(
        """async ({ sid, title }) => {
            await api(`/api/spaces/${sid}/items`, 'POST', {
                kind: 'note', area: 'work', title, body: '旧标签兼容',
                status: 'active', meta: { tag: '旧标签QA' },
            });
            state.tagsLoadedSpace = null;
            await loadItems();
            await loadSpaceTags();
        }""",
        {"sid": sid, "title": legacy_title},
    )
    expect(page.locator(".tag-filter-chip", has_text="旧标签QA")).to_be_visible()
    page.locator(".tag-filter-chip", has_text="旧标签QA").click()
    expect(page.locator(".note-card", has_text=legacy_title)).to_have_count(1)
    page.locator(".tag-filter-chip", has_text="全部").click()
    report("真实模式：多标签上限/筛选条/旧 tag 兼容通过")


def check_search_real(page):
    """跨空间搜索：双空间命中、daily 命中、密码箱搜不到、点击跳转、429 中文。"""
    keyword = "QA暗号" + uuid.uuid4().hex[:8]
    spaces = page.evaluate("api('/api/spaces').then((r) => r.spaces || r)")
    sid1 = spaces[0]["id"]
    second = page.evaluate(
        """async (name) => {
            const result = await api('/api/spaces', 'POST', { name });
            return result.space;
        }""",
        "QA 第二空间 " + uuid.uuid4().hex[:4],
    )
    sid2 = second["id"]
    page.evaluate(
        """async ({ sid, keyword }) => {
            await api(`/api/spaces/${sid}/items`, 'POST', {
                kind: 'task', area: 'work', title: `第一空间 ${keyword}`,
                body: '跨空间搜索播种', status: 'active',
                meta: { due: new Date().toISOString().slice(0, 10) },
            });
        }""",
        {"sid": sid1, "keyword": keyword},
    )
    page.evaluate(
        """async ({ sid, keyword }) => {
            await api(`/api/spaces/${sid}/items`, 'POST', {
                kind: 'note', area: 'life', title: `第二空间 ${keyword}`,
                body: '另一个成员空间', status: 'active', meta: {},
            });
            await api(`/api/spaces/${sid}/daily/entries`, 'POST', {
                area: 'work', title: `今日记录 ${keyword}`,
                body: 'daily 也应被搜到', imageIds: [],
            });
        }""",
        {"sid": sid2, "keyword": keyword},
    )
    # 刷新 state.spaces，保证搜索结果点击时能找到新空间
    page.evaluate("enterApp()")
    expect(page.locator("#app-shell")).to_be_visible()

    # 密码箱：创建一条含唯一词的私密记录
    vault_word = "金库密语" + uuid.uuid4().hex[:8]
    page.locator('[data-view="vault"]').first.click()
    page.get_by_label("设置独立密码", exact=True).fill("Vault-QA-only-2026")
    page.get_by_label("确认密码", exact=True).fill("Vault-QA-only-2026")
    page.get_by_role("button", name="创建密码箱", exact=True).click()
    expect(page.get_by_role("button", name="添加私密记录", exact=True).first).to_be_visible()
    page.get_by_role("button", name="添加私密记录", exact=True).first.click()
    page.get_by_label("名称", exact=True).fill(f"私密 {vault_word}")
    page.get_by_label("私密内容", exact=True).fill(vault_word)
    page.get_by_role("button", name="加密保存").click()
    expect(page.locator("#modal")).not_to_be_visible()

    # 打开跨空间搜索视图（⌘K 入口）
    page.keyboard.press("Meta+k")
    expect(page.locator("#search-view-input")).to_be_visible()
    # 无关键词时是引导而非空列表
    expect(page.locator(".search-view-results .empty-state")).to_be_visible()
    page.locator("#search-view-input").fill(keyword)
    expect(page.locator(".search-space-group")).to_have_count(2)
    expect(page.locator(".search-result")).to_have_count(3)
    expect(page.locator(".search-result", has_text="今日记录")).to_have_count(1)

    # 密码箱内容搜不到
    page.locator("#search-view-input").fill(vault_word)
    expect(page.locator(".search-view-results .empty-state")).to_be_visible()

    # daily 结果点击跳到今日记录
    page.locator("#search-view-input").fill(keyword)
    page.locator(".search-result", has_text="今日记录").first.click()
    # 侧边栏已移除「今日记录」菜单，改用当前视图判断是否跳转成功
    # CSP 禁止字符串求值，用函数形式等待视图切换
    page.wait_for_function("() => state.view === 'daily'", timeout=8000)
    assert page.evaluate("state.view") == "daily"

    # item 结果点击：跨空间时自动切换并打开编辑 modal
    page.locator('[data-view="search"]').first.click()
    page.locator("#search-view-input").fill(keyword)
    target = page.locator(".search-result", has_text="第二空间").first
    expect(target).to_be_visible()
    target.click()
    expect(page.locator("#modal")).to_be_visible()
    expect(page.locator("#modal-title")).to_contain_text("笔记")
    page.locator("#modal-close").click()

    # 429 限流响应走 apiStatusMessage 中文
    def fulfill_429(route):
        route.fulfill(
            status=429,
            headers={"Retry-After": "9"},
            content_type="application/json",
            body='{"error":""}',
        )

    search_route = re.compile(r"/api/search")
    page.route(search_route, fulfill_429)
    page.locator('[data-view="search"]').first.click()
    page.locator("#search-view-input").fill(keyword + "x")
    expect(page.locator(".search-view-results .form-error")).to_contain_text(
        "操作太频繁，请 9 秒后再试",
    )
    page.unroute(search_route)
    report("真实模式：跨空间搜索含 daily、密码箱隔离、跳转与 429 文案通过")


def check_calendar_real(page):
    """月历：任务落格、上一月/今天/下一月、重复任务多次出现、点日期筛选。"""
    first_day = date.today().replace(day=1).isoformat()
    title = "QA 月历重复 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, title)
    set_due(page, first_day)
    select_freq(page, "每天")
    choose_end_mode(page, "固定次数")
    page.locator("#modal input[aria-label='重复总次数']").fill("5")
    save_modal(page)

    single_title = "QA 月历单日 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, single_title)
    set_due(page, today_str())
    save_modal(page)

    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
    page.evaluate("navigate('calendar')")
    expect(page.locator(".calendar-grid .calendar-cell")).to_have_count(42)
    expect(page.locator(".calendar-weekdays span")).to_have_count(7)
    month_title = page.locator(".calendar-title").inner_text()
    current_month = today_str()[:7]

    # 月历只展示未来 3 次落点，期望数与前端纯函数 nextOccurrences 一致
    expected = page.evaluate(
        """([title, month]) => {
            const item = state.items.find((entry) => entry.title === title);
            return nextOccurrences(item, 3).filter((d) => d.startsWith(month)).length;
        }""",
        [title, current_month],
    )
    expect(page.locator(".calendar-entry", has_text=title)).to_have_count(expected)
    expect(page.locator(".calendar-entry", has_text=single_title)).to_have_count(1)
    expect(page.locator(".calendar-cell.today")).to_have_count(1)

    # 上一月 / 下一月 / 今天导航
    page.locator("button.calendar-nav-btn", has_text="下一月").click()
    expect(page.locator(".calendar-title")).not_to_have_text(month_title)
    page.locator("button.calendar-nav-btn", has_text="上一月").click()
    expect(page.locator(".calendar-title")).to_have_text(month_title)
    page.locator("button.calendar-nav-btn", has_text="今天").click()
    expect(page.locator(".calendar-cell.today")).to_have_count(1)

    # 点日期数字（避开格内的任务按钮）出现当日详情与新建入口，预填日期正确
    page.locator(".calendar-cell.today .calendar-day").click()
    expect(page.locator(".calendar-day-detail")).to_be_visible()
    expect(page.locator(".calendar-day-detail", has_text=single_title)).to_be_visible()
    new_btn = page.locator(".calendar-day-detail button", has_text="新建这一天的待办")
    expect(new_btn).to_be_visible()
    new_btn.click()
    expect(page.locator("#modal input[type='date']").first).to_have_value(today_str())
    page.locator("#modal-close").click()
    # 再点一次取消筛选
    page.locator(".calendar-cell.picked .calendar-day").click()
    expect(page.locator(".calendar-day-detail")).to_have_count(0)
    report("真实模式：月历落格/导航/重复落点/日期详情通过")


def daily_payload(title, entry_date, tag):
    return {
        "entries": [{
            "id": uuid.uuid4().hex,
            "spaceId": "qa-space",
            "area": "work",
            "entryDate": entry_date,
            "title": title,
            "summary": "",
            "body": title,
            "status": "active",
            "meta": {"tags": [tag]},
            "createdBy": "qa-user",
            "createdAt": entry_date + "T08:00:00.000Z",
            "updatedAt": entry_date + "T08:00:00.000Z",
            "images": [],
        }],
        "digests": {},
        "month": entry_date[:7],
    }


def check_calendar_month_loading(page):
    """切换月份必须请求该月记录，而非复用自然当月缓存。"""
    previous_day = date.today().replace(day=1) - timedelta(days=1)
    previous_month = previous_day.isoformat()[:7]
    title = "QA 跨月记录 " + uuid.uuid4().hex[:6]
    requested_months = []
    pattern = re.compile(r"/api/spaces/[^/]+/daily/entries\?month=")

    def handle_daily(route):
        month = route.request.url.rsplit("month=", 1)[-1]
        requested_months.append(month)
        payload = daily_payload(title, previous_day.isoformat(), "历史") \
            if month == previous_month else {"entries": [], "digests": {}, "month": month}
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    page.route(pattern, handle_daily)
    try:
        page.evaluate("navigate('calendar')")
        page.locator("button.calendar-nav-btn", has_text="今天").click()
        page.locator("button.calendar-nav-btn", has_text="上一月").click()
        expect(page.locator(
            ".calendar-grid .calendar-entry.agenda-daily", has_text=title,
        )).to_have_count(1)
        assert previous_month in requested_months, requested_months
    finally:
        page.unroute(pattern, handle_daily)
    report("真实模式：月历切换月份会加载对应今日记录")


def check_space_response_isolation(page):
    """旧空间的慢 daily/tags 回包不能覆盖刚切换到的新空间。"""
    second = page.evaluate(
        """async (name) => {
            const result = await api('/api/spaces', 'POST', {name});
            state.spaces.push(result.space);
            return result.space;
        }""",
        "QA 慢响应空间 " + uuid.uuid4().hex[:4],
    )
    old_sid = page.evaluate("state.space.id")
    new_sid = second["id"]
    current_month = today_str()[:7]
    old_title = "QA 旧空间慢记录"
    new_title = "QA 新空间记录"
    pending = {"daily": [], "tags": []}
    daily_pattern = re.compile(r"/api/spaces/[^/]+/daily/entries\?month=")
    tags_pattern = re.compile(r"/api/spaces/[^/]+/tags$")

    def fulfill(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False),
        )

    def handle_daily(route):
        if f"/spaces/{old_sid}/" in route.request.url:
            pending["daily"].append(route)
        elif f"/spaces/{new_sid}/" in route.request.url:
            fulfill(route, daily_payload(new_title, today_str(), "新空间标签"))
        else:
            route.continue_()

    def handle_tags(route):
        if f"/spaces/{old_sid}/" in route.request.url:
            pending["tags"].append(route)
        elif f"/spaces/{new_sid}/" in route.request.url:
            fulfill(route, {"tags": [{"name": "新空间标签", "count": 1}]})
        else:
            route.continue_()

    page.route(daily_pattern, handle_daily)
    page.route(tags_pattern, handle_tags)
    try:
        page.wait_for_function("() => !state.daily.loading && !state.tagsLoading")
        page.evaluate(
            """() => {
                state.daily.loadedSpace = null;
                state.daily.month = '';
                state.tagsLoadedSpace = null;
                loadDailyEntries();
                loadSpaceTags();
            }""",
        )
        deadline = time.monotonic() + 10
        while (not pending["daily"] or not pending["tags"]) and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert pending["daily"] and pending["tags"], "旧空间请求未进入延迟队列"
        page.evaluate("(sid) => switchSpace(sid)", new_sid)
        page.wait_for_function(
            """(sid) => state.space?.id === sid &&
                state.daily.loadedSpace === sid && state.tagsLoadedSpace === sid""",
            arg=new_sid,
        )
        assert page.evaluate("state.daily.entries[0]?.title") == new_title
        assert page.evaluate("state.spaceTags[0]?.name") == "新空间标签"

        # 上一月月历与自然当月总览可能各留下一个旧空间请求，全部释放后逐一核验隔离。
        daily_count = len(pending["daily"])
        tags_count = len(pending["tags"])
        for index in range(daily_count):
            fulfill(
                pending["daily"].pop(0),
                daily_payload(f"{old_title} {index + 1}", today_str(), "旧空间标签")
                | {"month": current_month},
            )
            page.wait_for_timeout(50)
            assert page.evaluate("state.daily.entries[0]?.title") == new_title
        for _ in range(tags_count):
            fulfill(
                pending["tags"].pop(0),
                {"tags": [{"name": "旧空间标签", "count": 1}]},
            )
            page.wait_for_timeout(50)
            assert page.evaluate("state.spaceTags[0]?.name") == "新空间标签"
        page.wait_for_timeout(250)
        assert page.evaluate("state.space.id") == new_sid
        assert page.evaluate("state.daily.entries[0]?.title") == new_title
        assert page.evaluate("state.spaceTags[0]?.name") == "新空间标签"
        assert all(not routes for routes in pending.values()), {
            name: len(routes) for name, routes in pending.items()
        }
    finally:
        for routes in pending.values():
            for route in routes:
                try:
                    route.continue_()
                except Exception as exc:  # 已取消或已释放的请求无需重复处理
                    if "already handled" not in str(exc):
                        raise
        page.unroute(daily_pattern, handle_daily)
        page.unroute(tags_pattern, handle_tags)
    report("真实模式：旧空间慢回包不会污染新空间记录与标签")


def check_calendar_images_tags_real(page, image_path):
    """重复待办的真实首图、标签筛选、日程联动与日期格响应式布局。"""
    title = "QA 月历图片学习 " + uuid.uuid4().hex[:6]
    control_title = "QA 月历无标签对照 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, title)
    set_due(page, today_str())
    select_freq(page, "每天")
    tag_input = page.locator("#modal .tag-editor input")
    tag_input.fill("学习")
    tag_input.press("Enter")
    expect(page.locator("#modal .tag-chip", has_text="学习")).to_have_count(1)
    page.locator("#modal input[type='file']").set_input_files(str(image_path))

    sid = page.evaluate("state.space.id")
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith(f"/api/spaces/{sid}/items")
    ) as created_response:
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and re.search(r"/api/items/[a-f0-9]{32}/images$", response.url) is not None
        ) as uploaded_response:
            save_modal(page)
    assert created_response.value.status == 201
    created_item = created_response.value.json()["item"]
    assert "images" in created_item and created_item["images"] == [], created_item
    assert uploaded_response.value.status == 201
    uploaded = uploaded_response.value.json()
    assert len(uploaded.get("images", [])) >= 1, uploaded
    image = uploaded["image"]
    assert image["mime"] == "image/png", image
    assert image["size"] == image_path.stat().st_size, image
    assert image["url"].startswith("/api/files/"), image

    page.wait_for_function(
        """(title) => {
            const item = state.items.find((entry) => entry.title === title);
            return item && Array.isArray(item.images) && item.images.length >= 1;
        }""",
        arg=title,
    )
    item = item_by_title(page, title)
    assert item["id"] == created_item["id"]
    assert item["meta"]["recurrence"]["freq"] == "daily", item["meta"]
    assert item["meta"].get("tags") == ["学习"], item["meta"]
    assert any(entry["id"] == image["id"] for entry in item["images"]), item["images"]
    persisted = page.evaluate(
        """async ({ sid, id }) => {
            const result = await api(`/api/spaces/${sid}/items`);
            return result.items.find((entry) => entry.id === id);
        }""",
        {"sid": sid, "id": item["id"]},
    )
    assert persisted and any(
        entry["id"] == image["id"] for entry in persisted.get("images", [])
    ), persisted

    open_editor_for_new_task(page)
    fill_task_title(page, control_title)
    set_due(page, today_str())
    save_modal(page)
    control = item_by_title(page, control_title)
    assert control["meta"].get("tags") == [], control["meta"]

    page.evaluate("navigate('calendar')")
    expect(page.locator(".calendar-grid .calendar-cell")).to_have_count(42)
    learning = page.locator(".tag-filter-bar .tag-filter-chip").filter(
        has_text=re.compile(r"^学习\s+\d+$"),
    )
    expect(learning).to_be_visible()
    occurrences = page.evaluate(
        """(title) => {
            const item = state.items.find((entry) => entry.title === title);
            return nextOccurrences(item, 3);
        }""",
        title,
    )
    assert len(occurrences) == 3, occurrences
    month_dates = [value for value in occurrences if value.startswith(today_str()[:7])]
    assert month_dates, occurrences
    calendar_rows = page.locator(".calendar-grid .calendar-entry", has_text=title)
    expect(calendar_rows).to_have_count(len(month_dates))
    actual_dates = calendar_rows.evaluate_all(
        """(rows) => rows.map((row) =>
            row.closest('.calendar-cell').getAttribute('aria-label').slice(0, 10)
        ).sort()""",
    )
    assert actual_dates == sorted(month_dates), (actual_dates, month_dates)
    expect(
        calendar_rows.locator(".entry-thumb.compact img"),
    ).to_have_count(len(month_dates))
    expect(
        calendar_rows.locator(".entry-tags .tag", has_text="学习"),
    ).to_have_count(len(month_dates))
    first_image = calendar_rows.locator(".entry-thumb.compact img").first
    expect(first_image).to_have_attribute("src", image["url"])
    first_image.scroll_into_view_if_needed()
    first_image.evaluate("(element) => element.decode()")
    assert first_image.evaluate("(element) => element.naturalWidth") == 1
    expect(
        page.locator(".calendar-grid .calendar-entry", has_text=control_title),
    ).to_have_count(1)

    learning.click()
    expect(learning).to_have_attribute("aria-pressed", "true")
    expect(calendar_rows).to_have_count(len(month_dates))
    filtered_dates = calendar_rows.evaluate_all(
        """(rows) => rows.map((row) =>
            row.closest('.calendar-cell').getAttribute('aria-label').slice(0, 10)
        ).sort()""",
    )
    assert filtered_dates == actual_dates
    expect(
        page.locator(".calendar-grid .calendar-entry", has_text=control_title),
    ).to_have_count(0)
    assert page.locator(".calendar-grid .calendar-entry").evaluate_all(
        """(rows) => rows.every((row) =>
            [...row.querySelectorAll('.entry-tags .tag')]
                .some((tag) => tag.textContent === '学习')
        )""",
    )

    page.locator(".calendar-mode-btn", has_text="日程").click()
    expect(page.locator(".agenda-list")).to_be_visible()
    agenda_dates = [
        value for value in occurrences if shift_day(-7) <= value <= shift_day(29)
    ]
    assert agenda_dates == occurrences, (agenda_dates, occurrences)
    agenda_rows = page.locator(".agenda-list .agenda-item", has_text=title)
    expect(agenda_rows).to_have_count(len(agenda_dates))
    expect(agenda_rows.locator(".entry-thumb img")).to_have_count(len(agenda_dates))
    expect(
        page.locator(".agenda-list .agenda-item", has_text=control_title),
    ).to_have_count(0)
    assert page.locator(".agenda-list .agenda-item").evaluate_all(
        """(rows) => rows.every((row) =>
            [...row.querySelectorAll('.entry-tags .tag')]
                .some((tag) => tag.textContent === '学习')
        )""",
    )

    page.locator(".calendar-mode-btn", has_text="日历").click()
    expect(page.locator(".calendar-grid")).to_be_visible()
    learning.click()
    expect(learning).to_have_attribute("aria-pressed", "false")
    expect(calendar_rows).to_have_count(len(month_dates))
    expect(
        page.locator(".calendar-grid .calendar-entry", has_text=control_title),
    ).to_have_count(1)
    page.locator(".calendar-mode-btn", has_text="日程").click()
    expect(agenda_rows).to_have_count(len(agenda_dates))
    expect(
        page.locator(".agenda-list .agenda-item", has_text=control_title),
    ).to_have_count(1)
    page.locator(".calendar-mode-btn", has_text="日历").click()
    expect(page.locator(".calendar-grid")).to_be_visible()

    for width, height in ((320, 720), (375, 812), (1920, 1080)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(100)
        layout = page.evaluate(
            """() => ({
                viewport: innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                containers: ['.calendar-scroll', '.calendar-card', '#view-content', '.main-content', '.workspace-body', 'body']
                    .map((selector) => {
                        const node = document.querySelector(selector);
                        if (!node) return { selector };
                        const rect = node.getBoundingClientRect();
                        return { selector, left: rect.left, right: rect.right,
                            width: rect.width, clientWidth: node.clientWidth,
                            scrollWidth: node.scrollWidth,
                            overflowX: getComputedStyle(node).overflowX };
                    }),
                cells: [...document.querySelectorAll('.calendar-grid .calendar-cell')]
                    .map((cell) => {
                        const bounds = cell.getBoundingClientRect();
                        return {
                            date: cell.getAttribute('aria-label'),
                            clientWidth: cell.clientWidth,
                            scrollWidth: cell.scrollWidth,
                            parts: [...cell.querySelectorAll('.entry-thumb, .entry-tags')]
                                .filter((part) => part.getClientRects().length)
                                .map((part) => {
                                    const rect = part.getBoundingClientRect();
                                    return {
                                        className: part.className,
                                        left: rect.left - bounds.left,
                                        right: rect.right - bounds.left,
                                        cellWidth: bounds.width,
                                    };
                                }),
                        };
                    }),
            })""",
        )
        assert layout["documentWidth"] <= width, (width, layout["containers"])
        assert len(layout["cells"]) == 42
        for cell in layout["cells"]:
            assert cell["scrollWidth"] <= cell["clientWidth"], (width, cell)
            for part in cell["parts"]:
                # getBoundingClientRect 使用子像素，允许最多 1px 的舍入误差。
                assert part["left"] >= -1, (width, cell["date"], part)
                assert part["right"] <= part["cellWidth"] + 1, (
                    width,
                    cell["date"],
                    part,
                )
    page.set_viewport_size({"width": 1440, "height": 1000})
    report("真实模式：重复待办首图/标签筛选/日程恢复及 320/375/1920px 布局通过")


def check_markdown_real(page):
    """Markdown 渲染与 XSS 降级：无 script 执行、javascript: 链接不可点。"""
    title = "QA Markdown 笔记 " + uuid.uuid4().hex[:6]
    body = (
        "**粗体内容**\n\n- 列表甲\n- 列表乙\n\n"
        "[安全链接](https://example.com/page)\n\n"
        "<script>window.__qaXssHit = 1;</script>\n\n"
        "[危险链接](javascript:alert('xss'))\n"
    )
    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
    page.evaluate("navigate('notes')")
    page.get_by_role("button", name="写一条笔记").first.click()
    page.locator("#modal .editor-main input").first.fill(title)
    page.locator("#modal textarea").fill(body)
    save_modal(page)
    page.locator(".note-card", has_text=title).first.click()
    host = page.locator("#modal .markdown-body")
    expect(host).to_be_visible()
    expect(host.locator("strong")).to_have_text("粗体内容")
    expect(host.locator("ul li")).to_have_count(2)
    expect(host.locator("a", has_text="安全链接")).to_have_attribute(
        "href", "https://example.com/page",
    )
    # 原始 HTML 只显示文本、不执行
    assert "<script>" in host.inner_text()
    assert page.evaluate("window.__qaXssHit === undefined")
    assert host.locator("script").count() == 0
    # javascript: 链接被降级为不可点击纯文本
    assert host.locator("a", has_text="危险链接").count() == 0
    assert "javascript:alert" in host.inner_text()
    page.locator("#modal-close").click()
    assert page.evaluate("typeof window.ZhixuMarkdown.render === 'function'")
    report("真实模式：Markdown 渲染与危险内容降级通过")


def check_narrow_and_motion(base, playwright):
    """320px 窄屏无横向溢出；reduced-motion 下动画被禁用。"""
    browser = playwright.chromium.launch(
        executable_path=os.environ.get("QA_CHROMIUM", "/usr/bin/chromium"),
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(
        viewport={"width": 320, "height": 720},
        is_mobile=True,
        has_touch=True,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    context.add_init_script("delete URLSearchParams.prototype.size;")
    page = context.new_page()
    page.goto(base, wait_until="networkidle")
    register(page)
    # 320px 下侧边栏是离屏抽屉，直接调用视图切换函数覆盖各视图
    for view in ["overview", "tasks", "notes", "search", "calendar", "daily"]:
        page.evaluate(f"navigate('{view}')")
        page.wait_for_timeout(120)
        overflow = page.evaluate(
            "({width: innerWidth, scroll: document.documentElement.scrollWidth})",
        )
        assert overflow["scroll"] <= overflow["width"], (view, overflow)
    page.evaluate("showEditor('task')")
    expect(page.locator("#modal")).to_be_visible()
    overflow = page.evaluate(
        "({width: innerWidth, scroll: document.documentElement.scrollWidth})",
    )
    assert overflow["scroll"] <= overflow["width"], overflow
    page.locator("#modal-close").click()
    report("320px 窄屏各视图与编辑器无横向溢出")
    context.close()

    reduced = browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        reduced_motion="reduce",
    )
    reduced_page = reduced.new_page()
    reduced_page.goto(base, wait_until="networkidle")
    assert reduced_page.evaluate(
        "matchMedia('(prefers-reduced-motion: reduce)').matches",
    )
    duration = reduced_page.evaluate(
        """() => {
            const node = document.createElement('div');
            node.className = 'toast';
            document.body.appendChild(node);
            const value = getComputedStyle(node).transitionDuration;
            node.remove();
            return value;
        }""",
    )
    assert duration == "0s", duration
    report("prefers-reduced-motion 下过渡动画被禁用")
    reduced.close()
    browser.close()


def check_demo_mode(base, playwright):
    """演示模式：本地滚动语义与跨空间本地搜索各验证一次（独立上下文）。"""
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
    page.goto(base, wait_until="networkidle")
    page.locator("#demo-button").click()
    expect(page.locator("#app-shell")).to_be_visible()
    assert page.evaluate("state.demo === true")

    title = "QA 演示重复 " + uuid.uuid4().hex[:6]
    open_editor_for_new_task(page)
    fill_task_title(page, title)
    set_due(page, today_str())
    select_freq(page, "每天")
    choose_end_mode(page, "固定次数")
    page.locator("#modal input[aria-label='重复总次数']").fill("2")
    save_modal(page)

    complete_task_once(page, title)
    wait_item(page, title, "daily_completed_1")
    assert item_by_title(page, title)["meta"]["due"] == shift_day(1)
    complete_task_once(page, title)
    wait_item(page, title, "exhausted")
    report("演示模式重复任务本地滚动与耗尽语义正确")

    keyword = "演示暗号" + uuid.uuid4().hex[:6]
    page.evaluate(
        """(keyword) => {
            state.demoStore['demo-team'].unshift({
                id: 'local-search-' + crypto.randomUUID(),
                kind: 'task', area: 'work', title: `协作空间 ${keyword}`,
                body: '演示跨空间', status: 'active', meta: {},
                created_at: new Date().toISOString(),
            });
        }""",
        keyword,
    )
    page.locator('[data-view="search"]').first.click()
    page.locator("#search-view-input").fill(keyword)
    expect(page.locator(".search-result", has_text=keyword)).to_have_count(1)
    expect(page.locator(".search-space-title")).to_contain_text("一起做点事")
    report("演示模式跨空间本地搜索可用")
    context.close()
    browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="zhixu-recur-qa-") as data:
        image_path = Path(data) / "calendar-pixel.png"
        image_path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII="
            )
        )
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
                page_errors = []
                dialogs = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "dialog",
                    lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()),
                )
                page.goto(base, wait_until="networkidle")
                register(page)

                check("真实模式重复任务", lambda: check_recurrence_real(page))
                check("真实模式周/月规则", lambda: check_weekly_monthly_real(page))
                check("真实模式子任务", lambda: check_subtasks_real(page))
                check("真实模式多标签", lambda: check_tags_real(page))
                check("真实模式跨空间搜索", lambda: check_search_real(page))
                check("真实模式月历", lambda: check_calendar_real(page))
                check("真实模式月历跨月记录", lambda: check_calendar_month_loading(page))
                check("真实模式空间慢响应隔离", lambda: check_space_response_isolation(page))
                check(
                    "真实模式月历图片与标签筛选",
                    lambda: check_calendar_images_tags_real(page, image_path),
                )
                check("真实模式 Markdown", lambda: check_markdown_real(page))
                check("窄屏与 reduced-motion", lambda: check_narrow_and_motion(base, playwright))
                check("演示模式", lambda: check_demo_mode(base, playwright))

                assert not page_errors, "Page errors: " + " | ".join(page_errors)
                assert not dialogs, "Dialogs: " + " | ".join(dialogs)
                report("全程无 pageerror、无浏览器 dialog")
                # 延迟路由场景可能留下已取消的请求；关闭前忽略其清理异常。
                page.unroute_all(behavior="ignoreErrors")
                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    print(
        f"\n==== recur/calendar QA 完成：PASS {PASS_COUNT}，FAIL {len(FAILURES)} ====",
        flush=True,
    )
    if FAILURES:
        for failure in FAILURES:
            print(" -", failure, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
