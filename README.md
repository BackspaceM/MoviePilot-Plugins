---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 07f272cb44def02a5cda2469aa4dcd95_be93a0b5dc3b11f19280525400393706
    ReservedCode1: c5vBKylEv42srztHeVsXQcfm2b7HsIZQe0nODAV4QpPYJc+aml7fcatbrHt7miT2VTdvdSxhEi4ar+uo3JvTzU+j3x1kRD2xROQmOYCjjERla8bSZ8ybp/nuXmu5kLYrgpU1/4rFShDnrpIWiLm8OV1y5NYzBziv7Ou6ootCkUiiJnjlC+d5mYRDIM0=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 07f272cb44def02a5cda2469aa4dcd95_be93a0b5dc3b11f19280525400393706
    ReservedCode2: c5vBKylEv42srztHeVsXQcfm2b7HsIZQe0nODAV4QpPYJc+aml7fcatbrHt7miT2VTdvdSxhEi4ar+uo3JvTzU+j3x1kRD2xROQmOYCjjERla8bSZ8ybp/nuXmu5kLYrgpU1/4rFShDnrpIWiLm8OV1y5NYzBziv7Ou6ootCkUiiJnjlC+d5mYRDIM0=
---





# HDWinG 辅种插件（MoviePilot V3）

针对 hdwing.org（HDWinG / 影音天地）的辅种插件：扫描下载器中已完成的任务，在站点按发布名检索同源种子，逐文件校验一致后下载原种加入做种。

## 目录结构

```
hdwingcrossseed-plugin/
├── plugins.v3/
│   └── hdwingcrossseed/
│       ├── __init__.py        # 插件主体（V3 契约钩子、配置表单、运行流程、API）
│       ├── hdwing.py          # 站点客户端：HTTP、页面解析、.torrent 解析、名称归一
│       └── rank.py            # 候选排序：相似度优先、体积差异次之
├── tests/v3/hdwingcrossseed/
│   └── test_plugin.py         # 15 个离线用例（解析、匹配、缓存、V3 契约）
├── package.v3.json            # 仓库根插件索引（当前仅含本插件条目）
├── .gitignore                 # 排除 releases/、缓存与本地凭据文件
└── install.sh                 # 生成符合宿主约定的 Release 包（支持 --publish）
```

## 发布到 GitHub（作为自定义插件仓库）

本目录本身就是 MP V3 自定义仓库的最小结构，可直接作为仓库根：

```
<your-repo>/
├── package.v3.json            # 插件索引（本包仅含 HDWinGCrossSeed 一条）
├── plugins.v3/
│   └── hdwingcrossseed/       # 插件源码
└── releases/v3/               # 打包产物，已在 .gitignore 中排除，仅作 Release 资产上传
```

发布步骤：

```bash
# 1. 提交源码与索引（Cookie 不会入库，详见下方"敏感信息"）
git init && git add . && git commit -m "feat: HDWinG cross-seed plugin"
git branch -M main && git remote add origin git@github.com:<owner>/<repo>.git
git push -u origin main

# 2. 生成发布包（自动读取 package.v3.json 版本，命名符合宿主约定）
bash install.sh

# 3. 建 Release 并上传资产（tag 与资产名必须成对，否则 MP 识别不到）
git tag HDWinGCrossSeed_v1.0.0 && git push origin HDWinGCrossSeed_v1.0.0
gh release create HDWinGCrossSeed_v1.0.0 releases/v3/hdwingcrossseed_v1.0.0.zip \
  --title "HDWinGCrossSeed_v1.0.0" --notes "MoviePilot 插件首版"
# 或一条命令：bash install.sh --publish
```

命名规范（宿主安装逻辑强校验）：

| 项 | 规则 | 本插件示例 |
| --- | --- | --- |
| Release tag | `{插件类名}_v{版本}` | `HDWinGCrossSeed_v1.0.0` |
| Release 资产 | `{tag 全小写}.zip` | `hdwingcrossseed_v1.0.0.zip` |
| zip 内部 | 根目录直接是 `__init__.py` 等文件 | 无外层目录 |

## 图标

`icons/hdwingcrossseed.png`（256×256，圆角底 + 循环箭头）随仓库一起上传，`package.v3.json` 的 `icon` 使用 raw 绝对地址：

```json
"icon": "https://raw.githubusercontent.com/<owner>/<repo>/main/icons/hdwingcrossseed.png"
```

首次发布前把占位符 `GITHUB_USER` 换成自己的 GitHub 用户名（仓库名若不是 `MoviePilot-Plugins` 也要改）：

```bash
# macOS 与 Linux 通用（-i '' 为 BSD sed 写法，Linux 用 -i）
sed -i '' 's|GITHUB_USER|<你的GitHub用户名>|' package.v3.json
sed -i '' 's|/MoviePilot-Plugins/|/<你的仓库名>/|' package.v3.json
git grep -n "GITHUB_USER" || echo "占位符已全部替换"
```

注意：`icon` 必须是可直接访问的完整 URL（前端不做仓库内相对路径拼接）；mdi 图标名也可用，但自绘 PNG 更稳定。修改后建议 `curl -I` 验证 200。

## macOS 发布步骤

前置：系统自带 git（`xcode-select --install` 可补齐），GitHub CLI 用 Homebrew 安装：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install gh
gh auth login          # 选 GitHub.com → HTTPS → Login with a web browser
```

发布：

```bash
cd ~/Downloads/hdwingcrossseed-plugin     # 换成实际目录
python3 --version                          # 缺 python3 时 brew install python

# 1. 替换图标占位符（见上一节）
# 2. 初始化并推送
git init && git add . && git commit -m "feat: HDWinG cross-seed plugin v1.0.0"
git branch -M main
git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main

# 3. 打包 + 发 Release（脚本已适配 BSD 的 mktemp / shasum）
chmod +x install.sh && bash install.sh
bash install.sh --publish

# 4. 验证
curl -sI https://raw.githubusercontent.com/<owner>/<repo>/main/package.v3.json | head -1
gh release view HDWinGCrossSeed_v1.0.0
```

macOS 注意事项：

- 用 `bash install.sh` 显式调用，避免默认 zsh 下 shebang 行为差异；系统 bash 3.2 已兼容（脚本未用 bash 4 语法）。
- 系统无 `sha256sum`，脚本优先用 `shasum -a 256`；BSD `mktemp -d` 必须带模板，脚本已处理。
- Finder 里按 `Command + Shift + .` 可显示 `.gitignore` 等隐藏文件；不要用 Finder「压缩」生成发布包，命名会不符合宿主约定。
- `.DS_Store` 与 `releases/` 已被 `.gitignore` 排除，`git status` 中不应出现。
- 首次 `git push` 若用 HTTPS，密码处需填 Personal Access Token（classic，勾选 `repo`），GitHub 已不接受账号密码。

## 在 MoviePilot 中安装

1. 进入 `设置 → 插件市场 → 插件仓库`，添加 `<owner>/<repo>`（填 GitHub 仓库地址即可，公共仓库无需 Token）。
2. 等待索引刷新，在插件市场搜索「HDWinG 辅种」，选择版本安装。
3. 首次安装会从 Release 下载 zip；若 Release 未就绪，宿主会回退到按索引文件清单安装，但建议补齐 Release 以避免后续升级失败。
4. 安装后在插件配置页填写**自己的** Cookie（见下节），保存并启用，可用插件页的 `check` 接口验证登录态。

> 升级：改 `package.v3.json` 的 `version` 与 `history`，重新 `bash install.sh`，打新 tag（如 `HDWinGCrossSeed_v1.0.1`）并上传资产，MP 中点击更新即可。

## 敏感信息与 Cookie 脱敏

- 仓库内**不含任何真实 Cookie**：源码、测试、表单提示均为占位符（`<REDACTED>` / `<你的值>`），`git grep -nE "PHPSESSID=[0-9a-zA-Z]{8,}|cf_clearance=[A-Za-z0-9_-]{20,}"` 应为空。
- 真实 Cookie 只写入 MoviePilot 本机数据库（插件配置），`.gitignore` 已排除 `.env`、本地配置文件与 `releases/`。
- 提交前建议执行上面的 `git grep` 自检；若不慎推送过真实 Cookie，立即到站点退出全部会话以作废旧 `hdchina` / `PHPSESSID`，并重登获取新值（`cf_clearance` 与 IP/UA 绑定，需同环境重新获取）。
- 该 Cookie 等同于账号登录态，持有即可操作账号；不要贴在 Issue、日志或截图中。

## 配置

| 配置项 | 说明 |
| --- | --- |
| 启用插件 | 关闭后不注册定时服务、命令与 API 均拒绝执行 |
| 站点地址 | 默认 `https://hdwing.org`，可替换为镜像域名 |
| Cookie | 必填。从浏览器登录后复制整条 Cookie，至少包含 `hdchina` 与 `PHPSESSID`；启用 Cloudflare 的域名还需 `cf_clearance`。**只填自己的值**，本仓库中的示例一律为占位符 |
| 下载器 | 多选，来自宿主 `DownloaderHelper`，仅扫描已完成任务 |
| 执行周期 | 5 段 cron，默认 `0 4 * * *` |
| 单次最大请求数 / 任务数 / 候选数 | 控制对站点的访问压力与单轮耗时 |
| 相似度阈值 | 发布名 token 的 Jaccard 阈值，默认 0.6 |
| 体积容差 | 与站点种子体积的相对偏差上限（百分比） |
| 校验方式 | `strict`：文件名与字节数全部一致；`size`：仅比对总体积 |
| 辅种标签 | 加入下载器时附加的标签，同时作为已处理标记 |
| 跳过标签 / 跳过路径 | 命中即跳过，避免重复处理或误辅 |
| 最小体积 | 小于该体积的任务不处理 |
| 缓存天数 | 失败/跳过结果的冷却天数，成功结果固定冷却 |

Cookie 会明文保存在插件配置中（宿主数据库），请勿外传；`cf_clearance` 通常数小时到数天失效，失效后需重新复制。

## 匹配与辅种流程

1. 遍历所选下载器，取 `completed` 状态任务，转成统一视图（哈希、名称、体积、保存路径、标签、tracker）。
2. 过滤：已在本站 tracker、带跳过标签、低于最小体积、命中跳过路径、缓存未过期、非完成任务全部跳过。
3. 用发布名构造主/备检索词（片名+年份、片名），在 `torrents.php` 检索并按页翻取，识别登录失效（跳登录页）与限流（"访问过于频繁"）后中止当轮。
4. 候选按相似度、体积差异、做种数排序，逐个下载 `.torrent`，解析 `info_hash`、文件清单与总体积。
5. 校验：本地下载器文件清单与站内种子逐文件比对（strict），或仅总体积（size）；同时做体积容差校验。
6. 通过后以原保存路径加入下载器（qBittorrent 指定 `savepath` + 标签；Transmission 指定 `download_dir` + 标签），按 `info_hash` 查重避免重复添加。
7. 结果写入插件数据 `stats` / `history`，可选发送通知；单轮结果按标签过滤去重，失败项进入冷却缓存。

站点侧风险控制：请求间隔可配、单轮请求上限、候选数上限、命中限流立即停止。

## 远程命令与 API

- 命令：`/hdwing_seed`，等价于手动触发一轮辅种（仅响应 `plugin_id` 为本插件或未指定的事件）。
- API（宿主自动加 `/{plugin_id}` 前缀）：
  - `POST /run`：触发一轮，已有任务运行中时返回失败
  - `GET /check`：校验 Cookie 登录态与站点可达性
  - `POST /clear_cache`：清空去重/冷却缓存
  - `GET /status`：返回最近一次运行统计与缓存条目数

## 调度实现

`get_service()` 注册一个 `CronTrigger` 服务，宿主按 cron 调用 `run_scheduled()`。手动/命令触发时优先提交宿主一次性任务（`app.sdk.scheduler.add_plugin_once_job`，`plugin_id` 为类名，`job_id` 固定），宿主不支持时回退到插件自建线程；`stop_service()` 会取消未执行的一次性任务并回收线程。重复触发通过运行标志位拒绝。

## V3 契约要点

- 仅使用 `app.sdk.*`：`plugin._PluginBase`、`schemas`、`schemas.types`、`sdk.events`、`sdk.network`、`sdk.services`、`sdk.logging`、`sdk.scheduler`。
- 未触碰 `app.core` / `app.helper` / `app.utils` / `app.db` / 其他插件模块；不使用 `BackgroundScheduler` 自行建调度器。
- `get_command()` 为静态方法，声明 `EventType.PluginAction`；`handle_plugin_action()` 用 `@eventmanager.register` 消费事件并按 `action` + `plugin_id` 过滤。
- 插件标识：类名 `HDWinGCrossSeed`，配置前缀 `hdwingcrossseed_`，数据 key 独立。
- 官方导入边界门禁（`scripts/check_v3_imports.py`）扫描结果：legacy_imports 与 host_data_access 均为空，无违规。

## 测试

```bash
# 仓库内
pytest tests/v3/hdwingcrossseed -q

# 无宿主环境（生成 app stub 后运行，与 CI 同构）
python3 tests/v3/hdwingcrossseed/run_stub_tests.py
```

覆盖：体积解析（含千分位）、列表页解析、登录/限流识别、.torrent 解析与 info_hash 校验、名称 token 与相似度、候选排序、缓存冷却、标签/路径/体积过滤、文件级与总体积校验、命令/API/服务注册契约、表单与页面渲染。

## 已知限制

- 站点未提供公开 API，依赖 HTML 结构解析；页面结构变更需同步调整 `hdwing.py` 中的正则。
- 未登录或 `cf_clearance` 过期时会整轮中止并给出提示，不做重试风暴。
- 仅支持站点已存在且文件一致的种子，不做跨站重打包或改名辅种。
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
