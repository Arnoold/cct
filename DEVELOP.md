# cct 开发说明

写给想改这个程序的人。先读 README.txt 了解怎么用。

## 1. 文件和位置

| 位置 | 是什么 |
|---|---|
| `cct.py` | 全部代码，单文件，只用 Python 3.8+ 标准库 |
| `cct-windows.bat` / `cct-mac.command` | 启动脚本：找到 Python 后运行 `cct.py setup`（安装 + 打开网页） |
| `~/.cc_tokens/app/cct.py` | **安装后实际运行的那份**。`install` / `setup` 会把你手上的 `cct.py` 复制过来 |
| `~/.cc_tokens/accounts.json` | 账号数据（明文 token）。**别发给别人** |
| `~/.claude/settings.json` | **claude 自己的配置，cct 切换时往它的 `env` 里写当前号的 token 和代理**（权限 600）。设了 `CLAUDE_CONFIG_DIR` 就在那个目录下 |
| `~/.cc_tokens/switch.log` | 添加 / 切换 / 删除的记录 |
| `~/.cc_tokens/accounts.json.bak`、`backups/` | 上一版数据，以及每天一份备份（保留 7 天）。`accounts.json` 读不出来时自动用它们恢复，坏文件另存为 `accounts.json.broken-时间` |
| `~/.cc_tokens/backups/config/` | 改 `settings.json`（保留最近 20 份）/ `.zshrc` / PowerShell 配置之前的备份（权限 600，token 已打码，代理密码没打码）。旧版本留在家目录里的 `*.cct-bak-*`，安装时会自动挪到这里 |
| `test_cct.py` | 自动化测试 |

`~` 在 Windows 上是 `C:\Users\你的用户名`。

## 2. 改了代码怎么生效

1. 改文件夹里的 `cct.py`。
2. 重新双击启动脚本，或者运行 `python3 cct.py setup`（Windows 上是 `py -3 cct.py setup`）。
   - 它会把新代码复制到 `~/.cc_tokens/app/cct.py`。
   - 如果旧版本的网页服务还开着，会自动把它停掉，换成新版本。判断依据是文件内容的指纹 `VERSION`。
3. 浏览器里按 `Cmd+Shift+R` / `Ctrl+Shift+R` 强制刷新。

不要直接改 `~/.cc_tokens/app/cct.py`，下次运行 `setup` 会被文件夹里那份覆盖。

**改完一定要跑测试**：`python3 test_cct.py`（Windows 上是 `py -3 test_cct.py`），全部通过再用。它在临时沙盒里运行，不联网，也不碰真实数据。加了新功能，最好在里面补上对应的测试。有 pyflakes 的话再跑一下 `python3 -m pyflakes cct.py`。

## 3. 工作原理

### 3.1 切换账号怎么生效（2026-11-15 起）

**一句话**：每个号绑一个代理；切换时把「这个号的 token + 这个号的代理」一次写进 claude 的 `~/.claude/settings.json` 的 `env`。切换前要求一个 claude 都没在跑。

写进去的变量是 `CLAUDE_CODE_OAUTH_TOKEN`；在「一个号一个 IP」模式下还有 `HTTPS_PROXY` / `HTTP_PROXY` / `https_proxy` / `http_proxy`，以及 `NO_PROXY` / `no_proxy` 和空的 `ALL_PROXY` / `all_proxy`（`_bypass_env()`，和中转配置命令写的一致）：

- **为什么要 `NO_PROXY`**：settings.json 的 env 会传给 claude 起的所有子进程（Bash 工具的命令、hooks、本地 MCP）。没有它，curl、Python 访问 `localhost` / 内网也会被送去中转、连不上（11/17 实测 curl 和 Python urllib 都这样）；我们的用量上报域名也会绕到美国。原来没有就写 `NO_PROXY_DEFAULT`，原来有就保留、只补缺的 `localhost,127.0.0.1`。
- **为什么 `ALL_PROXY` 置空**：防客户 shell 里 Clash「复制终端代理命令」留下的 `all_proxy=socks5://…` 让部分工具绕开中转。
- 只补「哪些地址别走代理」，不改走哪条线，所以平时同步（装机）也会补，不用等切换。
- Windows 上这两个也只写大写的（理由同代理变量：Windows 不分大小写，两种都写时排后面的生效）。

`settings.json` 里别的内容（`NO_PROXY`、`ALL_PROXY`、permissions…）原样保留。负责写入的函数是 `sync_settings(write_proxy)`，写之前用 `backup_config()` 备份，写的时候原子替换。

为什么这样做？下面三条都是 11/15 在本机用临时配置目录实测出来的：

1. **`settings.json` 的 env 优先级高于进程环境变量**，token 和代理都是。所以终端 / IDE 启动时继承来的旧 token、旧代理都会被盖住，新启动的 claude 一定用 settings.json 里那一对。IDE 插件（VS Code / Cursor / Antigravity / JetBrains）和桌面版起的 claude 也读它，所以**切换后不用重启 IDE**。
2. **正在运行的 claude 会中途重读 settings.json 的一部分**。改了代理后，大部分连接还走旧代理，但有少量换到了新代理。如果旧号的会话还开着，就会出现「旧号的请求从新号的 IP 出去」。所以切换前 `ensure_no_claude()` 会检查进程，有就拒绝（抛 `ClaudeRunning`）。用户可以关掉后重试，也可以选「强制结束」：`kill_processes()` 在 posix 上先发 SIGTERM 再发 SIGKILL，Windows 上用 `taskkill /F`。
3. **把 token 写成空字符串 `""`，就能盖住进程环境里残留的旧 token**。claude 会当作没设这个变量，改用 `/login` 普通登录。所以切到「普通登录」时写 `""`，而不是删掉这个键：删掉的话，旧终端里 export 过的 token 会生效。

**怎么认出 claude 进程**（`claude_processes()` / `_is_claude()`）：只看当前用户自己的进程。
- macOS / Linux：用 `ps -A -o pid= -o uid= -o comm=` 和 `-o args=`。
  - 原生版、IDE 插件起的（`…/extensions/anthropic.claude-code-…/native-binary/claude`）、桌面版起的，进程名都是 `claude`。npm 版会把自己的进程名改成 `claude`。
  - 直接跑 `~/.local/share/claude/versions/2.1.x` 时，进程名是版本号，按路径认。
  - `node …/@anthropic-ai/claude-code/cli.js` 按命令行认。
  - macOS 桌面版本体叫 `Claude`（大写），不算。
- Windows：用 PowerShell 的 `Get-CimInstance Win32_Process`。桌面版本体也叫 `claude.exe`（在 `%LOCALAPPDATA%\AnthropicClaude\` 下），按路径排除。
- 查不了（没有 ps、PowerShell 出错）时不放行，要用户确认（风险代码 `nops`）。
- 写完 settings.json 后再查一次。如果检查和写入之间刚好有 claude 启动，就提醒用户重开它（返回值里的 `late`）。

**「一个号一个 IP」模式**（`bound(d)`：任何一个号或普通登录绑了代理就算）：
- 切换时连代理一起写。目标没绑代理时去掉代理变量，**绝不留着上一个号的代理**。
- 切换前先经目标代理查一次出口 IP（`_precheck_target()`），查不到或是大陆 IP 就拦下（风险代码 `ip`）。目标没绑代理也拦下（`unbound`）。
- 查用量（`refresh_one()`）和添加时验证 token（`add_account()`）都经**那个号自己的代理**发出（`proxy_of()`），不借当前号的。没绑代理的号用 `ENVP`（只看环境变量，不读 settings.json，否则会借到当前号的代理），也要确认；确认后切换前还会经它查一次实际出口，连不上或是大陆 IP 再拦一次（`ip`）。
- 一个代理绑给两个号要确认（`dup`）。
- 一个都没绑时（自己用 Clash 的旧用法），代理变量完全不碰，cct 发请求跟着 claude 的代理走（`CLAUDE` 哨兵值），和旧版一样。

**风险确认**：抛 `NeedForce(msg, code)`。命令行加 `--force` 一次全认；网页逐条弹确认，把确认过的 code 放进 `force` 列表重发。这样确认了 A 风险，不会顺带放过 B 风险。

**会改 settings.json 的操作都要先关 claude**：切换、切到普通登录、删除当前号（等于切到普通登录）、改当前号的代理。新加的号**不会自动启用**，因为启用就是切换。

**漂移**：如果 settings.json 里的代理被别的命令改了（比如中转换入口时让客户跑的 sed），`settings_drift()` 会发现它和绑定不一致，网页顶部会提示，`cct doctor` 也会报。重新切一次当前号就按绑定改回。如果是中转换了地址，要先在 cct 里改这个号的代理。

**从旧版升级**（`cmd_install()`）：
- 删掉旧版留下的东西：`.zshrc` / PowerShell 配置里的 `claude()` 启动函数整块删掉（`drop_block()`）；删掉 `current_token` 文件；Windows 上删掉 cct 写的用户环境变量（`win_drop_env()`）。
  - 升级前就开着的终端里还留着旧函数，它会调 `cct token`，所以 `cmd_token()` 保留。它传的环境变量反正会被 settings.json 盖过。
- `migrate_settings()`：
  - settings.json 里原来写死的 token 导入成账号。
  - 原来配的中转代理（`https://用户:密码@…`，就是我们给客户配的那种）绑给当前在用的号。只做一次（记在 `proxy_migrated`）。
  - 本机 Clash 那种不带账号的代理不自动绑。
- 最后 `sync_settings(write_proxy=False)`：只写 token（就是正在用的那个号），代理不动，所以正在跑的会话不受影响。

### 3.1.2 claude 实际走哪条线（顶部那一行，`route_info()`，11/18 起）

顶部显示成「出口 IP · [标签] 细节」，标签 6 种：**中转**（我们的中转：绑的，或地址是 https+账号+32230/32231）/ **端口**（本机代理端口，如 Clash 的 127.0.0.1:7890）/ **代理**（别的远程代理）/ **TUN**（直连被 TUN / VPN 接管）/ **直连** / **第三方**（ANTHROPIC_BASE_URL、Bedrock 等）。颜色是等级：绿正常、琥珀有隐患、红有问题（红的另弹红条写怎么处理，悬停看 `tip`）。`describe_route()` 是它的一行文字版（命令行用）。

「终端里会带的代理」= `term_proxy()`：先看启动换号台那个终端的环境变量，再看 `persistent_proxy()` 读到的 shell 配置文件（`.zshrc` / `.zprofile` / `.bashrc` / `.bash_profile` / `.profile` 行首的 export，函数里缩进的不算；Windows 是用户环境变量和 PowerShell 配置）。写进配置文件的，任何新开的终端都会带上，这样就不只看启动换号台的那个窗口。只在某个窗口里临时 export 的，看不到。

只管终端 / IDE 里的 claude，不管浏览器登录和桌面版本身。按 Claude Code 的判断顺序：

| 顺序 | 情况 | 怎么看出来 | 显示 / 等级 |
|---|---|---|---|
| ① | 设了 `ANTHROPIC_BASE_URL`（第三方 API 中转站）或 Bedrock / Vertex / Foundry | `gateway()`：用户级 / 管理员 settings.json 和换号台所在终端的环境变量 | 红：cct 的号、用量、出口都不作数 |
| ② | 当前号绑了中转 | 绑定 | 「中转 p12345a」 |
| ② | 终端代理变量 / settings.json 里的代理 | `env_proxy()` / `claude_proxy()` | 「终端的代理环境变量 127.0.0.1:7890」；**正好是别的号的中转 → 红**（会借用它的 IP） |
| ②+ | 连代理这一段又进了 TUN | `iface_to(代理主机)` 是隧道网卡，再用 `tls_rtt()` 量握手：> 150 ms 算绕路 | 快：正常并注明；慢：琥珀色「绕了一圈」 |
| ③ | 直连被 TUN / VPN 接管 | `iface_to("api.anthropic.com")`：问系统走哪张网卡（mac `route -n get`、Linux `ip route get`、Windows `Find-NetRoute`），网卡名 / 描述像 utun、tun、Wintun、WireGuard… | 「直连 → 被 TUN / VPN 接管（utun6）」，分不清是 TUN 还是 VPN |
| ③ | 只开了系统代理 | `system_proxy()`（mac `_scproxy`、Windows 注册表）有值，但上面都没有 | 红（出口不通）/ 琥珀：claude 不认系统代理 |
| ③ | 真直连 | 以上都没有，出口查不到或是大陆 | 红 |
| ③ | 路由器 / 网关代理，或机器在海外 | 以上都没有，出口却在海外 | 正常，注明是这两种之一（分不清） |

- 用 TLS 握手量时间，是因为开着 TUN 时 TCP 连接会被本机 TUN 直接应答，量不出真实路程。结果缓存 5 分钟。
- 「终端的代理环境变量」是**启动换号台那个终端**的。IDE 里的 claude 一般继承同样的设置，但不保证。
- 切到没绑代理的号之前，`_precheck_target()` 也用这套判断：出口不通、借用了别的号的中转等「红」的情况会再拦一次（风险代码 `ip`）。
- 项目目录：`claude_projects()` 读 `~/.claude.json` 的 projects（按修改时间缓存），`project_overrides()` 查每个项目 `.claude/settings*.json` 里的 token / 代理 / 中转站 / API key，列进冲突检查。家目录当项目时，它的 `.claude/settings.json` 就是用户级那份，跳过。
- `cct ip`、`cct doctor` 也会打印这一行；doctor 还会列出直连走的网卡、系统代理设置、项目目录里的覆盖。

### 3.1.1 认证优先级和冲突检查

根据官方文档（https://code.claude.com/docs/en/authentication 的 Authentication precedence 一节），优先级从高到低依次是：

1. `CLAUDE_CODE_USE_BEDROCK` / `VERTEX` / `FOUNDRY`
2. `ANTHROPIC_AUTH_TOKEN`
3. `ANTHROPIC_API_KEY`
4. `apiKeyHelper`
5. **`CLAUDE_CODE_OAUTH_TOKEN`（cct 用的）**
6. Anthropic profile
7. `/login` 普通登录

所以前 4 项只要存在，cct 的切换和普通登录就都不起作用。`auth_conflicts()` 会检查 shell 配置文件或 Windows 用户环境变量、`~/.claude/settings.json` / `settings.local.json`、`managed-settings.json`（macOS 在 `/Library/Application Support/ClaudeCode/`，Windows 在 `C:\Program Files\ClaudeCode\`），发现就在安装输出、`cct list` 和网页顶部红框里提示。

用户级 `~/.claude/settings.json` 里的 token 和代理归 cct 管，不算冲突。优先级更高的配置文件（`managed-settings.json`、`settings.local.json`）里如果写了 `CLAUDE_CODE_OAUTH_TOKEN`，或者在「一个号一个 IP」模式下写了代理，就会盖过 cct 写的，算冲突。

**项目目录里的 `.claude/settings.json` 优先级也比用户级高，但 cct 没有检查它。** 客户某个项目里要是配了 `env.HTTPS_PROXY`，在那个项目里 claude 就会走它。

`/logout` 只清除 `/login` 的登录信息，不会吊销 setup token（文档没有明说，是根据文档推断的）。

### 3.2 用量怎么查

`probe(token)` 向 `https://api.anthropic.com/v1/messages` 发一条 `max_tokens=1` 的请求，然后读响应头：

```
anthropic-ratelimit-unified-5h-utilization: 0.21      ← 用了 21%
anthropic-ratelimit-unified-5h-reset: 1790190600      ← 重置时间（Unix 时间戳，和时区无关）
anthropic-ratelimit-unified-5h-status: allowed        ← rejected = 用完
anthropic-ratelimit-unified-7d-…                      ← 本周（This week）
anthropic-ratelimit-unified-7d_oi-…                   ← Fable 本周（Fable this week，只有开通 Fable 的号才有）
anthropic-organization-id: 6391af7b-…                 ← 用来判断两个 token 是不是同一个号
```

几个要点：

- 请求里**必须带** Claude Code 的 system 标记（见 `_probe_body()`）。不带的话服务器不把它算作订阅流量，会返回一个假的 429。
- 先用 `FABLE_MODEL` 请求。开通了 Fable 的号，这一次就能拿到全部桶，包括 `7d_oi`。没开通的号会被直接拒绝（`credits_required`，不带用量头，也不消耗额度），这时再用 `MODEL`（Opus）请求一次。
- 桶的显示名在 `KNOWN_BUCKETS` 里：`5h` = 5 小时（Current session）、`7d` = 7 天（This week）、`7d_oi` = 7 天 Fable（Fable this week），都已经和 claude.ai 的 Usage 页面对照过。以后出现新的桶，会先按字段名显示（比如 `7d_xx` 显示成 "7 天·xx"），确认含义后再加进去。
- 判断"号用完了没"只看 `CORE = ("5h", "7d")`。其他桶用完了，只代表那个模型暂时不能用。
- 联网有两种方式，一种不行就换另一种：`_post_urllib()` 用 Python 自带的库和系统证书，**不读系统代理设置**（`_urllib_open()` 里的 `ProxyHandler({})`；Claude Code 只认代理环境变量、不认系统代理，cct 读了的话查到的出口就和 claude 的不一样，11/18 改）；`_post_curl()` 用 curl。Windows 上先试 urllib，macOS 上先试 curl，因为 python.org 版的 macOS Python 默认没有根证书。
- 用哪个代理：所有联网函数都带 `proxy` 参数（`probe(token, proxy)`、`egress_ip(proxy=…)`…），由 `proxy_of(d, 号)` 决定。绑了就用这个号的。一个代理都没绑过时是 `CLAUDE`，即 `claude_proxy()`（先 `settings.json` 的 env，再环境变量），这时 cct 显示的出口 IP 就是 claude 的出口。出口 IP 按代理分别缓存 60 秒。实际发请求用 `effective_proxy(proxy)`：如果代理是 `https://用户:密码@…`（连代理本身也走 TLS），而当前是 Windows（或没有 curl），就改走**本机转接口**——`_local_relay()` 在 `127.0.0.1` 随机端口开一个只给本进程用的明文 CONNECT 入口（每个上游代理各开一个），`_relay_client()` 用 TLS 连上游代理并补上 `Proxy-Authorization`，再双向转发。原因：urllib 不支持 https:// 代理（会用明文连，被拒）；Windows 自带的 curl 连 https 代理不发账号密码（407）。里面跑的仍是到 Anthropic 的 TLS，转接口看不到内容。macOS / Linux 的 curl 本来就支持，不启用转接。`_proxy_ssl_ctx()` 在 python.org 版 macOS Python 没根证书时从系统钥匙串补上。
- setup token 只有 `user:inference` 权限。`/api/oauth/profile` 这类接口会返回 403，所以拿不到邮箱和订阅等级。

### 3.3 不自动换号，启动时不联网

claude 启动时读的是 settings.json，cct 不参与，所以**不联网、不换号**（`cmd_token()` 只给旧版启动函数用，同样只读本地）。换号完全由用户手动操作：在网页或 `cct use` 里点「切换」。**故意没有「批量查全部」或「自动挑下一个号」**：那样会在同一时刻、同一出口同时发出多个账号的请求，把这些账号关联起来，风险更高。

联网只发生在用户主动触发的地方：`refresh_one(name)`（查某一个号的用量），以及 `add_account()` 验证 token。两者都只查一个号。

## 4. 代码结构（cct.py，按顺序）

| 区块 | 主要内容 |
|---|---|
| 文件头 | 说明、常量（`STORE`、`PORT`、`MODEL`、`CORE`…）、`VERSION` |
| store | `locked()` 跨进程锁（mac/Linux 用 fcntl，Windows 用 msvcrt）、`load()` / `save()` / `txn()` 读写 `accounts.json` |
| probe | `_post*()` 发请求、`probe()` 解析用量、`usable()`、`egress_ip()` / `ip_state()` / `ensure_safe_egress()` |
| 每个号绑的代理 | `normalize_proxy()` / `proxy_label()`（显示时去掉密码）/ `bound()` / `target_proxy()` / `proxy_of()` |
| 正在运行的 claude | `claude_processes()` / `_is_claude()` / `kill_processes()` / `ensure_no_claude()` |
| settings.json | `settings_path()` / `sync_settings()` / `settings_drift()`、`refresh_one()` |
| actions | `add_account()` / `_switch()`（`use_account()` / `use_normal()` 都走它）/ `set_proxy()` / `rename_account()` / `delete_account()` |
| format | 时间和进度条格式化、`acc_view()`（给网页的单个账号数据）、`build_state()`、`print_list()` |
| 旧版启动函数调用 | `cmd_token()`（保留兼容） |
| Windows 环境变量 | `win_get_env()` / `win_set_env()` / `win_drop_env()`（只用来清掉旧版写的） |
| install | `install_app_copy()`、配置块的删除（`drop_block()`）、`posix_install()` / `win_install()`、`migrate_settings()`、`cmd_install()` / `cmd_uninstall()` |
| web UI | `PAGE`：整个网页（HTML + CSS + JS）是一个字符串 |
| Handler | 本机 HTTP 服务和 API（见下） |
| CLI | `replace_old_server()`、`cmd_serve()`、`main()`（命令行参数） |

### 数据格式 `accounts.json`

```json
{
  "active": "账号名",               // null = 普通登录（不用 setup token）
  "normal_proxy": "https://用户:密码@IP:端口",   // 普通登录绑的代理（可无）
  "normal_egress": {"ip": "…", "country": "US", "city": "LAX", "at": 1790000000},
  "proxy_migrated": true,           // 已经把旧 settings.json 里的中转代理绑过了
  "accounts": [
    { "name": "账号名", "token": "sk-ant-oat01-…", "org": "组织ID", "added_at": 1790000000,
      "proxy": "https://用户:密码@IP:端口",       // 这个号绑的代理（可无）
      "egress": {"ip": "…", "country": "US", "city": "LAX", "at": 1790000000},   // 上次经它查到的出口
      "usage": { "state": "ok|limited|invalid|error|neterr", "checked_at": 1790000000,
                 "fable": true, "fable_why": "", "org": "…",
                 "buckets": { "5h": {"util": 0.21, "status": "allowed", "reset": 1790190600}, "7d": {…} } } }
  ]
}
```

### 网页 API（只接受本机访问）

所有 POST 都要求 `Content-Type: application/json`，`Host` 必须是 `127.0.0.1` 或 `localhost`，不接受跨站的 `Origin`。这样可以防止别的网页偷偷调用这些接口。

| 接口 | 作用 |
|---|---|
| `GET /` | 网页 |
| `GET /api/state` | 所有账号和用量（不含完整 token，代理不含密码）、`bound`、`drift`、`platform`、`version` |
| `GET /api/ip` | 当前号（经它的代理）的出口 IP |
| `POST /api/add` `{name, token, proxy}` | 添加（经 proxy 验证 token），不自动启用 |
| `POST /api/use` `{name}` | 切换 |
| `POST /api/normal` | 改用普通登录（不用 setup token） |
| `POST /api/proxy` `{name, proxy}` 或 `{normal:true, proxy}` | 绑定 / 解绑代理（proxy 为空 = 解绑） |
| `POST /api/check` `{name}` | 查**一个**号的用量（`name` 必填）；返回 `ok:false` 时附带失败原因 |
| `POST /api/rename` `{name, new}` / `POST /api/delete` `{name}` | 改名 / 删除 |

以上 POST 都可以带两个参数：`force: ["ip", "unbound", "dup", "nops"]`（确认过的风险）和 `kill: true`（强制结束正在运行的 claude）。失败时返回 `{ok:false, needForce: "风险代码", error}` 或 `{ok:false, running: [{pid, kind, path}], error}`。
| `POST /api/shutdown` | 让服务退出（新版本启动时用） |

### 网页前端

`PAGE` 里的 JS 每 5 秒请求一次 `/api/state`。会改东西的操作都走 `act()`：遇到 `needForce` 就弹确认，确认后带上风险代码重发；遇到 `running` 就弹 `runningDialog()`，列出正在运行的 claude，三个按钮是「取消」「我关好了，再试一次」「强制结束它们并继续」。数据没变时不会重绘，每整分钟重绘一次，让"剩余时间"更新。按钮点击后会显示加载状态，有请求在进行时也暂停重绘。时间一律按台北时间（`Asia/Taipei`，和北京时间一样是 UTC+8）显示。颜色变量在 `:root` 里，深色模式在 `@media (prefers-color-scheme:dark)` 里。

## 5. 可以用环境变量调整的

| 变量 | 默认 | 用途 |
|---|---|---|
| `CCT_STORE` | `~/.cc_tokens` | 数据目录，测试时指到临时目录，就不会影响真实数据 |
| `CCT_PORT` | `6061` | 网页端口 |
| `CCT_MODEL` | `claude-opus-5-5` | 查用量用的模型（模型下线了就改这里，或者改代码里的默认值） |
| `CCT_FABLE_MODEL` | `claude-fable-5-1` | 用来探测 Fable 桶的模型 |
| `CCT_ZSHRC` | — | 测试用：只写这一个 shell 配置文件 |

## 6. 怎么测试

不想动真实数据和配置的话：

```bash
# 用临时 HOME 模拟一台新电脑（macOS / Linux）
export HOME=/tmp/cct-test/home CCT_PORT=6199
mkdir -p $HOME && python3 cct.py install && python3 ~/.cc_tokens/app/cct.py serve

```

**端到端验证真 claude 用的是哪一对「号 + IP」**（11/15 做过一次，结果全部对得上）：
1. 用临时 `CLAUDE_CONFIG_DIR`，往 `accounts.json` 写两个假号，各绑一个 `http://127.0.0.1:端口` 的代理。
2. 在这两个端口起一个小监听，打印收到的请求行和 `Authorization` 头。
3. 切换后用 `ANTHROPIC_BASE_URL=http://随便.test` 启动真 claude，同时在环境变量里塞一个旧 token 和一个错的代理。
4. 请求会以明文 `POST http://随便.test/…` 发到代理，从监听的输出能同时看到走的是哪个端口、带的是哪个 token。

注意：本机要是开着别的 claude（比如 IDE 对话标签），真实的进程检查会拒绝切换。测试脚本里要把 `claude_processes` 换成返回空列表的函数。

- `python3 test_cct.py` 覆盖了：
  - 添加、切换、普通登录、只查单个号、删除
  - 有 claude 在运行时拒绝切换、强制结束
  - settings.json 的写法：token、空 token、保留别的设置、文件权限、备份打码、坏 JSON 不覆盖
  - 一个号一个 IP：每个号查用量走自己的代理；切换时 token 和代理一起换；没绑代理 / 代理不通 / 大陆 IP / 代理重复要确认；漂移检测
  - 认 claude 进程（含在真机上列一次进程）
  - 从旧版升级：删旧启动函数、绑中转代理
  - `cct token` 的退出码、数据损坏恢复、Windows 配置文件写法（模拟）、冲突检查
  - 网页 API（含 running / needForce 返回）和跨站拦截
- 测网页样式时，可以直接往 `accounts.json` 写假账号和假的 `usage`，不用真的去联网查。
- 测"查询失败"时，添加一个假 token，服务器会返回 401。

## 7. 已知限制

- **Windows 上的进程检查（PowerShell `Get-CimInstance`）和 settings.json 写入还没在真 Windows 上跑过。** 第一次用时要留意：切换时能不能认出 VS Code 里的 Claude 对话。
- Linux：只在模拟环境测过，没在真 Linux 机器上跑过。
- 项目目录里的 `.claude/settings.json` 优先级比用户级高，里面要是写了代理或 token，会盖过 cct，而 cct 查不到。
- 中转换入口地址时，客户机上 cct 绑的代理要一起改（`cct proxy 名字 新代理`），否则下次切换会按旧绑定写回去。
- 查用量的模型名是写死的默认值，模型下线后要改 `MODEL` / `FABLE_MODEL`。
- token 是明文存的（文件权限 600）。可以改成存进钥匙串或 Windows 凭据管理器。
- 网页只显示缓存，不会定时自动刷新；也不会在用量快满时提醒。
