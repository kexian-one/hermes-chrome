# 10 — 部署指南(全新电脑从零搭建)

把这套系统部署到一台**全新 Windows 电脑**的完整步骤。读完应该能从空机器跑到飞书 @bot 收到回复。

> 适用平台:**Windows 10 / 11**(代码也跑 macOS/Linux,但浏览器 / 注册表步骤是 Windows-only)

**关于安装路径**:整篇文档用 `<INSTALL_DIR>` 代表你选的安装目录(项目根)。常见选择:

- `C:\Users\you\all-in-ai`(家目录,推荐)
- `D:\projects\all-in-ai`(单独的项目盘)
- 任何你喜欢的位置,**只要是绝对路径就行**

装在哪都可以,**代码不依赖任何特定路径**。`config.yaml` 里填的就是你这台机器的实际路径。文档里看到 `<INSTALL_DIR>` 时,自行替换成你的真实路径。

---

## 0. 总览

部署完成后,这台机器上会有:

```
master 进程 (Python)
   ├── 飞书 bot 长连接 (lark-oapi)
   ├── 6 个 worker 子进程 (b1-b6,按需启动)
   └── 后台 loop: cron / log rotation / git pull / health check

6 个浏览器实例 (每个对应一个 1688 账号 + 一个 open-claude-in-chrome 扩展)
   ├── b1: Chrome
   ├── b2: Edge
   ├── b3: Brave
   ├── b4: Vivaldi
   ├── b5: Opera
   └── b6: 另一个 Chromium 变种

6 套 open-claude-in-chrome MCP server (Node.js)
   └── deploy/oicc-b{1..6}/host/mcp-server.js,监听 18765-18770 端口
```

---

## 1. 装基础依赖

按顺序装 **5 个东西**,每个都要在 PATH 里:

### 1.1 Git for Windows
- 下载:<https://git-scm.com/download/win>
- 装完打开新 PowerShell,跑 `git --version` 应能看到版本号

### 1.2 Python 3.11+(项目要求 ≥3.11,本机用 3.13 验证过)
- 下载:<https://www.python.org/downloads/windows/>
- 安装时**勾上 "Add Python to PATH"**
- 验证:`python --version`(应输出 `Python 3.13.x` 或类似)

### 1.3 Node.js 18+
- 下载:<https://nodejs.org/>(选 LTS)
- 验证:`node --version` 和 `npm --version`

### 1.4 6 个浏览器
按下表装,每个独立浏览器(不要重复装 Chrome 多次):

| Worker | 浏览器 | 下载 |
|---|---|---|
| b1 | Google Chrome | <https://www.google.cn/chrome/> |
| b2 | Microsoft Edge | (Windows 自带) |
| b3 | Brave | <https://brave.com/> |
| b4 | Vivaldi | <https://vivaldi.com/> |
| b5 | Opera | <https://www.opera.com/> |
| b6 | 另一个 Chromium(例如 Edge Dev、360极速、QQ 浏览器) | 自选 |

**注意**:每个浏览器用不同**指纹**(UA、画布指纹、cookie 池),目的是降低 6 个买家账号被关联的风险。所以建议真的用 6 个不同的浏览器内核,而不是 6 个 Chrome profile。

### 1.5 PowerShell 5.1
Windows 自带,不用装。验证:`$PSVersionTable.PSVersion`(应 ≥ 5.1)。

---

## 2. 拉项目代码

挑一个你喜欢的位置(下面以 `C:\Users\you\` 为例,你随意换):

```powershell
cd C:\Users\you\
git clone <你的项目仓库 URL> all-in-ai
cd all-in-ai
```

之后 `<INSTALL_DIR>` 就是 `C:\Users\you\all-in-ai`。

> 后面所有命令都默认在 `<INSTALL_DIR>` 下执行。
> 如果用 Task Scheduler 起 master,**起始目录必须填这个路径**(详见 §10)。

### 2.1 装 Python 依赖

```powershell
pip install -e .
# 或者只装运行依赖:
pip install openai mcp pyyaml croniter lark-oapi
```

跑一遍 pytest 验证环境 OK:

```powershell
pip install pytest pytest-asyncio
python -m pytest tests/ -q
```

期望看到 `186 passed`(或更多)。任何 `failed` 都说明环境装错了或代码有问题,**先解决再继续**。

---

## 3. 装 6 个浏览器扩展(`open-claude-in-chrome`)

### 3.1 一键克隆 6 个实例

```powershell
cd <INSTALL_DIR>\deploy
powershell -File clone-oicc.ps1 -Count 6
```

这一步会:
1. 把 `open-claude-in-chrome` 克隆 6 份到 `deploy/oicc-b1` … `deploy/oicc-b6`
2. 给每个写 `config.json`(端口 18765-18770)
3. 生成每个的 `.cmd` 启动器

### 3.2 给每个实例装 Node 依赖

```powershell
for ($i = 1; $i -le 6; $i++) {
    Push-Location "oicc-b$i"
    npm install
    Pop-Location
}
```

### 3.3 在每个浏览器里加载对应扩展

**关键步骤:每个浏览器要加载它对应的那个 `oicc-bX` 目录,不能搞混!**

以 Chrome (b1) 为例:

1. 浏览器地址栏输入 `chrome://extensions`
2. 右上角开启 **"开发者模式"**
3. 点 **"加载已解压的扩展程序"**
4. 选 `<INSTALL_DIR>\deploy\oicc-b1` 这个**目录**(不是子目录、不是某个文件)
5. 加载成功后,扩展会显示一个 **ID**(类似 `abcdefghijklmnop`)—— **把这个 ID 抄下来,后面要用**

重复给其他 5 个浏览器,扩展地址栏分别是:
- Edge: `edge://extensions`
- Brave: `brave://extensions`
- Vivaldi: `vivaldi://extensions`
- Opera: `opera://extensions`
- 第 6 个浏览器:对应的 `xxx://extensions`

### 3.4 注册 Native Messaging Host

每个浏览器扩展要和本地 Node 进程通信,需要在注册表里告诉浏览器"这个扩展 ID 对应这个 mcp-server.js"。

把 3.3 抄下来的 6 个扩展 ID 填进下面的命令:

```powershell
powershell -File register-native-host.ps1 -Browser Chrome   -Instance 1 -ExtensionId <b1 的 ID>
powershell -File register-native-host.ps1 -Browser Edge     -Instance 2 -ExtensionId <b2 的 ID>
powershell -File register-native-host.ps1 -Browser Brave    -Instance 3 -ExtensionId <b3 的 ID>
powershell -File register-native-host.ps1 -Browser Vivaldi  -Instance 4 -ExtensionId <b4 的 ID>
powershell -File register-native-host.ps1 -Browser Opera    -Instance 5 -ExtensionId <b5 的 ID>
powershell -File register-native-host.ps1 -Browser Chromium -Instance 6 -ExtensionId <b6 的 ID>
```

> **不需要管理员权限** —— 这些注册表项写在 HKCU(当前用户),不动 HKLM。
> 想偷懒:把 ID 写进 `deploy\oicc-b1\extension-id.txt`,脚本会自动读。

### 3.5 重启浏览器,验证扩展连接

- 关掉每个浏览器,重新打开(Chromium 系浏览器只在启动时读 native messaging 注册)
- 点扩展图标看弹窗:应该显示 "Connected" 或类似状态
- 如果显示未连接 → 检查 `register-native-host.ps1` 的输出 + 扩展 ID 是否一致

详细的 `open-claude-in-chrome` 调试看 [deploy/README.md](../deploy/README.md)。

---

## 4. 登录 6 个 1688 买家账号

每个浏览器**手动登录一个**不同的 1688 买家账号:

1. 打开浏览器(比如 b1 = Chrome)
2. 访问 <https://www.1688.com>
3. 用对应账号登录
4. **关掉浏览器**(登录态保存在 user profile 里,下次启动自动登录)

> **关键约束**:这 6 个账号用的浏览器之后会被 master 进程的 `restart_browser` 命令杀掉并重启。所以**不要拿 b1 那个 Chrome 当你日常浏览器用**,否则你正在看的网页会被 kill 掉。

---

## 5. 配 `config.yaml`(密钥 + 飞书 + 路径)

```powershell
cd <INSTALL_DIR>
copy config.example.yaml config.yaml
notepad config.yaml
```

按下面 5 块逐个填。

### 5.1 LLM 密钥(`llm:` 块)

这是最关键的一块,**两个模型必填**:

```yaml
llm:
  multimodal:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen3-vl-max
    api_key: sk-XXXXXXXXXXXXXX     # 阿里云 DashScope key

  reasoning:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen3.6-plus            # 注意:必须用按量付费 key,coding plan key 调不了
    api_key: sk-XXXXXXXXXXXXXX
```

去阿里云 DashScope 控制台开 key:<https://dashscope.console.aliyun.com/apiKey>

> 不用阿里云?把 base_url 换成 deepseek / OpenAI / GLM 等 OpenAI 兼容接口都行。

### 5.2 全局 machine_name

```yaml
machine_name: pc-jianghu      # 这台机器在所有卡片标题里的标签
```

### 5.3 路径(全部绝对路径,相对路径会被拒绝)

```yaml
skills:
  dir: <INSTALL_DIR>\skills
  repo_url: ""                     # 想自动 git clone 就填 URL
  pull_interval_secs: 1800

knowledge:
  enabled: true
  root: <INSTALL_DIR>\knowledge
  is_merger: false                 # 全集群只 1 台填 true
  consolidate_cron: "0 2 * * *"
  repo_url: ""
  pull_interval_secs: 300
```

### 5.4 浏览器配置(给 `restart_browser` 命令用)

`executable` 是**绝对路径**到浏览器 exe:

```yaml
browsers:
  b1:
    name: chrome
    executable: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    warmup_url: "https://work.1688.com"
  b2:
    name: edge
    executable: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
    warmup_url: "https://work.1688.com"
  # b3-b6 同理,填各浏览器真实路径
```

> **找 exe 路径**:打开任务管理器 → 找到该浏览器进程 → 右键"打开文件位置"。

### 5.5 飞书机器人(`bots:` 列表)

#### 5.5.1 先去飞书开放平台创建应用

1. 打开 <https://open.feishu.cn/app>
2. 创建"企业自建应用"
3. 在"凭证与基础信息"里抄 **App ID** 和 **App Secret**
4. 在"权限管理"里加权限:
   - `im:message`(收消息)
   - `im:message.group_at_msg`(收 @bot 的群消息)
   - `im:message:send_as_bot`(发消息)
   - `im:message.group_msg`(读群历史消息 — 让 bot "重试一下" 这类无上下文指令能从聊天回滚找到原任务)
   - `im:resource`(如果想发文件)
5. 在"事件与回调" → "事件订阅"里选 **"长连接模式"**(不需要 webhook URL)
6. 订阅事件:**`im.message.receive_v1`**(必选)+ **`card.action.trigger`**(按钮回调)
7. 在"应用发布"里发布版本(发版给租户管理员审批,审批后才能拉进群)

#### 5.5.2 把 bot 拉进群,拿群的 chat_id

1. 在飞书里建群(或选已有群)
2. 群设置 → 群机器人 → 添加你刚发布的应用
3. 拉进去之后,**群设置里最下面**会有 "**会话 ID:oc_xxxxx...**" —— 这就是 `alert_chat_id`

#### 5.5.3 填进 config.yaml

```yaml
bots:
  - type: feishu
    enabled: true
    app_id: cli_xxxxxxxx           # 飞书 App ID
    app_secret: xxxxxxxx           # 飞书 App Secret
    authorized_user_ids: []        # [] = 允许所有人 @bot;填 ou_xxx 限制白名单
    is_alert_target: true          # 这个 bot 收所有主动告警
    alert_chat_id: oc_8dfa8e4aa... # 5.5.2 里抄的 chat_id
```

**多个 bot**(可选):往 `bots:` 列表里追加。每个 bot 要不同的 `app_id`。只能**一个** bot 设 `is_alert_target: true`。

---

## 6. 第一次启动

### 6.1 启动 6 个浏览器(可以不全开,先试 1 个)

启动 b1 的浏览器(Chrome),让它**保持开着**:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" https://work.1688.com
```

这是为了唤醒 oicc-b1 扩展的 service worker。

### 6.2 启动 master

```powershell
cd <INSTALL_DIR>
python -m agent.master
```

正常启动输出大概长这样:

```
[zombies] killed 0 zombie oicc processes
[health] b1=✓ b2=✗(connection refused) ...        # 没开的浏览器会 ✗,正常
Master cron loop started. polling=60s
[bot thread] patched ws_client.loop: ...
[knowledge] merger loop started. cron='0 2 * * *' # 只 is_merger=true 才有
```

### 6.3 在飞书 @ bot 测试

去你刚才拉进 bot 的那个群,@bot 发:

```
查状态
```

期望收到一张蓝色卡片,标题 `[pc-jianghu] Worker 状态`,内容里列出 b1-b6 的状态。

如果没回:
- master 日志里有没有 `[bot thread] failed: ...`?
- bot 是否真的在群里?(右下角群成员列表能看到 bot)
- 你的 open_id 在不在 `authorized_user_ids` 白名单?(如果设了白名单)

---

## 7. 跑第一个任务

打开 b1 浏览器,确保已登录某个 1688 买家账号。在群里 @bot:

```
b1 抓 1688 申请中发票
```

bot 解析后会派 worker b1 跑 `fapiao-1688` skill,期望:
- 立即收到一张 "[pc-jianghu] 已派发" 卡片
- 几分钟后浏览器自动跳到 1688 发票页,自动滚屏抓数据
- 跑完后收到 "[pc-jianghu] b1 任务完成" 卡片
- 产出 CSV 文件存在 `<INSTALL_DIR>\outputs\b1-<时间戳>\` 下

---

## 8. 设定时任务

在群里 @bot:

```
每天 16:00 让 b1 抓 1688 申请中发票
```

bot 把 cron 写进 `state/schedule.yaml`,以后每天 16:00 自动触发。结果**回到你创建定时的那个群**(不是 alert_chat_id),前提是 bot 还在那个群里。

查看现有定时:`看定时任务`
删除定时:`删掉 #3`

---

## 9. 常用 @bot 指令(快查表)

| 指令 | 作用 |
|---|---|
| `查状态` | 列出 6 个 worker 状态 |
| `看 b3 日志` | 看 worker 最近 50 行日志 |
| `今天的统计` | 当日成功/失败任务数 |
| `b2 抓发票` | 派 b2 跑匹配的 skill |
| `让 b3 给商家发催开票` | 派 b3 跑 fapiao-1688-chase |
| `b2 去淘宝看订单` | freeform 模式(没匹配 skill 时兜底) |
| `重启 b3` | 重启 worker 进程 |
| `重启 b3 浏览器` | 重启 b3 对应的浏览器 |
| `重启你自己` | 重启 master 主进程(re-exec) |
| `暂停所有` / `继续` | toggle 全局调度 |
| `每天 16:00 让 b2 跑 fapiao-1688` | 加定时 |
| `看定时任务` | 列出所有定时 |
| `删掉 #3` | 删 #3 定时 |
| `看 skill 列表` | 列已装 skill |
| `更新 skill` | git pull skills 目录 |
| `查 knowledge 滑块` | 查知识库 |
| `/help` | 完整帮助 |

---

## 10. 设开机自启动(可选)

让 master 开机自动跑。用 Windows 任务计划程序:

1. 打开"任务计划程序" → 创建任务
2. **常规**:用户当前用户;勾"使用最高权限运行"
3. **触发器**:登录时启动
4. **操作**:启动程序
   - 程序:`C:\ProgramData\miniconda3\python.exe`(或你的 python 路径)
   - 参数:`-m agent.master`
   - 起始于:`<INSTALL_DIR>`(**很重要,必须填**)
5. **条件**:取消"只在计算机使用交流电源时运行"

> **重要**:不要在 systemd / 任务计划下让 cwd 变成奇怪的目录。master 现在用 `WORKER_PROJECT_ROOT` 环境变量传递项目根,但前提是它自己启动时知道项目根 —— 任务计划里**必须设"起始于"**。

---

## 11. 故障排查

### 11.1 飞书 bot 收不到消息

- 飞书开放平台 → 应用 → 事件与回调 → 长连接调试:看有没有事件推过来
- 检查权限:`im:message` + `im:message.group_at_msg` 必须有
- 应用发布:版本必须**通过审批**才能用
- 白名单:`authorized_user_ids: []` 是允许所有人;填了具体 ID 就只那些人能用

### 11.2 浏览器扩展显示 "Disconnected"

- 重启浏览器(`oicc-` 系列必须重启浏览器才会重新读 native messaging 注册)
- 检查注册表是否有对应键:`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b1`
- 检查扩展 ID 是否和注册时填的一致(扩展 ID 重新 load unpacked 后会变!)

### 11.3 worker 跑起来但卡死

- @bot 发 `看 bX 日志` 看最近 50 行
- 滑块卡住会自动告警到 `alert_chat_id`(等约 30s)
- 长时间不动 → master 会触发 idle watchdog 杀掉 worker(默认 10 分钟,可在 SKILL.md 里改 `max_idle_minutes`)

### 11.4 输出文件没收到

- 检查 `outputs/bX-<时间戳>/` 目录有没有文件 —— 文件**永远会写到磁盘**
- 飞书 bot 现在**不会自动发文件**(需要实现 FeishuChannel.send_file,见 [docs/13](#) 未实现)
- 短期方案:任务完成卡片里有任务标识,@bot 发 `看 bX 日志` 拿结果路径

### 11.5 端口冲突

`config.json` 里的端口 18765-18770 被别的进程占了?查:

```powershell
netstat -ano | findstr "18765 18766 18767 18768 18769 18770"
```

---

## 12. 多机部署 / 集群

要在第 2 台、第 3 台机器再装一套:

1. 重复本文档 1-9 步(不要复制 `config.yaml` —— 每台机器的 `machine_name` 和飞书 app_id 都要**不同**,不然飞书会冲突)
2. 在第 1 台机器上把 `is_merger: true`(全集群只能 1 台 merger),其他全 false
3. 共用 git 仓库:
   - `skills/` 用同一个 git repo → 自动同步(配 `skills.repo_url`)
   - `knowledge/` 用同一个 git repo → merger 机自动合并各机器的 by-machine → curated → push

**不做跨机协调**(永久决策,见 [DOC/00-context-and-goals.md](00-context-and-goals.md)),所以每台机器独立运行,只通过 git 仓库异步同步。

---

## 13. 卸载

```powershell
cd <INSTALL_DIR>\deploy
powershell -File uninstall.ps1
```

会清掉 6 个浏览器的 native messaging 注册 + 删 `oicc-b{1..6}` 目录。之后:
- 在每个浏览器的 `xxx://extensions` 里手动移除扩展
- 删项目根目录 `<INSTALL_DIR>\`
- 删飞书后台的应用(可选)

---

## 附录 — 文件位置速查

| 路径 | 内容 |
|---|---|
| `<INSTALL_DIR>\config.yaml` | 主配置(密钥 / 飞书 / 路径) |
| `<INSTALL_DIR>\state\schedule.yaml` | 定时任务(bot 写) |
| `<INSTALL_DIR>\logs\` | worker 日志 (按时间戳) |
| `<INSTALL_DIR>\outputs\bX-<时间戳>\` | 任务产出文件 |
| `<INSTALL_DIR>\skills\` | SKILL.md 技能库 |
| `<INSTALL_DIR>\knowledge\` | 知识库(by-machine + curated) |
| `<INSTALL_DIR>\deploy\oicc-b{1..6}\` | 6 套 open-claude-in-chrome 实例 |
| `HKCU\Software\<浏览器>\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.bX` | native host 注册 |
