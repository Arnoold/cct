#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_cct.py —— cct.py 的自动化测试。改完代码跑一遍，全绿再用。

    python3 test_cct.py          （Windows：py -3 test_cct.py）

全程在临时沙盒里跑：HOME、数据目录、shell 配置都指到临时目录；查用量用假数据，不联网；
不会碰本机真实的账号、~/.zshrc、钥匙串或环境变量。
"""
import os, sys, json, time, shutil, tempfile, threading, subprocess, importlib.util, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = tempfile.mkdtemp(prefix="cct_test_")
HOME = os.path.join(SANDBOX, "home")
os.makedirs(HOME)
os.environ.update({
    "HOME": HOME, "USERPROFILE": HOME,
    "CCT_STORE": os.path.join(HOME, ".cc_tokens"),
    "CCT_ZSHRC": os.path.join(HOME, ".zshrc"),
    "CCT_PORT": "0",
    "CLAUDE_CONFIG_DIR": os.path.join(HOME, ".claude"),
})
for v in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(v, None)

spec = importlib.util.spec_from_file_location("cct", os.path.join(HERE, "cct.py"))
cct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cct)
if cct.IS_WIN:                                                      # Windows 上别动真实注册表
    cct.win_set_env = lambda *a: None

# ---- 假的查用量：token 最后一段决定结果，不联网
NOW = int(time.time())


PROBED = []          # (token 尾巴, 用的代理)：检查每个号是不是走自己的代理


def fake_probe(tok, proxy=cct.CLAUDE):
    tag = tok.rsplit("-", 1)[-1]
    PROBED.append((tag, proxy))
    if tag.startswith("BAD"):
        return {"state": "invalid", "error": "OAuth access token is invalid.", "checked_at": NOW}
    util5 = {"FULL": 1.0, "LOW": 0.1, "MID": 0.5}.get(tag[:4].rstrip("0123456789"), 0.3)
    b = {"5h": {"util": util5, "status": "rejected" if util5 >= 1 else "allowed", "reset": NOW + 3600},
         "7d": {"util": 0.2, "status": "allowed", "reset": NOW + 86400}}
    return {"state": "limited" if util5 >= 1 else "ok", "checked_at": time.time(), "org": "org-" + tag,
            "fable": False, "fable_why": "credits_required", "buckets": b}


cct.probe = fake_probe
cct.egress_ip = lambda *a, **k: {"ip": "203.0.113.10", "country": "US", "city": "Los Angeles", "org": "test"}
# 进程检查用假的：FAKE_PROCS 里有什么就当作有什么 claude 在运行
FAKE_PROCS = []
KILLED = []
cct.claude_processes = lambda: list(FAKE_PROCS)
cct.kill_processes = lambda procs: (KILLED.extend(procs), FAKE_PROCS.clear())
SP = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
# 网络环境也用假的：NET 里写「直连走哪张网卡 / 系统代理 / 到入口的握手毫秒」
_real_iface, _real_sysp, _real_rtt = cct.iface_to, cct.system_proxy, cct.tls_rtt
NET = {"iface": {"name": "en0", "desc": "", "tunnel": False}, "sysproxy": None, "rtt": 40}
cct.iface_to = lambda host: dict(NET["iface"]) if NET["iface"] else None
cct.system_proxy = lambda: NET["sysproxy"]
cct.tls_rtt = lambda host, port, timeout=5: NET["rtt"]


def senv():
    try:
        return json.load(open(SP, encoding="utf-8")).get("env") or {}
    except (OSError, ValueError):
        return {}

T = lambda tag: "sk-ant-oat01-" + "x" * 30 + "-" + tag

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond or not detail else "  —— " + str(detail)))


def raises(fn, *a):
    try:
        fn(*a)
        return False
    except cct.CctError:
        return True


def cur():
    return cct.load()["active"]


print("沙盒：", SANDBOX)

print("\n[1] 添加")
cct.add_account("A", T("LOW1"))
check("新加的号不会自动启用（启用 = 切换，要先关 claude）", cur() is None)
cct.use_account("A")
cct.add_account("B", T("MID1"))
check("第二个号不会自动启用", cur() == "A")
check("同一个 token 不能重复添加", raises(cct.add_account, "A2", T("LOW1")))
check("名字不能重复", raises(cct.add_account, "A", T("LOW2")))
check("不像 setup token 的拒绝", raises(cct.add_account, "X", "hello"))
check("无效 token 拒绝", raises(cct.add_account, "X", T("BAD1")))
r = cct.add_account("随便", "sk-ant-oat01-" + "y" * 30 + "-LOW1")   # 同一个组织 org-LOW1
check("同一个号的新 token 替换旧的，名字不变", r["updated"] and r["name"] == "A")

print("\n[2] 切换 / 普通登录")
cct.use_account("B")
check("切换到 B", cur() == "B")
cct.use_normal()
check("切到普通登录（active=None）", cur() is None)
cct.add_account("C", T("FULL1"))
check("普通登录时添加新号不会自动启用", cur() is None)
check("没有「批量查全部」的函数（避免同一出口同时冒出多个账号请求）", not hasattr(cct, "next_account") and not hasattr(cct, "pick_next"))
check("refresh_one 只查一个号", cct.refresh_one("C")["state"] in ("ok", "limited", "invalid", "neterr", "error"))
FAKE_PROCS.append({"pid": 4242, "kind": "终端里的 claude", "path": "/usr/local/bin/claude"})
try:
    cct.use_account("B")
    check("有 claude 在运行时拒绝切换", False)
except cct.ClaudeRunning as e:
    check("有 claude 在运行时拒绝切换（列出进程）", e.procs and e.procs[0]["pid"] == 4242 and cur() is None)
check("拒绝时 settings.json 没被改", senv().get(cct.ENV) == "")
cct.use_account("B", kill=True)
check("kill=True：先结束它们再切", cur() == "B" and KILLED and KILLED[-1]["pid"] == 4242)
_real_cp = cct.claude_processes
cct.claude_processes = lambda: (_ for _ in ()).throw(OSError("没有 ps"))
check("查不了进程时要确认（nops）", raises(cct.use_normal))
cct.use_normal(force=["nops"])
check("确认 nops 后照常切", cur() is None)
cct.claude_processes = _real_cp

print("\n[3] 删除")
cct.use_account("B")
FAKE_PROCS.append({"pid": 4343, "kind": "VS Code 里的 Claude 对话", "path": "x"})
check("删除当前号也算切换：有 claude 在运行就拒绝", raises(cct.delete_account, "B") and cct.find(cct.load(), "B"))
FAKE_PROCS.clear()
r = cct.delete_account("B")
check("删除当前号 → 改用普通登录（不悄悄换号）", r["wasActive"] and cur() is None and not cct.find(cct.load(), "B"))
cct.use_account("A")
r = cct.delete_account("C")
check("删除非当前号不影响当前", not r["wasActive"] and cur() == "A")
check("删除不存在的号报错", raises(cct.delete_account, "不存在"))

print("\n[4] cct token（启动函数用）")
env = dict(os.environ)
run_token = lambda: subprocess.run([sys.executable, os.path.join(HERE, "cct.py"), "token"],
                                   capture_output=True, text=True, env=env, timeout=30)
p = run_token()
check("有当前号：退出码 0，输出 token", p.returncode == 0 and p.stdout.strip() == cct.find(cct.load(), "A")["token"], p)
cct.use_normal()
p = run_token()
check("普通登录：退出码 3，不输出 token", p.returncode == 3 and not p.stdout.strip(), p)
cct.use_account("A")

print("\n[5] 启动 claude 时绝不联网")
import io, contextlib
real_probe = cct.probe
calls = []
cct.probe = lambda tok: calls.append(tok) or (_ for _ in ()).throw(AssertionError("不该联网"))
cct.add_account.__globals__["probe"] = cct.probe
with cct.txn() as d:
    cct.find(d, "A")["usage"] = {"state": "limited", "checked_at": 0,
                                 "buckets": {"5h": {"util": 1.0, "status": "rejected", "reset": NOW + 3600}}}
out = io.StringIO()
with contextlib.redirect_stdout(out):
    cct.cmd_token(None)
check("当前号就算显示用完了，cct token 也只输出当前 token、不联网、不换号",
      not calls and cur() == "A" and out.getvalue().strip() == cct.find(cct.load(), "A")["token"], calls)
cct.probe = real_probe

print("\n[6] 数据文件损坏自动恢复")
good = cct.load()
cct.save(good)                                   # 生成 .bak
with open(cct.DB, "w") as f:
    f.write("{ 写坏了")
with contextlib.redirect_stderr(io.StringIO()):
    d = cct.load()
check("accounts.json 写坏后自动用备份恢复", [a["name"] for a in d["accounts"]] == [a["name"] for a in good["accounts"]])
check("坏文件另存了一份", any(n.startswith("accounts.json.broken-") for n in os.listdir(cct.STORE)))
with open(cct.DB, "w") as f:
    f.write("坏")
os.remove(cct.BAK)
shutil.rmtree(cct.BACKUP_DIR, ignore_errors=True)
check("备份也没有时报错，不用空数据覆盖", raises(cct.load) and open(cct.DB).read() == "坏")
cct._write_json_atomic(cct.DB, good)

print("\n[7] settings.json：切换时写 token（IDE 和终端都读它）")
cct.use_account("A")
tokA = cct.find(cct.load(), "A")["token"]
check("当前号的 token 写进 settings.json 的 env", senv().get(cct.ENV) == tokA)
if not cct.IS_WIN:
    check("settings.json 权限 600（里面有 token）", oct(os.stat(SP).st_mode & 0o777) == "0o600")
js = json.load(open(SP))
js["permissions"] = {"allow": ["Bash(ls)"]}
js["env"]["NO_PROXY"] = "localhost"
json.dump(js, open(SP, "w"))
cct.use_normal()
check("普通登录：token 写成空字符串（盖住终端里残留的旧 token）", senv().get(cct.ENV) == "")
check("settings.json 里别的设置原样保留", json.load(open(SP)).get("permissions") == {"allow": ["Bash(ls)"]}
      and senv().get("NO_PROXY") == "localhost")
check("没绑过代理时，代理变量完全不碰", not any(k in senv() for k in cct.PROXY_KEYS))
check("改之前备份过（token 打码）", any(n.startswith("settings.json.") for n in os.listdir(cct.CONFIG_BACKUP_DIR))
      and all(tokA not in open(os.path.join(cct.CONFIG_BACKUP_DIR, n)).read()
              for n in os.listdir(cct.CONFIG_BACKUP_DIR) if n.startswith("settings.json.")))
open(SP, "w").write("{ 写坏了")
check("settings.json 不是合法 JSON：报错、不覆盖", raises(cct.use_account, "A") and open(SP).read() == "{ 写坏了")
os.remove(SP)
cct.use_account("A")

print("\n[7b] 一个号一个 IP")
P1, P2, P3 = "https://p1:pw@1.1.1.1:32230", "https://p2:pw@2.2.2.2:32230", "https://n:pw@3.3.3.3:32230"
EG = {P1: {"ip": "9.9.9.1", "country": "US", "city": "LAX", "org": ""}, P2: {"ip": "9.9.9.2", "country": "US", "city": "SJC", "org": ""},
      P3: {"ip": "9.9.9.3", "country": "US", "city": "SEA", "org": ""}, cct.ENVP: {"ip": "9.9.9.0", "country": "US", "city": "", "org": ""}}
cct.egress_ip = lambda force=False, timeout=8, proxy=cct.CLAUDE: dict(EG.get(proxy) or {"error": "查不到出口 IP"})
check("代理格式检查", raises(cct.set_proxy, "A", "socks5://1.2.3.4:1080") and raises(cct.set_proxy, "A", "1.2.3.4:8080"))
cct.set_proxy("A", P1)
check("给当前号绑代理：立即写进 settings.json（四个代理变量）", all(senv().get(k) == P1 for k in cct.PROXY_KEYS))
check("绑过之后进入「一个号一个 IP」模式", cct.load() and cct.bound(cct.load()))
check("绑代理时自动写上 NO_PROXY（和中转配置命令一样的名单）", senv().get("NO_PROXY") == cct.NO_PROXY_DEFAULT
      and senv().get("no_proxy") == cct.NO_PROXY_DEFAULT, senv())
check("ALL_PROXY 置空（防 Clash 的 all_proxy 绕开中转）", senv().get("ALL_PROXY") == "" and senv().get("all_proxy") == "")
js = json.load(open(SP))
for k in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
    js["env"].pop(k, None)
json.dump(js, open(SP, "w"))
cct.sync_settings(write_proxy=False)
check("原来没有 NO_PROXY：补上和中转配置命令一样的名单（平时同步也补，不用等切换）",
      senv().get("NO_PROXY") == cct.NO_PROXY_DEFAULT and senv().get("no_proxy") == cct.NO_PROXY_DEFAULT, senv())
check("补 NO_PROXY 不动代理本身", senv().get("HTTPS_PROXY") == P1)
js = json.load(open(SP))
js["env"]["NO_PROXY"] = js["env"]["no_proxy"] = "intranet.corp,localhost"
json.dump(js, open(SP, "w"))
cct.sync_settings(write_proxy=False)
check("客户自己加的 NO_PROXY 保留", senv().get("NO_PROXY") == "intranet.corp,localhost,127.0.0.1", senv())
js = json.load(open(SP))
js["env"]["NO_PROXY"], js["env"]["no_proxy"] = "a.corp", "b.corp,localhost"
json.dump(js, open(SP, "w"))
cct.sync_settings(write_proxy=False)
check("NO_PROXY 和 no_proxy 内容不一样：取并集，哪边的都不丢", senv().get("NO_PROXY") == "a.corp,b.corp,localhost,127.0.0.1"
      and senv().get("no_proxy") == senv().get("NO_PROXY"), senv())
check("卡片上显示代理时不露密码", "pw" not in json.dumps(cct.build_state()) and "p1@1.1.1.1:32230" in json.dumps(cct.build_state()))
cct.add_account("D", T("LOW4"), P2)
check("添加时验证 token 走它自己的代理", PROBED[-1] == ("LOW4", P2))
check("添加时用的代理已被占用 → 要确认（dup）", raises(cct.add_account, "E", T("LOW5"), P1))
check("没填代理 → 要确认（unbound）", raises(cct.add_account, "E", T("LOW5")))
cct.refresh_one("A")
check("查 A 的用量走 A 的代理", PROBED[-1] == ("LOW1", P1))
cct.refresh_one("D")
check("查 D 的用量走 D 的代理（不借当前号 A 的）", PROBED[-1] == ("LOW4", P2))
check("记住了 D 的出口 IP", (cct.find(cct.load(), "D").get("egress") or {}).get("ip") == "9.9.9.2")
cct.use_account("D")
check("切到 D：token 和代理一起换成 D 的", senv().get(cct.ENV) == cct.find(cct.load(), "D")["token"]
      and all(senv().get(k) == P2 for k in cct.PROXY_KEYS))
cct.add_account("C", T("LOW6"), force=["unbound"])
check("确认后能加没绑代理的号，验证时不借别的号的代理", PROBED[-1] == ("LOW6", cct.ENVP))
check("没绑代理的号 C 要确认才能切", raises(cct.use_account, "C") and cur() == "D")
check("没绑代理的号查用量也要确认", raises(cct.refresh_one, "C"))
cct.use_account("C", force=["unbound"])
check("确认后切到没绑代理的号：settings.json 里的代理去掉，绝不留着上一个号的",
      cur() == "C" and not any(k in senv() for k in cct.PROXY_KEYS))
check("没绑代理的号：查用量 / 切换只看终端代理变量，不借 settings.json 里别的号的代理",
      cct.proxy_of(cct.load(), cct.find(cct.load(), "C")) == cct.ENVP)
_saved_env = {k: os.environ.pop(k, None) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")}
check("顶部线路说明：没绑代理、终端也没设代理 → 直连 / TUN", "直连" in cct.route_of(cct.load(), cct.find(cct.load(), "C"))[1])
os.environ["https_proxy"] = "http://127.0.0.1:7890"
check("顶部线路说明：没绑代理、终端设了代理 → 写明是终端代理变量",
      cct.route_of(cct.load(), cct.find(cct.load(), "C"))[1] == "终端的代理环境变量 127.0.0.1:7890")
del os.environ["https_proxy"]
for _k, _v in _saved_env.items():
    if _v is not None:
        os.environ[_k] = _v
check("顶部线路说明：绑了代理 → 中转 + 中转账号", cct.route_of(cct.load(), cct.find(cct.load(), "D"))[1] == "中转 p2")
check("普通登录没绑代理：也要确认", raises(cct.use_normal))
EG[cct.ENVP] = {"ip": "1.2.3.4", "country": "CN", "city": "", "org": ""}
cct._ip_cache.clear()
_saved_env = {k: os.environ.pop(k, None) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")}
try:
    cct.use_normal(force=["unbound"])
    check("确认了没绑代理，但切过去实际出口是大陆 IP → 再拦一次（ip）", False)
except cct.NeedForce as e:
    check("确认了没绑代理，但切过去实际出口是大陆 IP → 再拦一次（ip）", e.code == "ip" and "直连" in str(e), str(e))
for _k, _v in _saved_env.items():      # 在 claude 里跑测试时，环境里就有 claude 的代理，测完放回去
    if _v is not None:
        os.environ[_k] = _v
EG[cct.ENVP] = {"ip": "9.9.9.0", "country": "US", "city": "", "org": ""}
cct._ip_cache.clear()
cct.set_proxy(None, P3, force=["nops"])
cct.use_normal()
check("普通登录用它自己绑的代理", senv().get(cct.ENV) == "" and senv().get("HTTPS_PROXY") == P3)
EG[P2] = {"ip": "1.2.3.4", "country": "CN", "city": "", "org": ""}
cct._ip_cache.clear()
check("目标号的代理出口是大陆 IP → 拦下（ip）", raises(cct.use_account, "D") and cur() is None)
EG[P2] = {"error": "查不到出口 IP"}
check("目标号的代理不通 → 拦下", raises(cct.use_account, "D"))
EG[P2] = {"ip": "9.9.9.2", "country": "US", "city": "SJC", "org": ""}
cct.use_account("D")
js = json.load(open(SP))
js["env"]["HTTPS_PROXY"] = "https://x:y@5.5.5.5:1"
json.dump(js, open(SP, "w"))
check("settings.json 的代理被别的命令改了 → 发现对不上", cct.settings_drift(cct.load()) is not None)
cct.use_account("D")
check("重新切一次当前号就按绑定改回", cct.settings_drift(cct.load()) is None and senv().get("HTTPS_PROXY") == P2)
_w = cct.IS_WIN
cct.IS_WIN = True
try:
    cct._sync_settings(write_proxy=True)     # 只把写 settings.json 这一步当成 Windows（文件锁等别的部分不模拟）
    check("Windows：只写大写的代理变量，小写的删掉（大小写是同一个变量，别留旧值）",
          senv().get("HTTPS_PROXY") == P2 and senv().get("HTTP_PROXY") == P2
          and "https_proxy" not in senv() and "http_proxy" not in senv(), senv())
    check("Windows：NO_PROXY / ALL_PROXY 也只写大写的", "NO_PROXY" in senv() and senv().get("ALL_PROXY") == ""
          and "no_proxy" not in senv() and "all_proxy" not in senv(), senv())
finally:
    cct.IS_WIN = _w
cct.use_account("D")
FAKE_PROCS.append({"pid": 77, "kind": "终端里的 claude", "path": "claude"})
check("改当前号的代理也要先关 claude", raises(cct.set_proxy, "D", "https://p4:pw@4.4.4.4:1"))
check("改别的号的代理不用关 claude", not raises(cct.set_proxy, "C", "https://p5:pw@6.6.6.6:1", ["ip"]))
FAKE_PROCS.clear()
lcl = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.local.json")
json.dump({"env": {"HTTPS_PROXY": "http://127.0.0.1:7890", cct.ENV: "sk-ant-oat01-zzz"}}, open(lcl, "w"))
found = {c["what"] for c in cct.auth_conflicts() if c["where"] == lcl}
check("优先级更高的配置里写了 token / 代理 → 当作冲突提示", {"env." + cct.ENV, "env.HTTPS_PROXY"} <= found, found)
check("cct 自己管的 settings.json 里的 token / 代理不算冲突", not [c for c in cct.auth_conflicts() if c["where"] == SP])
os.remove(lcl)
cct.egress_ip = lambda *a, **k: {"ip": "203.0.113.10", "country": "US", "city": "Los Angeles", "org": "test"}

print("\n[7b2] claude 实际走哪条线（顶部那一行）")
_saved_env = {k: os.environ.pop(k, None) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")}
cct.set_proxy("C", None)          # 前面给 C 绑过 p5，这里要一个没绑代理的号
d = cct.load()
D, C = cct.find(d, "D"), cct.find(d, "C")
US = {"ok": True, "info": {"ip": "9.9.9.2", "country": "US"}, "why": ""}
CNST = {"ok": False, "info": {"ip": "1.2.3.4", "country": "CN"}, "why": "出口是中国大陆 IP"}
RI = lambda acc, st=US: cct.route_info(cct.load(), acc, st)
TL = lambda r: (r["tag"], r["level"])
r = RI(D)
check("绑了中转 → [中转] p2", TL(r) == ("中转", "ok") and r["detail"] == "p2", r)
NET["iface"] = {"name": "utun6", "desc": "", "tunnel": True}
r = RI(D)
check("连入口进了 TUN，但握手快（Clash 对国内 IP 直连）→ 正常，说明放在悬停提示里", TL(r) == ("中转", "ok") and "是直连" in r["tip"], r)
NET["rtt"] = 380
r = RI(D)
check("连入口进了 TUN 且握手慢 → [中转] 琥珀：入口被 TUN 绕路", TL(r) == ("中转", "warn") and "绕路" in r["detail"], r)
NET["rtt"] = 40
r = RI(C)
check("没绑代理、直连被 TUN 接管 → [TUN] utun6", TL(r) == ("TUN", "ok") and r["detail"] == "utun6", r)
NET["iface"] = {"name": "en0", "desc": "", "tunnel": False}
NET["sysproxy"] = "http://127.0.0.1:7897"
r = RI(C, CNST)
check("只开了系统代理、没 TUN → [直连] 红：claude 不认系统代理", TL(r) == ("直连", "bad") and "系统代理" in r["detail"], r)
NET["sysproxy"] = None
r = RI(C, CNST)
check("什么都没有、出口大陆 → [直连] 红", TL(r) == ("直连", "bad"), r)
r = RI(C)
check("什么都没有、出口却在海外 → [直连] 出口在海外（路由器 / 机器在海外）", TL(r) == ("直连", "ok") and "路由器" in r["tip"], r)
os.environ["https_proxy"] = "http://127.0.0.1:7890"
r = RI(C)
check("终端设了本机代理端口 → [端口] 127.0.0.1:7890 · 终端变量", TL(r) == ("端口", "ok") and r["detail"] == "127.0.0.1:7890 · 终端变量", r)
os.environ["https_proxy"] = "http://10.0.0.1:3128"
r = RI(C)
check("终端设了别的远程代理 → [代理]", TL(r) == ("代理", "ok"), r)
os.environ["https_proxy"] = P2
r = RI(C)
check("终端里的代理正好是别的号的中转 → [中转] 红：会借用它的 IP", TL(r) == ("中转", "bad") and "「D」的中转" in r["detail"], r)
check("切到这种号之前也会拦下（ip）", raises(cct.use_account, "C", ["unbound"]) and cur() != "C")
_np = len(PROBED)
check("这种号查用量也会拦下，不会让它的 token 从别的号的 IP 发出去", raises(cct.refresh_one, "C", ["unbound"]) and len(PROBED) == _np)
cct.refresh_one("C", ["unbound", "ip"])
check("确认后才查，而且不把这次的出口记成它的代理出口", len(PROBED) == _np + 1 and not cct.find(cct.load(), "C").get("egress"))
del os.environ["https_proxy"]
# 写在 shell 配置文件里的代理：每个新开的终端都会带上
_rc = os.environ["CCT_ZSHRC"]
_rc_old = open(_rc).read() if os.path.exists(_rc) else None
open(_rc, "w").write("alias ll='ls -l'\nproxy_on() {\n  export https_proxy=http://127.0.0.1:1111\n}\n"
                    "export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897 all_proxy=socks5://127.0.0.1:7897\n")
cct._persist_cache["key"] = None
r = RI(C)
check("读到 .zshrc 里 Clash「复制终端代理命令」那行 → [端口] · ~/.zshrc（函数里缩进的不算）",
      TL(r) == ("端口", "ok") and r["detail"] == "127.0.0.1:7897 · ~/" + os.path.basename(_rc), r)
open(_rc, "a").write("export HTTPS_PROXY=%s\n" % P2)
cct._persist_cache["key"] = None
r = RI(C)
check(".zshrc 里写死了别的号的中转 → 红，并提示去删那一行", TL(r) == ("中转", "bad") and "里那行要删掉" in r["tip"], r)
if _rc_old is None:
    os.remove(_rc)
else:
    open(_rc, "w").write(_rc_old)
cct._persist_cache["key"] = None
json.dump(dict(json.load(open(SP)), env=dict(senv(), ANTHROPIC_BASE_URL="https://api.some-relay.com")), open(SP, "w"))
r = RI(D)
check("设了 ANTHROPIC_BASE_URL → [第三方] 红：cct 的数据不作数", TL(r) == ("第三方", "bad") and r["detail"] == "api.some-relay.com", r)
check("冲突检查里也列出 ANTHROPIC_BASE_URL", any(c["what"] == "env.ANTHROPIC_BASE_URL" for c in cct.auth_conflicts()))
js = json.load(open(SP)); js["env"].pop("ANTHROPIC_BASE_URL"); json.dump(js, open(SP, "w"))
proj = os.path.join(SANDBOX, "proj1")
os.makedirs(os.path.join(proj, ".claude"))
json.dump({"env": {"HTTPS_PROXY": "http://10.0.0.1:3128", "ANTHROPIC_BASE_URL": "https://x.example"}},
          open(os.path.join(proj, ".claude", "settings.json"), "w"))
cj = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
json.dump({"projects": {proj: {}, HOME: {}, "/不存在的目录": {}}}, open(cj, "w"))
hits = {c["what"] for c in cct.auth_conflicts() if c["where"].startswith(proj)}
check("项目目录里写的代理和中转站会列出来", {"env.HTTPS_PROXY", "env.ANTHROPIC_BASE_URL"} <= hits, hits)
check("家目录当项目时不把用户级 settings.json 重复算", not [c for c in cct.auth_conflicts() if c["where"] == SP and "项目" in c["why"]])
os.remove(cj)
for _k, _v in _saved_env.items():
    if _v is not None:
        os.environ[_k] = _v
if not cct.IS_WIN:
    try:
        _f = _real_iface("api.anthropic.com")
        check("真机：能问到直连 Anthropic 走哪张网卡", _f is None or _f.get("name"), _f)
    except Exception as ex:
        check("真机：能问到直连 Anthropic 走哪张网卡", False, ex)

print("\n[7c] 认 claude 进程")
ic = cct._is_claude
check("原生版 / npm 版（进程名 claude）", ic("/Users/x/.local/bin/claude", "/Users/x/.local/bin/claude") and ic("claude", "claude"))
check("versions 目录里直接跑的原生版", ic("/Users/x/.local/share/claude/versions/2.1.310", "/Users/x/.local/share/claude/versions/2.1.310 -c"))
check("IDE 插件起的", ic("/Users/x/.antigravity-ide/extensions/anthropic.claude-code-2.1.292-darwin-arm64/resources/native-binary/claude", "x"))
check("node 跑 cli.js", ic("/usr/local/bin/node", "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"))
check("不误认：桌面版本体 Claude、cct 自己、别的 node", not ic("/Applications/Claude.app/Contents/MacOS/Claude", "")
      and not ic("/usr/bin/python3", "python3 cct.py") and not ic("/usr/local/bin/node", "node server.js"))
check("IDE 名字认得出", "Antigravity" in cct._proc_kind("/Users/x/.antigravity-ide/extensions/anthropic.claude-code-2/claude")
      and "VS Code" in cct._proc_kind(r"C:\Users\x\.vscode\extensions\anthropic.claude-code-2\resources\claude.exe"))
_w = cct.IS_WIN
cct.IS_WIN = True
check("Windows：桌面版本体 claude.exe 不算，Claude Code 的 claude.exe 算",
      not ic(r"C:\Users\x\AppData\Local\AnthropicClaude\app-0.9\claude.exe", "")
      and ic(r"C:\Users\x\.local\bin\claude.exe", "claude.exe"))
cct.IS_WIN = _w
if not cct.IS_WIN:
    try:
        rows = _real_cp()
        check("真机上能列进程（不报错）", isinstance(rows, list))
    except Exception as ex:
        check("真机上能列进程（不报错）", False, ex)

if not cct.IS_WIN:
    print("\n[8] 安装 / 卸载（沙盒里的 .zshrc）")
    rc = os.environ["CCT_ZSHRC"]
    old_block = "\n".join([cct.BEGIN, "claude() {", "  command claude \"$@\"", "}", cct.END])
    with open(rc, "w") as f:
        f.write('export PATH="$HOME/bin:$PATH"\nexport CLAUDE_CODE_OAUTH_TOKEN="%s"\n\n%s\n' % (T("LOW9"), old_block))
    open(cct.CUR_TOKEN_FILE, "w").write("old")
    legacy = rc + ".cct-bak-20260101000000"
    with open(legacy, "w") as f:
        f.write('export CLAUDE_CODE_OAUTH_TOKEN="%s"\n' % T("LOW9"))
    with contextlib.redirect_stdout(io.StringIO()):
        cct.cmd_install(None)
    text = open(rc).read()
    check("旧版本留在家目录的 *.cct-bak-* 被挪走并打码", not os.path.exists(legacy)
          and any("已打码" in open(os.path.join(cct.CONFIG_BACKUP_DIR, n)).read()
                  for n in os.listdir(cct.CONFIG_BACKUP_DIR) if n.startswith("zshrc.cct-bak")))
    check("原来写死的 token 被导入并注释掉", any(a["token"] == T("LOW9") for a in cct.load()["accounts"])
          and 'export CLAUDE_CODE_OAUTH_TOKEN="sk' not in text, text)
    check("旧版的 claude 启动函数被移除", cct.BEGIN not in text and "claude()" not in text and "export PATH" in text)
    check("旧版的 current_token 文件删掉", not os.path.exists(cct.CUR_TOKEN_FILE))
    with contextlib.redirect_stdout(io.StringIO()):
        cct.cmd_install(None)
    check("重复安装不重复写", open(rc).read() == text)
    home_baks = [n for n in os.listdir(HOME) if ".cct-bak" in n]
    cfg_dir = cct.CONFIG_BACKUP_DIR
    baks = [os.path.join(cfg_dir, n) for n in os.listdir(cfg_dir)] if os.path.isdir(cfg_dir) else []
    check("配置备份放在 ~/.cc_tokens/backups/config，不留在家目录", baks and not home_baks, (home_baks, baks))
    check("配置备份里没有完整 token（已打码）", baks and all(T("LOW9") not in open(p).read() for p in baks))
    check("配置备份文件权限 600", baks and all(oct(os.stat(p).st_mode & 0o777) == "0o600" for p in baks))
    check("程序复制到 app/cct.py", os.path.exists(cct.APP))

    class A: purge = False
    with contextlib.redirect_stdout(io.StringIO()):
        cct.cmd_uninstall(A)
    check("卸载：settings.json 去掉 token，代理恢复成普通登录绑的", cct.ENV not in senv() and senv().get("HTTPS_PROXY") == P3)
    check("卸载保留账号数据", len(cct.load()["accounts"]) > 0)

print("\n[8b] 从旧版升级：settings.json 里的中转代理绑给当前号")
_store = cct.load()
cct._write_json_atomic(cct.DB, {"active": None, "accounts": []})
cct.add_account("M", T("LOW7"))
cct.use_account("M")
json.dump({"env": {"HTTPS_PROXY": "https://m1:pw@8.8.8.8:32230", "NO_PROXY": "localhost"}}, open(SP, "w"))
msgs = []
cct.migrate_settings(msgs)
check("中转代理绑给了当前号", cct.find(cct.load(), "M").get("proxy") == "https://m1:pw@8.8.8.8:32230" and msgs, msgs)
cct._write_json_atomic(cct.DB, {"active": None, "accounts": []})
json.dump({"env": {"HTTPS_PROXY": "http://127.0.0.1:7890"}}, open(SP, "w"))
cct.migrate_settings([])
check("本机 Clash 这种不带账号的代理不自动绑（自己用的照旧）", not cct.bound(cct.load()))
cct._write_json_atomic(cct.DB, _store)
os.remove(SP)

print("\n[9] Windows 配置文件写法（在任何系统上模拟）")
prof = os.path.join(SANDBOX, "Microsoft.PowerShell_profile.ps1")
with open(prof, "wb") as f:
    f.write("# 旧配置\r\nSet-Alias ll Get-ChildItem\r\n".encode("utf-8"))
real = cct.IS_WIN
cct.IS_WIN = True
try:
    blk = "\n".join([cct.BEGIN, "function claude { & claude.exe @args }", cct.END])   # 旧版装的启动函数
    check("（准备旧版配置块）", cct.put_block(prof, blk, bom=True))
    raw = open(prof, "rb").read()
    check("UTF-8 BOM + 全部 CRLF + 旧内容保留",
          raw[:3] == b"\xef\xbb\xbf" and raw.count(b"\n") == raw.count(b"\r\n") and "旧配置" in raw.decode("utf-8-sig"))
    removed = cct.drop_block(prof)
    after = open(prof, "rb").read().decode("utf-8-sig")
    check("升级时能移除旧版启动函数，旧内容还在、BOM 还在", removed and cct.BEGIN not in after and "旧配置" in after
          and open(prof, "rb").read()[:3] == b"\xef\xbb\xbf")
finally:
    cct.IS_WIN = real

print("\n[10] 认证冲突检查")
os.makedirs(os.environ["CLAUDE_CONFIG_DIR"], exist_ok=True)
sp = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
check("没有冲突时为空", not [c for c in cct.auth_conflicts() if c["where"] == sp])
with open(sp, "w") as f:
    json.dump({"apiKeyHelper": "/bin/echo", "env": {"ANTHROPIC_API_KEY": "sk-ant-api-x", "CLAUDE_CODE_OAUTH_TOKEN": "x"}}, f)
found = {c["what"] for c in cct.auth_conflicts() if c["where"] == sp}
check("发现 apiKeyHelper、env 里的 API key", {"apiKeyHelper", "env.ANTHROPIC_API_KEY"} <= found, found)
os.remove(sp)

print("\n[10b] 出口 IP 检查")
check("正常美国 IP → ip_state ok", cct.ip_state()["ok"])
cct.egress_ip = lambda *a, **k: {"ip": "1.2.3.4", "country": "CN", "city": "", "org": ""}
check("中国大陆 IP → ip_state 不 ok", not cct.ip_state()["ok"])
check("IP 不对时 refresh_one 被拦截", raises(cct.refresh_one, cur() or "A"))
_p = []
cct.probe = lambda tok, proxy=None: _p.append(tok) or fake_probe(tok, proxy)
cct.refresh_one(cur() or "A", force=True)
check("force=True 时绕过 IP 检查、照常查", len(_p) == 1)
cct.egress_ip = lambda *a, **k: {"error": "查不到出口 IP（代理可能没生效，或网络不通）"}
check("查不到 IP → 也拦截", raises(cct.refresh_one, cur() or "A"))
cct.egress_ip = lambda *a, **k: {"ip": "203.0.113.10", "country": "US", "city": "LA", "org": "t"}
cct.probe = fake_probe
cct._ip_cache.clear()

print("\n[10c] claude 代理 + doctor")
import io as _io, contextlib as _cl
os.makedirs(os.environ["CLAUDE_CONFIG_DIR"], exist_ok=True)
_sp = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_saved_proxy = {k: os.environ.pop(k, None) for k in _PROXY_VARS}   # 本机多半真挂着代理，先清干净再测
check("没配代理时 claude_proxy()=None", cct.claude_proxy() is None)
open(_sp, "w").write(json.dumps({"env": {"HTTPS_PROXY": "http://127.0.0.1:9999"}}))
check("读到 settings.json 里给 claude 配的代理", cct.claude_proxy() == "http://127.0.0.1:9999")
os.remove(_sp)
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8888"
check("其次读环境变量代理", cct.claude_proxy() == "http://127.0.0.1:8888")
del os.environ["HTTPS_PROXY"]

# https:// 代理的本机转接（Windows 上 urllib / curl 都用不了带账号密码的 https 代理）
import re
open(_sp, "w").write(json.dumps({"env": {"HTTPS_PROXY": "http://127.0.0.1:9999"}}))
check("http:// 代理原样使用，不开转接", cct.effective_proxy() == "http://127.0.0.1:9999")
open(_sp, "w").write(json.dumps({"env": {"HTTPS_PROXY": "https://u:p@127.0.0.1:9999"}}))
_was_win = cct.IS_WIN
cct.IS_WIN = False
check("mac/Linux 有 curl 时 https:// 代理原样交给 curl", cct.effective_proxy() == "https://u:p@127.0.0.1:9999")
cct.IS_WIN = True
check("Windows 上 https:// 代理换成本机转接口", re.match(r"^http://127\.0\.0\.1:\d+$", cct.effective_proxy() or ""))
cct.IS_WIN = _was_win
os.remove(_sp)

import socket as _sock, ssl as _ssl, base64 as _b64, shutil as _sh, subprocess as _sub
from urllib.parse import urlparse
_openssl = _sh.which("openssl")
if not _openssl:
    print("  （跳过转接端到端测试：本机没有 openssl，无法生成测试证书）")
else:
    _cd = tempfile.mkdtemp()
    _crt, _key = os.path.join(_cd, "c.pem"), os.path.join(_cd, "k.pem")
    _sub.run([_openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
              "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", _key, "-out", _crt], capture_output=True)
    _want = "Basic " + _b64.b64encode(b"us%er:p@ss").decode()   # 账号密码里有特殊字符，链接里是转义过的

    def _fake_https_proxy(lsn):
        # 假的 https 代理：TLS 握手 → 校验 Proxy-Authorization → 200 → 原样回显
        sctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(_crt, _key)
        while True:
            try:
                c, _ = lsn.accept()
                s = sctx.wrap_socket(c, server_side=True)
                head, _x = cct._read_head(s)
                ok = ("Proxy-Authorization: " + _want).encode() in head and head.startswith(b"CONNECT echo.test:443 ")
                s.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" if ok else
                          b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                if ok:
                    d = s.recv(100)
                    s.sendall(d)
                s.close()
            except Exception:
                pass

    _l = _sock.socket()
    _l.bind(("127.0.0.1", 0))
    _l.listen(5)
    threading.Thread(target=_fake_https_proxy, args=(_l,), daemon=True).start()
    _orig_ctx = cct._proxy_ssl_ctx
    cct._proxy_ssl_ctx = lambda: _ssl.create_default_context(cafile=_crt)   # 信任测试证书

    def _through(proxy_url):
        r = cct._local_relay(proxy_url)
        h, p = urlparse(r).hostname, urlparse(r).port
        c = _sock.create_connection((h, p), timeout=10)
        c.sendall(b"CONNECT echo.test:443 HTTP/1.1\r\nHost: echo.test:443\r\n\r\n")
        resp, _x = cct._read_head(c)
        out = None
        if resp.startswith(b"HTTP/1.1 200"):
            c.sendall(b"ping-123")
            out = c.recv(100)
        c.close()
        return resp.split(b"\r\n")[0], out

    _port = _l.getsockname()[1]
    _st, _echo = _through("https://us%%25er:p%%40ss@127.0.0.1:%d" % _port)
    check("转接：补上账号密码、隧道双向转发", _st.endswith(b"200 Connection established") and _echo == b"ping-123", (_st, _echo))
    _st, _echo = _through("https://us%%25er:wrong@127.0.0.1:%d" % _port)
    check("转接：密码错时把上游的 407 原样返回", b"407" in _st and _echo is None, _st)
    cct._proxy_ssl_ctx = _orig_ctx
    _sh.rmtree(_cd, ignore_errors=True)

for _k, _v in _saved_proxy.items():        # 恢复本机原来的代理，后面 doctor 才测得准
    if _v is not None:
        os.environ[_k] = _v
_o = _io.StringIO()
with _cl.redirect_stdout(_o):
    cct.cmd_doctor(None)
_out = _o.getvalue()
check("doctor 跑通、含关键小节", all(k in _out for k in ("网络体检", "出口 IP", "Anthropic 连通性", "体检结束")))

print("\n[10d] 不读系统代理（和 Claude Code 一致）")
import http.server as _hs
_ls = _hs.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (_hs.BaseHTTPRequestHandler,), {
    "do_GET": lambda self: (self.send_response(200), self.end_headers(), self.wfile.write(b"DIRECT-OK")),
    "log_message": lambda *a: None}))
threading.Thread(target=_ls.serve_forever, daemon=True).start()
_old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY")}
os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"     # 一个连不上的代理
try:
    with cct._urllib_open(urllib.request.Request("http://127.0.0.1:%d/" % _ls.server_address[1]), 5, None) as _r:
        _body = _r.read()
except Exception as _e:
    _body = repr(_e)
check("没有代理时直连：不去读系统 / 环境里的默认代理", _body == b"DIRECT-OK", _body)
for _k, _v in _old.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
_ls.shutdown()

print("\n[11] 网页服务和 API")
srv = cct.Server(("127.0.0.1", 0), cct.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method="GET" if body is None else "POST")
    try:
        with op.open(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


code, html = call("/")
check("GET / 返回网页", code == 200 and "Claude 换号台" in html)
code, st = call("/api/state")
st = json.loads(st)
check("/api/state 不含完整 token", not any(a["token"] in json.dumps(st) for a in cct.load()["accounts"]))
check("/api/state 带版本和普通登录信息", st.get("version") and "normal" in st)
code, _ = call("/api/normal", {}, {"Origin": "https://evil.example"})
check("跨站请求被拒（403）", code == 403)
code, body = call("/api/normal", {})
check("POST /api/normal 生效", code == 200 and json.loads(body)["ok"] and cur() is None)
code, body = call("/api/use", {"name": "A"})
check("POST /api/use 生效", json.loads(body)["ok"] and cur() == "A")
FAKE_PROCS.append({"pid": 99, "kind": "终端里的 claude", "path": "claude"})
code, body = call("/api/normal", {})
b = json.loads(body)
check("有 claude 在运行：API 返回进程列表，不切", not b["ok"] and b.get("running") and b["running"][0]["pid"] == 99 and cur() == "A")
code, body = call("/api/normal", {"kill": True})
check("API 带 kill：结束后再切", json.loads(body)["ok"] and cur() is None)
cct.set_proxy("C", None)
code, body = call("/api/use", {"name": "C"})
b = json.loads(body)
check("API：风险要确认时返回风险代码", not b["ok"] and b.get("needForce") in ("unbound", "ip"), b)
srv.shutdown()

shutil.rmtree(SANDBOX, ignore_errors=True)
ok = sum(RESULTS)
print("\n%s  %d/%d 通过" % ("全部通过 ✅" if ok == len(RESULTS) else "有失败 ❌", ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
