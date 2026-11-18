#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cct.py —— Claude Code setup-token 换号器（本机自用 · macOS / Windows / Linux · 纯标准库）

一句话：把多个 `claude setup-token` 生成的 token 存起来，看每个号的用量（5 小时 / 7 天 / 7 天 Fable），
        一个号用完了就切到下一个。

新电脑上怎么装
  把整个文件夹复制过去，双击启动脚本（Windows：cct-windows.bat；macOS：cct-mac.command）。
  它会自动安装（见下面 install），然后打开网页，在网页里添加账号即可。
  电脑原来的普通登录（/login）不用动：cct 里没有账号时，claude 照常用原来的登录。

切换是怎么生效的
  每个号可以绑一个代理（中转），切换时把【这个号的 token + 这个号的代理】一次写进
  ~/.claude/settings.json 的 env（CLAUDE_CODE_OAUTH_TOKEN、HTTPS_PROXY 等）。
  · settings.json 的 env 优先级高于终端 / IDE 继承来的环境变量（实测），所以终端、VS Code / Cursor /
    JetBrains 插件、桌面版里新启动的 claude 读到的都是同一对「号 + IP」，不用重启 IDE。
  · 正在运行的 claude 会中途重读 settings.json 的一部分（实测：改了代理后有少量连接换到新代理），
    所以切换前必须先关掉所有 claude（终端里的、IDE 对话标签里的），cct 会检查，有就拒绝切换（可强制结束）。
  · 切到「普通登录」时 token 写成空字符串：盖住终端里残留的旧 token，claude 用本机 /login 登录。

用量怎么查
  向 /v1/messages 发一条 max_tokens=1 的请求，读响应头 anthropic-ratelimit-unified-*。
  先用 Fable 请求（能用 Fable 的号一次拿全所有桶），没开通 Fable 的号会被直接拒，再换 Opus。
  判断「号用完了没」只看 5h / 7d；别的桶（模型专属的）用完了只是那个模型不能用。
  必须带 Claude Code 的 system 标记，否则不算订阅流量、会返回假 429。
  每查一次消耗极少量额度，所以结果会缓存，不会每次都查。
  setup token 没有 user:profile 权限，查不到邮箱 / 订阅等级，只能拿到组织 ID。

用法
  cct                      启动本地网页 http://127.0.0.1:6061 并打开浏览器
  cct setup                安装（见下）+ 启动网页；启动脚本调的就是它
  cct list                 列出账号和上次查到的用量（不联网）
  cct check [名字]         联网刷新用量（不写名字 = 全部）
  cct add <名字> <token> [--proxy 代理]   添加账号（会先经它的代理验证 token、识别组织）
  cct use <名字>           切换当前账号（先检查有没有 claude 在运行）
  cct proxy <名字|normal> <代理|none>     给账号 / 普通登录绑定代理
  cct ps                   列出正在运行的 claude
  cct rename <旧> <新>     改名
  cct rm <名字>            删除
  cct install              安装：程序复制到 ~/.cc_tokens/app/，装 cct 命令，导入原来写死的 token，
                           settings.json 里原来的中转代理绑给当前号；清掉旧版装的 claude 启动函数 / 用户环境变量
  cct uninstall            撤掉 install 做的所有改动，恢复普通登录（账号数据保留，--purge 连数据一起删）
  cct token                （旧版启动函数用，保留兼容）输出当前 token，只读本地、不联网

数据：~/.cc_tokens/accounts.json（里面是明文 token，别外传、别放网盘）
"""
import os, sys, re, json, time, shutil, argparse, datetime, tempfile, threading, subprocess, webbrowser
import ssl, shlex, socket, select, signal, base64, urllib.parse, urllib.request, urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

IS_WIN = os.name == "nt"
PLATFORM = "windows" if IS_WIN else ("mac" if sys.platform == "darwin" else "linux")
if IS_WIN:
    import msvcrt
else:
    import fcntl

STORE = os.path.expanduser(os.environ.get("CCT_STORE", "~/.cc_tokens"))
DB = os.path.join(STORE, "accounts.json")
LOCKFILE = os.path.join(STORE, ".lock")
LOGFILE = os.path.join(STORE, "switch.log")
APP_DIR = os.path.join(STORE, "app")
APP = os.path.join(APP_DIR, "cct.py")      # install 后固定用这一份
CUR_TOKEN_FILE = os.path.join(STORE, "current_token")   # 旧版用的（.zshrc 打开终端时读它）；新版安装时删掉
PORT = int(os.environ.get("CCT_PORT", "6061"))
MODEL = os.environ.get("CCT_MODEL", "claude-opus-5-5")
FABLE_MODEL = os.environ.get("CCT_FABLE_MODEL", "claude-fable-5-1")
API_URL = "https://api.anthropic.com/v1/messages"
PREFIX = "anthropic-ratelimit-unified-"
ENV = "CLAUDE_CODE_OAUTH_TOKEN"
PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")   # 切号时写进 settings.json env 的代理变量
# 绑了代理时一起写的「不走代理」名单，和中转配置命令写的一致：本机地址、我们自己的用量上报域名。
# 没有它，claude 起的命令（curl、Python…）访问 localhost / 内网都会被送去中转、连不上。
NO_PROXY_DEFAULT = "localhost,127.0.0.1,usage.ytprofit.top,cc.ytprofit.top,cc.ccforyou.com"
NO_PROXY_MUST = ("localhost", "127.0.0.1")
PROBE_TIMEOUT = 30        # 单次查用量请求最长等多久（秒）
LOCK_TIMEOUT = 10         # 等账号数据文件的锁最长等多久（秒）：超时报错，不无限自旋
CORE = ("5h", "7d")      # 所有模型共用的桶：任何一个卡住，这个号就整个不能用
BUCKET_RE = re.compile(r"^(.+)-utilization$")


def _file_version():
    """程序文件内容的指纹。改了代码指纹就变，启动时据此把旧版本的网页服务换掉。"""
    import hashlib
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return "unknown"


VERSION = _file_version()


# ------------------------------------------------------------------------ store
def ensure_store():
    os.makedirs(STORE, mode=0o700, exist_ok=True)
    os.chmod(STORE, 0o700)


@contextmanager
def locked():
    """跨进程锁（网页和命令行可能同时写）。按打开的文件算，同一进程的不同线程之间也互斥。"""
    ensure_store()
    with open(LOCKFILE, "a+") as f:
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                if IS_WIN:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise CctError("账号数据被另一个 cct 进程占着超过 %d 秒，"
                                   "请关掉其它换号台窗口后重试。" % LOCK_TIMEOUT)
                time.sleep(0.05)
        try:
            yield
        finally:
            if IS_WIN:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


BAK = DB + ".bak"                            # 上一版
BACKUP_DIR = os.path.join(STORE, "backups")  # 每天一份，保留 7 天


def _read_db(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not isinstance(d.get("accounts", []), list):
        raise ValueError("格式不对")
    return d


def _write_json_atomic(path, d):
    """先写临时文件、刷到磁盘，再整体替换：断电也不会留下写了一半的文件。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".accounts.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _recover(err):
    """accounts.json 读不出来：用最近一份能读的备份恢复（坏文件另存一份，不直接丢掉）。
    备份也都不行就报错，绝不用空数据把原文件盖掉。"""
    cands = [BAK]
    if os.path.isdir(BACKUP_DIR):
        cands += sorted((os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)), reverse=True)
    for p in cands:
        try:
            d = _read_db(p)
        except Exception:
            continue
        broken = DB + ".broken-" + time.strftime("%Y%m%d%H%M%S")
        try:
            shutil.copy2(DB, broken)
        except OSError:
            pass
        _write_json_atomic(DB, d)
        log("accounts.json 读不出来（%s），已用 %s 恢复；坏文件另存为 %s" % (err, p, broken))
        print("cct: 账号数据文件读不出来，已用备份 %s 自动恢复" % p, file=sys.stderr)
        return d
    raise CctError("账号数据文件 %s 读不出来（%s），备份也不可用。文件没有被改动，请手动检查。" % (DB, err))


def load():
    try:
        d = _read_db(DB)
    except FileNotFoundError:
        d = {}
    except (ValueError, OSError) as e:
        d = _recover(e)
    d.setdefault("active", None)
    d.setdefault("accounts", [])
    return d


def save(d):
    ensure_store()
    if os.path.exists(DB):
        shutil.copy2(DB, BAK)
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        daily = os.path.join(BACKUP_DIR, "accounts-%s.json" % time.strftime("%Y%m%d"))
        if not os.path.exists(daily):
            shutil.copy2(DB, daily)
            for old in sorted(os.listdir(BACKUP_DIR))[:-7]:
                try:
                    os.remove(os.path.join(BACKUP_DIR, old))
                except OSError:
                    pass
    _write_json_atomic(DB, d)


@contextmanager
def txn():
    """读-改-写。块里抛异常就不落盘。"""
    with locked():
        d = load()
        yield d
        save(d)


def log(msg):
    ensure_store()
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write("%s  %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


class CctError(Exception):
    pass


class NeedForce(CctError):
    """可以继续但有风险：要用户确认后带 force 再来一次。code 是风险代码，网页逐条确认：
    ip=出口 IP 不对，unbound=号没绑代理，dup=代理已绑给别的号，nops=查不了有没有 claude 在运行。"""
    def __init__(self, msg, code):
        self.code = code
        CctError.__init__(self, msg)


class ClaudeRunning(CctError):
    """切换前发现还有 claude 在运行。procs 是 claude_processes() 的结果。"""
    def __init__(self, procs):
        self.procs = procs
        CctError.__init__(self, "还有 %d 个 claude 在运行（%s），先全部关掉再切换"
                          % (len(procs), "、".join(sorted({p["kind"] for p in procs}))))


def find(d, name):
    for a in d["accounts"]:
        if a["name"] == name:
            return a
    return None


def must_find(d, name):
    a = find(d, name)
    if not a:
        raise CctError("没有叫「%s」的账号" % name)
    return a


def active_acc(d):
    return find(d, d["active"]) if d["active"] else None


def mask(tok):
    """只露出 sk- 和最后 9 位，方便对照是哪个 token。"""
    return "sk-…" + tok[-9:] if len(tok) > 20 else "…"


# ------------------------------------------------------------------------ probe
def _probe_body(model):
    return json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
        # 必须带 Claude Code 标记，否则不算订阅流量、会返回假 429
        "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
    })


def _err_of(resp):
    try:
        e = json.loads(resp).get("error", {})
        return e.get("message", ""), (e.get("details") or {}).get("error_code", "")
    except Exception:
        return "", ""


def _settings_env(name):
    """读 claude settings.json 的 env 块里某个变量（多份配置取第一个有值的）。"""
    for p in claude_settings_files():
        try:
            with open(p, encoding="utf-8") as f:
                v = ((json.load(f).get("env") or {}) or {}).get(name)
            if v:
                return v
        except (OSError, ValueError):
            continue
    return None


def claude_proxy():
    """claude 实际会用的代理：优先 settings.json 的 env.HTTPS_PROXY/HTTP_PROXY，其次当前环境变量。
    返回 None 表示没配代理：claude 直连（开了 TUN 就经过 TUN）。Claude Code 只认代理环境变量，不读系统代理设置，
    cct 也照这个规则走，好让显示的出口 IP = claude 真正连 Anthropic 那条线。"""
    for src in (_settings_env, os.environ.get):
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            v = src(name)
            if v:
                return v
    pp = persistent_proxy()       # 写在 shell 配置文件里的：新开的终端都会带上
    return pp[0] if pp else None


# ---------- https:// 代理的本机转接 ----------
# claude 支持 https://用户:密码@主机:端口 这种「连代理本身也加密」的代理，但 cct 用的两种 HTTP 方式都不行：
#   · Python 的 urllib 不支持 https:// 代理，会用明文去连，被代理拒绝；
#   · Windows 自带的 curl 能连上，却不发代理的账号密码（代理回 407）。
# 所以遇到 https:// 代理，在 127.0.0.1 开一个只给本进程用的明文转接口：urllib / curl 用明文 CONNECT 连它，
# 它再用 TLS 连上游代理、补上 Proxy-Authorization，然后双向转发。里面跑的仍是到 Anthropic 的 TLS，转接口看不到内容。
# mac / Linux 上 curl 本来就支持 https 代理，只有 Windows（或没有 curl）才启用转接。
_relay = {}              # 上游代理 → 本机转接口（每个号可能有自己的代理，各开一个，进程内复用）
_relay_lock = threading.Lock()
_mac_roots = {"pem": None}


def _proxy_ssl_ctx():
    """连上游代理用的 TLS 上下文。python.org 版 macOS Python 没带根证书，从系统钥匙串补上。"""
    ctx = ssl.create_default_context()
    if PLATFORM == "mac" and not ctx.get_ca_certs():
        if _mac_roots["pem"] is None:
            try:
                _mac_roots["pem"] = subprocess.run(
                    ["security", "find-certificate", "-a", "-p",
                     "/System/Library/Keychains/SystemRootCertificates.keychain"],
                    capture_output=True, text=True, timeout=10).stdout
            except Exception:
                _mac_roots["pem"] = ""
        if _mac_roots["pem"]:
            ctx.load_verify_locations(cadata=_mac_roots["pem"])
    return ctx


def _read_head(sock, limit=65536):
    """读到 HTTP 头结束（\\r\\n\\r\\n）。返回 (头, 头后面多读到的字节)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            break
    head, sep, rest = buf.partition(b"\r\n\r\n")
    return head + sep, rest


def _relay_pump(a, b):
    """在两个连接之间双向转发，直到一边关闭或 5 分钟没动静。b 是 TLS 连接，它自己缓冲里的数据要先读完。"""
    socks = [a, b]
    while True:
        if b.pending():
            ready = [b]
        else:
            ready, _, _ = select.select(socks, [], [], 300)
            if not ready:
                return
        for s in ready:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


def _relay_client(conn, up):
    upstream = None
    try:
        conn.settimeout(30)
        head, early = _read_head(conn)
        m = re.match(rb"CONNECT (\S+) HTTP/1\.[01]", head)
        if not m:
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        target = m.group(1).decode("latin-1")
        raw = socket.create_connection((up.hostname, up.port or 443), timeout=15)
        upstream = _proxy_ssl_ctx().wrap_socket(raw, server_hostname=up.hostname)
        req = "CONNECT %s HTTP/1.1\r\nHost: %s\r\n" % (target, target)
        if up.username is not None:
            cred = "%s:%s" % (urllib.parse.unquote(up.username), urllib.parse.unquote(up.password or ""))
            req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(cred.encode()).decode()
        upstream.sendall((req + "\r\n").encode())
        resp, extra = _read_head(upstream)
        conn.sendall(resp + extra)          # 把上游的 200 / 407 原样告诉 urllib / curl
        if not re.match(rb"HTTP/1\.[01] 200", resp):
            return
        if early:
            upstream.sendall(early)
        conn.settimeout(None)
        upstream.settimeout(None)
        _relay_pump(conn, upstream)
    except Exception:
        pass
    finally:
        for s in (conn, upstream):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass


def _local_relay(proxy):
    """为 https:// 代理开本机转接口（每个上游一个，进程内复用），返回 http://127.0.0.1:端口。"""
    with _relay_lock:
        if _relay.get(proxy):
            return _relay[proxy]
        up = urlparse(proxy)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(32)

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=_relay_client, args=(c, up), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        _relay[proxy] = "http://127.0.0.1:%d" % srv.getsockname()[1]
        return _relay[proxy]


# 「跟着 claude 走」：没给号单独绑代理时（旧用法 / 自己用 Clash），cct 发请求用 claude 自己会用的代理。
CLAUDE = "<claude>"
# 「只看环境变量」：一个号一个 IP 模式下没绑代理的号。不能读 settings.json——那里写的是当前号的代理，借过来就串了。
ENVP = "<env>"


def env_proxy():
    """终端里会带上的代理：先看当前进程（换号台是从哪个终端启动的，就是那个终端的），
    再看 shell 配置文件里写死的（每个新开的终端都会带上，见 persistent_proxy）。没有返回 None。"""
    tp = term_proxy()
    return tp[0] if tp else None


def term_proxy():
    """(代理地址, 来源)。来源是「终端变量」或配置文件名（如 ~/.zshrc）。没有返回 None。"""
    pp = persistent_proxy()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(name):
            v = os.environ[name]
            return v, (pp[1] if pp and pp[0] == v else "终端变量")
    return pp


_RC_EXPORT_RE = re.compile(r"^export\s+(.+)$")
_PS_ENV_RE = re.compile(r"""^\s*\$env:(https?_proxy)\s*=\s*["']([^"']+)["']""", re.I)
_persist_cache = {"key": None, "val": None}


def persistent_proxy():
    """写在 shell 配置文件里、每个新开的终端都会带上的代理：(地址, 来源) 或 None。
    换号台只能看到启动它的那个终端；但写进配置文件的，任何新终端里启动的 claude 都会用到，这里能读出来。
    - macOS / Linux：.zshrc / .zprofile / .bashrc / .bash_profile / .profile 里行首的 export
      （比如 Clash「复制终端代理命令」粘进去的那行；函数里缩进的不算，那要手动执行才生效）；
    - Windows：用户环境变量 HTTPS_PROXY / HTTP_PROXY，以及 PowerShell 配置里的 $env:HTTPS_PROXY = …。
    只取 https / http 代理：Claude Code 不用 all_proxy（socks）。按文件修改时间缓存。"""
    if IS_WIN:
        files = ps_profiles()
    elif os.environ.get("CCT_ZSHRC"):            # 测试用
        files = [os.environ["CCT_ZSHRC"]]
    else:
        files = [os.path.expanduser(f) for f in ("~/.zshrc", "~/.zprofile", "~/.bashrc", "~/.bash_profile", "~/.profile")]
    key = []
    for f in files:
        try:
            key.append((f, os.path.getmtime(f)))
        except OSError:
            pass
    if IS_WIN:
        key += [(n, win_get_env(n)) for n in ("HTTPS_PROXY", "HTTP_PROXY")]
    key = tuple(key)
    if _persist_cache["key"] == key:
        return _persist_cache["val"]
    val = None
    if IS_WIN:
        for n in ("HTTPS_PROXY", "HTTP_PROXY"):
            if win_get_env(n):
                val = (win_get_env(n), "Windows 用户环境变量")
                break
    for f, _ in (k for k in key if not isinstance(k[1], (str, type(None)))):
        if val:
            break
        found = {}
        try:
            lines = BLOCK_RE.sub("", read_text(f)).splitlines()
        except OSError:
            continue
        for ln in lines:
            if IS_WIN:
                m = _PS_ENV_RE.match(ln)
                if m:
                    found[m.group(1).lower()] = m.group(2)
                continue
            m = _RC_EXPORT_RE.match(ln)
            if not m:
                continue
            try:
                parts = shlex.split(m.group(1), comments=True)
            except ValueError:
                continue
            for kv in parts:            # 一行里可能 export 好几个：https_proxy=… http_proxy=… all_proxy=…
                k, _, v = kv.partition("=")
                if k.lower() in ("https_proxy", "http_proxy") and v:
                    found[k.lower()] = v     # 同一个变量写了好几次，以后面的为准
        v = found.get("https_proxy") or found.get("http_proxy")
        if v:
            val = (v, f if IS_WIN else "~/" + os.path.basename(f))
    _persist_cache.update(key=key, val=val)
    return val


def effective_proxy(proxy=CLAUDE):
    """cct 自己发请求时实际用的代理。proxy=CLAUDE 跟 claude 一致（settings.json → 环境变量）；ENVP 只看环境变量；
    结果是 None 就直连（开了 TUN 自然经过 TUN）。不读系统代理设置：Claude Code 也不读，读了出口 IP 就对不上。
    https:// 代理在 curl 不可靠（Windows / 没有 curl）时换成本机转接口。"""
    if proxy == CLAUDE:
        proxy = claude_proxy()
    elif proxy == ENVP:
        proxy = env_proxy()
    if proxy and proxy.lower().startswith("https://") and (IS_WIN or not shutil.which("curl")):
        try:
            return _local_relay(proxy)
        except OSError:
            return proxy
    return proxy


def _urllib_open(req, timeout, proxy=CLAUDE):
    """按指定代理打开一个请求；没有代理就直连。ProxyHandler({}) 是关键：urllib 默认会读系统代理设置
    （Windows 注册表 / macOS 网络设置），而 Claude Code 不读，不关掉的话 cct 查到的出口和 claude 的不一样。"""
    proxy = effective_proxy(proxy)
    ctx = ssl.create_default_context()
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    return urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=ctx)).open(req, timeout=timeout)


def _curl_proxy_args(proxy=CLAUDE):
    proxy = effective_proxy(proxy)
    return ["-x", proxy] if proxy else []


def _post_urllib(token, model, proxy=CLAUDE):
    """Python 自带的 HTTP。用系统证书；代理只按 proxy 参数走，不读系统代理设置（见 _urllib_open）。
    连不上（证书、代理、断网）返回 None，交给下一种方式。"""
    req = urllib.request.Request(API_URL, data=_probe_body(model).encode(), method="POST", headers={
        "authorization": "Bearer %s" % token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    })
    try:
        with _urllib_open(req, PROBE_TIMEOUT, proxy) as r:
            code, hdrs, resp = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        code, hdrs, resp = e.code, e.headers, e.read()
    except (urllib.error.URLError, OSError, ssl.SSLError):
        return None
    headers = {k.lower(): v for k, v in hdrs.items()}
    err, ecode = _err_of(resp.decode("utf-8", "replace"))
    return 0, code, headers, err, ecode


def _post_curl(token, model, proxy=CLAUDE):
    """curl。python.org 版 macOS Python 默认没根证书，所以 Mac 上优先用它。
    token 走 stdin，不进进程列表。curl 不存在或连不上返回 None。"""
    curl = shutil.which("curl")
    if not curl:
        return None
    fd, out = tempfile.mkstemp(prefix="cct-")   # Windows 上 NamedTemporaryFile 打开期间别的进程写不进去
    os.close(fd)
    try:
        p = subprocess.run(
            [curl, "-s", "-m", str(PROBE_TIMEOUT)] + _curl_proxy_args(proxy) + ["-D", "-", "-o", out, "-X", "POST", API_URL,
             "-H", "@-",
             "-H", "anthropic-version: 2023-06-01",
             "-H", "anthropic-beta: oauth-2025-04-20",
             "-H", "content-type: application/json",
             "-d", _probe_body(model)],
            input="authorization: Bearer %s\n" % token, capture_output=True, text=True,
            creationflags=0x08000000 if IS_WIN else 0)   # Windows：不弹黑窗
        with open(out, encoding="utf-8", errors="replace") as f:
            resp = f.read()
    except OSError:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if p.returncode != 0:
        return None
    # 走代理时前面会多一段 "HTTP/1.1 200 Connection established"，只取最后一段响应头
    code, headers = 0, {}
    for line in p.stdout.splitlines():
        if line.startswith("HTTP/"):
            code, headers = int(line.split()[1]), {}
            continue
        k, sep, v = line.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    err, ecode = _err_of(resp)
    return 0, code, headers, err, ecode


def _post(token, model, proxy=CLAUDE):
    """发一条 1 token 的消息。返回 (失败码, HTTP 码, 响应头 dict, 错误信息, 错误码)；失败码非 0 = 网络不通。"""
    ways = (_post_urllib, _post_curl) if IS_WIN else (_post_curl, _post_urllib)
    for way in ways:
        r = way(token, model, proxy)
        if r is not None:
            return r
    return 1, 0, {}, "", ""


def probe(token, proxy=CLAUDE):
    """查一个号的用量（经 proxy 发出：每个号走自己的代理，别让 B 号的 token 从 A 号的 IP 出去）。
    返回 usage dict（会存进账号里）。

    先用 Fable 请求：能用 Fable 的号，这一次就能拿到全部的桶（5h、7d、7d_oi = Fable 的周额度）；
    没开通 Fable 的号会直接被拒（credits_required，不带用量头、不消耗额度），
    这时再用 Opus 请求一次，拿 5h / 7d。
    """
    now = time.time()
    fable, fable_why = None, ""
    for model in (FABLE_MODEL, MODEL):
        rc, code, headers, err, ecode = _post(token, model, proxy)
        if rc:
            return {"state": "neterr", "error": "网络连不上（检查网络或代理）", "checked_at": now}
        h = {k[len(PREFIX):]: v for k, v in headers.items() if k.startswith(PREFIX)}
        if model != FABLE_MODEL:
            break
        if any(BUCKET_RE.match(k) for k in h):
            fable = True
            break
        if code == 401:
            break
        fable, fable_why = False, ecode or err or ("HTTP %d" % code)

    buckets = {}
    for k in h:
        m = BUCKET_RE.match(k)
        if m:
            b = m.group(1)
            reset = h.get(b + "-reset", "")
            try:
                util = float(h[k])
            except (TypeError, ValueError):
                continue          # 服务端返回了看不懂的值：跳过这个桶
            buckets[b] = {"util": util, "status": h.get(b + "-status"),
                          "reset": int(reset) if reset.isdigit() else None}

    u = {"http": code, "checked_at": now, "org": headers.get("anthropic-organization-id"),
         "model": model, "fable": fable, "fable_why": fable_why, "buckets": buckets,
         "status": h.get("status"), "overage": h.get("overage-status")}
    core_blocked = any(bucket_blocked(buckets.get(b)) for b in CORE)
    extra_blocked = any(bucket_blocked(bk) for b, bk in buckets.items() if b not in CORE)
    if code == 401:
        u["state"], u["error"] = "invalid", err or "token 无效或已被吊销"
    elif core_blocked or (h.get("status") == "rejected" and not extra_blocked):
        u["state"] = "limited"
    elif code == 200 or buckets:
        # 只是某个模型专属的桶（比如 Fable 的）用完了：号还能用别的模型，不算用完
        u["state"] = "ok"
    else:
        u["state"], u["error"] = "error", err or "HTTP %d" % code
    return u


def buckets_of(u):
    """兼容旧缓存（旧版把 5h / 7d 直接存在 usage 下）。"""
    return (u or {}).get("buckets") or {b: u[b] for b in CORE if (u or {}).get(b)}


def bucket_blocked(bk, now=None):
    """这个桶现在是否还卡着（被拒且没到重置时间）。"""
    now = now or time.time()
    return bool(bk) and bk.get("status") == "rejected" and not (bk.get("reset") and bk["reset"] <= now)


def usable(a):
    u = a.get("usage") or {}
    if u.get("state") in ("invalid", "error", "neterr", None):
        return False
    bs = buckets_of(u)
    return not any(bucket_blocked(bs.get(b)) for b in CORE)


# 用 Anthropic 自己域名下的 Cloudflare trace 查出口 IP：走的就是 api.anthropic.com 那条代理规则，
# 和 claude 真正连 Anthropic 用的是同一条线（如果单独给 anthropic 设了节点，这里才测得准）。
# 不带 token、不带 Claude Code 标记，只问「我现在出口 IP 是什么」。
IP_URL = "https://api.anthropic.com/cdn-cgi/trace"
_ip_cache = {}    # 代理 → {"data", "at"}：每个号的代理各查各的


def _http_get(url, timeout=8, proxy=CLAUDE):
    """GET 一个 URL 的文本（经 proxy）。和查用量一样支持两种方式：
    mac 上 python.org 的 Python 没根证书，所以优先用 curl。失败返回 None。"""
    def via_urllib():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cct"})
            with _urllib_open(req, timeout, proxy) as r:
                return r.read().decode("utf-8")
        except Exception:
            return None

    def via_curl():
        curl = shutil.which("curl")
        if not curl:
            return None
        try:
            p = subprocess.run([curl, "-s", "-m", str(timeout)] + _curl_proxy_args(proxy) + [url], capture_output=True, text=True,
                               creationflags=0x08000000 if IS_WIN else 0)
            return p.stdout if p.returncode == 0 and p.stdout.strip() else None
        except OSError:
            return None

    for way in ((via_urllib, via_curl) if IS_WIN else (via_curl, via_urllib)):
        r = way()
        if r is not None:
            return r
    return None


def egress_ip(force=False, timeout=8, proxy=CLAUDE):
    """经 proxy 查出口 IP 和归属地（不带 token）。结果按代理分别缓存 60 秒。
    返回 {"ip","country","city","org"} 或 {"error": ...}。"""
    c = _ip_cache.get(proxy)
    if not force and c and time.time() - c["at"] < 60:
        return c["data"]
    body = _http_get(IP_URL, timeout, proxy)
    try:
        # trace 是 key=value 一行一条：ip=… loc=US colo=LAX …
        kv = dict(line.split("=", 1) for line in body.strip().splitlines() if "=" in line)
        data = {"ip": kv.get("ip"), "country": kv.get("loc"), "city": kv.get("colo"), "org": ""}
        if not data["ip"]:
            raise ValueError
    except Exception:
        data = {"error": "查不到出口 IP（代理可能没生效，或网络不通）"}
    _ip_cache[proxy] = {"data": data, "at": time.time()}
    return data


def ip_state(proxy=CLAUDE, force=False):
    """给界面用：{info, ok, why}。ok=False 表示 IP 不对（查不到 / 中国大陆），不该向 Anthropic 发请求。"""
    info = egress_ip(force=force, proxy=proxy)
    if info.get("error"):
        return {"info": info, "ok": False, "why": info["error"]}
    if info.get("country") == "CN":
        return {"info": info, "ok": False, "why": "出口是中国大陆 IP（%s），代理可能漏了；换成外区节点再查" % (info.get("ip") or "")}
    return {"info": info, "ok": True, "why": ""}


def _forced(force, code):
    """force=True 表示全部风险都确认过（命令行 --force）；网页传的是确认过的风险代码列表。"""
    return force is True or (isinstance(force, (list, tuple, set)) and code in force)


def ensure_safe_egress(proxy=CLAUDE, force=False):
    """向 Anthropic 发请求前调用：出口 IP 不对就中止，别用可疑 IP 触发风控。"""
    st = ip_state(proxy)
    if not st["ok"] and not _forced(force, "ip"):
        raise NeedForce(st["why"] + "。确认代理没问题后再试，或强制继续。", "ip")
    return st


# ------------------------------------------------------------------ 每个号绑的代理
PROXY_RE = re.compile(r"^https?://(?:[^@/\s]+@)?[^@/:\s]+(?::\d{1,5})?/?$", re.I)


def normalize_proxy(p):
    """检查并整理填进来的代理。空 / none 返回 None（= 不绑）。"""
    p = (p or "").strip()
    if p.lower() in ("", "none", "-") or p == "无":
        return None
    if not PROXY_RE.match(p):
        raise CctError("代理格式不对：应该像 https://用户:密码@IP:端口（中转），或 http://127.0.0.1:7890")
    return p.rstrip("/")


def proxy_label(p):
    """显示用：去掉密码，只留 协议://用户@主机:端口。"""
    if not p:
        return ""
    u = urlparse(p)
    host = (u.hostname or "") + (":%d" % u.port if u.port else "")
    return "%s://%s%s" % (u.scheme, urllib.parse.unquote(u.username) + "@" if u.username else "", host)


def proxy_via(p):
    """卡片上「经 …」那段：入口主机 · 账号（不带协议、端口和密码）。"""
    if not p:
        return ""
    u = urlparse(p)
    return " · ".join(x for x in (u.hostname or "", urllib.parse.unquote(u.username) if u.username else "") if x)


def _settings_proxy_raw():
    return next((v for v in (_settings_env(k) for k in PROXY_KEYS) if v), None)


def proxy_user(p):
    """代理里的账号名（中转账号，如 p12345a），没有返回空。"""
    u = urlparse(p) if p else None
    return urllib.parse.unquote(u.username) if u and u.username else ""


def bound(d):
    """有没有任何号（含普通登录）绑了代理。绑过就是「一个号一个 IP」模式：
    切换时连代理一起写进 settings.json，没绑代理的号要确认才能切 / 查。
    一个都没绑（自己用 Clash 之类）时，代理完全不碰，和旧版一样跟着 claude 的设置走。"""
    return bool(d.get("normal_proxy")) or any(a.get("proxy") for a in d["accounts"])


def target_proxy(d, acc):
    """号（acc=None 表示普通登录）绑的代理，没绑返回 None。"""
    return (acc.get("proxy") if acc else d.get("normal_proxy")) or None


def proxy_of(d, acc):
    """cct 替这个号发请求（查用量 / 查 IP）时用的代理：绑了就用它；
    「一个号一个 IP」模式下没绑的号只看环境变量（ENVP），不借 settings.json 里别的号的代理；
    没绑过任何代理时跟着 claude 走（CLAUDE）。"""
    p = target_proxy(d, acc)
    if p:
        return p
    return ENVP if bound(d) else CLAUDE


def route_of(d, acc):
    """claude 用这个号时实际走哪条线：(cct 替它发请求用的代理参数, 给人看的说明)。顶部出口 IP 用它。"""
    p = target_proxy(d, acc)
    if p:
        return p, "中转 " + (proxy_user(p) or urlparse(p).hostname or "")
    src = proxy_of(d, acc)
    real = claude_proxy() if src == CLAUDE else env_proxy()
    if real:
        u = urlparse(real)
        where = "settings.json 里的代理" if src == CLAUDE and _settings_proxy_raw() else "终端的代理环境变量"
        return src, "%s %s%s" % (where, u.hostname or real, ":%d" % u.port if u.port else "")
    return src, "直连（开了 TUN 就走 TUN）"


# --------------------------------------------------------------- claude 实际走哪条线
# Claude Code 的判断顺序：① 有没有改「发给谁」（ANTHROPIC_BASE_URL / Bedrock…）② 有没有代理环境变量
# ③ 都没有就直连——这时出口取决于系统：TUN / VPN 虚拟网卡、路由器、机器所在地。它不认系统代理设置。
API_HOST = "api.anthropic.com"
TUNNEL_RE = re.compile(r"utun|(^|[^a-z])tun\d*($|[^a-z])|wintun|wireguard|(^|[^a-z])wg\d|tap-|clash|mihomo|meta|sing-?box"
                       r"|tailscale|zerotier|vpn", re.I)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


_iface_cache = {}


def iface_to(host):
    """带 1 分钟缓存的 _iface_to（顶部每分钟刷新一次，Windows 上每问一次都要起 PowerShell）。"""
    c = _iface_cache.get(host)
    if c and time.time() - c[1] < 60:
        return c[0]
    r = _iface_to(host)
    _iface_cache[host] = (r, time.time())
    return r


def _iface_to(host):
    """直连这个地址时系统会用哪张网卡：{"name","desc","tunnel"}；查不了返回 None。
    开着 TUN / VPN 时连接会被它的虚拟网卡接走（Clash 的 fake-ip 也是先解析成 198.18.x 再路由进 TUN），
    所以「问系统走哪张网卡」比看路由表猜更直接，三个系统都能问。"""
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET)[0][4][0]
    except Exception:
        ip = host if re.match(r"^\d+\.\d+\.\d+\.\d+$", host or "") else None
    if not ip:
        return None
    name = desc = ""
    try:
        if PLATFORM == "mac":
            out = subprocess.run(["route", "-n", "get", ip], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            name = m.group(1) if m else ""
        elif PLATFORM == "linux":
            out = subprocess.run(["ip", "route", "get", ip], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"\bdev\s+(\S+)", out)
            name = m.group(1) if m else ""
        else:
            cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                   "$r=Find-NetRoute -RemoteIPAddress '%s' -ErrorAction Stop | Select-Object -First 1; "
                   "$a=Get-NetAdapter -InterfaceIndex $r.InterfaceIndex -ErrorAction SilentlyContinue; "
                   "@{name=[string]$r.InterfaceAlias; desc=[string]$a.InterfaceDescription} | ConvertTo-Json -Compress") % ip
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                               timeout=20, creationflags=0x08000000)
            js = json.loads(r.stdout.decode("utf-8-sig", "replace").strip() or "{}")
            name, desc = js.get("name") or "", js.get("desc") or ""
    except Exception:
        return None
    if not name:
        return None
    return {"name": name, "desc": desc, "tunnel": bool(TUNNEL_RE.search(name + " " + desc))}


_rtt_cache = {}
DETOUR_MS = 150     # 到国内入口的 TLS 握手超过这么久，多半是被 TUN 送去了海外节点再绕回来


def tls_rtt(host, port, timeout=5):
    """到这个地址做一次 TLS 握手要多少毫秒（取两次里快的）。查不了返回 None；结果缓存 5 分钟。
    用 TLS 握手而不是 TCP 连接来测：开着 TUN 时 TCP 连接会被本机的 TUN 直接应答，测不出真实路程。"""
    key = (host, port)
    c = _rtt_cache.get(key)
    if c and time.time() - c[1] < 300:
        return c[0]
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE      # 只量时间，不校验证书
    best = None
    for _ in range(2):
        try:
            t0 = time.time()
            with socket.create_connection((host, port), timeout=timeout) as s:
                with ctx.wrap_socket(s, server_hostname=host):
                    ms = (time.time() - t0) * 1000
            best = ms if best is None else min(best, ms)
        except Exception:
            pass
    _rtt_cache[key] = (best, time.time())
    return best


def system_proxy():
    """系统代理设置（macOS 网络设置 / Windows 注册表）。Claude Code 不认它，这里只是用来提醒「你以为开了代理」。"""
    try:
        if PLATFORM == "mac":
            d = urllib.request.getproxies_macosx_sysconf()
        elif IS_WIN:
            d = urllib.request.getproxies_registry()
        else:
            return None
    except Exception:
        return None
    return d.get("https") or d.get("http") or None


def _hostport(url):
    u = urlparse(url if "://" in url else "http://" + url)
    return (u.hostname or url) + (":%d" % u.port if u.port else "")


GATEWAY_VARS = ("ANTHROPIC_BASE_URL",)
PROVIDER_NAMES = {"CLAUDE_CODE_USE_BEDROCK": "Amazon Bedrock", "CLAUDE_CODE_USE_VERTEX": "Google Vertex",
                  "CLAUDE_CODE_USE_FOUNDRY": "Microsoft Foundry"}


def gateway():
    """claude 是不是根本不直接连 Anthropic（第三方 API 中转站 / Bedrock 等）。返回说明或 None。
    看用户级、管理员的 settings.json 和换号台所在终端的环境变量（项目目录里的单独在冲突检查里报）。"""
    envs = []
    for p in claude_settings_files():
        try:
            js = json.loads(read_text(p))
            if isinstance(js, dict) and isinstance(js.get("env"), dict):
                envs.append(js["env"])
        except (OSError, ValueError):
            continue
    envs.append(os.environ)
    for env in envs:
        if env.get("ANTHROPIC_BASE_URL"):
            return "第三方 API 地址 " + _hostport(env["ANTHROPIC_BASE_URL"])
        for v, nm in PROVIDER_NAMES.items():
            if str(env.get(v) or "").strip() not in ("", "0", "false"):
                return nm
    return None


RELAY_PORTS = (32230, 32231)      # 我们中转的端口：没绑但地址长这样的，也认成「中转」


def route_info(d, acc, st):
    """顶部那一行：claude 用这个号时实际走哪条线。st 是经同一条线查出口的结果（ip_state）。
    返回 {"tag", "detail", "level", "tip"}：
      tag   中转 / 端口（本机代理端口）/ 代理（别的远程代理）/ TUN / 直连 / 第三方（不直接连 Anthropic）
      level ok 正常；warn 能用但有隐患；bad 号和 IP 对不上 / 连不上 / cct 的数据不作数
      tip   悬停提示和红条里的说明（怎么回事、怎么处理）"""
    def R(tag, detail, level="ok", tip=""):
        return {"tag": tag, "detail": detail, "level": level, "tip": tip}

    gw = gateway()
    if gw:
        return R("第三方", gw.replace("第三方 API 地址 ", ""), "bad",
                 "claude 不直接连 Anthropic，改发到这个地址：cct 切换的号、查到的用量和出口都不作数，token 还会发给这个第三方。"
                 "删掉 ANTHROPIC_BASE_URL / Bedrock 等设置后才正常")
    p = target_proxy(d, acc)
    src = proxy_of(d, acc)
    if p:
        real, where = p, ""
    elif src == CLAUDE and _settings_proxy_raw():
        real, where = claude_proxy(), "settings.json"
    else:
        real, where = term_proxy() or (None, "")
    if real:
        u = urlparse(real)
        host = u.hostname or ""
        owners = []
        if not p:
            # 终端里带的代理正好是别的号绑的中转（跑过中转安装命令的终端、写进了 .zshrc…）：这个号会借用别人的 IP
            owners = [_label(a["name"]) for a in d["accounts"] if a.get("proxy") == real and a is not acc]
            if acc is not None and d.get("normal_proxy") == real:
                owners.append("普通登录")
        local = host in LOCAL_HOSTS
        relay = bool(p or owners) or (u.scheme == "https" and bool(u.username) and u.port in RELAY_PORTS)
        tag = "中转" if relay else ("端口" if local else "代理")
        detail = (proxy_user(real) or host) if relay else _hostport(real)
        if where:
            detail += " · " + where
        if owners:
            return R(tag, "是%s的中转，会借用它的 IP" % "、".join(owners), "bad",
                     "这个号没绑代理，claude 会用%s里的代理，两个号就从同一个 IP 出去。新开一个没设代理的终端%s，"
                     "或者给这个号绑它自己的代理"
                     % (where, "（%s 里那行要删掉）" % where if where.startswith(("~", "C:", "Windows")) else ""))
        tip = ""
        if host and not local:
            f = iface_to(host)
            if f and f["tunnel"]:
                # 连代理这一段也进了 TUN。Clash 规则对国内 IP 一般是直连，那就没事；被送去海外节点才是绕路——量一下就知道
                ms = tls_rtt(host, u.port or 443) if u.scheme == "https" else None
                if ms is not None and ms > DETOUR_MS:
                    return R(tag, detail + " · 入口被 TUN 绕路 %d ms" % ms, "warn",
                             "连代理这一段被 TUN / VPN（%s）送去海外节点又绕回来，会变慢：在 Clash 规则里把 %s 设成直连"
                             % (f["name"], host))
                tip = "连代理这一段经过 TUN %s%s，正常" % (f["name"], "，握手 %d ms 是直连" % ms if ms is not None else "")
        return R(tag, detail, "ok", tip or "代理：%s（密码不显示）" % proxy_label(real))
    f = iface_to(API_HOST)
    if f and f["tunnel"]:
        return R("TUN", f["name"], "ok", "没有代理变量，直连的流量被 TUN / VPN 虚拟网卡（%s）接管，按它的规则出去"
                 % (f["desc"] or f["name"]))
    sp = system_proxy()
    fine = bool(st and st.get("ok"))
    if sp:
        return R("直连", "只开了系统代理 %s，claude 不认" % _hostport(sp), "warn" if fine else "bad",
                 "Claude Code 不认系统代理设置，开着也没用：要开 Clash 的 TUN 模式，或者给这个号绑代理")
    if not fine:
        return R("直连", "本机没有代理、也没开 TUN，多半连不上", "bad", "给这个号绑代理，或者开 Clash 的 TUN 模式")
    if f is None:
        return R("直连", "查不到走哪张网卡", "ok", "分不清有没有 TUN；出口 IP 正常")
    return R("直连", "出口在海外", "ok", "本机没有代理和隧道，出口却在海外：路由器 / 网关做了代理，或者这台机器本身在海外")


def describe_route(d, acc, st):
    """route_info 的一行文字版（命令行、提示框用）：(「[标签] 细节」, 等级)。"""
    r = route_info(d, acc, st)
    return ("[%s] %s" % (r["tag"], r["detail"]) if r["detail"] else "[%s]" % r["tag"]), r["level"]


def _label(name):
    return "普通登录" if name is None else "「%s」" % name


def _remember_egress(d, name, st):
    """把查到的出口 IP 记在号上，卡片上显示。"""
    i = (st or {}).get("info") or {}
    if not i.get("ip"):
        return
    e = {"ip": i["ip"], "country": i.get("country") or "", "city": i.get("city") or "", "at": time.time()}
    if name is None:
        d["normal_egress"] = e
    else:
        a = find(d, name)
        if a:
            a["egress"] = e


def _dup_proxy(d, proxy, name):
    """这个代理已经绑给了谁（除了 name 自己）。返回显示名列表。"""
    out = ["「%s」" % a["name"] for a in d["accounts"] if a.get("proxy") == proxy and a["name"] != name]
    if name is not None and d.get("normal_proxy") == proxy:
        out.append("普通登录")
    return out


# ---------------------------------------------------------------- 正在运行的 claude
# 切换会改 settings.json，而正在跑的 claude 会中途重读它的一部分（实测：改代理后少量连接换到新代理），
# 旧号就会有请求从新号的 IP 出去。所以切换前必须一个 claude 都没在跑。
IDE_DIRS = {".vscode": "VS Code", ".vscode-insiders": "VS Code Insiders", ".vscode-oss": "VSCodium", ".cursor": "Cursor",
            ".windsurf": "Windsurf", ".antigravity": "Antigravity", ".antigravity-ide": "Antigravity",
            ".trae": "Trae", ".trae-cn": "Trae", ".kiro": "Kiro"}


def _proc_kind(path):
    p = path.replace("\\", "/")
    m = re.search(r"/([^/]+)/extensions/anthropic\.claude-code", p, re.I)
    if m:
        return "%s 里的 Claude 对话" % IDE_DIRS.get(m.group(1).lower(), m.group(1).lstrip("."))
    if re.search(r"/Application Support/Claude/|/Claude/claude-code/", p, re.I):
        return "Claude 桌面版"
    return "终端里的 claude"


def _is_claude(comm, args):
    """是不是 Claude Code 进程：原生版 / IDE 插件 / 桌面版起的都叫 claude（npm 版会把进程名改成 claude），
    直接跑 versions 目录里的原生版时文件名是版本号，用 node 跑 cli.js 时看命令行。"""
    c = comm.replace("\\", "/")
    a = args.replace("\\", "/")
    base = c.rsplit("/", 1)[-1]
    if IS_WIN:
        base = base.lower()
        if "/anthropicclaude/" in c.lower():      # Windows 桌面版本体也叫 claude.exe，它不是 Claude Code
            return False
    if base in ("claude", "claude.exe"):
        return True
    if "/claude/versions/" in c or "/claude/versions/" in a.split(" ", 1)[0]:
        return True
    return base.split(".")[0].lower() in ("node", "bun") and "@anthropic-ai/claude-code" in a


def _posix_procs():
    uid = str(os.getuid())

    def ps(field):
        r = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "uid=", "-o", field + "="],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or "ps 运行失败")
        res = {}
        for ln in r.stdout.splitlines():
            f = ln.strip().split(None, 2)
            if len(f) >= 2 and f[1] == uid and f[0].isdigit():
                res[int(f[0])] = f[2] if len(f) > 2 else ""
        return res

    comm, args = ps("comm"), ps("args")
    return [(pid, c, args.get(pid, "")) for pid, c in comm.items()]


def _win_procs():
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
           "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(claude|node|bun|[0-9.]+)(\\.exe)?$' } | "
           "Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                       timeout=40, creationflags=0x08000000)
    if r.returncode != 0:
        raise OSError(r.stderr.decode("utf-8", "replace").strip() or "PowerShell 运行失败")
    t = r.stdout.decode("utf-8-sig", "replace").strip()
    if not t:
        return []
    js = json.loads(t)
    if isinstance(js, dict):
        js = [js]
    return [(int(x["ProcessId"]), x.get("ExecutablePath") or x.get("Name") or "", x.get("CommandLine") or "") for x in js]


def claude_processes():
    """正在运行的 Claude Code（终端里的、IDE 插件起的、桌面版起的），只看当前用户自己的。
    返回 [{"pid", "kind", "path"}]。查不了（没有 ps / PowerShell 出错）时抛异常，由调用方决定。"""
    me = os.getpid()
    out = []
    for pid, comm, args in (_win_procs() if IS_WIN else _posix_procs()):
        if pid != me and _is_claude(comm, args):
            out.append({"pid": pid, "kind": _proc_kind(comm + " " + args), "path": comm})
    return out


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_processes(procs):
    """强制结束这些 claude：先礼后兵（posix 先 SIGTERM，4 秒还在就 SIGKILL；Windows 直接 taskkill /F）。"""
    for p in procs:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"], capture_output=True, timeout=15,
                               creationflags=0x08000000)
            else:
                os.kill(p["pid"], signal.SIGTERM)
        except Exception:
            pass
    if not IS_WIN:
        left, deadline = list(procs), time.time() + 4
        while left and time.time() < deadline:
            time.sleep(0.2)
            left = [p for p in left if _alive(p["pid"])]
        for p in left:
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except OSError:
                pass
    time.sleep(0.5)
    log("强制结束 claude：%s" % ", ".join("%d(%s)" % (p["pid"], p["kind"]) for p in procs))


def ensure_no_claude(kill=False, force=False):
    """切换前调用：还有 claude 在运行就拒绝（kill=True 先强制结束它们）。"""
    try:
        procs = claude_processes()
        if procs and kill:
            kill_processes(procs)
            procs = claude_processes()
    except ClaudeRunning:
        raise
    except Exception as e:
        if _forced(force, "nops"):
            return
        raise NeedForce("查不了有没有 claude 在运行（%s）。请自己确认终端和 IDE 里的 claude 都关掉了，再强制继续" % e, "nops")
    if procs:
        raise ClaudeRunning(procs)


# ------------------------------------------------------------- claude 的 settings.json
def settings_path():
    """claude 的用户级 settings.json（cct 把当前号的 token 和代理写在这里）。"""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "settings.json")


def _read_settings(path):
    if not os.path.exists(path):
        return {}
    text = read_text(path).strip()
    if not text:
        return {}
    try:
        js = json.loads(text)
    except ValueError as e:
        raise CctError("%s 不是合法的 JSON（%s），cct 不敢改它。修好后再试" % (path, e))
    if not isinstance(js, dict):
        raise CctError("%s 的格式不对（最外层不是 {}），cct 不敢改它" % path)
    return js


SETTINGS_BACKUPS_KEEP = 20


def _write_settings(path, js):
    """改之前备份（token 打码），再原子替换。Windows 上文件正被读时替换可能失败，重试几次。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    bom = had_bom(path)
    if os.path.exists(path):
        backup_config(path)
        olds = sorted(n for n in os.listdir(CONFIG_BACKUP_DIR) if n.startswith("settings.json."))
        for n in olds[:-SETTINGS_BACKUPS_KEEP]:
            try:
                os.remove(os.path.join(CONFIG_BACKUP_DIR, n))
            except OSError:
                pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.")
    with os.fdopen(fd, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\n") as f:
        json.dump(js, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if not IS_WIN:
        os.chmod(tmp, 0o600)        # 里面有 token 了，只给自己读
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                os.remove(tmp)
                raise
            time.sleep(0.2)


def sync_settings(write_proxy=False):
    """加锁的外壳：网页和命令行可能同时在改（读-改-写 settings.json 不能交错）。说明见 _sync_settings。"""
    with locked():
        return _sync_settings(write_proxy)


def _sync_settings(write_proxy=False):
    """让 claude 的 settings.json 跟上当前账号：
    - 当前是 setup token 账号：env.CLAUDE_CODE_OAUTH_TOKEN = 它的 token；
    - 普通登录：写成空字符串（盖住终端 / IDE 里残留的旧 token，claude 就用 /login）；一个账号都没有时整个去掉。
    write_proxy=True（切换 / 改当前号的代理）且是「一个号一个 IP」模式时，连 HTTPS_PROXY 等一起写成这个号绑的代理；
    这个号没绑就去掉，绝不留着上一个号的代理。返回是否改了文件。"""
    d = load()
    cur = active_acc(d)
    path = settings_path()
    js = _read_settings(path)
    env = js.get("env") if isinstance(js.get("env"), dict) else {}
    new = dict(env)
    if cur:
        new[ENV] = cur["token"]
    elif d["accounts"]:
        new[ENV] = ""
    else:
        new.pop(ENV, None)
    if write_proxy and bound(d):
        p = target_proxy(d, cur)
        # Windows 的环境变量不分大小写：HTTPS_PROXY 和 https_proxy 是同一个，settings.json 里两个都写时
        # 排在后面的那个说了算。中转给 Windows 的配置命令只改大写的，cct 要是留着小写的旧值，
        # 改完代理 claude 实际还走旧的。所以 Windows 上只写大写，小写的删掉。
        keys = PROXY_KEYS[:2] if IS_WIN else PROXY_KEYS
        for k in PROXY_KEYS:
            if p and k in keys:
                new[k] = p
            else:
                new.pop(k, None)
    if bound(d) and target_proxy(d, cur):
        _bypass_env(new)      # 不改走哪条线，只补「哪些地址别走代理」，所以平时同步（装机等）也补上
    if new == env and (env or "env" not in js):
        return False
    js["env"] = new
    _write_settings(path, js)
    return True


def _bypass_env(env):
    """绑了代理时补上 NO_PROXY 和空的 ALL_PROXY（和中转配置命令一致）。
    NO_PROXY 原来没有就写默认名单；原来有就保留，只补上缺的 localhost、127.0.0.1。
    ALL_PROXY 置空：防 Clash「复制终端代理命令」留下的 all_proxy=socks5://… 让部分工具绕开中转。
    Windows 环境变量不分大小写，只写大写的、删掉小写的（同 PROXY_KEYS 的处理）。"""
    items = []
    for k in ("NO_PROXY", "no_proxy"):        # 大小写两份内容不一样时取并集，客户加的一条都不丢
        for x in str(env.get(k) or "").split(","):
            if x.strip() and x.strip() not in items:
                items.append(x.strip())
    items = items or NO_PROXY_DEFAULT.split(",")
    items += [x for x in NO_PROXY_MUST if x not in items]
    no_proxy = ",".join(items)
    if IS_WIN:
        env["NO_PROXY"], env["ALL_PROXY"] = no_proxy, ""
        env.pop("no_proxy", None)
        env.pop("all_proxy", None)
    else:
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
        env["ALL_PROXY"] = env["all_proxy"] = ""


def settings_proxy():
    """settings.json 里现在写的代理（没有返回 None）。"""
    try:
        env = _read_settings(settings_path()).get("env") or {}
    except CctError:
        return None
    return next((env[k] for k in PROXY_KEYS if env.get(k)), None)


def settings_drift(d):
    """「一个号一个 IP」模式下，settings.json 里的代理和当前号绑的不一样（被别的命令改过）时返回说明。"""
    if not bound(d):
        return None
    want, have = target_proxy(d, active_acc(d)), settings_proxy()
    if (want or None) == (have or None):
        return None
    w, h = proxy_label(want) or "（没绑代理）", proxy_label(have) or "（没配代理）"
    if w == h:      # 只差在密码上（显示时密码是隐藏的，不说清楚会看成两个一样的地址）
        h += "（密码不一样）"
    return {"want": w, "have": h}


def refresh_one(name, force=False):
    """只查一个号的用量并写回，经这个号自己的代理发出。故意不做「批量查全部」：
    那样同一时刻、同一出口会同时出现多个账号的请求。出口 IP 不对 / 号没绑代理时先中止（force 可跳过）。"""
    d = load()
    a = must_find(d, name)
    p = proxy_of(d, a)
    if p == ENVP and not _forced(force, "unbound"):
        raise NeedForce("「%s」没绑代理：查用量不走中转，只认终端的代理环境变量或 TUN（不认系统代理设置），"
                        "出口可能和它平时用的 IP 不一样" % name, "unbound")
    st = ensure_safe_egress(p, force)
    if p == ENVP:
        _check_borrow(d, a, st, force, "查用量")
    u = probe(a["token"], p)
    with txn() as d:
        acc = find(d, name)
        if acc:
            acc["usage"] = u
            if u.get("org"):
                acc["org"] = u["org"]
            if st["ok"] and p != ENVP:      # 没绑代理的号不记：卡片上的出口专指它代理的出口
                _remember_egress(d, name, st)
    return u


def _check_borrow(d, acc, st, force, what):
    """没绑代理的号要联网（查用量 / 验证 token）前：终端里带的代理正好是别的号的中转、出口不通等「红」的情况先拦下。"""
    r = route_info(d, acc, st)
    if r["level"] == "bad" and not _forced(force, "ip"):
        raise NeedForce("%s会走：[%s] %s。%s" % (what, r["tag"], r["detail"], r["tip"]), "ip")


# ----------------------------------------------------------------------- actions
def add_account(name, token, proxy=None, force=False):
    """添加账号。proxy 是这个号绑的代理：验证 token 就经它发出（号还没加进来就先走对 IP）。
    新加的号不会自动启用（启用 = 切换，要先关掉 claude），点「切换」才用它。"""
    name, token = (name or "").strip(), (token or "").strip()
    proxy = normalize_proxy(proxy)
    if not name:
        raise CctError("名字不能为空")
    if not token.startswith("sk-ant-oat"):
        raise CctError("这不像 setup token（应以 sk-ant-oat 开头）")
    d = load()
    if any(a["token"] == token for a in d["accounts"]):
        raise CctError("这个 token 已经存在，账号名是「%s」" % next(a["name"] for a in d["accounts"] if a["token"] == token))
    if proxy:
        dup = _dup_proxy(d, proxy, name)
        if dup and not _forced(force, "dup"):
            raise NeedForce("这个代理已经绑给了 %s：两个号共用一个出口 IP，和「一个号一个 IP」冲突" % "、".join(dup), "dup")
        p = proxy
    elif bound(d):
        if not _forced(force, "unbound"):
            raise NeedForce("别的号都绑了代理，这个号没填：验证 token 时不走中转，只认终端的代理环境变量或 TUN"
                            "（不认系统代理设置）", "unbound")
        p = ENVP
    else:
        p = CLAUDE
    st = ensure_safe_egress(p, force)
    if p == ENVP:
        _check_borrow(d, {"name": name}, st, force, "验证 token ")
    u = probe(token, p)
    if u["state"] == "invalid":
        raise CctError("token 无效：%s" % u.get("error", ""))
    if u["state"] == "neterr":
        raise CctError(u["error"] + "，没法验证 token，稍后再试")
    with txn() as d:
        same_org = next((a for a in d["accounts"] if u.get("org") and a.get("org") == u["org"]), None)
        if same_org:
            # 同一个号重新生成了 token：换掉旧 token，名字不变；填了代理就一起换
            same_org["token"], same_org["usage"] = token, u
            if proxy:
                same_org["proxy"] = proxy
            result = {"name": same_org["name"], "updated": True, "usage": u}
        else:
            if find(d, name):
                raise CctError("「%s」这个名字已经用过了" % name)
            acc = {"name": name, "token": token, "org": u.get("org"), "added_at": time.time(), "usage": u}
            if proxy:
                acc["proxy"] = proxy
            d["accounts"].append(acc)
            result = {"name": name, "updated": False, "usage": u}
        if st["ok"] and p not in (ENVP, CLAUDE):
            _remember_egress(d, result["name"], st)
        was_active = d["active"] == result["name"]
    log("%s：%s（代理 %s）" % ("更新 token" if result["updated"] else "添加", result["name"], proxy_label(proxy) or "没填"))
    sync_settings(write_proxy=False)    # 只有当前号换了 token 时才会改文件（同一个号，IP 不变）
    result["active"] = was_active
    return result


def _precheck_target(d, name, force):
    """切到某个号之前：「一个号一个 IP」模式下，号没绑代理 / 代理不通 / 出口是大陆 IP 都先拦下。
    返回目标代理的出口检查结果（没绑代理时 None）。"""
    if not bound(d):
        return None
    acc = None if name is None else must_find(d, name)
    p = target_proxy(d, acc)
    if not p:
        if not _forced(force, "unbound"):
            raise NeedForce("%s 没绑代理：切过去后 claude 不走中转，只认终端里设的代理环境变量或 Clash 的 TUN 模式"
                            "（Claude Code 不认「系统代理」设置）；都没有就直连，在国内多半连不上" % _label(name), "unbound")
        # 确认了也先看看切过去后实际走哪条线：连不上、是大陆 IP、借用了别的号的中转，都再提醒一次
        st = ip_state(ENVP, force=True)
        r = route_info(d, acc, st)
        if (r["level"] == "bad" or not st["ok"]) and not _forced(force, "ip"):
            raise NeedForce("切到 %s 后 claude 实际走：[%s] %s%s。%s" % (_label(name), r["tag"], r["detail"],
                            "" if st["ok"] else "（%s）" % st["why"], r["tip"]), "ip")
        return dict(st, unbound=True)
    st = ip_state(p, force=True)
    if not st["ok"] and not _forced(force, "ip"):
        raise NeedForce("%s 的代理有问题：%s" % (_label(name), st["why"]), "ip")
    return st


def _switch(name, force=False, kill=False, mutate=None):
    """切到 name（None = 普通登录）：检查代理 → 确认没有 claude 在跑 → 改 accounts.json → 写 settings.json。
    mutate(d) 在同一个事务里先执行（删除当前号时用）。"""
    d = load()
    st = _precheck_target(d, name, force)
    ensure_no_claude(kill, force)
    with txn() as d2:
        if mutate:
            mutate(d2)
        prev, d2["active"] = d2["active"], name
        if st and st["ok"] and not st.get("unbound"):    # 没绑代理的号不记：卡片上的出口专指它代理的出口
            _remember_egress(d2, name, st)
    sync_settings(write_proxy=True)
    try:
        late = claude_processes()       # 检查和写入之间刚好有 claude 启动：提醒重开
    except Exception:
        late = []
    log("切换：%s → %s%s" % (_label(prev), _label(name), "（出口 %s）" % st["info"].get("ip") if st and st["ok"] else ""))
    d = load()
    acc = active_acc(d)
    route = describe_route(d, acc, st if st else ip_state(route_of(d, acc)[0]))[0]
    return {"from": prev, "to": name, "egress": (st or {}).get("info"), "route": route, "late": late}


def use_account(name, force=False, kill=False):
    must_find(load(), name)
    return _switch(name, force, kill)


def use_normal(force=False, kill=False):
    """不用任何 setup token，走这台电脑原来的普通登录（claude /login）。账号都留着，随时可以切回来。"""
    return _switch(None, force, kill)


def set_proxy(name, proxy, force=False, kill=False):
    """给号（name=None 表示普通登录）绑定 / 解绑代理。改的是当前正在用的那个时，等于一次切换：同样要先关掉 claude。"""
    proxy = normalize_proxy(proxy)
    d = load()
    if name is not None:
        must_find(d, name)
    st = None
    if proxy:
        dup = _dup_proxy(d, proxy, name)
        if dup and not _forced(force, "dup"):
            raise NeedForce("这个代理已经绑给了 %s：两个号共用一个出口 IP，和「一个号一个 IP」冲突" % "、".join(dup), "dup")
        st = ip_state(proxy, force=True)
        if not st["ok"] and not _forced(force, "ip"):
            raise NeedForce("这个代理有问题：%s" % st["why"], "ip")
    is_cur = d["active"] == name
    if is_cur:
        ensure_no_claude(kill, force)
    with txn() as d2:
        if name is None:
            d2["normal_proxy"] = proxy
            d2.pop("normal_egress", None)
        else:
            a = must_find(d2, name)
            a["proxy"] = proxy
            a.pop("egress", None)
        if st and st["ok"]:
            _remember_egress(d2, name, st)
    if is_cur:
        sync_settings(write_proxy=True)
    log("%s 代理：%s" % (_label(name), proxy_label(proxy) or "解绑"))
    return {"egress": (st or {}).get("info"), "current": is_cur}


def normal_login_email():
    """本机普通登录（/login）是哪个账号。只读 ~/.claude.json 里的邮箱，不碰登录凭据。"""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    for p in (os.path.join(base, ".claude.json"), os.path.expanduser("~/.claude.json")):
        try:
            with open(p, encoding="utf-8") as f:
                acc = json.load(f).get("oauthAccount") or {}
            return acc.get("emailAddress") or ""
        except (OSError, ValueError):
            continue
    return ""


def rename_account(old, new):
    new = (new or "").strip()
    if not new:
        raise CctError("新名字不能为空")
    with txn() as d:
        if find(d, new):
            raise CctError("「%s」这个名字已经用过了" % new)
        must_find(d, old)["name"] = new
        if d["active"] == old:
            d["active"] = new
    log("改名：%s → %s" % (old, new))


def delete_account(name, force=False, kill=False):
    """删掉本机保存的 token（不会吊销它）。删的是当前账号时改用普通登录（等于一次切换，要先关掉 claude），
    不悄悄换到别的号。"""
    d = load()
    must_find(d, name)
    was_active = d["active"] == name
    if was_active:
        _switch(None, force, kill, mutate=lambda d2: d2["accounts"].remove(must_find(d2, name)))
    else:
        with txn() as d2:
            d2["accounts"].remove(must_find(d2, name))
        sync_settings(write_proxy=False)
    log("删除：%s%s" % (name, "（是当前账号，改用普通登录）" if was_active else ""))
    return {"wasActive": was_active}


# ------------------------------------------------------------------------ format
def fmt_ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def fmt_left(ts):
    if not ts:
        return ""
    m = max(0, int(ts - time.time())) // 60
    d, rem = divmod(m, 1440)
    h, m = divmod(rem, 60)
    return (("%d天" % d) if d else "") + (("%d小时" % h) if h or d else "") + "%d分" % m


def fmt_ago(ts):
    """用量数据的更新时间文案，如「更新于 2 分钟前」。"""
    if not ts:
        return "尚未查询"
    s = int(time.time() - ts)
    if s < 60:
        return "刚刚更新"
    if s < 3600:
        return "更新于 %d 分钟前" % (s // 60)
    if s < 86400:
        return "更新于 %d 小时前" % (s // 3600)
    return "更新于 %d 天前" % (s // 86400)


STATE_TXT = {"ok": "可用", "limited": "已用完", "invalid": "token 无效", "error": "出错", "neterr": "网络错误"}


# 已经和 claude.ai 的 Usage 页面对照确认过含义的桶
KNOWN_BUCKETS = {
    "5h": "5 小时",          # Current session
    "7d": "7 天",            # This week
    "7d_oi": "7 天 Fable",   # Fable this week（Separate weekly limit for Fable）
}


def bucket_label(b):
    """桶的显示名。认识的用 KNOWN_BUCKETS；不认识的按字段名拼（如 7d_xx → 7 天·xx），不猜含义。"""
    if b in KNOWN_BUCKETS:
        return KNOWN_BUCKETS[b]
    m = re.match(r"^(\d+)([hd])(?:_(.+))?$", b)
    if not m:
        return b
    label = m.group(1) + (" 小时" if m.group(2) == "h" else " 天")
    return label + ("·" + m.group(3) if m.group(3) else "")


def acc_view(a, active):
    u = a.get("usage") or {}
    st = u.get("state")
    if st == "limited" and usable(a):
        st = "ok"   # 缓存里是用完，但已经过了重置时间
    v = {"name": a["name"], "masked": mask(a["token"]), "org": a.get("org") or "",
         "active": a["name"] == active, "state": st, "stateTxt": STATE_TXT.get(st, "未查询"),
         "error": u.get("error", ""), "checkedAgo": fmt_ago(u.get("checked_at")),
         "usable": usable(a), "fable": u.get("fable"), "fableWhy": u.get("fable_why", ""),
         "proxy": proxy_label(a.get("proxy")), "via": proxy_via(a.get("proxy")), "pxUser": proxy_user(a.get("proxy")),
         "egress": a.get("egress"),
         "buckets": []}
    bs = buckets_of(u)
    for b in sorted(bs, key=lambda b: (b not in CORE, CORE.index(b) if b in CORE else 0, b)):
        bk = bs[b]
        passed = bk.get("reset") and bk["reset"] <= time.time()
        v["buckets"].append({"key": b, "label": bucket_label(b), "core": b in CORE,
                             "pct": 0 if passed else round(bk["util"] * 100, 1),
                             "blocked": bucket_blocked(bk),
                             "reset": "" if passed else fmt_ts(bk.get("reset")),
                             "left": "已重置" if passed else fmt_left(bk.get("reset")),
                             "ts": None if passed else bk.get("reset")})
    return v


def build_state():
    d = load()
    return {"active": d["active"], "store": DB, "model": MODEL, "platform": PLATFORM, "version": VERSION,
            "normal": {"active": not active_acc(d), "email": normal_login_email(),
                       "proxy": proxy_label(d.get("normal_proxy")), "via": proxy_via(d.get("normal_proxy")),
                       "pxUser": proxy_user(d.get("normal_proxy")),
                       "egress": d.get("normal_egress")},
            "bound": bound(d), "drift": settings_drift(d), "settings": settings_path(),
            "conflicts": auth_conflicts(),
            "accounts": [acc_view(a, d["active"]) for a in d["accounts"]],
            }


def pad(s, width):
    """按显示宽度补空格（中文占两格）。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, width - w)


def bar(pct, width=16):
    n = round(min(max(pct, 0), 100) / 100 * width)
    return "█" * n + "░" * (width - n)


def ip_line(st):
    i = st["info"]
    if i.get("error"):
        return "查不到（%s）" % i["error"]
    loc = "·".join(x for x in (i.get("country"), i.get("city")) if x)
    return "%s  %s%s" % (i.get("ip") or "?", loc, "" if st["ok"] else "  ⚠ " + st["why"])


def proxy_line(v, is_bound):
    if not v["proxy"]:
        return "⚠ 没绑（切过去 / 查用量要确认）" if is_bound else "没绑（跟着 claude 的设置走）"
    e = v.get("egress") or {}
    return v["proxy"] + ("  → 出口 %s %s" % (e["ip"], e.get("country") or "") if e.get("ip") else "")


def print_list():
    st = build_state()
    if not st["accounts"]:
        print("还没有账号，现在用的是普通登录（claude /login）。用 `cct add <名字> <token>` 添加，或运行 `cct` 打开网页。")
        return
    print("数据：%s\n" % st["store"])
    nm_ = st["normal"]
    print("%s 普通登录（claude /login）%s" % ("★" if nm_["active"] else " ",
          "：" + nm_["email"] if nm_["email"] else "：本机还没用 /login 登录过"))
    if nm_["proxy"] or st["bound"]:
        print("     代理 %s" % proxy_line(nm_, st["bound"]))
    for a in st["accounts"]:
        mark = "★" if a["active"] else " "
        print("%s %s %s %s  （%s）" % (mark, pad(a["name"], 16), pad(a["stateTxt"], 10), a["masked"], a["checkedAgo"]))
        if a["proxy"] or st["bound"]:
            print("     代理 %s" % proxy_line(a, st["bound"]))
        for x in a["buckets"]:
            if x["blocked"]:
                extra = "  ← 已用完"
            elif x["left"] == "已重置":
                extra = "  ← 已重置"
            elif x["left"]:
                extra = "  剩余 " + x["left"]
            else:
                extra = ""
            clock = time.strftime("%m-%d %H:%M", time.gmtime(x["ts"] + 8 * 3600)) if x.get("ts") else "-"
            print("     %s %s %5.1f%%  重置 %s%s" % (pad(x["label"], 9), bar(x["pct"]), x["pct"], clock, extra))
        if a["error"]:
            print("     ⚠ %s" % a["error"])
    if st["drift"]:
        print("\n⚠ settings.json 里现在的代理是 %s，和当前绑的 %s 不一样（被别的命令改过？）。"
              "重新切换一次当前号就会按绑定改回。" % (st["drift"]["have"], st["drift"]["want"]))
    print_conflicts()
    print("\n★ = 当前账号。新启动的 claude（终端和 IDE 里新开的对话）都用它，不用重启 IDE。")


# ------------------------------------------------- 旧版 claude() 启动函数调用（保留兼容）
def cmd_token(args):
    """旧版装在终端里的 claude 启动函数会调它（升级前就开着的终端里还有）：stdout 只输出当前账号的 token。
    只读本地文件，不联网、不自动换号。没有账号或选了「普通登录」时以退出码 3 退出。
    新版靠 settings.json 切换，它的优先级高于这里传的环境变量，所以旧函数还在也不影响。"""
    cur = active_acc(load())
    if not cur:
        sys.exit(3)
    print(cur["token"])


def eprint(msg):
    print(msg, file=sys.stderr)


# ------------------------------------------------------------------ Windows 环境变量
def win_get_env(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def win_set_env(name, value):
    """写 / 删「用户环境变量」，再广播 WM_SETTINGCHANGE，让之后新开的程序读到。value=None 表示删除。"""
    import winreg, ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def broadcast():   # 有程序卡住时可能要等几秒，放后台线程
        try:
            res = ctypes.c_size_t()
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, ctypes.byref(res))
        except Exception:
            pass
    threading.Thread(target=broadcast, daemon=True).start()


def win_drop_env(msgs):
    """旧版把当前 token 写进 Windows 用户环境变量；新版改写 settings.json，安装 / 卸载时把 cct 写的那个删掉。
    只删 cct 自己写的（记下的值，或列表里任何一个号的 token），用户原本手动设的不碰。"""
    d = load()
    have = win_get_env(ENV)
    if have and (have == d.get("env_set") or any(a["token"] == have for a in d["accounts"])):
        win_set_env(ENV, None)
        msgs.append("已删除旧版写的用户环境变量 %s（现在改由 settings.json 切换）" % ENV)
    if d.get("env_set"):
        with txn() as d2:
            d2["env_set"] = None


# ------------------------------------------------------------------ 认证冲突检查
CONFLICT_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def claude_settings_files():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    files = [os.path.join(base, "settings.json"), os.path.join(base, "settings.local.json")]
    if IS_WIN:
        files.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ClaudeCode", "managed-settings.json"))
    elif PLATFORM == "mac":
        files.append("/Library/Application Support/ClaudeCode/managed-settings.json")
    else:
        files.append("/etc/claude-code/managed-settings.json")
    return files


def rc_exports(name):
    """shell 配置文件里（cct 自己的配置块以外）有没有 export 这个变量。"""
    pat = re.compile(r"^\s*export\s+%s=" % re.escape(name), re.M)
    hits = []
    for rc in rc_files():
        if os.path.exists(rc) and pat.search(BLOCK_RE.sub("", read_text(rc))):
            hits.append(rc)
    return hits


def auth_conflicts():
    """找出会让 cct 的切换不生效的其他认证设置。返回 [{"where", "what", "why"}]。
    依据官方文档的优先级（高 → 低）：Bedrock/Vertex/Foundry > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY
    > apiKeyHelper > CLAUDE_CODE_OAUTH_TOKEN（cct 用的）> /login 普通登录。
    https://code.claude.com/docs/en/authentication （Authentication precedence）"""
    out = []

    def add(where, what, why):
        out.append({"where": where, "what": what, "why": why})

    for v in CONFLICT_VARS + PROVIDER_VARS + GATEWAY_VARS:
        why = _conflict_why(v)
        if IS_WIN:
            if win_get_env(v):
                add("用户环境变量", v, why)
            elif os.environ.get(v):
                add("当前进程的环境变量", v, why)
        else:
            files = rc_exports(v)
            if files:
                add("、".join(files), "export " + v, why)
            elif os.environ.get(v):
                add("当前终端的环境变量", v, why)
    mine = os.path.normcase(os.path.abspath(settings_path()))
    is_bound = bound(load())
    for p in claude_settings_files():
        try:
            js = json.loads(read_text(p))
        except (OSError, ValueError):
            continue
        if not isinstance(js, dict):
            continue
        if js.get("apiKeyHelper"):
            add(p, "apiKeyHelper", "优先级高于 cct 的 token（官方文档），cct 切换的账号和普通登录都不生效")
        env = js.get("env") if isinstance(js.get("env"), dict) else {}
        other = os.path.normcase(os.path.abspath(p)) != mine    # cct 自己管的是用户级 settings.json，别的文件优先级更高
        if other and ENV in env:
            add(p, "env." + ENV, "这份配置的优先级高于 cct 写的 settings.json，会盖过 cct 切换的账号")
        if other and is_bound:
            for k in PROXY_KEYS:
                if env.get(k):
                    add(p, "env." + k, "这份配置的优先级高于 cct 写的 settings.json，会盖过每个号绑的代理，号和 IP 就对不上了")
                    break
        for v in CONFLICT_VARS + PROVIDER_VARS + GATEWAY_VARS:
            if env.get(v):
                add(p, "env." + v, _conflict_why(v))
    # 项目目录里的 .claude/settings*.json：优先级比用户级高，在那个项目里打开 claude 时会盖过 cct 写的
    for proj, items in project_overrides():
        for what, why in items:
            add(proj, what, "只在这个项目里生效：" + why)
    return out


def _conflict_why(v):
    if v in PROVIDER_VARS:
        return "会让 Claude Code 改走 Bedrock / Vertex / Foundry，cct 的账号和普通登录都不用"
    if v in GATEWAY_VARS:
        return ("claude 改发到这个地址（第三方 API 中转站），不直接连 Anthropic：cct 切换的号、查到的用量和出口 IP 都不作数，"
                "token 还会发给这个第三方")
    return "优先级高于 cct 的 token（官方文档），cct 切换的账号和普通登录都不生效"


_proj_cache = {"key": None, "dirs": [], "files": {}}


def claude_projects():
    """claude 打开过的项目目录（~/.claude.json 的 projects）。这个文件可能很大，按修改时间缓存。"""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    for p in (os.path.join(base, ".claude.json"), os.path.expanduser("~/.claude.json")):
        try:
            key = (p, os.stat(p).st_mtime)
        except OSError:
            continue
        if _proj_cache["key"] != key:
            try:
                projs = json.loads(read_text(p)).get("projects") or {}
            except (OSError, ValueError, AttributeError):
                projs = {}
            _proj_cache.update(key=key, dirs=[k for k in projs if isinstance(k, str)])
        return [k for k in _proj_cache["dirs"] if os.path.isdir(k)]
    return []


def project_overrides():
    """每个项目的 .claude/settings.json、settings.local.json 里会盖过 cct 的设置：[(文件, [(项, 原因)])]。按修改时间缓存。"""
    out = []
    mine = {os.path.normcase(os.path.abspath(x)) for x in claude_settings_files()}
    for proj in claude_projects():
        for name in ("settings.json", "settings.local.json"):
            f = os.path.join(proj, ".claude", name)
            if os.path.normcase(os.path.abspath(f)) in mine:
                continue        # 在家目录里开过 claude：「项目」的 .claude/settings.json 就是用户级那份，不重复算
            try:
                mt = os.stat(f).st_mtime
            except OSError:
                continue
            c = _proj_cache["files"].get(f)
            if not c or c[0] != mt:
                items = []
                try:
                    js = json.loads(read_text(f))
                    env = js.get("env") if isinstance(js, dict) and isinstance(js.get("env"), dict) else {}
                    if isinstance(js, dict) and js.get("apiKeyHelper"):
                        items.append(("apiKeyHelper", _conflict_why("ANTHROPIC_API_KEY")))
                except (OSError, ValueError):
                    env = {}
                if ENV in env:
                    items.append(("env." + ENV, "会盖过 cct 切换的账号"))
                k = next((k for k in PROXY_KEYS if env.get(k)), None)
                if k:
                    items.append(("env." + k, "会盖过 cct 按号写的代理（%s），号和 IP 就对不上了" % _hostport(env[k])))
                for v in CONFLICT_VARS + PROVIDER_VARS + GATEWAY_VARS:
                    if env.get(v):
                        items.append(("env." + v, _conflict_why(v)))
                c = _proj_cache["files"][f] = (mt, items)
            if c[1]:
                out.append((f, c[1]))
    return out


# ---------------------------------------------------------------------- install
BEGIN = "# >>> cct (Claude setup-token switcher) >>>"
END = "# <<< cct <<<"
OLD_BEGINS = ("# >>> cct (Claude setup-token 换号器) >>>",)   # 旧版的标记，升级时一并替换掉
BLOCK_RE = re.compile("(?:%s).*?%s\n?" % ("|".join(re.escape(b) for b in (BEGIN,) + OLD_BEGINS), re.escape(END)), re.S)
EXPORT_RE = re.compile(r'^\s*export\s+CLAUDE_CODE_OAUTH_TOKEN=["\']?(sk-ant-oat[^"\'\s]+)["\']?\s*$')
TOKEN_RE = re.compile(r"sk-ant-oat[0-9A-Za-z_\-]+")


def install_app_copy(msgs=None):
    """把自己复制到 ~/.cc_tokens/app/cct.py。启动函数、cct 命令都指向这一份，
    所以原来复制过来的文件夹之后随便挪、随便删都不影响。
    换了程序本体就往 msgs 里记一条：否则升级时只会看到「已是最新，无需改动」，容易以为没生效。"""
    ensure_store()
    os.makedirs(APP_DIR, exist_ok=True)
    me = os.path.abspath(__file__)
    if os.path.normcase(me) != os.path.normcase(APP):
        with open(me, "rb") as f:
            new = f.read()
        old = None
        if os.path.exists(APP):
            with open(APP, "rb") as f:
                old = f.read()
        if new != old:
            with open(APP, "wb") as f:
                f.write(new)
            if msgs is not None:
                msgs.append("程序已安装到 %s" % APP if old is None
                            else "程序本体已更新到这一版（%s）" % APP)
    if not IS_WIN:
        os.chmod(APP, 0o755)
    return APP


def read_text(path):
    """读文本文件。Windows 上老的 PowerShell 配置可能是 ANSI(GBK) 编码，读不了 UTF-8 就按系统编码读。"""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("mbcs" if IS_WIN else "latin-1")
    return text.replace("\r\n", "\n")      # 统一成 \n 处理，写回时再按系统换行


CONFIG_BACKUP_DIR = os.path.join(BACKUP_DIR, "config")


def mask_tokens(text):
    """把文本里的 setup token 打码（只留最后 4 位），备份文件里不留明文。"""
    return TOKEN_RE.sub(lambda m: "sk-ant-oat01-…已打码…" + m.group(0)[-4:], text)


def backup_config(path):
    """改 shell / PowerShell 配置之前备份一份：放进 ~/.cc_tokens/backups/config/（不留在家目录），
    token 打码。账号的 token 本来就存在 accounts.json 和它的备份里，打码不会丢东西。"""
    if not os.path.exists(path):
        return None
    os.makedirs(CONFIG_BACKUP_DIR, mode=0o700, exist_ok=True)
    name = os.path.basename(path).lstrip(".") or "config"
    stamp = time.strftime("%Y%m%d%H%M%S")
    dst = os.path.join(CONFIG_BACKUP_DIR, "%s.%s" % (name, stamp))
    n = 1
    while os.path.exists(dst):              # 同一秒里备份了两次：加序号，别把前一份盖掉
        n += 1
        dst = os.path.join(CONFIG_BACKUP_DIR, "%s.%s-%d" % (name, stamp, n))
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(mask_tokens(read_text(path)))
    return dst


def put_block(path, block, bom=False):
    """把 cct 的配置块写进文件（已有就替换成最新的）。返回是否改动了文件；改动前先备份。"""
    text = read_text(path) if os.path.exists(path) else ""
    if BLOCK_RE.search(text):
        new = BLOCK_RE.sub(lambda _: block + "\n", text, count=1)
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    if new == text:
        return False
    if os.path.exists(path):
        backup_config(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\r\n" if IS_WIN else "\n") as f:
        f.write(new)
    return True


def had_bom(path):
    """文件原本带不带 UTF-8 BOM。读不到就当作没有。"""
    try:
        with open(path, "rb") as f:
            return f.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def drop_block(path):
    if not os.path.exists(path):
        return False
    bom = had_bom(path)          # 原来有就还给它，原来没有就别擅自加上
    text = read_text(path)
    new = BLOCK_RE.sub("", text)
    if new == text:
        return False
    backup_config(path)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\r\n" if IS_WIN else "\n") as f:
        f.write(new)
    return True


def import_token(tok, name_hint=None):
    """导入一个已有的 token。已存在就跳过；验证失败返回错误信息。"""
    if any(a["token"] == tok for a in load()["accounts"]):
        return None
    name = name_hint or "账号%d" % (len(load()["accounts"]) + 1)
    try:
        # 这台电脑本来就在用这个 token，没有可绑的代理也照样导入（之后在网页上给它绑）
        r = add_account(name, tok, force=["unbound"])
        return "已导入原来的 token，账号名「%s」" % r["name"]
    except CctError as e:
        if "已经存在" in str(e):
            return None
        return "导入原来的 token 失败：%s" % e


# ---- macOS / Linux
def rc_files():
    if os.environ.get("CCT_ZSHRC"):          # 测试用
        return [os.environ["CCT_ZSHRC"]]
    files = [os.path.expanduser("~/.zshrc")]
    uses_bash = os.environ.get("SHELL", "").endswith("bash")
    for f in ("~/.bashrc", "~/.bash_profile"):   # macOS 上 bash 读 .bash_profile，Linux 上读 .bashrc
        f = os.path.expanduser(f)
        if os.path.exists(f) or uses_bash:
            files.append(f)
    return files


def path_dirs():
    return [os.path.normcase(os.path.abspath(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def shim_dir(cands):
    """在 PATH 里找一个能放 cct 命令的用户目录。"""
    pd = path_dirs()
    for c in cands:
        if os.path.normcase(os.path.abspath(c)) in pd:
            os.makedirs(c, exist_ok=True)
            return c
    return None


def posix_install(py, app, msgs):
    for rc in rc_files():
        text = read_text(rc) if os.path.exists(rc) else ""
        lines, found = text.splitlines(), []
        for n, ln in enumerate(lines):
            m = EXPORT_RE.match(ln)
            if m:
                found.append(m.group(1))
                lines[n] = "# [cct 已导入，改由 cct 管理] " + ln.split("=", 1)[0] + "=…"
        failed = False
        for tok in dict.fromkeys(found):
            m = import_token(tok)
            if m and m.startswith("导入原来的 token 失败"):
                msgs.append(m + "（%s 里那一行先保留不动）" % rc)
                failed = True
            elif m:
                msgs.append(m)
        if found and not failed:
            backup_config(rc)
            with open(rc, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            msgs.append("%s 里写死的 token 已注释掉（改由 cct 管理）" % rc)
        if drop_block(rc):      # 旧版装的 claude 启动函数：新版靠 settings.json 切换，用不上了
            msgs.append("已从 %s 移除旧版的 claude 启动函数（现在改由 settings.json 切换）" % rc)
    try:
        os.remove(CUR_TOKEN_FILE)
    except OSError:
        pass
    # cct 命令
    if not shutil.which("cct"):
        d = shim_dir([os.path.expanduser("~/.local/bin"), os.path.expanduser("~/bin")])
        if d:
            p = os.path.join(d, "cct")
            with open(p, "w") as f:
                f.write("#!/bin/sh\n# cct (Claude setup-token switcher)\nexec %s %s \"$@\"\n" % (shlex.quote(py), shlex.quote(app)))
            os.chmod(p, 0o755)
            msgs.append("已创建 cct 命令：%s" % p)


# ---- Windows
def win_known_folder(csidl):
    """Documents(5) / Desktop(0x10) 的真实位置（可能被 OneDrive 挪走了）。"""
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
        return buf.value
    return os.path.expanduser("~\\Documents" if csidl == 5 else "~\\Desktop")


def win_bat_path(p):
    """.bat 文件是按系统编码（中文 Windows 是 GBK）读的。用户名是中文时，
    尽量换成 %USERPROFILE% 这类变量，保证 .bat 内容是纯英文。"""
    p = os.path.abspath(p)
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(var)
        if base and os.path.normcase(p).startswith(os.path.normcase(base) + "\\"):
            p = "%" + var + "%" + p[len(base):]
            break
    if p.isascii():
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(520)
        if ctypes.windll.kernel32.GetShortPathNameW(os.path.expandvars(p), buf, 520) and buf.value.isascii():
            return buf.value
    except Exception:
        pass
    return p


def write_bat(path, lines):
    body = "\r\n".join(lines) + "\r\n"
    try:
        data = body.encode("ascii")
    except UnicodeEncodeError:
        data = body.encode("mbcs", "replace")
    with open(path, "wb") as f:
        f.write(data)


def ps_profiles():
    docs = win_known_folder(5)
    return [os.path.join(docs, "WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"),   # Windows 自带的 5.1
            os.path.join(docs, "PowerShell", "Microsoft.PowerShell_profile.ps1")]          # PowerShell 7


def win_install(py, app, msgs, interactive):
    # 1. 原来手动设过的 CLAUDE_CODE_OAUTH_TOKEN 用户环境变量 → 导入；旧版 cct 写的 → 删掉
    old = win_get_env(ENV)
    if old and TOKEN_RE.fullmatch(old.strip()) and old != load().get("env_set"):
        m = import_token(old.strip())
        if m:
            msgs.append(m)
    win_drop_env(msgs)
    # 2. 旧版装在 PowerShell 配置里的 claude 启动函数：用不上了，移除
    for prof in ps_profiles():
        if drop_block(prof):
            msgs.append("已从 %s 移除旧版的 claude 启动函数（现在改由 settings.json 切换）" % prof)
    # 3. cct 命令（放进 Claude Code 自己用的 ~/.local/bin 或 npm 全局目录）
    if not shutil.which("cct"):
        d = shim_dir([os.path.join(os.path.expanduser("~"), ".local", "bin"),
                      os.path.join(os.environ.get("APPDATA", ""), "npm")])
        if d:
            write_bat(os.path.join(d, "cct.cmd"), ["@echo off", "REM cct (Claude setup-token switcher)",
                                                   '"%s" "%s" %%*' % (win_bat_path(py), win_bat_path(app))])
            msgs.append("已创建 cct 命令：%s" % os.path.join(d, "cct.cmd"))
    # 4. 桌面快捷启动
    desk = os.path.join(win_known_folder(0x10), "Claude换号台.bat")
    if not os.path.exists(desk):
        write_bat(desk, ["@echo off", "title Claude token switcher",
                         '"%s" "%s" serve' % (win_bat_path(py), win_bat_path(app)), "if errorlevel 1 pause"])
        msgs.append("已在桌面创建「Claude换号台.bat」，以后双击它打开")


def _curl_ip(proxy=None, direct=False, timeout=6):
    """用 curl 查出口 IP。proxy 指定代理；direct=True 强制不走代理。查不到返回 None。"""
    curl = shutil.which("curl")
    if not curl:
        return None
    cmd = [curl, "-s", "-m", str(timeout)]
    if direct:
        cmd += ["--noproxy", "*"]
    elif proxy:
        cmd += ["-x", proxy]
    cmd.append("https://api.ip.sb/ip")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000 if IS_WIN else 0)
        ip = p.stdout.strip()
        return ip if p.returncode == 0 and ip and len(ip) < 60 else None
    except OSError:
        return None


def _http_code(url, proxy=None, direct=False, timeout=10):
    curl = shutil.which("curl")
    if not curl:
        return "?"
    cmd = [curl, "-s", "-o", os.devnull, "-w", "%{http_code}", "-m", str(timeout)]
    if direct:
        cmd += ["--noproxy", "*"]
    elif proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              creationflags=0x08000000 if IS_WIN else 0).stdout.strip() or "?"
    except OSError:
        return "?"


def cmd_doctor(args):
    """网络体检：看代理、TUN、各条线出口 IP 是否一致、Anthropic 通不通。只读，不碰账号。"""
    print("===== cct 网络体检 =====")

    # 1) TUN：mac/BSD 用 netstat；Linux 用 ip route（netstat 的 -f 是 BSD 语法，且很多发行版没装）
    tun = None
    if PLATFORM == "linux":
        try:
            r = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=6)
            for ln in r.stdout.splitlines():
                f = ln.split()
                # Clash/sing-box 的 TUN 常见为 0.0.0.0/1、128.0.0.0/1 两条半默认路由，或 default dev tun*/utun*
                if f and (f[0] in ("0.0.0.0/1", "128.0.0.0/1")
                          or (f[0] == "default" and "dev" in f
                              and re.match(r"(tun|utun|wg|clash|meta|sing)", f[f.index("dev") + 1]))):
                    tun = f[f.index("dev") + 1] if "dev" in f else f[-1]
                    break
        except Exception:
            pass
    elif not IS_WIN:
        try:
            r = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True, timeout=6)
            for ln in r.stdout.splitlines():
                f = ln.split()
                if f and f[0] in ("0/1", "128.0/1"):
                    tun = f[-1]
                    break
        except Exception:
            pass
    print("[TUN 虚拟网卡] %s" % ("已开启（%s）→ 全系统流量走隧道，cct 和 claude 出口一致" % tun if tun else "未开启（或测不到）"))

    # 2) 环境变量代理（Claude Code 只认这个，不认系统代理设置）
    envp = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    print("[环境变量代理] %s" % (", ".join("%s=%s" % (k, proxy_label(v) if "@" in v else v) for k, v in envp.items()) if envp else "无"))
    d = load()
    cur = active_acc(d)
    print("[当前账号] %s" % _label(cur["name"] if cur else None))
    if bound(d):
        print("[一个号一个 IP] 已开启：当前号绑的代理 %s" % (proxy_label(target_proxy(d, cur)) or "⚠ 没绑"))
        dr = settings_drift(d)
        if dr:
            print("   ⚠ settings.json 里现在是 %s，和绑定的不一样：重新切换一次当前号就会改回" % dr["have"])
    cp = claude_proxy()
    print("[claude 会用的代理] %s" % (proxy_label(cp) if cp and "@" in cp else cp or "没配（直连；开了 TUN 就走 TUN。Claude Code 不认系统代理设置）"))
    ep = effective_proxy()
    if ep != cp:
        print("   （https:// 代理：cct 经本机转接 %s 连它，账号密码由转接补上）" % ep)

    # 2.5) claude 用当前号时实际走哪条线（和网页顶部一样）
    st0 = ip_state(route_of(d, cur)[0], force=True)
    r0 = route_info(d, cur, st0)
    print("[claude 实际走] [%s] %s%s" % (r0["tag"], r0["detail"], {"warn": "  ⚠", "bad": "  ✗"}.get(r0["level"], "")))
    if r0["tip"]:
        print("   %s" % r0["tip"])
    pp = persistent_proxy()
    print("[配置文件里写死的代理（新开的终端都会带上）] %s" % ("%s（%s）" % (proxy_label(pp[0]) if "@" in pp[0] else pp[0], pp[1])
                                                     if pp else "没有"))
    f = iface_to(API_HOST)
    print("[直连 Anthropic 时走的网卡] %s" % ("查不到" if not f else "%s%s%s" % (
        f["name"], "（%s）" % f["desc"] if f["desc"] else "", " → TUN / VPN" if f["tunnel"] else "")))
    sysp = system_proxy()
    print("[系统代理设置] %s" % ("%s（Claude Code 不认它）" % _hostport(sysp) if sysp else "没开"))
    po = project_overrides()
    if po:
        print("[项目目录里会盖过 cct 的设置]")
        for f_, items in po:
            print("   · %s：%s" % (f_, "、".join(w for w, _ in items)))

    # 3) settings.json
    sp = settings_proxy()
    print("[settings.json 代理] %s" % (proxy_label(sp) or "没配"))
    try:
        procs = claude_processes()
        print("[正在运行的 claude] %s" % ("、".join("%s(%d)" % (p["kind"], p["pid"]) for p in procs) if procs else "没有"))
    except Exception as e:
        print("[正在运行的 claude] 查不了：%s" % e)

    # 4) 三条线出口 IP，应当一致
    trace = egress_ip(force=True).get("ip") or "查不到"
    via = _curl_ip(proxy=ep) or "查不到"
    direct = _curl_ip(direct=True) or "查不到"
    print("[出口 IP（三条应一致）]")
    print("   claude 那条线（anthropic trace）: %s" % trace)
    print("   走 claude 的代理（没有就直连）:    %s" % via)
    print("   强制直连 / 走 TUN:                %s" % direct)
    ips = {x for x in (trace, via, direct) if x != "查不到"}
    if len(ips) > 1:
        print("   ⚠ 出口 IP 不一致！说明有的走代理、有的直连（可能泄漏）。查一下代理/TUN 设置。")
    elif not ips:
        print("   ⚠ 三条都查不到，网络可能不通。")
    else:
        print("   ✓ 一致：%s" % ips.pop())

    # 5) Anthropic 连通性（400/401/405 都算通，能握手到）
    print("[Anthropic 连通性（数字=通，000=不通）]")
    print("   走 claude 的代理（没有就直连）: HTTP %s" % _http_code("https://api.anthropic.com/v1/messages", proxy=ep))
    print("   强制直连 / 走 TUN:         HTTP %s" % _http_code("https://api.anthropic.com/v1/messages", direct=True))

    # 6) 冲突认证设置（会让 cct 切换不生效）
    cs = auth_conflicts()
    if cs:
        print("[⚠ 冲突认证设置]")
        for c in cs:
            print("   · %s（%s）" % (c["what"], c["where"]))
    print("===== 体检结束 =====")


def print_conflicts():
    cs = auth_conflicts()
    if cs:
        print("⚠ 发现和 cct 冲突的认证设置（Claude Code 会优先用它们，删掉或注释掉之后 cct 的切换才会生效）：")
        for c in cs:
            print("  · %s：%s —— %s" % (c["where"], c["what"], c["why"]))
        print()


def tidy_legacy_backups(msgs):
    """旧版本把配置备份（*.cct-bak-时间）直接放在家目录 / 文档目录里，而且没给 token 打码：
    挪进 ~/.cc_tokens/backups/config/（打码），原文件删掉。"""
    import glob
    targets = ps_profiles() if IS_WIN else rc_files()
    for t in targets:
        for old in sorted(glob.glob(glob.escape(t) + ".cct-bak-*")):
            try:
                dst = backup_config(old)
                os.remove(old)
                msgs.append("旧备份 %s 已挪到 %s（token 已打码）" % (old, dst))
            except OSError:
                pass


def migrate_settings(msgs):
    """第一次装 / 从旧版升级：settings.json 里写死的 token 导入；里面原来配的中转代理（https://用户:密码@…）
    绑给当前在用的号（这条线本来就是给它配的）。只做一次，之后由 cct 管。"""
    path = settings_path()
    try:
        env = _read_settings(path).get("env") or {}
    except CctError as e:
        msgs.append(str(e))
        return
    tok = str(env.get(ENV) or "").strip()
    if TOKEN_RE.fullmatch(tok):
        m = import_token(tok)
        if m:
            msgs.append(m + "（来自 %s）" % path)
    p = next((env[k] for k in PROXY_KEYS if env.get(k)), None)
    d = load()
    if p and not bound(d) and not d.get("proxy_migrated"):
        u = urlparse(p)
        if u.scheme == "https" and u.username:
            with txn() as d2:
                cur = active_acc(d2)
                if cur:
                    cur["proxy"] = p
                else:
                    d2["normal_proxy"] = p
                d2["proxy_migrated"] = True
                who = _label(cur["name"] if cur else None)
            msgs.append("settings.json 里原来的中转代理 %s 已绑给%s；其它号请在网页上「⋯ → 设置代理」各绑各的"
                        % (proxy_label(p), who))


def cmd_install(args, quiet=False):
    msgs = []
    app = install_app_copy(msgs)
    py = os.path.abspath(sys.executable)
    tidy_legacy_backups(msgs)
    if IS_WIN:
        win_install(py, app, msgs, interactive=sys.stdin is not None and sys.stdin.isatty())
    else:
        posix_install(py, app, msgs)
    migrate_settings(msgs)
    try:
        if sync_settings(write_proxy=False):    # 只写 token（就是现在在用的号），代理不动
            msgs.append("当前账号已写进 %s" % settings_path())
    except CctError as e:
        msgs.append(str(e))
    if msgs or not quiet:
        print("—— cct 安装 ——")
        for m in msgs or ["已是最新，无需改动"]:
            print("  · " + m)
        print()
    print_conflicts()


def cmd_uninstall(args):
    msgs = []
    # settings.json：去掉 cct 写的 token，代理恢复成普通登录绑的那个（没绑就不动）
    try:
        path = settings_path()
        js = _read_settings(path)
        env = js.get("env") if isinstance(js.get("env"), dict) else {}
        if ENV in env or (bound(load()) and load().get("normal_proxy")):
            new = dict(env)
            new.pop(ENV, None)
            np_ = load().get("normal_proxy")
            if np_:
                for k in PROXY_KEYS:
                    new[k] = np_
            if new != env:
                js["env"] = new
                _write_settings(path, js)
                msgs.append("已从 %s 去掉 cct 写的 token%s" % (path, "，代理恢复成普通登录绑的 %s" % proxy_label(np_) if np_ else ""))
    except CctError as e:
        msgs.append(str(e))
    if IS_WIN:
        win_drop_env(msgs)
        files = ps_profiles()
        shims = [os.path.join(os.path.expanduser("~"), ".local", "bin", "cct.cmd"),
                 os.path.join(os.environ.get("APPDATA", ""), "npm", "cct.cmd"),
                 os.path.join(win_known_folder(0x10), "Claude换号台.bat")]
    else:
        files = rc_files()
        shims = [os.path.expanduser("~/.local/bin/cct"), os.path.expanduser("~/bin/cct")]
    for f in files:
        if drop_block(f):
            msgs.append("已从 %s 移除旧版的 claude 启动函数" % f)
    for p in shims:
        try:
            if os.path.isfile(p) and not os.path.islink(p) and (
                    p.endswith("Claude换号台.bat")
                    or "cct (Claude setup-token switcher)" in read_text(p)):
                os.remove(p)
                msgs.append("已删除 %s" % p)
        except OSError:
            pass
    if not IS_WIN:
        try:
            os.remove(CUR_TOKEN_FILE)
        except OSError:
            pass
    if args.purge:
        shutil.rmtree(STORE, ignore_errors=True)
        msgs.append("已删除全部数据 %s" % STORE)
    else:
        msgs.append("账号数据保留在 %s（要一起删加 --purge）" % STORE)
    print("—— cct 卸载 ——")
    for m in msgs:
        print("  · " + m)
    print("新启动的 claude 会恢复用原来的普通登录（正在运行的 claude 请退出重开）。")


# ------------------------------------------------------------------------ web UI
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 换号台</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+CjxyZWN0IHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgcng9IjciIGZpbGw9IiNjODQzMWIiLz4KPHJlY3QgeD0iMTUuMSIgeT0iNC41IiB3aWR0aD0iMS44IiBoZWlnaHQ9IjQiIHJ4PSIwLjkiIGZpbGw9IiNmZmYiLz4KPGNpcmNsZSBjeD0iMTYiIGN5PSI0IiByPSIxLjgiIGZpbGw9IiNmZmYiLz4KPHJlY3QgeD0iNi41IiB5PSI5IiB3aWR0aD0iMTkiIGhlaWdodD0iMTUiIHJ4PSI1IiBmaWxsPSIjZmZmIi8+CjxjaXJjbGUgY3g9IjEyLjMiIGN5PSIxNi4yIiByPSIyLjIiIGZpbGw9IiNjODQzMWIiLz4KPGNpcmNsZSBjeD0iMTkuNyIgY3k9IjE2LjIiIHI9IjIuMiIgZmlsbD0iI2M4NDMxYiIvPgo8cmVjdCB4PSIxMS44IiB5PSIyMCIgd2lkdGg9IjguNCIgaGVpZ2h0PSIxLjkiIHJ4PSIwLjk1IiBmaWxsPSIjYzg0MzFiIi8+Cjwvc3ZnPg==">
<style>
 :root{
  --bg:#ebe8e1;--panel:#f8f6f1;--head:#f1eee7;--sel:#fbf9f5;
  --line:#d8d2c6;--line2:#bfb8aa;--ink:#1d1c1a;--soft:#57534b;--mute:#8e8779;
  --red:#c23f18;--ok:#2f6f45;--warn:#c8860a;--track:#e3ddd1;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
  --mono:"SF Mono",ui-monospace,Menlo,monospace;
 }
 @media (prefers-color-scheme:dark){:root{
  --bg:#141312;--panel:#1c1b19;--head:#22211e;--sel:#211f1c;
  --line:#312f2a;--line2:#46433c;--ink:#ebe6db;--soft:#bdb6a8;--mute:#878073;
  --red:#ef6a3d;--ok:#6fbf8a;--warn:#e0a92e;--track:#34322d;
 }}
 *{box-sizing:border-box}
 html,body{background:var(--bg)}
 body{margin:0;color:var(--ink);font:13px/1.45 var(--sans);-webkit-font-smoothing:antialiased}
 .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
 .wrap{max-width:1100px;margin:0 auto;padding:16px 20px 24px}

 /* buttons */
 .btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 13px;border:1px solid var(--line2);border-radius:6px;
  background:var(--panel);color:var(--ink);font:500 13px var(--sans);cursor:pointer;white-space:nowrap;transition:background .1s,border-color .1s}
 .btn:hover:not(:disabled){background:var(--head);border-color:var(--soft)}
 .btn:active:not(:disabled){transform:translateY(1px)}
 .btn:disabled{opacity:.4;cursor:not-allowed}
 .btn.pri{background:var(--ink);border-color:var(--ink);color:var(--panel)}
 .btn.pri:hover:not(:disabled){background:var(--soft);border-color:var(--soft)}
 .btn.txt{border-color:transparent;background:transparent;color:var(--mute);padding:0 8px}
 .btn.txt:hover:not(:disabled){color:var(--ink);background:var(--head);border-color:transparent}
 .btn.txt.del:hover:not(:disabled){color:var(--red)}
 .btn .k{font-size:14px;line-height:1}
 .btn.busy:disabled{opacity:.75;cursor:progress}
 .spin{width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .7s linear infinite;flex:none}
 @keyframes spin{to{transform:rotate(360deg)}}

 /* toolbar */
 .tool{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px 10px 16px}
 .tool h1{margin:0;font:650 17px var(--sans);display:flex;align-items:baseline;gap:10px}
 .tool h1 small{font:12.5px var(--sans);color:var(--mute);font-weight:400}
 .grp{display:flex;align-items:center;gap:8px;flex-wrap:wrap}

 /* add form */
 .add{display:none;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
 .add.on{display:flex}
 .add .lb{font-weight:600;margin-right:4px}
 .inp{height:32px;border:1px solid var(--line2);border-radius:6px;background:var(--bg);color:var(--ink);padding:0 10px;font:13px var(--sans);outline:none}
 .inp:focus{border-color:var(--ink)}
 #aName{width:180px}
 #aTok{flex:1;min-width:240px;font:12.5px var(--mono)}
 #aProxy{flex:1 1 100%;font:12.5px var(--mono)}
 .add .hint{flex:1 1 100%;font-size:11.5px;color:var(--mute)}

 /* list */
 .lh{display:flex;justify-content:space-between;align-items:baseline;margin:20px 2px 8px}
 .lh b{font-size:14px}
 .lh span{font-size:12px;color:var(--mute)}
 .list{display:flex;flex-direction:column;gap:8px}
 .acc{display:grid;grid-template-columns:320px minmax(0,1fr) 232px;gap:24px;align-items:center;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
 .acc.on{border-color:var(--ok);box-shadow:inset 4px 0 0 var(--ok),0 0 0 1px var(--ok);background:var(--sel)}
 .acc.normal{border-style:dashed}
 .acc.normal.on{border-style:solid}

 .who{min-width:0}
 /* 左栏只有两级字号：名字 15px，其余 12px 左右（编号用等宽 11.5px，视觉上和 12px 一样大） */
 .who .nm{font-size:15px;font-weight:650;line-height:1.35;word-break:break-all;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .who .tags{display:flex;gap:6px 10px;flex-wrap:wrap;align-items:center;margin-top:6px;min-width:0}
 .who .tags .id{font:11.5px var(--mono);color:var(--mute);white-space:nowrap}
 .who .tags .em{font-size:12px;color:var(--soft);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tag{font-size:11px;font-weight:600;padding:1px 6px;border-radius:4px;border:1px solid var(--line2);color:var(--soft);white-space:nowrap}
 .cur-mark{display:inline-flex;align-items:center;gap:5px;height:30px;padding:0 12px;font-size:12.5px;font-weight:600;color:var(--ok);white-space:nowrap}
 .cur-mark::before{content:"●";font-size:9px}
 .tag.ok{border-color:color-mix(in srgb,var(--ok) 55%,transparent);color:var(--ok)}
 .tag.bad{border-color:var(--red);background:var(--red);color:#fff}
 .tag.warn{border-color:color-mix(in srgb,var(--warn) 60%,transparent);color:var(--warn)}
 .tag.dim{border-color:var(--line);color:var(--mute);font-weight:500}
 /* 代理行：● 出口 IP 国家 · 中转账号（入口地址在悬停提示里） */
 .who .px{margin-top:6px;font:11.5px var(--mono);color:var(--soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .who .px .dot{color:var(--ok);font-size:8px;margin-right:6px;vertical-align:1px}
 .who .px .acct{color:var(--mute)}
 .who .px.wait{color:var(--mute)}
 .who .px.wait .dot{color:var(--mute)}
 .who .px.bad,.who .px.bad .dot{color:var(--red)}
 .org{cursor:pointer;border-bottom:1px dashed var(--line2)}
 .org:hover{color:var(--ink);border-bottom-color:var(--ink)}
 .org.copied{color:var(--ok);border-bottom-color:transparent}

 .usage{display:flex;flex-direction:column;gap:7px}
 .m{display:grid;grid-template-columns:70px minmax(60px,1fr) 46px 226px;gap:12px;align-items:center}
 .m .lbl{font-size:12px;color:var(--soft);white-space:nowrap}
 .m .pct{font:600 13.5px var(--mono);font-variant-numeric:tabular-nums;text-align:right}
 .m .pct.warn{color:var(--warn)}
 .m .pct.x{color:var(--red)}
 .track{height:6px;background:var(--track);border-radius:3px;overflow:hidden}
 .track i{display:block;height:100%;background:var(--ok);border-radius:3px;transition:width .3s}
 .track i.warn{background:var(--warn)}
 .track i.x{background:var(--red)}
 .m .when{font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums;display:grid;grid-template-columns:118px auto;align-items:baseline}
 .m .rm{color:var(--ink);font-weight:600}
 .m .rm.done{color:var(--ok)}
 .m .at{color:var(--mute);font-size:11.5px}
 .err{color:var(--red);font-size:12.5px}
 .nil{color:var(--mute);font-size:12.5px}
 /* 用量查询时间：浮在「查用量」按钮正下方，不占布局，按钮还和「切换」对齐 */
 .chkw{position:relative;display:inline-flex}
 .chkw .ago{position:absolute;top:calc(100% + 4px);left:50%;transform:translateX(-50%);font-size:10.5px;color:var(--mute);opacity:.5;white-space:nowrap;pointer-events:none}

 .ops{display:grid;grid-template-columns:72px 78px 34px;gap:5px;align-items:center;justify-content:end;justify-items:end}
 .ops .btn.pri{margin-right:0}
 .more{position:relative}
 .morebtn{font-size:17px;line-height:1;padding:0 9px;letter-spacing:1px}
 .moremenu{display:none;position:absolute;right:0;top:calc(100% + 4px);z-index:5;background:var(--panel);border:1px solid var(--line2);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.2);padding:4px;min-width:92px}
 .moremenu.on{display:block}
 .moremenu button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ink);font:13px var(--sans);padding:7px 12px;border-radius:5px;cursor:pointer;white-space:nowrap}
 .moremenu button:hover{background:var(--head)}
 .moremenu button.del{color:var(--red)}

 .empty{background:var(--panel);border:1px dashed var(--line2);border-radius:10px;padding:32px;text-align:center;color:var(--mute)}

 .status{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:12px;font-size:11.5px;color:var(--mute);padding:0 2px}
 .status details{max-width:640px}
 .status summary{cursor:pointer}
 .status details p{margin:4px 0 0;line-height:1.6;color:var(--soft)}
 .status a{color:var(--soft)}
 code{font:11.5px var(--mono);color:var(--soft)}

 .ipbar{display:none;margin-top:10px;padding:7px 12px;border-radius:8px;font-size:12px;align-items:center;gap:6px}
 .ipbar.show{display:flex}
 .ipbar.bad{color:#fff;background:var(--red);border:1px solid var(--red);font-weight:600}
 .ipinline .ipnum{font-family:var(--mono);color:var(--soft)}
 .ipinline .rtag{display:inline-block;margin-left:10px;font:600 11px var(--sans);padding:0 6px;border-radius:4px;line-height:17px;
  border:1px solid color-mix(in srgb,var(--ok) 55%,transparent);color:var(--ok);vertical-align:1px}
 .ipinline .rtag.warn{border-color:color-mix(in srgb,var(--warn) 60%,transparent);color:var(--warn)}
 .ipinline .rtag.bad{border-color:var(--red);background:var(--red);color:#fff}
 .ipinline .rdet{margin-left:6px;color:var(--mute)}
 .warnbox{display:none;margin-top:10px;border:1px solid var(--red);border-left-width:4px;border-radius:10px;background:var(--panel);
  padding:10px 14px;font-size:12.5px;color:var(--soft);line-height:1.6}
 .warnbox.on{display:block}
 .warnbox b{color:var(--red)}
 .warnbox code{font:11.5px var(--mono);color:var(--ink)}
 .warnbox a{color:var(--red);font-weight:600}
 .mask{position:fixed;inset:0;background:rgba(0,0,0,.38);display:none;align-items:center;justify-content:center;z-index:20;padding:16px}
 .mask.on{display:flex}
 .dlg{background:var(--panel);color:var(--ink);border:1px solid var(--line2);border-radius:12px;width:min(520px,100%);
  box-shadow:0 20px 60px rgba(0,0,0,.35);padding:20px 22px 16px}
 .dlg h3{margin:0 0 4px;font-size:16px;font-weight:650}
 .dlg .from{font-size:12.5px;color:var(--mute);margin-bottom:14px}
 .dlg .from b{color:var(--ink);font-weight:600}
 .dlg ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:10px}
 .dlg li{display:grid;grid-template-columns:22px 1fr;gap:6px;font-size:13px;line-height:1.55;color:var(--soft)}
 .dlg li .i{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;margin-top:1px;
  background:var(--head);border:1px solid var(--line2);color:var(--ink)}
 .dlg li b{color:var(--ink);font-weight:600}
 .dlg code{font:12px var(--mono);color:var(--ink);background:var(--head);padding:0 4px;border-radius:3px}
 .dlg .foot{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
 .dlg .foot .btn.kill{color:var(--red);border-color:var(--red)}
 .dlg .foot .btn.kill:hover:not(:disabled){background:var(--red);color:#fff}
 #toast{position:fixed;left:50%;top:18px;transform:translate(-50%,-8px);max-width:min(560px,calc(100vw - 32px));z-index:9;
  display:flex;align-items:flex-start;gap:9px;background:var(--panel);color:var(--ink);border:1px solid var(--line2);border-left:4px solid var(--soft);
  font-size:13.5px;line-height:1.5;padding:10px 16px 10px 12px;border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.22);
  opacity:0;transition:opacity .18s,transform .18s;pointer-events:none}
 #toast .ic{font-weight:700;flex:none;width:18px;height:18px;border-radius:50%;display:grid;place-items:center;font-size:11.5px;margin-top:1px;
  background:var(--soft);color:var(--panel)}
 #toast.on{opacity:1;transform:translate(-50%,0)}
 #toast.ok{border-left-color:var(--ok)} #toast.ok .ic{background:var(--ok)}
 #toast.err{background:var(--red);border-color:var(--red);color:#fff} #toast.err .ic{background:#fff;color:var(--red)}
 .btn.flash-ok{color:var(--ok)!important;border-color:var(--ok)!important;background:var(--panel)!important}
 .btn.flash-err{color:#fff!important;border-color:var(--red)!important;background:var(--red)!important}

 @media (max-width:1060px){
  .acc{grid-template-columns:1fr;gap:14px}
  .ops{justify-content:start;justify-items:start}
  .ops:has(.ago){padding-bottom:16px}   /* 按钮排在卡片最下面时，给下面那行「1 天前」留位置 */
 }
 @media (max-width:520px){
  .wrap{padding:10px}
  .m{grid-template-columns:66px 1fr 42px}
  .m .when{grid-column:2/4}
  #aName,#aTok,#aProxy{width:100%;flex:1 1 100%}
 }
</style></head>
<body><div class="wrap">
 <div class="tool">
   <h1>Claude 换号台<small>setup token · 本机<span class="ipinline" id="ipinline"></span></small></h1>
   <div class="grp">
     <button class="btn" onclick="toggleAdd()"><span class="k">＋</span>添加账号</button>
   </div>
 </div>

 <div class="ipbar" id="ipbar"></div>
 <div class="warnbox" id="warnbox"></div>
<div class="add" id="addBox">
   <span class="lb">添加账号</span>
   <input class="inp" id="aName" placeholder="名字，比如邮箱">
   <input class="inp" id="aTok" type="password" placeholder="sk-ant-oat01-…（claude setup-token 生成）" autocomplete="off">
   <button class="btn pri" id="addBtn" onclick="addAcc()">添加</button>
   <button class="btn txt" onclick="toggleAdd(false)">取消</button>
   <input class="inp" id="aProxy" type="password" placeholder="这个号的代理：https://用户:密码@IP:端口（中转给的；自己用 Clash 可以不填）" autocomplete="off">
   <span class="hint">验证 token 就经这个代理发出；以后切到这个号，claude 也走它。一个号一个代理，别和别的号共用。</span>
 </div>

 <div class="lh"><b>账号</b><span id="cnt"></span></div>
 <div class="list" id="list"><div class="empty">读取中…</div></div>

 <div class="status">
   <details>
     <summary>使用说明</summary>
     <p><span id="tip">切换会把这个号的 token 和它绑的代理一起写进 <code>settings.json</code>：之后新启动的 claude（终端里的、IDE 里新开的对话）都用这一对，不用重启 IDE。正在运行的 claude 会中途读到一部分新设置，号和 IP 就对不上了，所以<b>切换前要先关掉所有 claude</b>（换号台会检查）；之后用 <code>claude -c</code> 接着上次的对话。</span>
     号用完了就在这里手动切换：点别的号的「切换」。要看某个号还剩多少，点它的「查用量」。启动 claude 时不会联网，也不会批量查所有号。
     用量靠发一条 1 token 的消息、读响应头得到，页面只显示缓存，点「查用量」才联网。「7 天 Fable」是 Fable 单独的周额度，只有开通 Fable 的号才有，用完只影响 Fable。</p>
   </details>
   <span>数据 <code id="store"></code> · <span id="upd"></span></span>
 </div>
</div>
<div id="toast"></div>
<div class="mask" id="mask"><div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgT">
  <h3 id="dlgT"></h3><div class="from" id="dlgFrom"></div>
  <ul id="dlgBody"></ul>
  <div class="foot"><button class="btn" id="dlgNo">取消</button><button class="btn kill" id="dlgKill">强制结束它们并继续</button>
    <button class="btn pri" id="dlgYes">我关好了，再试一次</button></div>
</div></div>

<script>
let ST=null;
function esc(s){return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
// kind：'ok' 成功（绿）、true/'err' 失败（红）、不填为普通提示
function toast(t,ms,kind){const e=document.getElementById('toast');const k=kind===true?'err':(kind||'');
  e.className=k; e.innerHTML=`<span class="ic">${k==='ok'?'✓':k==='err'?'✕':'i'}</span><span>${esc(t)}</span>`;
  void e.offsetWidth; e.classList.add('on');
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('on'),ms||(k==='err'?6000:3000))}
// 请求结束后让按钮本身闪一下结果
function flash(sel,ok){const b=typeof sel==='string'?document.querySelector(sel):sel; if(!b) return;
  const html=b.innerHTML; HOLD=Date.now()+1600;
  b.classList.add(ok?'flash-ok':'flash-err'); b.innerHTML=ok?'✓ 已更新':'✕ 失败';
  setTimeout(()=>{if(b.isConnected){b.innerHTML=html;b.classList.remove('flash-ok','flash-err')}HOLD=0},1500)}
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});return r.json()}
// 数据没变就不重绘（只在整分钟时重绘一次，让「剩余」倒计时往下走），避免点击时元素被替换掉
let LAST='', HOLD=0;
async function refresh(force){try{
  ST=await api('/api/state');
  const key=JSON.stringify(ST)+'|'+Math.floor(Date.now()/60000);
  if(!force&&(key===LAST||Date.now()<HOLD||BUSY)) return;
  LAST=key; render();
}catch(e){}}

// 网页里的时间一律按台北时间（UTC+8，与北京时间相同）显示，不跟电脑系统时区走
const TPE=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',
  hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function tpe(d){const o={};TPE.formatToParts(d).forEach(x=>o[x.type]=x.value);return o}
function resetAt(b){const t=tpe(new Date(b.ts*1000));return `${t.month}-${t.day} ${t.hour}:${t.minute} 重置`}
// 距离重置还剩多久：按当前时间实时算，过了重置时刻就是「已重置」
function remain(b){
  if(!b.ts) return '已重置';
  let m=Math.floor((b.ts*1000-Date.now())/60000);
  if(m<0) return '已重置';
  const d=Math.floor(m/1440),h=Math.floor(m%1440/60); m%=60;
  return '剩余 '+(d?d+'天':'')+(d||h?h+'小时':'')+m+'分';
}
// 进度条颜色（参考 claude.ai 用量页）：用完 / ≥90% 红，≥75% 琥珀，其余绿
function usageLevel(b){
  const rm=remain(b);
  if(rm==='已重置') return '';
  if(b.blocked||b.pct>=90) return 'x';
  if(b.pct>=75) return 'warn';
  return '';
}
function meter(b){
  const rm=remain(b), lv=usageLevel(b);
  return `<div class="m"><span class="lbl">${esc(b.label)}</span>`
    +`<div class="track"><i class="${lv}" style="width:${Math.min(b.pct,100)}%"></i></div>`
    +`<span class="pct${lv?' '+lv:''}">${b.pct}%</span>`
    +`<span class="when"><span class="at">${b.ts&&rm!=='已重置'?resetAt(b):''}</span><span class="rm${rm==='已重置'?' done':''}">${rm}</span></span></div>`;
}
function stateTag(x){
  // 「已用完」不再单独显示：进度条已经变红了，标签多余。可用 / token 无效 / 出错 / 网络错误 仍显示。
  if(x.state==='limited') return '';
  const cls={ok:'ok',invalid:'bad',error:'bad',neterr:'bad'}[x.state]||'';
  return `<span class="tag ${cls}" title="${esc(x.error)}">${esc(x.stateTxt)}</span>`;
}
function render(){
  const a=ST.accounts||[];
  const cf=ST.conflicts||[], wb=document.getElementById('warnbox'), dr=ST.drift;
  wb.classList.toggle('on',cf.length>0||!!dr);
  wb.innerHTML=(dr?`<div><b>⚠ settings.json 里的代理被改过</b>：现在是 <code>${esc(dr.have)}</code>，当前号绑的是 <code>${esc(dr.want)}</code>，号和 IP 对不上。`
      +`<a href="#" onclick="${ST.active===null?'useNormal(null)':'useAcc('+(ST.accounts||[]).findIndex(x=>x.active)+',null)'};return false">按绑定改回</a>`
      +`（如果是中转换了地址，先在「⋯ → 设置代理」里把这个号的代理改成新的）</div>`:'')
    +(cf.length?'<b>⚠ 发现和 cct 冲突的设置</b>：Claude Code 会优先用它们，删掉或注释掉之后，cct 的切换才会生效。'
    +cf.map(c=>`<div>· <code>${esc(c.what)}</code>（${esc(c.where)}）—— ${esc(c.why)}</div>`).join(''):'');
  document.getElementById('store').textContent=ST.store;
  const t=tpe(new Date());
  document.getElementById('upd').textContent=`页面刷新于 ${t.hour}:${t.minute}:${t.second}（台北时间）`;
  document.getElementById('cnt').textContent = (a.length ? `共 ${a.length} 个 setup token · ${a.filter(x=>x.usable).length} 个可用` : '')
    + (ST.normal.active ? (a.length?' · ':'')+'当前：普通登录' : '');
  const box=document.getElementById('list');
  // 「普通登录」卡片：不用 setup token，走这台电脑原来的 claude /login 登录
  const nm0=ST.normal, em=nm0.email;
  const normalCard=`<div class="acc normal${nm0.active?' on':''}">
      <div class="who"><div class="nm">普通登录</div>
        <div class="tags"><span class="tag">claude /login</span>${pxTag(nm0)}<span class="em" title="${esc(em)}">${em?esc(em):'本机还没用 /login 登录过'}</span></div>
        ${pxLine(nm0)}</div>
      <div class="usage"><div class="nil">不使用 setup token，Claude Code 用这台电脑自己的登录。${a.length?'下面保存的账号都还在，随时可以切回。':''}用量在 Claude Code 里用 <code>/usage</code> 查看。${(!nm0.active&&em)?'<br><b style="color:var(--warn)">当前被 setup token 盖过</b>：这个 /login 账号还在，但现在优先用着上面选中的 setup token（想用它就点这行「切换」）。':''}</div></div>
      <div class="ops"><span></span>${nm0.active?'<span class="cur-mark">使用中</span>':`<button class="btn pri" onclick="useNormal(this)">切换</button>`}
        <div class="more"><button class="btn txt morebtn" title="更多" onclick="toggleMore('n',event)">⋯</button>
          <div class="moremenu" data-m="n"><button onclick="setProxy(null)">设置代理</button></div></div></div>
    </div>`;
  if(!a.length){box.innerHTML=normalCard+'<div class="empty">还没有 setup token 账号。运行 <code>claude setup-token</code> 生成 token，再点右上角「＋ 添加账号」。</div>';toggleAdd(true);return}
  box.innerHTML=normalCard+a.map((x,i)=>{
    const bs=x.buckets||[];
    const usage = bs.length ? bs.map(meter).join('')
      : (x.error?`<div class="err">${esc(x.error)}</div>`:'<div class="nil">尚未查询用量，点「查用量」</div>');
    // token 尾号和组织 ID 跟在状态标签后面同一行
    const id='<span class="id">'+esc(x.masked)+(x.org?` · <span class="org" title="点击复制完整组织 ID：${esc(x.org)}" onclick="copyOrg(${i},this)">${esc(x.org.slice(0,8))}</span>`:'')+'</span>';
    return `<div class="acc${x.active?' on':''}">
      <div class="who"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div>
        <div class="tags">${stateTag(x)}${pxTag(x)}${id}</div>
        ${pxLine(x)}</div>
      <div class="usage">${usage}</div>
      <div class="ops"><span class="chkw"><button class="btn chk" data-k="chk-${i}" title="用量${esc(x.checkedAgo)}" onclick="checkOne(${i},this)">查用量</button>${agoTxt(x)}</span>
        ${x.active?'<span class="cur-mark">使用中</span>':`<button class="btn pri" onclick="useAcc(${i},this)">切换</button>`}
        <div class="more"><button class="btn txt morebtn" title="更多" onclick="toggleMore(${i},event)">⋯</button>
          <div class="moremenu" data-m="${i}">
            <button onclick="renAcc(${i})">改名</button>
            <button onclick="setProxy(${i})">设置代理</button>
            <button class="del" onclick="delAcc(${i})">删除</button>
          </div></div></div>
    </div>`;
  }).join('');
}
// 「查用量」按钮下面的小字：更新于 1 天前 → 1 天前；刚刚更新 → 刚刚；没查过不显示
function agoTxt(x){
  const s=x.checkedAgo||'';
  if(s.startsWith('尚未')) return '';
  return `<span class="ago">${esc(s==='刚刚更新'?'刚刚':s.replace(/^更新于\s*/,''))}</span>`;
}
// 「未绑代理」不放在状态标签那行，和绑了的号一样放在第三行（代理行），见 pxLine
function pxTag(x){ return ''; }
// 代理行：● 出口 IP 国家 · 中转账号。出口是大陆 IP 时整行变红；还没查过显示「出口未查」
function pxLine(x){
  // 没绑代理：第三行灰色「○ 未绑代理」，只是说明；走得对不对看顶部的线路标签（有问题那里会变红）。
  // 一个号都没绑过代理（自己用 Clash 的老用法）时不提示。
  if(!x.proxy) return ST.bound?'<div class="px wait" title="这个号没绑代理：用它时 claude 走终端代理 / TUN / 直连，实际走哪条看顶部；切过去、查用量要确认"><span class="dot">○</span>未绑代理</div>':'';
  const e=x.egress||{}, bad=e.country==='CN', acct=x.pxUser?` <span class="acct">· ${esc(x.pxUser)}</span>`:'';
  const tip=`代理：${x.proxy}（经 ${x.via}，密码不显示）`+(e.ip?'':'。点「查用量」或「切换」时会经这个代理查一次出口');
  return e.ip?`<div class="px${bad?' bad':''}" title="${esc(tip)}"><span class="dot">●</span>${esc(e.ip)}${e.country?' '+esc(e.country):''}${acct}</div>`
    :`<div class="px wait" title="${esc(tip)}"><span class="dot">○</span>出口未查${acct}</div>`;
}
function toggleAdd(on){const b=document.getElementById('addBox');const v=on===undefined?!b.classList.contains('on'):on;
  b.classList.toggle('on',v);if(v)document.getElementById('aName').focus()}
function nm(i){return ST.accounts[i].name}
function copyOrg(i,el){
  const org=ST.accounts[i].org;
  // 先用同步的 execCommand（在点击事件里最可靠），不行再用 clipboard API
  const t=document.createElement('textarea');t.value=org;t.setAttribute('readonly','');
  t.style.cssText='position:fixed;top:0;left:0;opacity:0';document.body.appendChild(t);t.select();
  let ok=false;try{ok=document.execCommand('copy')}catch(e){}
  t.remove();
  const done=()=>{toast('已复制组织 ID：'+org,3200,'ok');
    if(el){HOLD=Date.now()+1600;const old=el.textContent;el.textContent='已复制 ✓';el.classList.add('copied');
      setTimeout(()=>{el.textContent=old;el.classList.remove('copied');HOLD=0},1500)}};
  if(ok) return done();
  if(navigator.clipboard) navigator.clipboard.writeText(org).then(done,()=>toast('复制失败，完整 ID：'+org,6000,true));
  else toast('复制失败，完整 ID：'+org,6000,true);
}
// 按钮 loading：点了哪个按钮，哪个按钮转圈并禁用；请求期间暂停自动重绘，免得按钮被替换掉
let BUSY=0;
function setBusy(btn,txt){
  if(!btn||btn.dataset.html) return;
  btn.dataset.html=btn.innerHTML; btn.disabled=true; btn.classList.add('busy');
  btn.innerHTML='<span class="spin"></span>'+txt;
}
function clearBusy(btn){
  if(!btn||!btn.dataset.html) return;
  btn.innerHTML=btn.dataset.html; delete btn.dataset.html; btn.disabled=false; btn.classList.remove('busy');
}
async function run(path,body,okMsg,busyTxt,btns,flashSel){
  btns=(btns||[]).filter(Boolean); BUSY++;
  btns.forEach(b=>setBusy(b,busyTxt||'处理中…'));
  const t0=Date.now(); let d;
  try{d=await api(path,body)}catch(e){d={ok:false,error:'连不上换号台服务，它可能已经停了'}}
  if(Date.now()-t0<350) await new Promise(r=>setTimeout(r,350-(Date.now()-t0)));  // 太快也让人看到一下
  BUSY--; btns.forEach(clearBusy);
  if(d.ok){toast(typeof okMsg==='function'?okMsg(d):okMsg,3500,'ok')}else if(!d.needForce&&!d.running){toast('失败：'+(d.error||'未知原因'),7000,true)}
  await refresh(true);
  if(flashSel) flash(flashSel,d.ok);
  return d;
}
// 带确认的操作：服务端说「有风险」(needForce) 就弹确认，确认后带上这条风险代码重发；
// 说「还有 claude 在运行」(running) 就弹窗列出来，用户关掉后重试，或选强制结束
async function act(path,body,okMsg,busyTxt,btns,flashSel){
  body=Object.assign({},body,{force:[]});
  for(;;){
    const d=await run(path,body,okMsg,busyTxt,btns,flashSel);
    if(d.ok||!(d.needForce||d.running)) return d;
    if(d.needForce){
      if(!confirm('⚠ '+(d.error||'')+'\n\n仍要继续吗？')) return d;
      body.force.push(d.needForce); continue;
    }
    const c=await runningDialog(d.running);
    if(!c) return d;
    if(c==='kill') body.kill=true;
  }
}
function runningDialog(procs){
  const n={};procs.forEach(p=>{n[p.kind]=(n[p.kind]||0)+1});
  document.getElementById('dlgT').textContent='先关掉正在运行的 claude';
  document.getElementById('dlgFrom').innerHTML='切换会改 <code>settings.json</code>，正在运行的 claude 会中途读到一部分新设置，旧号就可能从新号的 IP 发出请求。';
  const items=Object.entries(n).map(([k,c])=>['!',`<b>${esc(k)}</b> × ${c}`+(k.includes('对话')?'：在 IDE 里把 Claude 对话标签关掉':k.includes('桌面')?'：退出 Claude 桌面版':'：在那个终端里退出 claude（Ctrl+C 两次或 /exit）')]);
  items.push(['i','进程号：'+procs.map(p=>p.pid).join('、')+'。关好之后点「再试一次」；切换完用 <code>claude -c</code> 接着刚才的对话。']);
  items.push(['✕','「强制结束」会直接结束这些 claude，<b>正在跑的任务会中断</b>。']);
  document.getElementById('dlgBody').innerHTML=items.map(([i,t])=>`<li><span class="i">${i}</span><span>${t}</span></li>`).join('');
  const mask=document.getElementById('mask'); mask.classList.add('on'); document.getElementById('dlgYes').focus();
  return new Promise(res=>{
    const done=v=>{mask.classList.remove('on');document.removeEventListener('keydown',key);res(v)};
    const key=e=>{if(e.key==='Escape')done(false)};
    document.addEventListener('keydown',key);
    document.getElementById('dlgYes').onclick=()=>done('retry');
    document.getElementById('dlgKill').onclick=()=>{if(confirm('确定强制结束这 '+procs.length+' 个 claude？正在跑的任务会中断。'))done('kill')};
    document.getElementById('dlgNo').onclick=()=>done(false);
    mask.onclick=e=>{if(e.target===mask)done(false)};
  });
}
function switched(d){
  setTimeout(loadIP,300);    // 顶部出口跟着换
  const e=d.egress||{};
  let m='已切到'+(d.to===null?'普通登录':'「'+d.to+'」')+(e.ip?'（出口 '+e.ip+(e.country?' '+e.country:'')+(d.route?'，走'+d.route:'')+'）':'')
    +'。新启动的 claude 和 IDE 里新开的对话都用它，不用重启 IDE';
  if(d.late&&d.late.length) setTimeout(()=>toast('切换那一刻有 '+d.late.length+' 个 claude 启动了，它可能读到一半旧设置，请退出重开',9000,true),3800);
  return m;
}
async function useAcc(i,b){act('/api/use',{name:nm(i)},switched,'切换中…',[b])}
async function useNormal(b){act('/api/normal',{},switched,'切换中…',[b])}
async function checkOne(i,b){act('/api/check',{name:nm(i)},'「'+nm(i)+'」用量已刷新','查询中…',[b],`[data-k="chk-${i}"]`)}
function setProxy(i){closeAllMore();
  const x=i===null?ST.normal:ST.accounts[i], who=i===null?'普通登录':'「'+x.name+'」';
  const v=prompt('给'+who+'绑定代理（中转给的 https://用户:密码@IP:端口）。\n'
    +(x.proxy?'现在是：'+x.proxy+'\n':'')+'留空 = 解绑。'+(x.active?'\n\n这是当前在用的，改完会立即写进 settings.json（要先关掉 claude）。':''),'');
  if(v===null) return;
  if(!v.trim()&&!x.proxy) return;
  if(!v.trim()&&!confirm('解绑'+who+'的代理？')) return;
  act('/api/proxy',i===null?{normal:true,proxy:v.trim()}:{name:x.name,proxy:v.trim()},
    d=>(setTimeout(loadIP,300),v.trim()?who+' 的代理已设好'+(d.egress&&d.egress.ip?'，出口 '+d.egress.ip:''):who+' 的代理已解绑'),'检查代理…')}
function toggleMore(i,ev){ev.stopPropagation();const m=document.querySelector('.moremenu[data-m="'+i+'"]');const open=m&&m.classList.contains('on');closeAllMore();if(m&&!open)m.classList.add('on')}
function closeAllMore(){document.querySelectorAll('.moremenu.on').forEach(m=>m.classList.remove('on'))}
document.addEventListener('click',e=>{if(!e.target.closest('.more'))closeAllMore()});
function renAcc(i){closeAllMore();const n=prompt('新名字',nm(i));if(n&&n.trim()&&n.trim()!==nm(i))run('/api/rename',{name:nm(i),new:n.trim()},'已改名')}
function delAcc(i){closeAllMore();const x=ST.accounts[i];
  const msg='从本机删除「'+x.name+'」？\n\n只是删掉本机保存的 token（和它绑的代理），不会吊销它。'
    +(x.active?'\n\n⚠ 这是当前账号：删除后改用普通登录（claude /login），不会自动换到别的号；要先关掉正在运行的 claude。':'');
  if(confirm(msg))act('/api/delete',{name:x.name},d=>d.wasActive?'已删除，现在改用普通登录':'已删除')}
async function addAcc(){const name=document.getElementById('aName').value.trim(),token=document.getElementById('aTok').value.trim(),
  proxy=document.getElementById('aProxy').value.trim();
  if(!name||!token){toast('名字和 token 都要填',2800,true);return}
  const d=await act('/api/add',{name,token,proxy},d=>d.updated?('这个号已经在「'+d.name+'」里了，已把它的 token 换成新的'):('已添加「'+d.name+'」，点它的「切换」开始用'),'验证中…',[document.getElementById('addBtn')]);
  if(d.ok){['aName','aTok','aProxy'].forEach(k=>document.getElementById(k).value='');toggleAdd(false)}}
document.getElementById('aTok').addEventListener('keydown',e=>{if(e.key==='Enter')addAcc()});
async function loadIP(){try{renderIP(await api('/api/ip'))}catch(e){}}
function renderIP(d){const el=document.getElementById('ipbar'), inl=document.getElementById('ipinline');
  if(!d){el.className='ipbar'; if(inl)inl.textContent=''; return;}
  const i=d.info||{};
  // 顶部显示 claude 用当前号时实际走的线（route_info）：[标签] 细节；颜色表示有没有问题，红色另弹红条说明怎么处理
  const r=d.route||{}, lv=r.level||'ok';
  const tag=r.tag?`<span class="rtag ${lv}" title="${esc(r.tip||'')}">${esc(r.tag)}</span><span class="rdet" title="${esc(r.tip||'')}">${esc(r.detail||'')}</span>`:'';
  const loc=[i.country,i.city].filter(Boolean).map(esc).join('·');
  if(inl) inl.innerHTML=(i.ip?' · 出口 <span class="ipnum">'+esc(i.ip)+'</span>'+(loc?' '+loc:''):'')+tag;
  if(d.ok&&lv!=='bad'){ el.className='ipbar'; }
  else { el.className='ipbar show bad';   // 有问题：弹红条，写清楚怎么回事、怎么处理
    el.innerHTML='⚠ '+(lv==='bad'?esc(`[${r.tag}] ${r.detail}`)+(r.tip?'——'+esc(r.tip):'')
      :esc(d.why||'出口 IP 异常')+'——claude 可能连不上 Anthropic，查用量前请先确认线路。');}}
refresh();setInterval(refresh,5000);
loadIP();setInterval(loadIP,60000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj, ensure_ascii=False))

    def _trusted(self):
        """只接受本机页面：Host 必须是 127.0.0.1/localhost（防 DNS rebinding），
        跨站来的 Origin 一律拒绝（防别的网页偷偷切号/加号）。"""
        port = self.server.server_address[1]
        ok_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        if self.headers.get("Host") not in ok_hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {"http://" + h for h in ok_hosts}

    def do_GET(self):
        if not self._trusted():
            return self._json({"ok": False, "error": "forbidden"}, 403)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(build_state())
        if path == "/api/ip":
            d = load()
            acc = active_acc(d)
            st = ip_state(route_of(d, acc)[0])
            return self._json({"ok": True, **st, "route": route_info(d, acc, st)})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._trusted() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            body = {}
        path = urlparse(self.path).path
        force = body.get("force") or []
        force = True if force is True else [str(x) for x in force] if isinstance(force, list) else []
        kill = bool(body.get("kill"))
        try:
            if path == "/api/add":
                r = add_account(body.get("name"), body.get("token"), body.get("proxy"), force)
                return self._json({"ok": True, "name": r["name"], "updated": r["updated"], "active": r["active"]})
            if path == "/api/use":
                return self._json({"ok": True, **use_account(body.get("name"), force, kill)})
            if path == "/api/normal":
                return self._json({"ok": True, **use_normal(force, kill)})
            if path == "/api/check":
                if not body.get("name"):
                    return self._json({"ok": False, "error": "缺少账号名"})
                u = refresh_one(body["name"], force=force)
                bad = u.get("state") in ("neterr", "error", "invalid")
                return self._json({"ok": not bad, "error": (u.get("error") or "查询失败") if bad else ""})
            if path == "/api/proxy":
                name = None if body.get("normal") else body.get("name")
                return self._json({"ok": True, **set_proxy(name, body.get("proxy"), force, kill)})
            if path == "/api/rename":
                rename_account(body.get("name"), body.get("new"))
                return self._json({"ok": True})
            if path == "/api/delete":
                return self._json({"ok": True, **delete_account(body.get("name"), force, kill)})
            if path == "/api/shutdown":          # 新版本启动时用来替换掉旧版本
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json({"ok": True})
        except ClaudeRunning as e:
            return self._json({"ok": False, "running": e.procs, "error": str(e)})
        except NeedForce as e:
            return self._json({"ok": False, "needForce": e.code, "error": str(e)})
        except CctError as e:
            return self._json({"ok": False, "error": str(e)})
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return self._json({"ok": False, "error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True


# --------------------------------------------------------------------------- CLI
def replace_old_server(port):
    """端口被占用时：如果占着的是【别的版本】的换号台（比如刚改过代码），让它退出，返回 True 以便重新启动。
    同版本、或者是别的程序占着端口，返回 False。"""
    base = "http://127.0.0.1:%d" % port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机请求别走代理
    try:
        with opener.open(base + "/api/state", timeout=3) as r:
            ver = json.loads(r.read().decode("utf-8")).get("version")
    except Exception:
        return False
    if ver == VERSION:
        return False
    print("发现正在运行的是另一个版本的换号台（%s → %s），正在替换…" % (ver or "旧版", VERSION))
    try:
        req = urllib.request.Request(base + "/api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        opener.open(req, timeout=3).read()
    except Exception:
        print("旧版本不支持自动退出：请关掉原来那个换号台窗口（或结束它的进程）后再打开。")
        return False
    for _ in range(30):
        time.sleep(0.2)
        try:
            Server(("127.0.0.1", port), Handler).server_close()
            return True
        except OSError:
            pass
    return False


def cmd_serve(args):
    ensure_store()
    try:
        sync_settings(write_proxy=False)
    except CctError as e:
        print("cct: %s" % e, file=sys.stderr)
    port = args.port or PORT
    url = "http://127.0.0.1:%d" % port
    srv = None
    for attempt in range(2):
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            if attempt or not replace_old_server(port):
                print("端口 %d 已被占用，可能换号台已经开着了，直接打开 %s" % (port, url))
                if not args.no_open:
                    webbrowser.open(url)
                return
    print("Claude 换号台已启动：%s" % url)
    print("数据：%s    （Ctrl-C 退出）" % DB)
    if not args.no_open:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


def main():
    ap = argparse.ArgumentParser(prog="cct", description="Claude Code setup-token 换号器（本机自用）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="启动本地网页（默认命令）")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-open", action="store_true")
    sub.add_parser("list", help="列出账号和缓存的用量（不联网）")
    p = sub.add_parser("check", help="联网刷新某个号的用量"); p.add_argument("name"); p.add_argument("--force", action="store_true", help="出口 IP 不对也强制查")
    sub.add_parser("ip", help="显示当前出口 IP 和归属地")
    sub.add_parser("doctor", help="网络体检：代理 / TUN / 出口 IP 是否一致 / Anthropic 连通性")
    p = sub.add_parser("add", help="添加账号"); p.add_argument("name"); p.add_argument("token")
    p.add_argument("--proxy", help="这个号绑的代理，如 https://用户:密码@IP:端口")
    p.add_argument("--force", action="store_true", help="IP 不对 / 没绑代理 / 代理重复也继续")
    for cmd, hlp in (("use", "切换当前账号"), ("normal", "不用 setup token，改用本机普通登录（claude /login）"),
                     ("rm", "删除账号")):
        p = sub.add_parser(cmd, help=hlp)
        if cmd != "normal":
            p.add_argument("name")
        p.add_argument("--kill", action="store_true", help="有 claude 在运行时强制结束它们再继续")
        p.add_argument("--force", action="store_true", help="IP 不对 / 没绑代理 / 查不了进程也继续")
    p = sub.add_parser("proxy", help="给账号（或 normal = 普通登录）绑定代理，none = 解绑")
    p.add_argument("name"); p.add_argument("proxy")
    p.add_argument("--kill", action="store_true"); p.add_argument("--force", action="store_true")
    sub.add_parser("ps", help="列出正在运行的 claude（切换前要全部关掉）")
    p = sub.add_parser("rename", help="改名"); p.add_argument("old"); p.add_argument("new")
    sub.add_parser("install", help="安装：装 claude 启动函数和 cct 命令，导入原来写死的 token")
    sub.add_parser("setup", help="安装 + 启动网页（启动脚本用）")
    p = sub.add_parser("uninstall", help="撤掉安装，恢复普通登录"); p.add_argument("--purge", action="store_true", help="连账号数据一起删")
    sub.add_parser("token", help="（内部）输出当前 token")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):      # Windows 控制台是 GBK，个别符号打不出来时用 ? 代替，别报错
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    try:
        if args.cmd in (None, "serve"):
            if args.cmd is None:
                args.port, args.no_open = 0, False
            cmd_serve(args)
        elif args.cmd == "list":
            print_list()
        elif args.cmd == "check":
            d = load()
            p, route = route_of(d, must_find(d, args.name))
            print("「%s」的出口 IP：%s（走%s）" % (args.name, ip_line(ip_state(p)), route))
            print("查询中…")
            refresh_one(args.name, force=args.force)
            print_list()
        elif args.cmd == "ip":
            d = load()
            cur = active_acc(d)
            st = ip_state(route_of(d, cur)[0])
            r = route_info(d, cur, st)
            print("%s 的出口 IP：%s" % (_label(cur["name"] if cur else None), ip_line(st)))
            print("claude 实际走：[%s] %s%s" % (r["tag"], r["detail"], {"warn": "  ⚠", "bad": "  ✗"}.get(r["level"], "")))
            if r["tip"]:
                print("    %s" % r["tip"])
        elif args.cmd == "ps":
            procs = claude_processes()
            if not procs:
                print("没有正在运行的 claude，可以切换。")
            for p in procs:
                print("%7d  %s  %s" % (p["pid"], pad(p["kind"], 24), p["path"]))
        elif args.cmd == "doctor":
            cmd_doctor(args)
        elif args.cmd == "add":
            r = add_account(args.name, args.token, args.proxy, args.force)
            print(("这个号已经在「%s」里了，已把它的 token 换成新的" if r["updated"] else
                   "已添加「%s」，`cct use 名字` 切过去才用它") % r["name"])
            print_list()
        elif args.cmd == "use":
            print_switch(use_account(args.name, args.force, args.kill))
        elif args.cmd == "normal":
            print_switch(use_normal(args.force, args.kill))
        elif args.cmd == "proxy":
            d = load()
            name = None if args.name in ("normal", "普通登录") and not find(d, args.name) else args.name
            r = set_proxy(name, args.proxy, args.force, args.kill)
            e = r["egress"] or {}
            print("%s 的代理已%s%s%s" % (_label(name), "设好" if normalize_proxy(args.proxy) else "解绑",
                  "，出口 %s %s" % (e["ip"], e.get("country") or "") if e.get("ip") else "",
                  "；它是当前号，已写进 settings.json" if r["current"] else ""))
        elif args.cmd == "rename":
            rename_account(args.old, args.new)
            print("已改名：%s → %s" % (args.old, args.new))
        elif args.cmd == "rm":
            r = delete_account(args.name, args.force, args.kill)
            print("已删除「%s」（只删本机保存的 token，不会吊销它）%s" % (args.name, "；它是当前账号，现在改用普通登录" if r["wasActive"] else ""))
        elif args.cmd == "install":
            cmd_install(args)
        elif args.cmd == "setup":
            cmd_install(args, quiet=True)
            args.port, args.no_open = 0, False
            cmd_serve(args)
        elif args.cmd == "uninstall":
            cmd_uninstall(args)
        elif args.cmd == "token":
            cmd_token(args)
    except ClaudeRunning as e:
        eprint("cct: %s：" % e)
        for p in e.procs:
            eprint("  %7d  %s  %s" % (p["pid"], p["kind"], p["path"]))
        eprint("退出它们（IDE 里关掉 Claude 对话标签）后再试；或加 --kill 强制结束（会中断正在跑的任务）。")
        sys.exit(1)
    except NeedForce as e:
        eprint("cct: %s\n确认没问题要继续，加 --force。" % e)
        sys.exit(1)
    except CctError as e:
        eprint("cct: %s" % e)
        sys.exit(1)


def print_switch(r):
    e = r.get("egress") or {}
    print("已切到%s%s。新启动的 claude（终端和 IDE 里新开的对话）都用它，不用重启 IDE；"
          "要接着刚才的对话用 claude -c。" % (_label(r["to"]), "（出口 %s %s）" % (e["ip"], e.get("country") or "") if e.get("ip") else ""))
    if r.get("late"):
        print("⚠ 切换那一刻有 claude 启动了：%s。它可能读到的是一半旧设置，请退出重开。"
              % "、".join("%s(%d)" % (p["kind"], p["pid"]) for p in r["late"]))


if __name__ == "__main__":
    main()
