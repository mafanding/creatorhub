# CreatorHub

> 本地运行的多平台内容管理面板，支持 **抖音 / 小红书 / 快手 / 视频号**。

[在线预览](https://3441293738.github.io/creatorhub/) · [快速开始](#快速开始) · [平台能力](#平台能力) · [基本使用](#基本使用) · [配置](#配置) · [常见问题](#常见问题) · [交流群](#交流群)

> 在线预览由 GitHub Pages 提供，使用脱敏示例数据，仅展示界面与交互；登录、抓取、下载和发布仍需在本地运行。

CreatorHub 使用 Python + FastAPI 提供统一 Web 界面，用于管理账号、监控作品与评论、下载内容、发布作品和接收通知。账号登录态、数据库及媒体文件均保存在本地。

浏览器交互默认使用 CloakBrowser 隐身内核（小红书固定走系统 Chrome CDP），内核由免费开源的 [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 拉起，业务层统一使用兼容的 Playwright API。每个账号使用独立的浏览器 Profile，Cookie、缓存和本地存储互不共享。

## 平台能力

| 功能 | 抖音 | 小红书 | 快手 | 视频号 |
|---|:---:|:---:|:---:|:---:|
| 登录 | 扫码 / 创作者 / Cookie | 扫码 / 创作者 | 扫码 / 创作者 | 扫码 |
| 关键词批量采集 | ✅ 作品 / 评论 / 媒体 | 规划中 | — | — |
| 作品监控 | ✅ | ✅ 创作者 / 关键词 | ✅ | 仅本账号 |
| 评论监控 | ✅ | ✅ | ✅ | 仅本账号 |
| 短视频弹幕监控 | ✅ 播放页 / 创作中心 | — | — | — |
| 内容下载 | ✅ 可选画质 | ✅ 图集 / 视频 | ✅ | — |
| 发布 | ✅ | ✅ | ✅ | ✅ |
| 自动评论 / 回复 | ✅ | ✅ | ✅ | — |
| AI 创作（正文 + 配图卡片） | ✅ | ✅ | ✅ | ✅ |
| 本账号管理 | 作品 / 关注 / 粉丝 / 私信 | 作品 / 关注 / 粉丝 / 私信 | 作品 / 关注 / 粉丝 | 作品 / 数据 / 评论 |
| 通知 | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram |

> 视频号只支持创作者助手中的本账号数据，不支持监控或下载他人作品。

## 快速开始

### 环境要求

- Python 3.10+
- 桌面环境（扫码登录时需要弹出浏览器）
- Google Chrome 稳定版（可选，但小红书扫码登录建议安装）
- Node.js 18+（仅启用小红书 `api` 发布兼容模式时需要）
- 系统 ffmpeg（可选；未安装时自动使用 Python 依赖附带的 ffmpeg）

### 一键启动

克隆项目：

```bash
git clone https://github.com/3441293738/creatorhub.git
cd creatorhub
```

Windows：

```bat
.\start.cmd
```

macOS / Linux：

```bash
chmod +x start.sh
./start.sh
```

首次运行会自动创建虚拟环境、安装依赖和 Chromium、生成 `config.yaml`，随后打开：

```text
http://127.0.0.1:8000
```

> **小红书登录建议：** 尽量使用本机系统中已安装的稳定版 Google Chrome。CreatorHub 会优先通过 CDP 启动系统 Chrome，并为每个账号使用独立的持久化 Profile，不会读取或复用个人 Chrome 的日常 Profile；未安装 Chrome 时会自动回退到可见的 Patchright Chromium。

常用命令：

```bash
.\start.cmd install        # 重新安装或更新依赖
.\start.cmd check          # 环境自检
.\start.cmd --no-open      # 启动后不自动打开页面
.\start.cmd --port 8080    # 使用其他端口
.\start.cmd --reload       # 开发模式
```

> macOS / Linux 将 `.\start.cmd` 换成 `./start.sh`。

<details>
<summary>手动安装</summary>

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
python -m patchright install chromium

# 复制 config.example.yaml 为 config.yaml 后启动
python selftest.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

仅当显式启用小红书 API 发布兼容模式时，才需安装 Node.js 依赖：

```bash
npm install
```

</details>

## 界面预览

> 截图使用脱敏示例数据；界面配色会跟随当前平台切换。

![总览面板](assets/screenshots/overview-douyin.png)

### 更多界面

> 截图统一为 1600 × 1000，点击可查看高清原图。

<table>
  <tr>
    <td width="50%" align="center" valign="top"><strong>小红书浅色主题</strong><br><a href="assets/screenshots/overview-xiaohongshu.png"><img src="assets/screenshots/overview-xiaohongshu.png" alt="小红书总览面板" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>账号与代理</strong><br><a href="assets/screenshots/accounts-proxy.png"><img src="assets/screenshots/accounts-proxy.png" alt="账号登录与代理池" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>作品监控</strong><br><a href="assets/screenshots/monitor-posts.png"><img src="assets/screenshots/monitor-posts.png" alt="作品监控与下载" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>评论监控</strong><br><a href="assets/screenshots/monitor-comments.png"><img src="assets/screenshots/monitor-comments.png" alt="评论监控" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>内容发布</strong><br><a href="assets/screenshots/publish-workflow.png"><img src="assets/screenshots/publish-workflow.png" alt="内容发布与任务队列" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>链接下载</strong><br><a href="assets/screenshots/share-download.png"><img src="assets/screenshots/share-download.png" alt="分享链接解析与下载历史" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>自动评论</strong><br><a href="assets/screenshots/autocomment-rules.png"><img src="assets/screenshots/autocomment-rules.png" alt="自动评论规则与任务记录" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>本账号管理与私信</strong><br><a href="assets/screenshots/account-hub-dm.png"><img src="assets/screenshots/account-hub-dm.png" alt="本账号数据与私信管理" width="100%"></a></td>
  </tr>
</table>

## 基本使用

### 1. 添加账号

1. 在顶部选择平台。
2. 打开左侧「账号」。
3. 选择扫码、创作者登录或 Cookie 登录。
4. 登录完成后，可在账号列表中刷新资料、检测状态或重新登录。

小红书扫码登录只获取主站读取态；需要发布时，请再使用独立的「创作者登录」入口。添加笔记或创作者监控时，建议使用包含 `xsec_token` 的完整链接。

### 2. 监控与下载

- **关键词批量采集**：当前仅支持抖音；一次输入最多 20 个关键词，设置每词作品数、每作品评论数、是否包含二级评论及是否下载媒体；已结束任务支持编辑配置、保留结果去重续跑和 Excel 导出。小红书关键词批量采集尚在规划中。
- **作品监控**：添加创作者主页、作品链接、短链或平台 ID，发现新作品后自动入库。
- **评论监控**：可订阅单条作品，也可监控账号近期作品的评论。
- **短视频弹幕监控**：独立于评论区，按视频内时间轴渐进探测并排序；支持时间范围、关键词、文本长度、点赞数和容量上限过滤，记录持久化到 SQLite；自己的视频走创作中心，公开视频走播放器拦截。
- **链接下载**：粘贴完整分享文案或链接，自动提取地址并下载。
- **历史内容**：新增目标默认只监控订阅后的作品，也可选择回填最近若干条。

下载支持断点续传和失败重试；抖音可选画质，小红书支持图集和视频。

关键词采集是一次性批处理，与持续轮询的作品监控相互独立。搜索结果受平台排序、账号登录态和当前可见范围影响，因此作品数和评论数是采集上限，不代表平台全量数据。

### 3. 发布与转发

- 小红书支持图集、视频和定时发布。
- 抖音、快手和视频号通过对应创作平台发布。
- 已下载的抖音作品可转发到小红书或视频号，小红书作品可转发到抖音；发布前可修改标题、正文和话题。

### 4. AI 创作（正文 + 配图卡片）

「AI 创作」用**提示词 + API** 生成一整篇图文笔记：模型写标题、正文、话题标签，
并填好每张配图卡片的数据；配图由本地模板渲染成 1080×1440 PNG。

**图不是文生图。** 卡片上全是中文密排的表格、金额和截止日 —— 版式引擎每次都能排得
分毫不差且零成本，扩散模型做不到，中文字还经常糊。模型只负责填数据，排版归模板。

流程：

```text
人设档案 + 一句方向 + 真实素材
  ↓  一次 API 调用 → 一段 JSON（标题 / 正文 / 标签 / 卡片数据）
  ↓  发布前预检：字数、风险词（正文和卡片一视同仁）、模板名、张数
  ↓  没过就把问题原样回喂给模型重写，最多 3 轮
  ↓  渲染成 PNG，再验一次（这次带上图）
草稿摆在面板上 → 人看过、改过 → 一键转成发布任务
```

- **人设档案**决定口吻、视觉风格、卡片张数和可用模板。换方向改这份档案，不改代码。
  视觉风格内置 5 套（账本 / 票据 / 街头海报 / 手记 / 制表），避免所有号长同一张脸。
  风格里用到的中文字族（宋体、魏碑、手写体等）取自 macOS 系统字库，Windows 上会
  回落到默认无衬线，版式不变但质感会弱一些。
- **真实素材**是正文里唯一可信的数字来源。不填就不会出现具体金额、比例和截止日 ——
  编一个数字的代价是读者真花的钱。
- **预检不过的草稿不能转发布。** 网址、域名、微信/二维码、极限词、绝对化承诺
  在正文和图片上一视同仁（平台会 OCR 配图）。
- 生成在后台跑，状态存库：页面刷新、服务重启都不会丢进度。
- 转成发布任务后进入统一的发布队列，**不设定时就会尽快发出去**；要挑时间的话
  先去「发布」页给那条任务设一个发布时间。
- 通道在「设置 → AI 内容创作」里配，支持 **OpenAI 兼容接口**和 **Anthropic 直连**；
  和自动评论那组设置分开，可以让写稿用贵模型、评论用便宜模型。每一项留空都会
  回落到自动评论那组，只有一个网关时不用重复填。

> Anthropic 直连需要额外 `pip install anthropic`；默认的 OpenAI 兼容通道只用 httpx，不装也能跑。

### 5. 本账号与通知

- 「本账号」中可同步自己的作品、关注、粉丝和私信，具体能力因平台而异。
- 小红书普通账号可通过新版 Web `/chat` 同步会话与消息；后台按正向随机间隔低频检查，首次启用只建立消息游标，不处理历史积压。
- 私信自动回复支持关键词、排除词、多模板、回复冷却和最大消息年龄。默认生成待审核草稿；选择免审核后也会经过活跃时段、统一写间隔、小时/每日上限及风险冷却。
- 自动回复只使用账号自己的可见 Chrome 会话和页面输入框，不拼装签名请求；提交结果不确定时不会自动重试。
- 私信同步、打开会话与发送复用每个账号唯一的后台工作标签页；Chrome 默认最小化启动，不会为每次操作新建或抢占前台窗口。点击“打开浏览器收发”时才主动恢复到前台。
- 可启用作品健康监控，对零播放、违规或下架状态发送提醒。
- 通知渠道支持 Bark、钉钉和 Telegram。

> 自动评论、回复、私信及关注操作受平台风控影响，建议低频使用。

## 浏览器环境与账号隔离

### 小红书系统 Chrome CDP

- 默认 `xhs_browser_mode: auto`：每个小红书账号启动一个独立、可见的系统 Chrome CDP 会话，并长期复用该账号自己的 Profile、Cookie、缓存和本地存储。
- 项目专用 Profile 与用户平时打开 Chrome 使用的默认 Profile 完全分开；请勿同时用其他 Chrome 进程打开项目的账号 Profile。
- 页面任务加载完整的图片、字体和媒体资源；搜索、滚动、输入、发布和评论统一走可见页面控件，且整台机器同一时刻只执行一个小红书可见操作。
- 同一轮搜索与笔记详情复用账号现有浏览器进程和 Profile，不再每条笔记重新启动内核；页面停留时间会按可见内容长度做有界抖动，滚动、输入、详情间隔和阶段性休息采用有限随机节奏。默认单轮详情量也会在较小范围内变化，显式配置“单轮上限”时仍严格按用户上限执行。
- 机器未安装稳定版 Chrome 时，`auto` 会回退到可见的 Patchright Chromium；账号列表会显示实际后端和回退原因。`cdp` 为严格模式，Chrome 缺失或 Profile 冲突时直接报错。
- 账号代理支持 HTTP、HTTPS、SOCKS5 及账号密码认证。代理不可连接或认证失败时按失败关闭处理，不会静默改走本机直连；建议同一账号长期保持稳定出口。
- 小红书发布和评论默认使用 `browser` 页面模式。提交按钮只点击一次；提交后若浏览器连接中断或缺少成功证据，任务会标记为“结果待确认”，不会自动重试，需先到平台核对。
- 如需直接使用 Patchright，可显式设置 `xhs_browser_mode: patchright`。旧配置值 `playwright` 会自动迁移为 `patchright`。

### 默认：CloakBrowser 隐身内核（小红书除外）

除小红书外，CreatorHub 默认使用
[CloakBrowser](https://cloakbrowser.dev) 作为浏览器内核。它是把指纹改在
Chromium C++ 源码层的隐身内核：UA/Client Hints、Canvas、Audio、WebGL、时区和
语言在渲染管线内部就已经改掉，而不是往页面里注入 JavaScript 补丁，因此不存在
“注入脚本本身被检测出来”的问题，无头模式的画像也是完整的。

CreatorHub 只复用官方 `cloakbrowser` 包的下载、签名校验和 License 校验；浏览器
仍旧由 Patchright 的持久化 context 拉起，账号、Profile、Cookie、代理、LRU 和
风控依然由 CreatorHub 管理，不需要外部商业浏览器或云端账号。该后端沿用账号已有
的 `fp_seed` 生成稳定的内核指纹种子，并显式抬高存储配额，避免常驻 Profile 被
BrowserScan 一类检测判成隐身窗口。

`browser_backend` 现在有三个取值：

- `cloak_browser`：默认值。内核自带完整画像，按 License 自动在 Pro 与 GitHub
  免费版之间切换。
- `fingerprint_chromium`：自备开源指纹内核，见下一节；账号在「环境」里单独选了
  具体内核时，优先于全局默认。
- `local`：Patchright 或系统 Chrome，不做内核级指纹改写。

CloakBrowser 账号与 Fingerprint Chromium 账号走同一套内核级指纹流程：首次登录前
同样会打开「登录前指纹配置」，新环境的首次可见会话仍会附带 BrowserScan 体检
标签；`native` 写操作闸门也把内核级指纹环境视作合规浏览器，不再额外要求系统
Chrome。小红书始终不进入该分支。

#### Pro 与 GitHub 免费版的自动切换

- 配置了 License Key 且服务端校验通过 → 使用 **CloakBrowser Pro · 隐身内核**，
  跟随官方最新构建。
- 没配 Key、Key 无效或过期、License 服务器不可达且本地无缓存，或 Pro 内核暂时
  拿不到 → 自动回退 **CloakBrowser · GitHub 版**（官方发布的免费构建，主站是
  `cloakbrowser.dev`，GitHub Releases 是备用源），不会因此启动失败。
- 直接指定 `cloak_browser_path` 时使用 **CloakBrowser · 自定义内核**，此时不再
  区分 Pro 与免费版。

License Key 的解析顺序是 **页面保存 > `config.yaml` > 环境变量**：

1. Web 面板「CloakBrowser 内核」卡片里保存的值，存在本地数据库，便携部署换机器
   时不必改配置文件；
2. `config.yaml` 的 `engine.cloak_browser_license_key`；
3. 环境变量 `CREATORHUB_CLOAKBROWSER_LICENSE_KEY` 或 `CLOAKBROWSER_LICENSE_KEY`。

另外两个环境变量会影响内核解析，排查“配了有效 Key 却拿不到 Pro”时先看它们：

- `CLOAKBROWSER_BINARY_PATH`：等价于 `cloak_browser_path`，直接指定可执行文件并
  跳过下载，此时版本类型固定为「自定义内核」。
- `CLOAKBROWSER_DOWNLOAD_URL`：指向私有镜像。**一旦设置，官方 Pro 通道会被上游
  停用，且内核不再经过官方签名校验**（只剩镜像自带的哈希）。CreatorHub 会在
  「CloakBrowser 内核」卡片里把这一点说明出来，并且始终禁止用
  `CLOAKBROWSER_SKIP_CHECKSUM` 把哈希校验也关掉。

在页面上删除 Key 只清掉第 1 项，`config.yaml` 和环境变量里的配置仍然生效。接口和
页面只回显打码后的片段，不会返回明文 Key。Key 从官方获取：命令行执行
`cloakbrowser login` 用 GitHub 账号登录可以拿到免费 Key，付费 Pro 在
[cloakbrowser.dev](https://cloakbrowser.dev) 购买。

> **免费 Key 和多账号并发是冲突的。** 只要 Key 校验通过就一律走 Pro 内核，包括
> `cloakbrowser login` 拿到的免费 Key —— 而免费档（`plan == "free"`）只允许
> **1 个并发会话**；而免费构建（Linux/Windows 是 v146，macOS 是 v145）反而没有
> 并发限制。所以
> “配了免费 Key 又要跑多账号”（默认 `max_live_contexts: 6`、`active_accounts: 3`）
> 开箱就会撞上 Pro 授权的并发上限：服务和第 1 个会话正常，第 2 个及之后并发拉起
> 的浏览器会被内核拒绝。CreatorHub 会把上游的英文授权错误翻译成中文提示；并发
> 超限是在 CDP 握手**之后**才判定的，浏览器会自行退出，这类情况由内核写下的授权
> 拒绝记录读回原因，不会只留下一句“浏览器被关闭”。要多并发只有两条路：购买 Pro，
> 或者干脆不配 Key、直接用 GitHub 免费版。

#### config.yaml 配置

```yaml
engine:
  browser_backend: cloak_browser  # 默认值；也可改为 local 或 fingerprint_chromium
  cloak_browser_license_key: ""   # cb_ 开头；留空即使用 GitHub 免费版
  cloak_browser_cache_dir: ./data/cloakbrowser # 内核与授权缓存目录，独立于 ~/.cloakbrowser
  cloak_browser_path: ""          # 已有可执行文件时直接指定，跳过下载
  cloak_browser_allow_headless: true # 无头画像完整，后台监控默认允许无头
  cloak_browser_platform: auto    # auto=跟随宿主系统；也可指定 windows/linux/macos
  cloak_browser_release_channel: stable # stable | preview（preview 仅 Pro 可用）
  cloak_browser_version: ""       # 固定内核版本回滚用，如 148.0.7778.215.2；留空=跟随最新
  cloak_browser_auto_download: true # 首次使用时自动下载内核（约 200MB）
```

与 Fingerprint Chromium 的 `fingerprint_chromium_allow_headless: false` 不同，
CloakBrowser 默认允许无头，因此后台监控任务不必一直弹窗口；扫码登录、发布等可见
操作仍使用有头窗口。

#### 首次使用与离线部署

内核约 200MB。当 `browser_backend` 为 `cloak_browser`（默认值）时，服务启动后会
在后台预热，不阻塞启动；全局用 `local`、只有个别账号选 CloakBrowser 时不会预热，
内核会在该账号首次启动浏览器时下载。也可以随时在账号页的
「CloakBrowser 内核」卡片里手动「检查 / 下载内核」，再用「启动自检」真跑一次
确认内核可用。同一份内核由所有账号共用，不会每个账号各下一份。

- `cloak_browser_auto_download: false` 可以关掉自动下载，之后需要先在页面手动
  下载内核。
- `cloak_browser_path` 指向已有的 CloakBrowser 可执行文件即可完全跳过下载。
- 内核主站是 `cloakbrowser.dev`，GitHub Releases 是备用源；两者都访问受限时，
  建议先在能联网的环境下载好，再用 `cloak_browser_path` 指定。
- 内核和授权缓存都落在 `cloak_browser_cache_dir`（默认 `./data/cloakbrowser`），
  与宿主机上的 `~/.cloakbrowser` 分开，便携部署随项目目录一起走。

#### 从旧版本升级

- 旧配置里显式写了 `browser_backend: local` 的部署不会被改变，仍然使用
  Patchright / 系统 Chrome。
- 没写这一项的部署会切换到 CloakBrowser。账号会启用新的 Profile 目录
  `<账号 profile_dir>/runtimes/cloak-pro|cloak-free|cloak-custom`，登录态由数据库
  里的 `storage_state` 自动桥接回来，一般不需要重新扫码。
- Profile 按内核版本隔离是有意为之：146 的 Profile 不该被 151 写过的目录顶掉，
  反向降级尤其危险。因此 Pro 与免费版之间切换（新增、过期或清除 Key）同样会换一
  个 Profile 目录，登录态照样从 `storage_state` 桥接。
- 小红书不受影响，仍固定使用系统 Chrome/CDP。
- 切换内核会改变账号的设备画像，建议切换后重新检查登录态和代理出口。

### 可选：Fingerprint Chromium 开源内核（小红书除外）

不想使用默认的 CloakBrowser 时，CreatorHub 也可以把开源的
[`fingerprint-chromium`](https://github.com/adryfish/fingerprint-chromium)
作为其他平台的可插拔 Chromium 运行时。账号、Profile、Cookie、代理、LRU 和风控仍由
CreatorHub 管理，不需要外部商业浏览器或云端账号。小红书始终使用系统 Chrome/CDP：
第三方指纹内核与既有 Profile 混用容易造成 UA/Client Hints、GPU 和站点持久状态不一致，
从而增加设备安全验证；后端、登录接口和账号设置页都会拒绝该组合。

1. 从上游 Release 下载适合当前系统的构建并自行校验文件。
2. 在账号页的「浏览器内核」区域扫描安装目录，或手动添加当前机器上的
   `chrome.exe` / `chrome`。每台机器单独保存本机路径，不依赖固定盘符。
3. 也可以在 `config.yaml` 中配置单个内核或多内核扫描根目录：

```yaml
engine:
  browser_backend: fingerprint_chromium # 全局改用开源指纹内核；不写则保持默认 cloak_browser
  fingerprint_chromium_path: D:/path/to/fingerprint-chromium/chrome.exe
  fingerprint_chromium_root: D:/path/to/browser-kernels
  fingerprint_chromium_allow_headless: false
  fingerprint_chromium_platform: auto
```

4. 重启 CreatorHub，在账号的「环境」设置中选择具体内核版本。
   `browser_backend: fingerprint_chromium` 会把默认指纹内核应用到所有未单独指定
   环境的非小红书账号；只想给个别账号使用时，保留默认的 `cloak_browser`，在账号
   「环境」里单独选内核即可。`browser_backend: local` 则是显式退回 Patchright /
   系统 Chrome。小红书仍固定走系统 Chrome/CDP。

添加新的非小红书账号时，选择具体 Fingerprint Chromium 内核和代理后，会在浏览器首次启动前
打开「登录前指纹配置」。语言、时区、位置和窗口可继续跟随出口 IP 自动生成，也可切换
为自定义；操作系统、浏览器品牌、CPU、WebGL、Canvas、WebRTC 和附加参数同样可编辑。
登录成功后，这套配置与该账号的独立 Profile 一起持久化，后续仍可通过账号行的「指纹」
按钮修改。

该后端使用账号现有 `fp_seed` 生成稳定的 32 位内核指纹种子，并由浏览器内核
统一处理 UA/Client Hints、Canvas、Audio、WebGL、语言和时区；CreatorHub 不会
再叠加 `legacy` JavaScript 指纹脚本。默认强制使用有头窗口，因为上游说明无头模式
只处理了部分无头特征。切换已有账号的浏览器环境会改变其设备画像，建议切换后重新
检查登录态和代理出口。小红书登录和后续任务不会进入该分支，也不会在出现设备验证时
自动刷新、跳转或重试；验证页会保留在可见系统 Chrome 窗口中供账号本人完成，期间
自动任务保持暂停。

每个新的指纹 Profile（以及内核、代理或指纹配置变化后的新环境）首次进入可见登录/
账号浏览器时，会额外打开 `https://www.browserscan.net/zh` 体检标签，供用户核对实际
IP、时区、WebRTC 与浏览器指纹。账号页的「环境检测」可以随时重新打开；页面显示的
“已提示”只表示检测页已打开，不代表目标平台风控一定通过。BrowserScan 是第三方站点，
会看到该环境的出口 IP 和浏览器特征。

项目不捆绑 Fingerprint Chromium 的上游二进制；CloakBrowser 内核则由官方
`cloakbrowser` 包下载并做签名校验。升级任一内核前，建议先备份 `data/profiles/`
并在测试账号上验证兼容性。

## 任务队列与平台风控

Web 面板的「任务队列」统一展示采集、发布、自动评论、账号动作和下载任务，支持按平台、队列类型、状态和关键词筛选。默认启用 `risk_control.mode: conservative`，所有平台操作统一经过持久化调度与风控检查。

### 默认策略

- 同一账号的评论、账号动作、私信和发布共享写操作间隔与额度，立即执行也不会绕过冷却。
- 使用同一网络出口的账号串行访问；未配置代理的账号统一归入 `direct` 出口组。
- `conservative` 模式中的写间隔、小时/每日额度和同出口并发是不可放宽的保护线；配置 `0` 表示沿用保护线。切换为 `custom` 后才完全按自定义值执行。
- 轻读取与重读取分别计时；命中 `403/429/461/471`、验证码或明确风控提示后，会按阶梯进入冷却。
- 被额度、时段、代理状态或冷却拦下的任务保持 `pending`，服务重启后继续恢复；登录态失效时任务会保留并等待重新登录。
- 冷却结束后先进行间隔式轻量探测，连续成功后再逐级恢复。

### 浏览器与网络出口

- 存量账号继续使用 `legacy` 浏览器画像，避免已有 Profile 漂移；新扫码与 Cookie 账号使用 `native` 模式。
- `native` 账号的发布、评论、关注和私信会检查系统 Chrome、有头页面、独立 Profile 及代理出口基线；使用 CloakBrowser 或 Fingerprint Chromium 等内核级指纹环境时，改为检查该内核是否可用。
- 账号页的「测试代理」会记录出口 IP、国家、ASN 和时区；出口漂移或基线过期后，写任务保留在队列，重新验证后再执行。
- 每个账号 Profile 都有跨进程占用保护；同一出口下多个账号集中触发风险时，会启用出口组熔断。

### 风控中心与恢复

「风控中心」集中展示正常、冷却、渐进恢复、登录失效、代理异常和网络熔断账号，并提供触发原因、冷却截止、恢复进度、任务级受阻原因、网络出口及事件时间线。规则页可调整读取间隔、冷却阶梯、恢复探测、写操作额度、出口组熔断和活跃时段；配置保存到本地数据库并立即生效。人工解除、规则修改和人工探测都会写入审计记录。

服务暴露到局域网时，建议设置环境变量 `CREATORHUB_ADMIN_TOKEN`。设置后，风控规则保存、人工探测和解除接口必须携带管理口令；Web 风控中心可通过“设置管理口令”仅在当前浏览器会话中保存它。

完整参数及保守默认值见 [`config.example.yaml`](config.example.yaml) 的 `risk_control` 段。

## 配置

首次启动会从 `config.example.yaml` 生成 `config.yaml`。大部分常用选项也可以在 Web 面板的「设置」中修改。

```yaml
engine:
  scan_interval_seconds: 300         # 默认轮询间隔
  monitor_initial_backfill_count: 0  # 0=只监控新作品，-1=尽可能回填
  worker_pool_size: 2                # 下载并发数
  scan_concurrency: 2                # 抓取并发数
  account_check_interval_seconds: 1800
  media_dir: ./data/media
  work_health_enabled: false

storage:
  db_path: ./data/creatorhub.db

proxies: []  # 不使用代理
# proxies:
#   - http://user:pass@host:port
#   - socks5://user:pass@host:port
```

完整配置及说明见 [`config.example.yaml`](config.example.yaml)。

- `config.yaml`、数据库、登录态和媒体文件默认不会提交到 Git。
- 每个账号使用独立浏览器配置目录；如使用代理，建议为账号绑定稳定的独立代理。
- 数据库字段会在启动时自动迁移，升级后通常无需删除旧数据库。

## 分享链接命令行下载

只解析链接，不访问网络：

```bash
python -m app.engine.share_downloader --links-only "完整分享文案或链接"
```

下载内容：

```bash
python -m app.engine.share_downloader "完整分享文案或链接" -o ./data/media/share -q 1080
```

在 Web 面板中也可以直接使用「链接下载」。

## 数据目录

```text
data/
├─ creatorhub.db   # SQLite 数据库
├─ media/          # 下载内容
├─ cloakbrowser/   # CloakBrowser 内核与授权缓存（约 200MB）
├─ ai_drafts/      # AI 创作渲染出来的配图 PNG（发布任务直接引用这里的文件）
└─ profiles/       # 账号浏览器配置与登录态；内核级指纹环境在各账号的 runtimes/ 下
```

备份项目前，建议一并备份 `config.yaml` 和 `data/`；`data/cloakbrowser/` 是可以
重新下载的内核缓存，不备份也不影响登录态。

## 常见问题

| 问题 | 处理方式 |
|---|---|
| macOS 安装依赖时报 `command /usr/bin/clang++ failed with code 1` | 更新代码后删除旧的 `.venv`，再运行 `./start.sh install`；安装器会先升级 pip/setuptools/wheel。 |
| Patchright 启动失败或找不到浏览器 | 运行 `python -m patchright install chromium` |
| 扫码登录没有弹窗 | 确认当前机器有桌面环境；抖音也可使用 Cookie 登录 |
| 小红书扫码登录出现设备安全验证 | 安装或更新本机稳定版 Google Chrome，并保持同一账号的 Profile 和网络出口稳定；验证页出现后自动任务会暂停且不刷新、不跳转、不自动重试，请在当前可见窗口按平台提示完成 |
| Windows 下出现 Patchright 子进程错误 | 使用单 worker 启动，不要添加 `--workers` |
| 抓取不到作品或评论 | 检查登录态、目标链接和网络状态，必要时重新登录并降低频率 |
| 小红书链接解析失败 | 重新复制包含有效 `xsec_token` 的完整链接 |
| 仅音频仍得到 MP4，或视频没有声音/画质受限 | 重新运行安装命令更新依赖；也可安装系统 ffmpeg 并加入 `PATH` |
| CloakBrowser 内核下载慢或失败 | 确认能访问 GitHub；也可在能联网的机器上下载后用 `cloak_browser_path` 指定，或设 `cloak_browser_auto_download: false` 后在页面手动下载 |
| 多账号运行时提示 License 并发上限 | `cloakbrowser login` 拿到的免费 Key 只允许 1 个并发会话；需要多账号并发请购买 Pro，或删掉 Key 改用 GitHub 免费版 |

仍有问题可提交 [Issue](https://github.com/3441293738/creatorhub/issues)，并附上平台、操作步骤和服务端错误日志。

## Star History

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/3441293738/creatorhub/star-history/assets/star-history-dark.svg">
  <img alt="CreatorHub Star History Chart" src="https://raw.githubusercontent.com/3441293738/creatorhub/star-history/assets/star-history.svg">
</picture>

## 使用须知

本项目用于技术学习和个人内容管理，不提供账号、Cookie、代理或平台数据。使用时请遵守目标平台规则及所在地法律法规，并尊重内容版权和个人隐私。
## 交流群

欢迎加入 **CreatorHub 交流群**，交流使用经验、问题反馈和功能建议。

<p align="center">
  <a href="https://3441293738.github.io/creatorhub/community/">
    <img src="assets/community/live-entry.png" alt="CreatorHub 交流群固定入口二维码" width="280">
  </a>
</p>

<p align="center">
  扫码或点击二维码打开<a href="https://3441293738.github.io/creatorhub/community/">交流群固定入口</a>；微信群二维码到期后会在入口页更新。
</p>

## 赞助商

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">
    <img src="assets/sponsors/ipwo-banner.png" alt="IPWO 爬虫住宅代理" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">IPWO</a>
  提供稳定的住宅代理网络，适用于公开数据采集、接口调试、自动化测试与多地区访问验证等合规场景。
  支持 HTTP / HTTPS / SOCKS5，优惠码：<code>0201</code>。
  <br>
  请在合法授权并遵守目标站点条款的前提下使用。
</p>

## 友链

- [LINUX DO](https://linux.do/) — 感谢社区提供的帮助与支持。
