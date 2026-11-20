# DYKSDownLoadPC · PC 桌面端批量下载器

输入**关键字**，自动搜抖音 / 快手的视频，按**最高质量**无水印下载到本地，
并按「当天日期_关键字」建目录、抖音快手分文件夹存放。

```
关键字：象棋
下载目录：D:\视频
数量：20
方向：竖屏

📁 D:\视频\2026年11月20日_象棋\
   抖音\   季哥象棋司马流，对手开局没马马士气减半.mp4   11.8MB  1080×1924
   快手\   我真记不清是真的还是假的了…mp4               2.1MB  720×1280
```

---

## 🖥 可视化界面（推荐）

### 一键启动（Windows）

```
C:\Users\49059\Desktop\抖音快手批量下载.bat
```

双击就开终端跑起来，浏览器自动打开 `http://127.0.0.1:8790/`。

> ⚠️ **bat 文件必须是纯 ASCII**（或写成系统 ANSI 代码页 GBK）。
> 存成 UTF-8 的话，cmd 按 GBK 读 → 满屏乱码 + 命令被拆坏
> （本机 cmd 是 UTF-8 模式，存 GBK 同样乱码 → **纯 ASCII 最保险**）。
> 中文提示交给 Python 输出（bat 里 `chcp 65001` + `PYTHONIOENCODING=utf-8`）。

### 手动启动

```bash
git clone git@github.com:Yirito/DYKSDownLoadPC.git
cd DYKSDownLoadPC
pip install -r requirements.txt
python -m playwright install chromium     # 装 Chromium（必须）

python webui.py          # 自动打开浏览器 → http://127.0.0.1:8790
```

> 端口被占用时会明确报错退出（曾经有残留进程占着端口，
> 导致「改了代码不生效」）。换个端口：`python webui.py --port 8791`。
> 本项目默认 **8790**（避开「一键签到」8765、「workbench回测」8000）。

界面里能做的事：

| 区块 | 说明 |
|---|---|
| 关键字 / 每个平台条数 | 搜索词和数量 |
| 下载目录 + 「选择文件夹…」 | 弹 **Windows 原生文件夹对话框**（系统自带的那个选择框，和「一键签到」看板同一套实现）；旁边的「网页浏览」是页内浏览器的兜底 |
| 画面方向 | 竖屏（默认）/ 横屏 / 都要 |
| 平台 | 抖音 / 快手，可单选 |
| 登录 / 验证状态 | 实时显示能不能搜、卡在哪一步，**每个平台一个「登录」按钮** |
| 下载进度 | 每条一行：平台、标题、分辨率、大小、进度条、状态 |
| 运行日志 | 实时输出搜索/下载/验证过程 |

**设置永久化**：下载目录、条数、画面方向、平台选择都存在项目根目录的
`config.json`，下次打开自动回填，改了就立刻存。

**登录怎么弄**：在「登录 / 验证状态」那栏点对应平台的 **「登录」** 按钮 →
弹出浏览器 → 抖音扫码、快手拖滑块。
**判定方式**：后台拿浏览器里的**实时 cookie** 真发一次搜索请求，
接口认了才算成功 —— **不截图、不猜**，一成功就**自动帮你把窗口关掉**。
**点「开始下载」不会再自动弹窗** —— 没登录的平台会被直接跳过并在日志里说明。

```bash
python webui.py --port 9000 --no-browser   # 换端口 / 不自动开浏览器
```

---

## ⌨️ 终端版

```bash
# 交互式：依次问关键字 / 下载目录 / 数量 / 横竖屏
python dyks_cli.py

# 直接给参数
python dyks_cli.py -k 象棋 -d "D:\视频" -n 20 -o portrait

# 提前把登录/验证做好
python dyks_cli.py --prepare

# 看看当前状态
python dyks_cli.py --status
```

### 第一次要先「准备」一下（见下文「关于登录」）

```bash
python dyks_cli.py --prepare    # 扫码登录抖音 / 过快手滑块
python dyks_cli.py --status     # 随时查看当前状态
```

**过期了不用管** —— 每次搜索前工具会自己探一次，失效就自动重新弹浏览器让你扫码。

### 参数

| 参数 | 说明 |
|---|---|
| `-k/--keyword` | 搜索关键字 |
| `-d/--dir` | 下载目录（默认 `./downloads`） |
| `-n/--count` | **每个平台**下载多少条（默认 10） |
| `-o/--orientation` | `portrait` 竖屏（默认）/ `landscape` 横屏 / `both` 都下 |
| `-p/--platform` | `douyin` / `kuaishou` / `both`（默认） |
| `--prepare` | 提前扫码登录 / 过人机验证 |
| `--status` | 查看登录/验证状态 |
| `--show-browser` | 搜索时显示浏览器窗口 |
| `--dry-run` | 只列清单不下载 |

---

## 关于登录（重要）

**「解析单条视频」不需要登录**（这部分的原理见手机端项目 DYKSDownLoad）；
**但「按关键字搜索」需要过平台的风控**，实测结论如下：

| 平台 | 门槛 | 实测返回 |
|---|---|---|
| **抖音** | **必须登录**（扫码） | `aweme/v1/web/search/item/` → `{"status_code":2483,"status_msg":"请先登录，再继续搜索吧"}` |
| **快手** | **人机验证**（滑块），**不必登录** | GraphQL `visionSearchPhoto` → `{"result":400002,"url":"https://captcha.zt.kuaishou.com/..."}` |

我把所有免登录接口都试过了（`discover/search`、`general/search/single`、
`search/item`、`iesdouyin/*`、`rest/v/search/feed`、GraphQL 各种 operation），
**没有一个能绕过**。所以本项目做的是：

1. **扫码 / 过验证**：打开有头浏览器 → 抖音点登录用手机 App 扫二维码；
   快手把弹出的滑块拖一下即可（顺便还会截一张图保存到 `profiles/` 并自动打开）
2. **存本地**：登录态写进本机浏览器 profile（`profiles/`，已在 .gitignore 里，**永不上传**）
3. **检测过期**：每次搜索前真发一次探测请求
   （抖音看是不是又返回 2483，快手看是不是又返回 400002）
4. **过期自动重弹**：探不过就自动打开浏览器让你重新扫码，不用手动管

> 快手这边只要过了滑块、cookie 还在，通常能顶很久不用再过。
> 抖音的登录态一般能撑几天到几周。

---

## 工作原理

### 搜索（浏览器驱动 + 拦截）

抖音的搜索接口要 `a_bogus` 签名，快手的风控参数是 `__NS_hxfalcon` ——
**都是浏览器自己算的**，所以最稳的做法是让真实浏览器去搜，我们把响应拦下来：

| 平台 | 拦截的请求 | 结果字段 |
|---|---|---|
| 抖音 | `aweme/v1/web/search/item/` | `data.aweme_list[]` |
| 快手 | `graphql`（`visionSearchPhoto`） | `data.feeds[]` |

### 画质：为什么取的是最大那一档

抖音的 detail 响应里带**一组转码档**（`video.bit_rate[]`，实测一条视频有 21 档），
外加 `play_addr` / `play_addr_h264` / `play_addr_265`。

⚠️ **`url_list[0]` 不保证是最高清**（社区实测有时是 720p、有时 480p，顺序不固定），
所以本项目**枚举全部候选，按 `data_size` → 分辨率挑最大的**：

```python
cands.sort(key=lambda x: (x[0], x[1]), reverse=True)   # 体积 → 分辨率
```

> `video.download_addr` 是**带水印的裁剪版**，不参与比较。

快手那边不用这招：拦截到的 `.mp4` 响应自带 `content-length`，取最大的即可。

**实测一条 3 分钟竖屏视频的 21 档：**

| gear_name | 分辨率 | 体积 |
|---|---|---|
| normal_1080_0 | 1080×1924 | **11.76 MB** ← 最高档 |
| 1080_1_1 | 1080×1924 | 9.02 MB |
| normal_720_0 | 720×1282 | 8.78 MB |
| …（共 21 档） | | |

> 💡 **「视频怎么这么小」多数不是被压小，是平台自己压的。**
> 平台存储的上限就是那个码率，任何工具都拿不到更高清 —— 因为平台没存更高清的版本。

### 下载

本地 httpx 流式写盘，带实时进度条；抖音自动加 `Referer: https://www.douyin.com/`
（抖音 CDN 硬性要求，否则 403）。先写 `.part` 再改名，避免半截文件。

---

## 目录结构

```
DYKSDownLoadPC/
├── webui.py             # 本地可视化界面（起服务 + 自动开浏览器）
├── web/index.html       # 界面页面
├── dyks_cli.py          # 终端入口
├── core/
│   ├── engine.py        # 解析引擎（Playwright + 无头 Chromium）
│   ├── search.py        # 搜索（浏览器拦截 + 最高质量选档）
│   ├── auth.py          # 登录/验证：状态检测、扫码、过滑块、过期重扫
│   └── download.py      # 流式下载 + 进度条
├── profiles/            # 本地浏览器 profile（登录态，.gitignore 掉了）
├── requirements.txt
└── .gitignore
```

## 免责声明

仅供个人学习、技术研究与素材参考使用。请遵守抖音/快手的用户协议与相关法律法规，
下载的内容请尊重原作者著作权。
