# Pieces Hash 辅种（PiecesHashReseed）

MoviePilot V3 插件。读取下载器中已有种子文件的 **pieces hash**（`sha1(info.pieces)`）作为内容指纹，
向站点的 `pieces-hash` 接口查询同内容种子，下载后按原保存路径添加到下载器复用已有文件做种，无需重复下载。

> 与 IYUU 类「按 infohash 查询」不同，pieces hash 能识别不同站点重新打包、tracker/文件名不同但内容完全相同的种子。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| pieces hash 辅种 | 自建零依赖 bencode 解析器，计算 info_hash / pieces_hash，添加前复核远端种子指纹 |
| 站点支持检测 | 自动探测 `api/pieces-hash`、`nexusapi/pieces-hash` 及自定义路径；GET 405 或 POST 标准 JSON 判为支持；结果持久化并在详情页展示 |
| cron 定时 | 5 段 crontab（默认 `0 4 * * *`）/ 固定间隔 / 禁用；支持「立即辅种一次」 |
| 下载器选择 | 下拉多选要扫描的下载器；可另选目标下载器，并配置跨下载器路径映射 |
| 跳过哈希校验 | qBittorrent 通过 `is_skip_checking`（兼容 seedMode）跳过校验；Transmission 自动 recheck |
| 去重与缓存 | 成功/失败/永久失败三类缓存，指纹错配永久跳过，缓存上限 10000 条，可一键清除 |
| 试运行 | 只统计命中数量，不下载、不添加种子 |
| 通知与统计 | 运行汇总通知、最近一次统计、错误清单、站点检测表格 |
| 手动触发 | 配置页三个开关、远程命令 `/辅种`、插件 API |

## 开始前

1. MoviePilot **V3**（索引声明 `system_version: ">=3.0.0"`）。
2. 站点提供 pieces-hash 接口（青蛙、AGSVPT、红豆饭、麒麟、UBits、聆音、憨憨等 NexusPHP 系常见），并在站点管理中启用、Cookie 有效。
3. 每个站点填写 **Passkey**（通常在站点「个人设置 / 密钥」页）。
4. 把下载器的种子缓存目录映射进 MoviePilot 容器：
   - qBittorrent：`/config/qBittorrent/BT_backup`（常见映射为 `/qb`）
   - Transmission：`/config/transmission/torrents`（常见映射为 `/tr`）

   插件默认会尝试这些路径，也可用「种子缓存目录」显式指定，格式 `下载器名称:/qb/BT_backup,/tr/torrents`。
5. 目标下载器能看到同一份媒体文件；跨下载器时需配置路径映射。

## 配置

| 配置项 | 说明 |
| --- | --- |
| 扫描的下载器 | 下拉多选；不选则使用全部已启用下载器 |
| 辅种目标下载器 | 默认「同下载器」；跨下载器需填路径映射 |
| 辅种站点 | 下拉多选，仅选中站点参与辅种 |
| 站点 Passkey | 每行 `站点名:Passkey[:接口路径[:请求间隔]]`，站点名需与下拉选项一致；接口路径可填 `nexusapi/pieces-hash` 或完整 URL；请求间隔单位秒，用于站点流控 |
| 种子缓存目录 | 每行 `下载器名:目录1,目录2`；不带下载器名时对所有下载器生效 |
| 定时方式 / 执行周期 | `计划任务（cron）` / `固定间隔` / `禁用`；cron 为 5 段，如 `0 4 * * *` |
| qBittorrent 跳过哈希校验 | 开启后不触发 recheck，直接以完成状态开始做种 |
| 辅种标签 / 分类 | 默认标签 `辅种`；分类仅 qBittorrent 生效 |
| 排除标签 / 排除路径 | 逗号分隔，命中的本地种子不参与辅种 |
| 跨下载器路径映射 | 每行 `/源前缀::/目标前缀`，最长前缀优先 |
| 单次查询条数 / 超时 / 单次最多辅种数 | 站点流控与运行保护 |
| 自动检测站点支持 | 关闭后直接使用缓存或默认接口路径 |
| 试运行 / 发送通知 / 立即检测 / 立即辅种 / 清除缓存 | 运行控制 |

## 站点支持检测

- 打开「立即检测站点支持」并保存，或调用 `POST /api/v1/plugin/PiecesHashReseed/check-sites`。
- 判定规则：接口对 GET 返回 **405**（接口存在但只允许 POST），或 POST 返回带 `data` 的标准 JSON → 支持；
  404、登录页 HTML、401/403 → 不支持或 Cookie / Passkey 失效。
- 启用「自动检测站点支持」后，定时任务会复用已支持的检测结果；未通过检测的站点每次运行会重试探测，并自动跳过。
- 结果写入插件数据区，可在插件详情页查看（站点、接口路径、原因、检测时间）。

## 怎么跑

```
下载器已完成任务 → 扫描本地 .torrent 建立 info_hash → 指纹索引
    → 聚合 pieces_hash → 保存路径（可选路径映射）
    → 站点 POST {"passkey": "...", "pieces_hash": [...]}
    → 下载命中的种子 → 复核 pieces_hash 一致、info_hash 未存在
    → 暂停状态添加到目标下载器（同路径 + 辅种标签 + qB 跳验）
    → 启动做种 / 写入缓存与统计
```

## 手动接口

| 路径 | 方法 | 作用 |
| --- | --- | --- |
| `/api/v1/plugin/PiecesHashReseed/status` | GET | 运行状态、支持站点、最近统计 |
| `/api/v1/plugin/PiecesHashReseed/check-sites` | POST | 异步重新检测站点 |
| `/api/v1/plugin/PiecesHashReseed/run` | POST | 异步执行一次辅种 |
| `/api/v1/plugin/PiecesHashReseed/clear-cache` | POST | 清除缓存与检测记录 |

所有接口使用 `bear` 鉴权；远程命令 `/辅种` 等价于 `/run`。

## 限制

- pieces hash 依赖相同的分块大小与文件集合；同一内容若站点重新分块，指纹会不同，无法匹配。
- Transmission 不支持跳过哈希校验，添加后会自动/显式触发重新校验。
- 跨下载器辅种要求目标下载器能看到相同路径下的文件，否则会停在校验失败状态。
- 请勿与 IYUU 辅种等插件对同一站点同时启用，避免重复添加。
- Passkey、Cookie 只用于请求，日志与 API 响应中不做明文输出。

## 测试

```bash
../MoviePilot/.venv/bin/python -m compileall plugins.v3/pieceshashreseed
../MoviePilot/.venv/bin/python -m pytest tests/v3/pieceshashreseed
../MoviePilot/.venv/bin/python .github/scripts/check_plugin_versions.py package.v3.json
git diff --check
```

插件无第三方依赖，因此不提供 `pyproject.toml` / `requirements.txt`。
