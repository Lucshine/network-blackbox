# 配置与架构

站点 JSON 同时驱动 Agent、专用 rsyslog、logrotate、systemd 和可选 journald drop-in。`manage.py init` 生成完整配置；只需更改现场差异。预览所有生成文件：

```bash
python3 manage.py render --config site.json --output rendered-site
```

这个操作只写本地预览目录，不连接设备，不安装或启动服务。

| 字段 | 用途 |
|---|---|
| site | 1–64 字符站点标识，字母/数字/点/下划线/短横线 |
| gateway / router_dns | 网关 ping / 路由器 DNS 的 IP |
| public_ips | 至少两个公网 IP，任一成功即互联网 IP 可用 |
| public_dns / dns_name | DNS 服务器列表和查询名称，要求 NOERROR + A answer |
| https_urls | 至少两个 HTTPS 目标，任一可信 TLS + 2xx/3xx 成功即可；按部署地区调整 |
| syslog.listen_address | 目标 Debian 实际 LAN IPv4，不可设为 0.0.0.0 |
| syslog.allowed_networks | 允许接收的 IPv4 CIDR 列表，必须包含本机监听地址 |
| syslog.port / api.port | 默认 5514 / 9911；占用时明确选择其他端口 |
| api.host | 固定 127.0.0.1，禁止默认暴露 LAN |
| data_dir | 首次部署时自选专用 /srv/... 或 /var/lib/... 路径 |
| journald.configure_persistent | 是否安装全局 journald 持久化/容量 drop-in，默认 true |
| cloud | 默认关闭，HTTPS URL 留空；JSON POST 或 Kuma GET；至少 60 秒间隔 |
| pve | 默认关闭，host 留空；不主动探测任何 PVE |

当前 receiver 是 IPv4；源码用 `ipv42num` 对 CIDR 的完整整数范围判断，并使用兼容 Debian rsyslog 版本的 `$AllowedSender`，没有以字符串前缀误判子网。只对专用 receiver 生效，不影响其他 syslog 流。参考：[rsyslog IP 转换函数](https://docs.rsyslog.com/doc/rainerscript/functions/rs-ipv4convert.html)、[来源 ACL](https://docs.rsyslog.com/doc/configuration/input_directives/rsconf1_allowedsender.html)。

## 数据模型

`data_dir/db/netblackbox.sqlite3`：WAL + synchronous=FULL；metrics（kind=probe/host，JSON data）、events（只记变化）、state、incidents、snapshot_jobs。

每 10 秒 probe，每 30 秒 host；3 次失败触发、3 次恢复关闭。一次 outage 只有一个 active incident，故障域可能变化但不重复创建目录。分类：网关不可达、WAN/上游、路由 DNS、DNS/上游、HTTP 层、本机 NIC 异常。分类仅是证据指向，不代表已确认根因。

incident 目录包含 summary.json、触发前 15 分钟 metrics/events 导出、T+0/15/60 与恢复快照。每个外部诊断命令具有超时和独立 stdout/stderr/返回码。恢复时间是重新观察到恢复的时间，不能推断系统关机期间的真实恢复时刻。

Agent 和系统重启后从 SQLite 续接 active incident/待执行任务；错误退出由 systemd/watchdog 重启。系统完全关机或宿主停止期间无法采样。

## 保留与容量

默认：metrics 7 天、events 90 天、已恢复 incident 90 天、syslog 至少 30 天；每 300 秒维护。SQLite 空闲页复用，checkpoint 处理 WAL，不随历史天数无限增长。syslog 按来源 IP 与接收日期保存；logrotate daily/rotate 30/compress，并以 16 MiB maxsize 每 5 分钟检查。旧的按天文件及归档由 Agent 清理。

低于 1 GiB 空闲或 incident 超过 1 GiB 时，保留状态/指标和 incident 元数据，跳过后续大快照并记录 STORAGE_PRESSURE。不会提前删掉 90 天内 incident。高流量日志、频繁故障、其他软件和手工导出仍可能填满共享盘，容量阈值不是硬配额。exports/审计/安装备份不自动清理，管理员负责归档。

程序时间为 UTC；syslog 接收日期按 rsyslog/系统时区格式化，不修改目标系统时区。定期清理允许日边界误差；排查时注意时区换算。

## Cloud / Kuma

默认 cloud.enabled=false、push_url 空。未来填入你自己的 HTTPS URL，选择 mode=json（POST 小 JSON）或 mode=kuma（GET status/msg），然后用 --replace-config 重装配置。只传 site/status 与 gateway/internet/router_dns/public_dns/https 布尔状态，不传日志、snapshot、SQLite 或 host 细节。失败隔离到独立线程；失败/恢复按状态变化记 event。URL 不写入进程参数，`netblackbox config` 会隐藏 URL；启用后配置和安装备份属于敏感文件，权限 0600。

Kuma 建议超时大于 60 秒，例如 120–180 秒，以便远端判断现场完全离线。没有真实 URL 不进行联调。
