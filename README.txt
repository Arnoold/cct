Claude 换号台（cct）使用说明
==========================

这是什么
  在一台电脑上管理多个 Claude Code 的 setup token：
  看每个号的用量（5 小时 / 7 天 / Fable），一个号用完了就换下一个。
  每个号可以绑一个自己的代理（中转）：切到哪个号，claude 就走哪个号的 IP。
  只给自己的电脑用。

文件夹里有什么
  cct.py              程序本体（只有这一个文件，纯 Python 标准库，不用装任何依赖）
  cct-windows.bat     Windows 启动脚本（双击）
  cct-mac.command     macOS 启动脚本（双击）
  README.txt          本说明
  DEVELOP.md          开发说明：原理、代码结构、怎么改代码
  CHANGELOG.md        更新记录
  test_cct.py         自动化测试（改了代码后运行：python3 test_cct.py）


一、在一台新电脑上装
----------------------
  1. 把整个文件夹复制过去（放哪都行；装完之后这个文件夹可以删，程序已经复制到 用户目录/.cc_tokens/app/）。
  2. 启动：
       macOS / Linux：打开“终端”，输入  python3  和一个空格，把这个文件夹里的 cct.py 拖进终端，
                      再输入  setup ，回车。整条像这样：  python3 /路径/cct.py setup
                      （macOS 也可以双击 cct-mac.command，但常被系统拦，见下方说明；用终端最省事。）
       Windows：      双击 cct-windows.bat
  3. 它会自动安装，然后打开网页 http://127.0.0.1:6061
  4. 网页右上角「＋ 添加账号」：填名字、粘贴 token，再填这个号的代理（中转给的 https://用户:密码@IP:端口）。
     添加完点它的「切换」才开始用。

  · 电脑上原来的 Claude Code 登录不用动。cct 里没有账号时，claude 照常用原来的登录；
    账号全删了也会自动恢复原来的登录。
  · Windows 没装 Python：cct-windows.bat 会问要不要用 winget 自动装 Python 3.12；
    不行就去 https://www.python.org/downloads/ 下载，安装时勾选 “Add python.exe to PATH”，再双击一次。
  · 装过旧版的直接重装一次就行：
    - 旧版装在 .zshrc / PowerShell 配置里的 claude 启动函数会被删掉，现在改由 settings.json 切换。
    - ~/.claude/settings.json 里原来配的中转代理，会自动绑给当前在用的号。
    - 其它号要在网页上「⋯ → 设置代理」各绑各的。
  · 【重要】macOS 上首选用终端跑 python3 …/cct.py setup，不要双击 cct-mac.command。
    双击 .command 经常被系统拦，有两种拦法：
      (a) “没有正确的访问权限 / 无法执行”——文件经过 Windows / 压缩 / 微信 / U 盘传过来丢了可执行权限。
      (b) “Apple 无法验证…是否包含恶意软件”（弹框有“移到废纸篓”按钮）——这是 macOS 门禁 Gatekeeper，
          因为这个脚本没有花钱做 Apple 签名公证，不是说它有病毒。【别点“移到废纸篓”，点“完成”】。
    用终端跑 python3 …/cct.py setup 可以完全绕开这两种拦截（推荐）。
    如果一定要双击，先处理：
      修 (a)：终端输入  chmod +x  （留个空格），把 cct-mac.command 拖进终端，回车。
      修 (b)：系统设置 → 隐私与安全性 → 往下找到“已阻止 cct-mac.command” → 点“仍要打开”；
             或终端运行  xattr -dr com.apple.quarantine  后面拖入整个文件夹。


二、token 从哪来
------------------
  在任意一台装了 Claude Code 的电脑上运行：   claude setup-token
  浏览器里登录【要添加的那个账号】并授权，完成后终端会打印一串 sk-ant-oat01-… ，这就是 token。
  · 浏览器里如果已经登着别的 Claude 账号，先退出或用无痕窗口，否则生成的是那个号的 token。
  · token 大约一年有效。过期后网页上会显示「token 无效」，重新生成一个添加即可（同一个号会自动替换）。


三、日常使用
--------------
  以后怎么打开网页
    · Windows：双击桌面上的「Claude换号台.bat」，或在终端输入  cct
    · macOS：  终端输入  cct ，或再双击 cct-mac.command
    （网页开着时会有一个命令行窗口，关掉它网页就停了。网页只是切换和看用量的界面，
     平时关着完全没关系：当前 token 照样生效。）

  网页上能做的
    · 看每个号的用量、重置时间、剩余时间：5 小时 / 7 天 /（开通 Fable 的号）7 天 Fable
      进度条颜色：绿=正常，琥珀=接近上限（≥75%），红=快满或已用完（≥90%）。当前账号是红框卡片、带「● 当前」标签。
      —— 分别对应 claude.ai 用量页的 Current session / This week / Fable this week。
      claude.ai 上的时间按你浏览器的时区显示，cct 固定按台北时间显示，所以钟点看起来可能不一样，但是同一个时刻。
    · 切换、查用量（每次只查一个号）、改名、删除
    · 「普通登录」：列表最上面那张卡片。点它的「切换」就不用任何 setup token，
      Claude Code 走这台电脑原来的 /login 登录（卡片上显示是哪个邮箱）；账号都还在，随时切回。
      处于普通登录时，添加新账号也不会自动启用。
    · 号用完了就手动切换：点别的号的「切换」。要看某个号还剩多少，点它自己的「查用量」。
      cct 不会自动换号，不会批量查所有号，启动 claude 时也不联网。
    · 页面上的用量是缓存；点某个号的「查用量」才联网查那一个号（一条 1 token 的请求）

  一个号一个 IP（用中转的客户必看）
    · 每个号绑一个代理：添加时填，或者之后在「⋯ → 设置代理」里改。「普通登录」那张卡片也能绑。
      一个代理只给一个号用；绑给第二个号时会提醒。
    · 切换时，cct 把【这个号的 token + 这个号的代理】一起写进 ~/.claude/settings.json。
      claude 以它为准，终端和 IDE 里新启动的 claude 都用这一对。
    · 绑了代理时还会一起写上 NO_PROXY（本机地址和我们的上报域名不走代理，和中转配置命令一样），
      否则 claude 里跑的命令访问 localhost、公司内网会连不上；ALL_PROXY 会置空，防 Clash 的设置干扰。
    · 查用量、添加号时验证 token，也都走那个号自己的代理，不会借当前号的 IP。
    · 切换前会先经目标号的代理查一下出口 IP，查不到或是大陆 IP 就拦下（可确认后继续）。
    · 没绑代理的号：切换、查用量都要确认。确认后 claude 不走中转，也不会沿用上一个号的代理，
      只认终端里设的代理环境变量或 Clash 的 TUN 模式；Claude Code 不认「系统代理」设置，都没有就直连（国内多半连不上）。
      切过去前 cct 会先查一下实际出口，连不上或是大陆 IP 会再提醒一次。
    · 一个号都没绑代理（自己用 Clash 的老用法）：cct 不碰代理，和以前一样。

  切换前要先关掉所有 claude（重要）
    · 正在运行的 claude 会中途读到一部分新设置，旧号的请求就可能从新号的 IP 出去。
      所以点「切换」时，cct 会检查有没有 claude 在运行：
        - 终端里的 claude；
        - VS Code / Cursor / Antigravity 等 IDE 里开着的 Claude 对话标签；
        - Claude 桌面版。
      有的话会列出来。可以关掉后点「我关好了，再试一次」；
      也可以点「强制结束它们并继续」，但正在跑的任务会中断。
    · 关掉后再切，切完用  claude -c  接着刚才的对话（对话记录在本机，不跟账号走）。
    · 切换后不用重启 IDE：IDE 里新开的 Claude 对话就是新号 + 新 IP。
    · 删除当前账号、改当前账号的代理，也算切换，同样要先关 claude。

  支持哪些终端 / IDE
    · macOS：系统终端、iTerm2、Warp、Ghostty，VS Code / Cursor / Antigravity / JetBrains 的内置终端和 Claude 插件
      （默认 shell 是 zsh 或 bash 都行；fish 不支持）。
    · Windows：PowerShell、cmd、Git Bash、Windows Terminal，VS Code / JetBrains 的内置终端和插件。
    · Linux：终端和 IDE 都行。cct doctor 的 TUN 检测用 ip route（需要 iproute2，一般都自带）。
    · 换号靠的是 claude 自己的 settings.json，所以 shell 种类（fish 也行）、脚本、定时任务都不影响。

  命令行（和网页功能一样）
    cct list               看所有号和缓存的用量
    cct check [名字]       联网刷新用量
    cct add 名字 token --proxy 代理     添加（带上这个号的代理）
    cct use 名字           切换（有 claude 在运行会拒绝；加 --kill 强制结束它们）
    cct normal             改用普通登录（不用 setup token）
    cct proxy 名字 代理    给号绑代理（名字写 normal = 普通登录；代理写 none = 解绑）
    cct ps                 看有哪些 claude 在运行
    cct ip                 显示当前号的出口 IP
    cct doctor             网络体检（代理 / TUN / 出口 IP 一致性 / 连通性）
    cct rename 旧名 新名 / cct rm 名字


四、不想用了
--------------
  终端运行  cct uninstall
  撤掉所有改动，新开的终端恢复原来的普通登录；账号数据保留。加 --purge 连数据一起删。


五、注意
----------
  · 不要在 Claude Code 里用 /logout：它会清掉这台电脑的普通登录（/login），之后「普通登录」就用不了，
    要重新 /login。它不会吊销 setup token；不想用某个 token，在 cct 里删掉或切走即可。
  · 如果网页顶部出现红框「发现和 cct 冲突的设置」：按提示把那些设置删掉或注释掉。
    否则 Claude Code 会优先用它们，cct 的切换和普通登录都不起作用。这类设置有：
    ANTHROPIC_API_KEY、ANTHROPIC_AUTH_TOKEN、apiKeyHelper、Bedrock/Vertex/Foundry 开关，
    以及优先级更高的配置文件里写的 token / 代理。安装时也会在命令行里提示。
  · 如果网页顶部提示「settings.json 里的代理被改过」：说明有别的命令改了 claude 的代理，号和 IP 对不上了。
    点「按绑定改回」即可。如果是中转换了地址，先在「⋯ → 设置代理」里把这个号的代理改成新的。
  · 某个项目目录里的 .claude/settings.json 如果自己写了代理，在那个项目里会盖过 cct 写的，cct 查不到这种情况。
  · 删除「当前」账号：会改用普通登录，不会悄悄换到别的号。
  · 账号数据在 用户目录/.cc_tokens/accounts.json，里面是明文 token 和代理密码，别外传、别放网盘同步。
    当前号的 token 也会写在 ~/.claude/settings.json 里（权限只给自己），这个文件同样别外传。
    每次修改前会自动备份（accounts.json.bak，以及 backups/ 里每天一份、保留 7 天）；
    文件万一写坏了，程序会自动用备份恢复。
  · setup token 查不到邮箱和订阅等级（没有这个权限），所以名字要自己起，建议写邮箱。
    Claude Code 自带的 /usage、/status 里看不到套餐额度也是这个原因——看额度用本网页。
  · 同一个号最好只在一台电脑上用，不同电脑用不同的号；同时从多个网络出口用同一个号更容易触发风控。
  · 出口 IP：网页顶部显示当前出口 IP（连 api.anthropic.com 的 trace 查，走的就是 claude 连 Anthropic 那条线，不带 token）。正常绿色；
    如果变成国内 IP 或查不到，会红色告警，并拦下「查用量」——确认代理走的是外区节点再查（可强制继续）。
    命令行看当前出口：cct ip；网络排查（TUN/代理/出口IP是否一致/连通性）：cct doctor
    顶部显示的是 claude 用当前号时实际走的线，后面会写明走的是哪条（有问题会变成红条）：
      「走第三方 API 地址 …」= 设了 ANTHROPIC_BASE_URL，claude 不直接连 Anthropic，cct 的号和用量都不作数（红）；
      「走终端的代理环境变量 …——这是「某号」的中转」= 终端里设的代理是别的号的，会借用它的 IP（红）；
      「直连 → 被 TUN / VPN 接管」；「直连：开着系统代理，但 claude 不认」（红 / 琥珀）；
      「中转 …，但连代理这一段被 TUN 送去绕了一圈」= 入口没设直连，会变慢（琥珀）。
    以及：
      「走中转 p12345a」= 这个号绑的代理；「走终端的代理环境变量 …」；「走直连（开了 TUN 就走 TUN）」。
    和 Claude Code 一样不认「系统代理」设置（Clash 只开了「设置为系统代理」时，claude 其实是直连的）。
    「终端的代理环境变量」按启动换号台的那个终端算；IDE 里的 claude 一般也继承同样的终端设置。
  · 联网情况：只有你主动点某个号的「查用量」，或添加账号验证 token 时，才会连 Anthropic 官方服务器
    （api.anthropic.com）查那一个号，是一条 1 token 的请求；其余功能都只在本机。不连任何第三方服务器，
    也不会一次把所有号都查一遍。
  · 想改程序：看 DEVELOP.md。
