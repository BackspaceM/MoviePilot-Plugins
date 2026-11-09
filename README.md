# Pieces Hash 辅种（PiecesHashReseed）

MoviePilot V3 插件。读取下载器中已有种子文件的 **pieces hash**（`sha1(info.pieces)`）作为内容指纹，
向站点的 `pieces-hash` 接口查询同内容种子，下载后按原保存路径添加到下载器复用已有文件做种，无需重复下载。

> 与 IYUU 类「按 infohash 查询」不同，pieces hash 能识别不同站点重新打包、tracker/文件名不同但内容完全相同的种子。


# HDWinG 辅种插件（MoviePilot V3）

针对 ’瓷器‘的辅种插件：扫描下载器中已完成的任务，在站点按发布名检索同源种子，逐文件校验一致后下载原种加入做种。

# Cloudflare 优选 IP → RouterOS（MoviePilot V3 插件）

自动找出解析到 Cloudflare 的 PT 站点域名，在 Cloudflare 官方网段内实测 TCP 握手延迟，把最优的若干个 IP 写入 RouterOS 的 `/ip/dns/static`，让内网设备走优选线路，同时兼顾站点登录态与访问速度。