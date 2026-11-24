# 熊猫好友买卖（PandaFriendTrade）

MoviePilot V3 插件：按用户自定义规则，自动完成熊猫PT（pandapt.net）「好友买卖」里的重复操作。

## 功能

| 模块 | 动作 | 风险 |
| --- | --- | --- |
| 收益 | 领取挂机收益 | 安全 |
| 任务 | 领成长任务奖励、领成就、领等级海报 | 安全 |
| 佣人工作 | 按收益/关系/心情/天赋四种策略自动安排 | 安全（不花魔力） |
| 佣人互动 | 免费项，或设单次魔力上限后用付费项 | 安全 / 消耗 |
| 主动互动 | 我作为佣人时自动回应 | 安全 |
| 事务所 | 自动派单（按 focus 属性挑人）、结算已完成委托 | 消耗 / 安全 |
| 海报 | 领典藏任务奖励、兑换典藏册、赠送白名单好友 | 安全 / 消耗 |
| 佣人保护 | 临近过期自动买保护（设时长与魔力上限） | 消耗 |
| 买入/抢人/追回 | 查实时价格，过预算与属性门槛才动手 | **不可逆** |
| 释放佣人 | 仅临时佣人，需双重开关 | **不可逆** |

## 安全设计

1. **默认试运行**：只产出计划写日志，不点任何按钮。
2. **不可逆动作默认全关**：买人/抢人/追回/释放必须自己打开，买人还要设价格上限与属性下限；买人前先查详情拿实时价格。
3. **保留名单**：填 UID 后这些佣人不参与工作、互动、保护、释放、派遣。
4. **不重复站点原生能力**：站点已开启自动派遣时，插件不派单、只结算。
5. **登录凭据隔离**：Cookie 只存本机插件配置，运行时才传给 HTTP 客户端，不写日志、不写仓库。
6. **渲染期零 IO**：配置页/详情页只读内存缓存，不联网、不读宿主 SDK；所有按钮端点兜底异常。

## 配置

1. 站点 Cookie：浏览器开发者工具 → Network → 任意请求 → 复制完整 `Cookie` 请求头。
2. 保留 UID：逗号/空格/换行分隔，不想被自动操作的人填这里。
3. 互动预算：免费 / 设定上限（如 300）/ 不限制 / 关闭。
4. 买人：先设价格上限（0=不买）与属性均值下限，再开「自动买入」。

## 使用

点「检测 Cookie」→ 确认登录态 → 点「试运行」→ 去插件日志看计划 → 确认无误关掉试运行 →
点「运行一轮」或开启定时。逐条动作的成败都写进插件卡片「…」→ 查看日志，页面只留静态状态卡。

## 接口实现

全部走 `POST /ajax.php`，body 为 `action=xxx&params[key]=val`，返回 `{ret, msg, data}`。
读接口：`friendTradeInitOrHome`、`friendTradeCommissionBoard`、`friendTradePosterTaskBoard`、
`friendTradeMarketList`、`friendTradeDetail`、`friendTradePosterCollection` 等。
写接口：`friendTradeWork`、`friendTradeInteract`、`friendTradeClaimIncome`、
`friendTradeCommissionStart/Settle`、`friendTradePosterTaskClaim`、`friendTradeBuy/Snatch/Reclaim` 等。
赠送与领海报奖励带 `request_uuid` 防重放。

纯标准库实现，无第三方依赖。核心规则在 `core.py`（可脱离宿主单测），
HTTP 客户端在 `site.py`，页面在 `renderer.py`。
