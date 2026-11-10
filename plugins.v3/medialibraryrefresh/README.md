# 媒体库精准刷新（MediaLibraryRefresh）

MoviePilot V3 插件。监听整理入库事件，把**具体文件路径**换算成媒体服务器侧路径后发起**文件级增量刷新**，而不是扫描整个媒体库。

## 和内置刷新的区别

| | MP 内置 | 本插件 |
| --- | --- | --- |
| Emby / Jellyfin | `Items/{库或剧集ID}/Refresh`（整目录 Recursive），匹配不到就 `Library/Refresh` 全库扫描 | `POST /Library/Media/Updated`，逐文件 `Created/Modified/Deleted`，不做全库扫描 |
| Plex | `/library/sections/{key}/refresh?path=`，匹配失败全库 `update()` | 同样按父目录局部刷新，但自动读取库列表、支持路径映射与分级回退 |
| 服务器数量 | 绑定主程序里配置的一台 | 任意多台 Emby / Jellyfin / Plex 混合 |
| 路径映射 | 依赖主程序与媒体服务器路径一致 | 每台服务器独立配置 `MP路径::媒体服务器路径` |
| 失败处理 | 单次请求 | 合并窗口去重 + 三级回退 + 指数退避重试 + 丢弃告警 |
| 可观测性 | 少量日志 | INFO 级 SSE 实时日志、逐服务器明细、MP 通知 |

## 配置

### 媒体服务器清单

配置页「媒体服务器清单」每行一台，`#` 开头为注释：

```
# 类型|名称|地址|密钥|路径映射|启用
emby|客厅Emby|http://192.168.1.10:8096|你的API_KEY|/media::/volume1/media|1
plex|客厅Plex|http://192.168.1.11:32400|你的Plex_TOKEN|/media::/mnt/media|1
jellyfin|JF备库|http://192.168.1.12:8096|你的API_KEY|/media::/data/media|0
```

- **类型**：`emby` / `jellyfin` / `plex`。Jellyfin 走不带 `/emby` 前缀的接口。
- **地址**：可以带反代路径（如 `https://emby.example.com/emby`），插件会自动避免拼出 `/emby/emby/...`。
- **密钥**：Emby/Jellyfin 在「控制台 → 高级 → 安全 → API 密钥」创建；Plex 的 `X-Plex-Token` 可从浏览器登录 Plex Web 后，在 `https://app.plex.tv/desktop/#!/settings/account` 或请求头/数据库中取得。密钥在日志与通知中会被脱敏。
- **路径映射**：`MP容器路径::媒体服务器看到的路径`，多个用分号分隔，按最长前缀匹配。这是最关键的配置：MP 容器里的 `/media/movies/A.mkv`，在 Emby 容器里可能挂在 `/volume1/media/movies/A.mkv`，在 Plex 里可能是 `/mnt/media/movies/A.mkv`。若两者路径完全一致可留空。
- **启用**：`1/0/true/false/yes/no`，留空默认启用。

第一次用先开「试运行（不发请求）」，看宿主日志里的映射后路径对不对，再关掉试运行。

### 主要参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| 合并窗口 | 20 秒 | 最后一次事件后静默多久才刷新，批量入库合并成一次请求 |
| 最大等待 | 120 秒 | 持续有事件时最迟多久强制执行，避免一直被推迟 |
| 单轮最多文件数 | 200 | 超出的留到下一轮并自动续跑 |
| 失败重试次数 | 3 | 失败项指数退避重入队列，超限丢弃并告警 |
| 失败回退策略 | 父目录 | `不回退 / 父目录·局部目录 / 全库扫描` |
| 定时对账周期 | `0 5 * * *` | 处理到期重试项与遗漏队列；可选额外做全库刷新 |
| 请求超时 | 8 秒 | 单台服务器单次请求 |
| 监听删除事件 | 关 | 开启后 `DownloadFileDeleted` 等事件发 `Deleted` 更新 |
| 试运行 | 关 | 不发任何 HTTP 请求，只验证路径映射与调度 |

## 怎么触发

- **事件**：`TransferComplete` → `Created`；`SubtitleTransferComplete` → `Modified`；`DownloadFileDeleted` / `DownloadDeleted` → `Deleted`（可分别开关）。
- **定时**：cron 对账，兜底处理重试与插件重载期间丢失的一次性任务。
- **立即**：配置页「立即执行一次」、详情页「立即刷新」、`/run` API、远程命令 `/mlr_run`。
- **全库**：详情页「全库刷新」、`/refresh_all` API、远程命令 `/mlr_all`。

远程命令：`/mlr_run`、`/mlr_check`、`/mlr_queue`、`/mlr_all`、`/mlr_stop`，按来源渠道回复。

## 刷新分级

1. **精准**：Emby/Jellyfin `POST /Library/Media/Updated`（`Updates: [{Path, UpdateType}]`）；Plex `GET /library/sections/{key}/refresh?path=<父目录>`，同目录多集自动合并成一个请求。
2. **父目录**：Emby `POST /Library/Refresh?Path=<目录>`；Plex 按目录逐个刷新。
3. **库级 / 全库**：Emby `POST /Library/Refresh`；Plex 对每个媒体库发起刷新。

每级结果和生效层级都写在宿主日志里。

## 日志和通知

- 关键节点全部写 **INFO 级**宿主日志（`plugins/medialibraryrefresh.log`）。逐服务器、逐文件路径和写入结果都在 MP 插件卡片「查看日志」里看。**MP 全局日志级别设为 WARNING 时 INFO 会被过滤**，把级别调回 INFO 就行。
- 详情页只留一行上次结果和操作按钮，点「刷新页面」拉最新数据。
- 通知走基类 `post_message`（自动沿用 MP 全局通知开关、收件人路由与跳回插件页的链接），支持「全部 / 仅异常 / 关闭」。队列为空不会发通知。
- 插件 API：`/run`、`/check`、`/status`、`/queue`、`/logs`、`/refresh_all`、`/clear_queue`、`/clear_logs`、`/stop`（路径前缀 `/api/v1/plugin/MediaLibraryRefresh`）。

## 常见问题

**Q：日志显示路径是 `/media/movies/A.mkv`，但 Emby 里没有这个路径。**
路径映射没配对。在服务器清单里加 `/media::/volume1/media`（右侧换成 Emby 容器里实际挂载的路径），开启试运行确认。

**Q：所有服务器都返回 HTTP 401/403。**
API Key / Plex Token 无效或权限不足。用详情页「检测服务器」按钮逐个验证。

**Q：Plex 提示「未解析到任何媒体库」。**
Token 无效，或反代把 `/library/sections` 的 XML 改成了 HTML。插件会退回 `/identity` 再探一次并在检测结果显示。

**Q：Emby 精准刷新返回成功但媒体库没出现。**
部分老版本 Emby 对 `Library/Media/Updated` 支持不完整。把「失败回退策略」设为「父目录」或「全库扫描」，或手动点一次「全库刷新」。

**Q：批量入库时会不会每个文件都请求一次？**
不会。合并窗口内的事件先进队列去重，静默后一次提交；Plex 还会把同目录的多集合并成一个请求。

## 已知限制

- 删除事件的路径字段因宿主版本而异，插件按常见键宽松提取；若你的版本不带路径，建议关闭「监听删除事件」并依赖媒体服务器自身的文件监控。
- Plex 的局部刷新粒度是**父目录**，无法做到单文件；Emby/Jellyfin 才是真正的单文件级。
- 刷新接口只提交扫描请求，不等待扫描完成；结果以媒体服务器实际入库为准。
- 插件不读取 MP 主程序里已配置的媒体服务器，需要在此单独填写（避免依赖内部不稳定 API，也便于一台 MP 对应多台媒体服务器）。
