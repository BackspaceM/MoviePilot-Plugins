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
