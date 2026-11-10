# ViewingWeeklyReport

观影周报 / 转码诊断插件主包。

- `__init__.py`：V3 插件主类（生命周期、表单、详情页、API、远程命令）。
- `core.py`：零宿主依赖纯逻辑（解析、时间窗、周报聚合、转码诊断规则、md_safe、cron 校验、脱敏）。
- `clients.py`：Emby / Jellyfin / Plex 历史与会话客户端。

详见插件目录上一级 README.md。
