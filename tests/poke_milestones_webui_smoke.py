"""Real Chromium regression of the native AstrBot Plugin View.

Run with `python tests/poke_milestones_webui_smoke.py`. Install Playwright +
Chromium beforehand, or set CHROME_PATH to an existing Chromium/Chrome/Edge.
All bridge calls use synthetic, in-memory fixtures; no AstrBot or QQ traffic.
"""
from __future__ import annotations

import os
import sys
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import Browser, Frame, Page, expect, sync_playwright

VIEW = Path(__file__).resolve().parents[1] / "views" / "Airi控制中心🌸" / "index.html"
SANDBOX = "allow-scripts allow-forms allow-downloads"

MOCK_SDK = r"""
(() => {
  const copy = v => structuredClone(v);
  const defaultRule = {
    id: "r1", name: "第一条规则", target: 30, tolerance: 2,
    mute_minutes: 1, enabled: false, llm_enabled: false, prompt: ""
  };
  const milestones = {enabled: false, rules: [defaultRule], history: [
    {id:"history-r1",created_at:"2026-11-27T18:30:01+08:00", group_id:"1122334455",
     user_id:"9876543210", rule_name:"第一条规则", count:30,
     status:"success", detail:"测试记录"}
  ]};
  const ranks = {
    daily_users:[{id:"9876543210",name:"测试QQ昵称 <img src=x onerror=alert(1)>",
      count:30}],
    history_users:[{id:"12345678901",name:"",count:100}],
    daily_groups:[{id:"1122334455",name:"测试群名称",count:20}],
    history_groups:[{id:"1122334455",name:"很长的群名称" + "长".repeat(70),count:76}]
  };
  const state = {
    settings:{
      self_mention_wakeup_enabled:true, at_command_suppress_llm_enabled:true,
      at_command_external_rules:[], ignore_official_qq_bots_enabled:true,
      keyword_wakeup_enabled:false, keyword_wakeup_private_enabled:false,
      keyword_wakeup_words:[], keyword_wakeup_mode:"contains",
      keyword_wakeup_group_whitelist:[], keyword_wakeup_user_whitelist:[],
      mention_tool_enabled:true, mute_tool_enabled:false,
      mute_duration_min:1, mute_duration_max:10, welcome_enabled:false,
      welcome_message:"你好！", welcome_images:[], auto_accept_friend_request:false,
      request_approval_enabled:false, request_approval_qq:"",
      poke_stats_enabled:true, poke_rank_max_limit:100,
      qzone_auto_like_enabled:true, qzone_poll_interval_sec:300,
      qzone_max_likes_per_cycle:3, qzone_dry_run:false, qzone_owner_qq:"",
      qzone_friend_batch_size:8, qzone_feed_count:30,
      qzone_delay_min_sec:15, qzone_delay_max_sec:30
    },
    runtime:{
      plugin_loaded:true, onebot_client_available:true, poke_stats_enabled:true,
      poke_milestones_available:true, mention_tool_enabled:true, mute_tool_enabled:false
    },
    stats:{total_pokes:100, daily_pokes:30, group_count:2, user_count:3,
      stats_day:"2026-11-27"},
    qzone:{enabled:true,dry_run:false,running:true,friend_count:10,
      seen_count:10,pending_count:2,initialized_count:2,liked:1,failed:0,
      dry_count:0,last_scan:"2026-11-27 18:00:00",last_error:"",suspended_seconds:0},
    poke_ranks:ranks,
    pending_requests:[{request_id:"A001",kind:"friend",user_id:"12345678901",
      nickname:"测试申请人",comment:"<img src=x onerror=alert(1)>",
      created_at:"2026-11-27 18:00:00"}]
  };
  const calls = [], reads = [];
  let fail, blocked, release, listener, nextRule = 2;
  const delay = ms => new Promise(resolve => setTimeout(resolve,ms));
  function rejectUnconfirmed(ok) {
    if (!ok) throw Error("模拟后端：缺少明确确认");
  }
  window.qa = {
    milestones:() => copy(milestones), state:() => copy(state),
    calls:() => copy(calls), reads:() => copy(reads),
    failNext:(route,mode="throw") => { fail={route,mode}; },
    blockNext:route => { blocked=route; },
    release:() => { if(release) release(); },
    runtime:(key,value) => {state.runtime[key]=value;},
    qzoneEnabled:value => {state.qzone.enabled=value; state.settings.qzone_auto_like_enabled=value;},
    dark:value => listener({isDark:value})
  };
  const failure = route => {
    if (fail?.route !== route) return null;
    const mode = fail.mode; fail = null;
    if (mode==="throw") throw Error("模拟请求失败");
    if (mode==="status") return {status:"error",message:"模拟请求失败"};
    if (mode==="malformed") return {data:{rules:[]}};
    if (mode==="partialRule") return {...copy(milestones),rules:[{id:"r1",enabled:false}]};
    if (mode==="duplicateRule") return {...copy(milestones),rules:[copy(defaultRule),copy(defaultRule)]};
    if (mode==="badRuleValues") return {...copy(milestones),rules:[{...copy(defaultRule),target:0}]};
    if (mode==="unchanged") return copy(milestones);
    if (mode==="malformedRuntime") return {...copy(state),
      runtime:{...state.runtime,onebot_client_available:"false"},
      settings:{...state.settings,keyword_wakeup_words:["不能覆盖可信状态"]}};
    throw Error("未知失败模式");
  };
  window.AstrBotPluginView = {
    ready: async () => delay(20),
    onContext: cb => {listener=cb; cb({isDark:false});},
    apiGet: async route => {
      reads.push(route); await delay(40);
      const error = failure(route); if(error) return error;
      switch(route){
        case "console/state": return copy(state);
        case "console/poke-ranks": return copy(ranks);
        case "poke-milestones/state": return copy(milestones);
        default: throw Error("Unexpected GET " + route);
      }
    },
    apiPost: async (route,body) => {
      calls.push({route,body:copy(body)});
      if(blocked===route) {
        blocked=null;
        await new Promise(resolve => {release=resolve;});
        release=null;
      }
      await delay(60);
      const commitThenThrow = fail?.route===route && fail.mode==="committedThenThrow";
      if (commitThenThrow) fail=null;
      else { const error = failure(route); if(error) return error; }
      switch(route) {
        case "poke-milestones/settings":
          rejectUnconfirmed(!body.enabled||body.confirm_side_effects===true);
          milestones.enabled=body.enabled; break;
        case "poke-milestones/rules/save": {
          rejectUnconfirmed(!milestones.enabled||!body.enabled||body.confirm_side_effects===true);
          const {confirm_side_effects, ...values}=body;
          const rule={...values,id:body.id||"r"+nextRule++};
          const i=milestones.rules.findIndex(row=>row.id===rule.id);
          if(i<0) milestones.rules.push(rule); else milestones.rules[i]=rule;
          break;
        }
        case "poke-milestones/rules/toggle":
          rejectUnconfirmed(!milestones.enabled||!body.enabled||body.confirm_side_effects===true);
          milestones.rules.find(row=>row.id===body.id).enabled=body.enabled; break;
        case "poke-milestones/rules/delete":
          milestones.rules=milestones.rules.filter(row=>row.id!==body.id); break;
        case "console/settings":
          for(const key of ["mute_tool_enabled","welcome_enabled","auto_accept_friend_request",
              "request_approval_enabled","qzone_auto_like_enabled"])
            rejectUnconfirmed(!(body.settings[key]===true&&state.settings[key]!==true)
              ||body.confirm_side_effects===true);
          Object.assign(state.settings,body.settings);
          state.runtime.mute_tool_enabled=state.settings.mute_tool_enabled;
          state.runtime.poke_stats_enabled=state.settings.poke_stats_enabled;
          state.qzone.enabled=state.settings.qzone_auto_like_enabled;
          state.qzone.dry_run=state.settings.qzone_dry_run;
          return copy(state);
        case "console/requests/review":
          rejectUnconfirmed(body.confirm===true);
          state.pending_requests=state.pending_requests.filter(r=>r.request_id!==body.request_id);
          return {status:"ok",message:"审批已完成",state:copy(state)};
        case "console/qzone/scan":
          rejectUnconfirmed(state.qzone.dry_run||body.confirm_side_effects===true);
          return {status:"ok",summary:{new:1,queued:1,attempted:1,liked:1,failed:0},state:copy(state)};
        default: throw Error("Unexpected POST " + route);
      }
      if (commitThenThrow) throw Error("模拟提交成功但响应连接中断");
      return copy(milestones);
    }
  };
})();
"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/":
            content = (
                '<!doctype html><meta charset="utf-8"><style>html,body{margin:0}'
                'iframe{display:block;width:100vw;height:100vh;border:0}</style>'
                f'<iframe name="plugin-view" sandbox="{SANDBOX}" src="/view"></iframe>'
            ).encode()
            mime = "text/html"
        elif route == "/view":
            content, mime = VIEW.read_bytes(), "text/html"
        elif route == "/api/plugin/page/bridge-sdk.js":
            content, mime = MOCK_SDK.encode(), "application/javascript"
        elif route == "/favicon.ico":
            content, mime = b"", "image/x-icon"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


@contextmanager
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def fixture_page(browser: Browser, url: str, width: int = 1280):
    page = browser.new_page(viewport={"width": width, "height": 860})
    page.set_default_timeout(5000)
    errors: list[str] = []
    modal_warnings: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def on_console(message):
        if "allow-modals" in message.text or "Ignored call to 'confirm()'" in message.text:
            modal_warnings.append(message.text)

    def on_dialog(dialog):
        modal_warnings.append(f"Unexpected native {dialog.type}: {dialog.message}")
        dialog.dismiss()

    page.on("console", on_console)
    page.on("dialog", on_dialog)
    # Even a mistakenly injected absolute resource must not leave this fixture.
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
    try:
        page.goto(url)
        expect(page.locator("iframe")).to_have_attribute("sandbox", SANDBOX)
        frame = page.frame(name="plugin-view")
        assert frame is not None, "Expected actual plugin iframe"
        frame.wait_for_function("window.origin === 'null' && !!window.qa")
        yield page, frame
        assert not errors, f"Unexpected uncaught JavaScript error(s): {errors}"
        assert not modal_warnings, f"Native dialogs blocked by sandbox: {modal_warnings}"
    finally:
        page.close()


def posts(frame: Frame):
    return frame.evaluate("qa.calls()")


def latest(frame: Frame, route: str, **expected):
    result = posts(frame)[-1]
    assert result["route"] == route, result
    for name, value in expected.items():
        assert result["body"].get(name) == value, result


def nav(frame: Frame, name: str):
    frame.locator(f'button[data-view="{name}"]').click()
    expect(frame.locator(f"#view-{name}")).to_be_visible()


def poke_ready(frame: Frame):
    nav(frame, "poke")
    expect(frame.locator("#milestone-global")).to_be_enabled()


def expect_confirmation(frame: Frame, old_count: int):
    expect(frame.locator("#confirm-dialog")).to_be_visible()
    assert len(posts(frame)) == old_count, "API write happened before explicit confirmation"


def cancel(frame: Frame):
    frame.locator("#confirm-cancel").click()
    expect(frame.locator("#confirm-dialog")).not_to_be_visible()


def accept(frame: Frame):
    frame.locator("#confirm-accept").click()
    expect(frame.locator("#confirm-dialog")).not_to_be_visible()


def refresh(frame: Frame):
    frame.locator("#refresh").click()
    expect(frame.locator("#refresh")).to_be_enabled()


def enable_global(frame: Frame):
    old_count = len(posts(frame))
    frame.locator("#milestone-global").click()
    expect_confirmation(frame, old_count)
    accept(frame)
    expect(frame.locator("#milestone-global")).to_be_checked()
    expect(frame.locator("#milestone-global")).to_be_enabled()
    latest(frame, "poke-milestones/settings", enabled=True, confirm_side_effects=True)


def test_refresh_unlocks_global(browser: Browser, url: str):
    with fixture_page(browser, url) as (_, frame):
        poke_ready(frame)
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_enabled()
        expect(frame.locator("#milestone-add")).to_be_enabled()
        assert not posts(frame)


def test_global_confirmation_cancel_success_and_retry(browser: Browser, url: str):
    with fixture_page(browser, url) as (page, frame):
        poke_ready(frame)
        switch = frame.locator("#milestone-global")
        expect(switch).not_to_be_checked()
        switch.click()
        expect_confirmation(frame, 0)
        expect(switch).not_to_be_checked()
        cancel(frame)
        expect(switch).not_to_be_checked()
        expect(switch).to_be_enabled()
        assert not posts(frame)

        switch.click()
        expect_confirmation(frame, 0)
        page.keyboard.press("Escape")
        expect(frame.locator("#confirm-dialog")).not_to_be_visible()
        expect(switch).not_to_be_checked()
        assert not posts(frame)

        # While the confirmed request is pending the last persisted state stays
        # visible and another user click cannot start a duplicate write.
        frame.evaluate("qa.blockNext('poke-milestones/settings')")
        switch.click()
        expect_confirmation(frame, 0)
        accept(frame)
        frame.wait_for_function("qa.calls().length === 1")
        expect(switch).to_be_disabled()
        expect(frame.locator("#refresh")).to_be_disabled()
        assert frame.evaluate("qa.milestones().enabled") is False
        frame.evaluate("qa.release()")
        expect(switch).to_be_checked()
        expect(switch).to_be_enabled()
        latest(frame, "poke-milestones/settings", enabled=True, confirm_side_effects=True)
        assert frame.evaluate("qa.milestones().enabled") is True
        assert len(posts(frame)) == 1

        switch.click()
        expect(switch).not_to_be_checked()
        expect(switch).to_be_enabled()
        latest(frame, "poke-milestones/settings", enabled=False)
        assert frame.evaluate("qa.milestones().enabled") is False

        for mode in ("throw", "status", "malformed", "partialRule", "duplicateRule", "badRuleValues", "unchanged"):
            old_count = len(posts(frame))
            frame.evaluate("(mode) => qa.failNext('poke-milestones/settings', mode)", mode)
            switch.click()
            expect_confirmation(frame, old_count)
            accept(frame)
            expect(frame.locator("#message.error")).to_be_visible()
            expect(switch).not_to_be_checked()
            expect(switch).to_be_enabled()
            # Invalid responses never replace the last trusted rule snapshot.
            expect(frame.locator("#milestone-rules")).to_contain_text("第一条规则")
            expect(frame.locator("#milestone-rules")).not_to_contain_text("undefined")
            expect(frame.locator("#milestone-rules")).to_contain_text("30 次")
            assert frame.locator('[data-edit="r1"]').count() == 1
            assert frame.evaluate("qa.milestones().enabled") is False
            assert len(posts(frame)) == old_count + 1

        enable_global(frame)
        refresh(frame)
        expect(switch).to_be_checked()
        expect(switch).to_be_enabled()


def test_rule_editor_and_destructive_confirmation(browser: Browser, url: str):
    with fixture_page(browser, url) as (_, frame):
        poke_ready(frame)
        editor = frame.locator("#milestone-dialog")
        frame.locator("#milestone-add").click()
        expect(editor).to_be_visible()
        expect(editor.locator('[name="target"]')).to_have_value("30")
        expect(editor.locator('[name="tolerance"]')).to_have_value("2")
        expect(editor.locator('[name="mute_minutes"]')).to_have_value("1")
        rule_name = "规则 <img src=x onerror=alert(1)>"
        editor.locator('[name="name"]').fill(rule_name)
        editor.locator('[name="enabled"]').uncheck()
        editor.locator('[name="llm_enabled"]').check()
        editor.locator('[name="prompt"]').fill("测试场景回复")
        frame.locator("#milestone-save").click()
        expect(editor).not_to_be_visible()
        expect(frame.locator("#milestone-rules")).to_contain_text(rule_name)
        assert frame.locator('#milestone-rules img[src="x"]').count() == 0
        assert frame.evaluate("qa.milestones().rules.find(r=>r.id==='r2').llm_enabled") is True
        latest(frame, "poke-milestones/rules/save", enabled=False, name=rule_name)

        enable_global(frame)
        toggle = frame.locator('[data-toggle="r2"]')
        old_count = len(posts(frame))
        toggle.click()
        expect_confirmation(frame, old_count)
        cancel(frame)
        assert len(posts(frame)) == old_count
        assert frame.evaluate("qa.milestones().rules.find(r=>r.id==='r2').enabled") is False
        toggle.click()
        expect_confirmation(frame, old_count)
        accept(frame)
        frame.wait_for_function("qa.milestones().rules.find(r=>r.id==='r2').enabled")
        expect(toggle).to_be_enabled()
        latest(frame, "poke-milestones/rules/toggle", id="r2", enabled=True, confirm_side_effects=True)

        frame.locator('[data-edit="r2"]').click()
        expect(editor).to_be_visible()
        editor.locator('[name="name"]').fill("失败重试规则")
        editor.locator('[name="mute_minutes"]').fill("5")
        frame.evaluate("qa.failNext('poke-milestones/rules/save','status')")
        old_count = len(posts(frame))
        frame.locator("#milestone-save").click()
        expect_confirmation(frame, old_count)
        accept(frame)
        expect(editor).to_be_visible()
        expect(frame.locator("#milestone-form-error")).to_be_visible()
        expect(editor.locator('[name="name"]')).to_have_value("失败重试规则")
        expect(editor.locator('[name="mute_minutes"]')).to_have_value("5")
        expect(frame.locator("#milestone-save")).to_be_enabled()
        assert frame.evaluate("qa.milestones().rules.find(r=>r.id==='r2').mute_minutes") == 1
        frame.locator("#milestone-save").click()
        expect_confirmation(frame, old_count + 1)
        accept(frame)
        expect(editor).not_to_be_visible()
        expect(frame.locator("#milestone-rules")).to_contain_text("失败重试规则")
        latest(frame, "poke-milestones/rules/save", id="r2", mute_minutes=5,
               enabled=True, confirm_side_effects=True)

        delete = frame.locator('[data-delete="r2"]')
        old_count = len(posts(frame))
        delete.click()
        expect_confirmation(frame, old_count)
        cancel(frame)
        assert len(posts(frame)) == old_count
        expect(delete).to_be_visible()
        delete.click()
        expect_confirmation(frame, old_count)
        accept(frame)
        expect(delete).not_to_be_visible()
        latest(frame, "poke-milestones/rules/delete", id="r2")


def test_new_rule_transport_failure_reconciles_before_retry(browser: Browser, url: str):
    with fixture_page(browser, url) as (_, frame):
        poke_ready(frame)
        editor = frame.locator("#milestone-dialog")
        save = frame.locator("#milestone-save")
        reconcile = frame.locator("#milestone-reconcile")

        # Lost response after the server persisted a brand new rule. A retry
        # must not blindly create a duplicate rule (which could duplicate mute actions).
        frame.locator("#milestone-add").click()
        editor.locator('[name="name"]').fill("响应丢失但已写入")
        frame.evaluate("qa.failNext('poke-milestones/rules/save','committedThenThrow')")
        save.click()
        expect(editor).to_be_visible()
        expect(frame.locator("#milestone-form-error")).to_be_visible()
        expect(reconcile).to_be_visible()
        expect(save).to_be_disabled()
        assert len(posts(frame)) == 1
        assert frame.evaluate("qa.milestones().rules.length") == 2

        reconcile.click()
        expect(editor).not_to_be_visible()
        expect(frame.locator("#milestone-rules")).to_contain_text("响应丢失但已写入")
        assert len(posts(frame)) == 1
        assert frame.evaluate("qa.milestones().rules.length") == 2

        # If the server did not persist anything, a successful read makes a
        # deliberate retry safe while preserving all draft fields.
        frame.locator("#milestone-add").click()
        editor.locator('[name="name"]').fill("提交前连接失败")
        editor.locator('[name="mute_minutes"]').fill("3")
        frame.evaluate("qa.failNext('poke-milestones/rules/save','throw')")
        save.click()
        expect(reconcile).to_be_visible()
        expect(save).to_be_disabled()
        assert len(posts(frame)) == 2
        assert frame.evaluate("qa.milestones().rules.length") == 2
        reconcile.click()
        expect(reconcile).not_to_be_visible()
        expect(editor).to_be_visible()
        expect(save).to_be_enabled()
        expect(editor.locator('[name="name"]')).to_have_value("提交前连接失败")
        expect(editor.locator('[name="mute_minutes"]')).to_have_value("3")
        save.click()
        expect(editor).not_to_be_visible()
        expect(frame.locator("#milestone-rules")).to_contain_text("提交前连接失败")
        assert len(posts(frame)) == 3
        assert frame.evaluate("qa.milestones().rules.length") == 3


def test_unavailable_controls_and_refresh_recovery(browser: Browser, url: str):
    with fixture_page(browser, url) as (_, frame):
        poke_ready(frame)
        frame.evaluate("qa.runtime('poke_milestones_available',false)")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_disabled()
        expect(frame.locator("#milestone-add")).to_be_disabled()

        frame.evaluate("qa.runtime('poke_milestones_available',true)")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_enabled()
        expect(frame.locator("#milestone-add")).to_be_enabled()

        frame.evaluate("qa.failNext('poke-milestones/state','throw')")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_disabled()
        expect(frame.locator("#milestone-add")).to_be_disabled()
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_enabled()

        # A string "false" is truthy in JavaScript; wrong runtime flag types
        # must never make unsafe controls actionable.
        frame.evaluate("qa.runtime('poke_milestones_available','false')")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_disabled()
        expect(frame.locator("#milestone-add")).to_be_disabled()
        frame.evaluate("qa.runtime('poke_milestones_available',true)")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_enabled()

        frame.evaluate("qa.runtime('plugin_loaded',false)")
        refresh(frame)
        expect(frame.locator("#milestone-global")).to_be_disabled()
        expect(frame.locator("#milestone-add")).to_be_disabled()
        expect(frame.locator("#plugin-status")).not_to_contain_text("控制中心在线")
        nav(frame, "moderation")
        expect(frame.locator('[data-key="mute_tool_enabled"]')).to_be_disabled()
        frame.evaluate("qa.runtime('plugin_loaded',true)")
        refresh(frame)
        expect(frame.locator('[data-key="mute_tool_enabled"]')).to_be_enabled()

        nav(frame, "qzone")
        frame.evaluate("qa.qzoneEnabled(false)")
        refresh(frame)
        expect(frame.locator("#qzone-scan")).to_be_disabled()
        frame.evaluate("qa.qzoneEnabled(true); qa.runtime('onebot_client_available',false)")
        refresh(frame)
        expect(frame.locator("#qzone-scan")).to_be_disabled()
        nav(frame, "requests")
        expect(frame.locator('[data-request-approve="A001"]')).to_be_disabled()
        frame.evaluate("qa.runtime('onebot_client_available','false')")
        refresh(frame)
        expect(frame.locator('[data-request-approve="A001"]')).to_be_disabled()
        nav(frame, "qzone")
        expect(frame.locator("#qzone-scan")).to_be_disabled()
        frame.evaluate("qa.runtime('onebot_client_available',true)")
        refresh(frame)
        expect(frame.locator("#qzone-scan")).to_be_enabled()
        nav(frame, "requests")
        expect(frame.locator('[data-request-approve="A001"]')).to_be_enabled()
        assert not posts(frame)


def test_other_side_effects_and_preserved_draft(browser: Browser, url: str):
    with fixture_page(browser, url) as (_, frame):
        nav(frame, "chat")
        keyword_draft = frame.locator('[data-key="keyword_wakeup_words"]')
        expect(keyword_draft).to_be_enabled()
        keyword_draft.fill("未保存的关键词")
        refresh(frame)
        expect(keyword_draft).to_have_value("未保存的关键词")
        assert not posts(frame)
        nav(frame, "moderation")
        mute = frame.locator('[data-key="mute_tool_enabled"]')
        save = frame.locator('form[data-section="moderation_settings"] button[type="submit"]')
        expect(mute).to_be_enabled()
        mute.check()
        save.click()
        expect_confirmation(frame, 0)
        cancel(frame)
        expect(mute).to_be_checked()  # Draft was not discarded by cancellation.
        assert frame.evaluate("qa.state().settings.mute_tool_enabled") is False

        for mode in ("throw", "status", "malformedRuntime"):
            old_count = len(posts(frame))
            frame.evaluate("(mode) => qa.failNext('console/settings', mode)", mode)
            save.click()
            expect_confirmation(frame, old_count)
            accept(frame)
            expect(frame.locator("#message.error")).to_be_visible()
            expect(save).to_be_enabled()
            expect(mute).to_be_checked()
            assert frame.evaluate("qa.state().settings.mute_tool_enabled") is False
            assert len(posts(frame)) == old_count + 1
        save.click()
        expect_confirmation(frame, 3)
        accept(frame)
        frame.wait_for_function("qa.state().settings.mute_tool_enabled")
        latest(frame, "console/settings", confirm_side_effects=True)
        assert posts(frame)[-1]["body"]["settings"] == {"mute_tool_enabled": True}
        expect(frame.locator("#refresh")).to_be_enabled()
        nav(frame, "chat")
        expect(keyword_draft).to_have_value("未保存的关键词")
        assert frame.evaluate("qa.state().settings.keyword_wakeup_words") == []

        nav(frame, "requests")
        approve = frame.locator('[data-request-approve="A001"]')
        old_count = len(posts(frame))
        approve.click()
        expect_confirmation(frame, old_count)
        cancel(frame)
        expect(approve).to_be_visible()
        assert len(posts(frame)) == old_count
        approve.click()
        expect_confirmation(frame, old_count)
        accept(frame)
        expect(approve).not_to_be_visible()
        latest(frame, "console/requests/review", request_id="A001", approve=True, confirm=True)

        nav(frame, "qzone")
        scan = frame.locator("#qzone-scan")
        old_count = len(posts(frame))
        scan.click()
        expect_confirmation(frame, old_count)
        cancel(frame)
        assert len(posts(frame)) == old_count
        scan.click()
        expect_confirmation(frame, old_count)
        accept(frame)
        expect(scan).to_be_enabled()
        latest(frame, "console/qzone/scan", confirm_side_effects=True)


def test_navigation_rank_identity_and_desktop_layout(browser: Browser, url: str):
    for width in (1100, 1450):
        with fixture_page(browser, url, width) as (page, frame):
            for theme in ("light", "dark"):
                frame.evaluate("(dark) => qa.dark(dark)", theme == "dark")
                expect(frame.locator("html")).to_have_attribute("data-theme", theme)
                for section in ("overview", "chat", "moderation", "requests", "poke", "qzone"):
                    nav(frame, section)
                    assert frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                        f"Page-level horizontal overflow: {width}px {theme} {section}"
                    )
                    shot_dir = os.environ.get("QA_SCREENSHOT_DIR")
                    if shot_dir:
                        path = Path(shot_dir)
                        path.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=path / f"{width}-{theme}-{section}.png")
            nav(frame, "poke")
            expect(frame.locator("#poke-daily-users")).to_contain_text("测试QQ昵称")
            expect(frame.locator("#poke-daily-users")).to_contain_text("9876543210")
            expect(frame.locator("#poke-history-users")).to_contain_text("12345678901")
            expect(frame.locator("#poke-daily-groups")).to_contain_text("测试群名称")
            expect(frame.locator("#poke-daily-groups")).to_contain_text("1122334455")
            expect(frame.locator("#milestone-history")).to_contain_text("2026-11-27")
            assert frame.locator('#poke-daily-users img[src="x"]').count() == 0
            assert not posts(frame)


TESTS = (
    test_refresh_unlocks_global,
    test_global_confirmation_cancel_success_and_retry,
    test_rule_editor_and_destructive_confirmation,
    test_new_rule_transport_failure_reconciles_before_retry,
    test_unavailable_controls_and_refresh_recovery,
    test_other_side_effects_and_preserved_draft,
    test_navigation_rank_identity_and_desktop_layout,
)


def main():
    selected = [test for test in TESTS if not sys.argv[1:] or test.__name__ in sys.argv[1:]]
    if not selected:
        raise SystemExit("No matching tests")
    failures = 0
    with fixture_server() as url, sync_playwright() as playwright:
        options = {"headless": True, "args": ["--no-sandbox"]}
        if os.environ.get("CHROME_PATH"):
            options["executable_path"] = os.environ["CHROME_PATH"]
        browser = playwright.chromium.launch(**options)
        try:
            for test in selected:
                try:
                    test(browser, url)
                    print(f"PASS {test.__name__}", flush=True)
                except Exception:
                    failures += 1
                    print(f"FAIL {test.__name__}", file=sys.stderr, flush=True)
                    traceback.print_exc()
        finally:
            browser.close()
    print(f"{len(selected)-failures}/{len(selected)} browser scenarios passed", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
