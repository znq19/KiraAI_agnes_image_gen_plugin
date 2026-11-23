"""agnes_image_gen：合并转发可配置超时 + 超时降级为逐张直发。

    python3 tests/test_forward_timeout.py
"""
import asyncio
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Logger:
    def debug(self, m, *a): pass
    def info(self, m, *a): pass
    def warning(self, m, *a): pass
    def error(self, m, *a): pass
    def exception(self, m, *a): pass


LOGGER = _Logger()


class BasePlugin:
    def __init__(self, ctx=None, cfg=None):
        self.ctx = ctx
        self.cfg = cfg or {}


class _Reg:
    def tool(self, **kw):
        def deco(fn): return fn
        return deco

    def __getattr__(self, _):
        def deco(*a, **kw):
            if a and callable(a[0]): return a[0]
            def inner(fn): return fn
            return inner
        return deco


class _On:
    def __getattr__(self, _):
        def deco(*a, **kw):
            if a and callable(a[0]): return a[0]
            def inner(fn): return fn
            return inner
        return deco


_mod("core")
_mod("core.plugin", BasePlugin=BasePlugin, logger=LOGGER, on=_On(), Priority=object(), register=_Reg())
_mod("core.chat", MessageChain=lambda els: els)
_mod("core.chat.message_elements", Image=lambda *a, **kw: a, Text=lambda *a, **kw: a)
_mod("core.chat.message_utils", KiraMessageEvent=object, KiraMessageBatchEvent=object)
_mod("core.provider", LLMRequest=object)
_mod("core.utils")
_mod("core.utils.path_utils", get_data_path=lambda: "/tmp/agnes_test_data")
_mod("aiohttp", ClientSession=object, ClientTimeout=lambda **kw: kw)

import importlib.util
spec = importlib.util.spec_from_file_location("agnes_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Plugin = mod.AgnesImageGenPlugin

FAILS = []


def check(label, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)
    return cond


class FakeClient:
    def __init__(self, mode="ok"):
        self.calls = []
        self.timeouts = []
        self.mode = mode

    async def send_action(self, action, params, timeout=10.0):
        self.calls.append((action, dict(params)))
        self.timeouts.append(timeout)
        if self.mode == "timeout":
            raise asyncio.TimeoutError(f"请求 {action} 超时")
        if self.mode == "conn":
            raise ConnectionError(f"NapCat WebSocket 未连接，无法执行 {action}")
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


class _Adapter:
    def __init__(self, client):
        self._client = client
        self.config = {"nickname": "测试Bot"}

    def get_client(self):
        return self._client


class _AdapterMgr:
    def __init__(self, client):
        self._a = _Adapter(client)

    def get_adapter(self, name):
        return self._a


class FakeCtx:
    def __init__(self, client):
        self.adapter_mgr = _AdapterMgr(client)
        self.direct_sent = []

    async def send_message_chain(self, sid, chain):
        self.direct_sent.append(chain)
        return type("R", (), {"ok": True})()


class _Sess:
    sid = "qq:gm:12345"


class _EvtAdapter:
    platform = "QQ"
    name = "qq"


class FakeEvent:
    sid = "qq:gm:12345"
    session = _Sess()
    adapter = _EvtAdapter()
    self_id = "10001"
    messages = []


def make_plugin(client, forward_timeout=60.0):
    p = Plugin.__new__(Plugin)
    p.ctx = FakeCtx(client)
    p.send_as_forward = True
    p.forward_timeout = forward_timeout
    p.save_generated = False
    p.image_storage_dir = "files/agnes"
    p.max_cache_files = 100
    p.proxy = ""
    p._storage_dir = None
    p._cache_cleanup_task = None
    p._send_locks = {}
    p._session_counts = {}
    p._gen_tasks = {}
    return p


PATHS = ["/tmp/agnes_test_data/a.png", "/tmp/agnes_test_data/b.png"]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


print("═══ 1) 正常：转发成功，只调一次，带 60s")
c = FakeClient("ok")
p = make_plugin(c)
ok = run(p._send_forward_images(FakeEvent(), PATHS))
check("返回 True", ok is True, repr(ok))
check("只调用 1 次", len(c.calls) == 1, str([a for a, _ in c.calls]))
check("★ 显式传 60s", c.timeouts == [60.0], str(c.timeouts))
check("群聊用 group_id", c.calls[0][1].get("group_id") == 12345)

print()
print("═══ 2) ★ 超时 ⇒ 降级逐张直发（宁可不吞消息）")
c2 = FakeClient("timeout")
p2 = make_plugin(c2)
errs = run(p2._send_paths(FakeEvent(), PATHS))
check("★ 超时后返回 False", run(make_plugin(FakeClient("timeout"))._send_forward_images(FakeEvent(), PATHS)) is False)
check("★ 降级逐张直发 2 条", len(p2.ctx.direct_sent) == 2, str(len(p2.ctx.direct_sent)))

print()
print("═══ 3) 明确故障（连接断开）⇒ 同样降级")
c3 = FakeClient("conn")
p3 = make_plugin(c3)
run(p3._send_paths(FakeEvent(), PATHS))
check("降级逐张直发 2 条", len(p3.ctx.direct_sent) == 2, str(len(p3.ctx.direct_sent)))

print()
print("═══ 4) forward_timeout 可配置")
c4 = FakeClient("ok")
p4 = make_plugin(c4, forward_timeout=25)
run(p4._send_forward_images(FakeEvent(), PATHS))
check("传入 25s", c4.timeouts == [25.0], str(c4.timeouts))

c5 = FakeClient("ok")
p5 = make_plugin(c5, forward_timeout=0)
run(p5._send_forward_images(FakeEvent(), PATHS))
check("0 ⇒ 用框架默认 10s", c5.timeouts == [10.0], str(c5.timeouts))

print()
print("═══ 5) 老核心（无 timeout 形参）自动退回")
class OldClient:
    def __init__(self):
        self.calls = []

    async def send_action(self, action, params):
        self.calls.append(action)
        return {"status": "ok", "retcode": 0}


c6 = OldClient()
p6 = make_plugin(c6)
ok6 = run(p6._send_forward_images(FakeEvent(), PATHS))
check("老核心下仍发送成功", ok6 is True, repr(ok6))
check("调用 1 次", len(c6.calls) == 1, str(c6.calls))

print()
if FAILS:
    print(f"✗ 失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("✓ 全部通过")
