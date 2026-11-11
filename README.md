# MoviePilot-Plugins

MoviePilot **V3** 第三方插件仓库：<https://github.com/BackspaceM/MoviePilot-Plugins>

包含 16 个面向 MoviePilot V3 的插件，覆盖系统总览、观影统计、媒体库维护、辅种、
插件维护以及一整套 RouterOS 网络管理能力。

## 安装与升级

把本仓库地址填入 MoviePilot 的 `PLUGIN_MARKET`（多个地址用逗号分隔），
插件市场只读取 `main` 分支的 `package.v3.json`。也可以直接 clone 到插件目录。

本仓库是第三方仓库，**没有 GitHub Release 发布包**，所以索引里每个插件都必须
声明 `"release": false`。宿主据此直接从 `plugins.v3/<插件ID小写>/` 拉取源码；
若误设为 `true`，宿主会先去找 `{插件ID}_v{版本}.zip`，失败后才回退，
日志里会出现 `Release 安装失败，回退文件列表安装`。

**升级步骤（按钮 404 时务必按此顺序做完）：**

1. 插件市场刷新索引，把**全部插件**更新到 `package.v3.json` 中的最新版本
   （只更新其中几个，未更新的插件仍会加载旧代码、按钮继续 404）。
2. **重启一次 MoviePilot 容器**：V3 动态路由只在插件加载/重载时注册，
   旧版本注册失败的路由不会自动恢复。
3. 确认容器日志中不再出现 `Error adding plugin route`，且
   `加载插件：<ID> 版本：<新版本号>` 与实际索引版本一致。
4. 回到插件详情页点按钮验证；若仍 404，先查日志里失败路由的类名是否还是旧
   `Response` 普通类——那说明插件文件根本没被更新。

## 插件一览

| 插件 | 说明 |
| --- | --- |
| MP 总览 | 零配置只读看板：系统负载、磁盘水位、订阅、近期入库、下载与服务状态；可作首页仪表盘组件 |
| 观影周报 | 汇总 Emby/Jellyfin/Plex 播放记录生成周报，并给转码诊断建议 |
| 播放调度 | 按在播会话动态调整下载器与 RouterOS 限速 |
| 媒体库刷新 | 按路径精准触发媒体库刷新，带队列与运行日志 |
| 带宽调度 | 带宽方案切换、Queue 队列管理与 FastTrack 诊断 |
| 插件更新 | 扫描并安装本仓库插件的新版本 |
| HDWinG 辅种 | 基于 HDWinG 站点数据自动辅种 |
| Hash 辅种 | 用 pieces hash 指纹跨站点查找同内容种子辅种 |
| CF 优选 IP | 优选 Cloudflare IP 并同步到 RouterOS |
| ROS 工作台 | 聚合查看 RouterOS 各项状态与已装插件 |
| 连接中心 | 统一保存并广播 RouterOS 连接参数 |
| 配置守卫 | 定期备份 RouterOS 配置并检测漂移 |
| 防火墙管理 | 四表规则可视化、隐患扫描、批量导入与逐条操作 |
| 场景模板 | 常用 RouterOS 场景一键生成执行计划并部署 |
| 外网访问向导 | 探测网络环境并规划安全的外网访问配置 |
| 站点分流 | 按域名/网段维护 RouterOS 分流规则 |

### MP 总览：怎么用

**装好即用，设置页只有一项「首页自动刷新」间隔**，媒体服务器、下载器、订阅、
入库与插件状态全部从 V3 稳定只读接口自动读取，无需填地址和密钥。插件只读，
不会修改配置或媒体文件。

- **详情页**：插件卡片点开即是完整看板，点「刷新数据」由宿主重新拉取页面。
- **首页仪表盘**：在首页「添加组件」里选择「MP 总览」（`get_dashboard_meta()`
  提供的 `overview`），按设置间隔自动刷新；紧凑布局会隐藏系统/运行状态明细。
- **外部调用**：`GET /api/v1/plugin/MPOverview/snapshot` 返回 JSON 快照，
  `POST /api/v1/plugin/MPOverview/refresh` 清缓存强制重采。

数据源不可用时会显示占位符并标注「离线预览」，不会把缺失数据伪装成 0。

## 目录结构

```text
plugins.v3/<plugin_id_lower>/   V3 插件源码（目录名 = 主类名小写）
tests/v3/<plugin_id_lower>/    离线 stub 测试
tests/v3/check_api_contract.py 全仓 API 契约校验
icons/                         统一风格图标（PNG + SVG 矢量源）
package.v3.json                V3 插件市场索引
```

## 开发约定

- 主类定义在 `__init__.py`，继承 `app.sdk.plugin._PluginBase`；目录名为主类名小写。
- `plugin_version`（类内字符串字面量）、`package.v3.json` 的 `version` 与最新
  `history` 三者保持一致；历史按语义版本降序、当前版本置顶。
- **插件 ID 与类名一经发布不得修改**（会导致已安装配置丢失），改名只改显示名。
- 图标统一为 `icons/<小写目录名>.png`（256×256），并保留同名 `.svg` 矢量源。
- 插件运行数据写插件数据目录，不写回源码目录；第三方依赖写在 `pyproject.toml`。

### V3 插件 API 契约（详情页按钮不 404 的关键）

宿主的动态路由注册等价于对 `get_api()` 的每条声明执行
`FastAPI.add_api_route(**api)`，失败时只写日志、跳过该路由：

1. `path` 用相对路径（如 `/run`），宿主自动补成 `/api/v1/plugin/<插件ID>/run`。
2. **endpoint 的返回注解必须是 Pydantic 可识别的类型**（`BaseModel` 子类、
   `dict`、`Any` 等）。普通 Python 类会触发
   `FastAPIError: Invalid args for response field`，路由被静默跳过，
   按钮点击即返回 404 Not Found。
3. 按钮 `events.click.api` 写 `plugin/<插件ID>/<action>`，`method` 必须与
   `get_api()` 的 `methods` 匹配；所有接口声明 `auth: "bear"`。
4. 返回统一三段式 `{success, message, data}`，前端普通数据客户端会严格校验。

## 校验

```bash
python3 tests/v3/check_api_contract.py
for f in tests/v3/*/run_stub_tests.py; do python3 "$f"; done
```

`check_api_contract.py` 会离线加载全部插件，按宿主方式用 FastAPI 注册每一条动态路由，
并逐项核对详情页按钮的路径与方法；还会向插件注入多种数据状态反复调用 `get_page()`，
把只在「有数据」分支里才出现的按钮也纳入扫描。任何注册失败、按钮引用不存在的接口
或方法冲突都会以非零退出码报错。它需要本机有 `fastapi` 与 `pydantic`，
不需要 MoviePilot 宿主。

`tests/v3/mpoverview/run_stub_tests.py` 额外覆盖新插件的首页仪表盘三元组、
设置项数量、离线采集、HTML 转义与响应模型类型。
