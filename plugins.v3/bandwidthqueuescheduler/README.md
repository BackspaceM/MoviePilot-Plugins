# 带宽/Queue 智能调度（BandwidthQueueScheduler）

在 RouterOS 上按"带宽方案（profile）"智能编排简单队列（Simple Queue）：可按时段自动切换方案，也可在详情页一键切换，并实时监控队列速率、诊断 FastTrack 绕过风险。

## 能做什么

- **设备定义**：`名称=IP/CIDR|角色`，角色 `download`（下载设备，观影时压制）/ `nas`（NAS/服务器）/ `media`（观影设备，不限速）/ `other`（其它）。
- **四套内置方案**：
  | 方案 | 说明 |
  |---|---|
  | 均衡 balanced | 下载适度限速，观影与 NAS 保留充足带宽 |
  | 观影优先 movie | 强力压制下载，观影设备不限速 |
  | 下载优先 download | 下载不限速，观影与其它适度限流 |
  | 夜间全速 night | 全部不限速，适合无人观影时跑满带宽 |
- **自定义方案 JSON**：同 key 覆盖内置、新 key 新增，例如
  `{"movie":{"label":"观影·自定义","rules":{"download":{"max":"1M/3M","priority":8}}}}`
- **时段规则**：`HH:MM-HH:MM=方案`，支持跨午夜（如 `23:00-06:00=night`），多条重叠时后写的优先；留空则始终用默认方案。
- **队列差异编排**：仅新建/更新/删除以队列前缀（默认 `bqs`）开头的队列，绝不触碰手工队列。
- **FastTrack 诊断**：FastTrack 匹配的流量会绕过 Queue 导致限速失效，插件检测并提示处理建议。
- **实时速率**：每轮调度后读取各队列 rate 并在详情页展示。
- **试运行**：只评估差异、不实际改动 RouterOS。

## 速率单位

`max-limit` / `limit-at` 采用 RouterOS 原生 **上行/下行** 格式（如 `20M/60M`、`512k/2M`），`0/0` 表示不限速。`priority` 为 1~8，**1 最高**。

## 界面

详情页每个按钮都绑定真实 API，点击即有响应，不存在灰色/无效按钮：

- 一键切换：`应用·均衡` / `应用·观影优先` / `应用·下载优先` / `应用·夜间全速`（及自定义方案）。
- 操作：`立即按规则调度`、`刷新队列状态`、`FastTrack 诊断`、`测试连接`、`清空本插件队列`、`停止`。

带参按钮（方案切换）用 POST body（`{"profile": key}`）传给 `apply_profile` endpoint。

## 配置

1. RouterOS 需开启 REST API（`/ip/service` 中 `www-ssl` 或 `www`），账号需具备读写权限。
2. 自签名证书可关闭「使用 HTTPS」改走 HTTP，或信任证书。
3. 若启用了 FastTrack，请按诊断建议在 FastTrack 规则前加 `accept` 放行目标 IP，否则限速不生效。
4. 队列名前缀一旦修改，旧的 `前缀_` 队列不会被自动清理——改前缀前建议先「清空本插件队列」。

## API / 远程命令

API：`/run` `/apply_profile` `/refresh` `/fasttrack` `/test` `/clear_queues` `/stop` `/status` `/profiles`（前缀 `/plugin/BandwidthQueueScheduler`）。

远程命令：`/bqs_status` `/bqs_run` `/bqs_refresh` `/bqs_fasttrack`，以及 `/bqs_<方案key>`（如 `/bqs_movie`）。
