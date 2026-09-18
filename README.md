# Network Blackbox

**面向 Debian 的轻量网络故障取证工具。** 持续记录网关、互联网、DNS、HTTPS 和本机状态；故障发生时自动保存现场，恢复或重启后可回溯分析。

Python 标准库实现，无需 pip、Docker 或监控平台。适用于 Debian 12 / 13 的实体机、虚拟机和运行 systemd 的 LXC。

## 能做什么

- 每 10 秒探测网络，每 30 秒记录主机和网卡状态。
- 连续失败自动生成 incident，保存故障前指标，以及触发、15 秒、60 秒和恢复时的快照。
- 接收指定 LAN 网段的 UDP/TCP syslog，按来源 IP 和日期保存。
- 使用 SQLite 和持久文件保存证据，服务重启后接续未结束的 incident。
- 提供命令行和仅监听 localhost 的状态 API。
- 支持可选 HTTPS heartbeat；默认关闭，只上报简要状态。

## 快速安装

下载仓库源码或解压部署包，在项目目录内执行：

```bash
# 自动读取本机默认网关和 LAN 地址，生成站点配置。
python3 manage.py init --auto --site site-a --output site.json
nano site.json

# 先检查，再安装。
sudo ./install.sh --config site.json --check
sudo ./install.sh --config site.json

# 验证当前状态和实际网络。
sudo netblackbox status
sudo python3 verify.py --network
```

需要 root/sudo、systemd 和 Python 3.9+。最小系统可先安装 `python3 iproute2`。安装器从现有 Debian apt 源安装缺少的诊断工具，不新增软件源。离线部署使用 `--offline`，需预先备齐依赖。

多网卡或多默认路由时，请手工指定。以下地址来自文档专用网段，**必须替换为实际地址**：

```bash
python3 manage.py init \
  --site site-a \
  --server-ip 192.0.2.10 \
  --gateway 192.0.2.1 \
  --lan-cidr 192.0.2.0/24 \
  --output site.json
```

默认 syslog 端口为 `5514`，API 为 `127.0.0.1:9911`；支持自定义端口、网段和数据目录。端口被占用时安装器报错，不会停止其他程序。

## 日常使用

| 命令 | 用途 |
|---|---|
| `netblackbox status` | 当前网络状态和 active incident |
| `netblackbox health` | Agent 是否正常采集 |
| `netblackbox test` | 立即测试网关、公网 IP、DNS 和 HTTPS |
| `netblackbox incidents` | 最近的故障事件 |
| `netblackbox last-incident` | 最后一次故障详情 |
| `netblackbox snapshot` | 手工保存一次诊断快照 |
| `netblackbox config` | 查看配置，隐藏 cloud URL |

以上管理命令通常需要 sudo。查看服务日志：

```bash
sudo systemctl status netblackbox netblackbox-syslog
sudo journalctl -u netblackbox --since '1 hour ago'
```

默认数据位置：

| 数据 | 路径 | 默认保留 |
|---|---|---|
| 配置 | `/etc/netblackbox/config.json` | — |
| 指标数据库 | `/srv/netblackbox/db/netblackbox.sqlite3` | 7 天 |
| Syslog | `/srv/netblackbox/syslog/` | 至少 30 天 |
| Incident 与自动快照 | `/srv/netblackbox/incidents/` | 恢复后 90 天 |
| 手工导出 | `/srv/netblackbox/exports/` | 手工管理 |

## 接收路由器日志

将路由器的 Remote log server 设置为 Debian 的 LAN IP，端口 `5514`，协议 UDP。非默认端口请以站点配置为准。详见 [OpenWrt / ImmortalWrt 配置](docs/IMMORTALWRT.md)。

接收器使用独立的 `netblackbox-syslog.service`，不覆盖现有 rsyslog 配置。仅接收配置中允许的 CIDR。安装器不修改防火墙；若已有入站限制，请手工放行对应 LAN 来源和端口。

## 更新配置

```bash
sudo cp /etc/netblackbox/config.json edited-site.json
sudo nano edited-site.json
sudo ./install.sh --config edited-site.json --replace-config
```

重复运行 `sudo ./install.sh` 会保留当前配置。覆盖前自动备份；安装校验失败会恢复项目文件和服务状态。APT 包变更和证据数据不自动删除。

## 卸载

```bash
sudo ./uninstall.sh --yes
```

移除项目服务和程序，**保留所有采集数据与软件包**。手工修改过的文件会先提示检查；详细选项及回滚方式见 [运维文档](docs/OPERATIONS.md)。

## 需要了解的限制

- 故障分类是排查线索，不是已确认的根因。
- 机器关机期间无法采集；UDP syslog 在链路故障时可能丢失。
- LXC 只能看到其权限允许的内核与网卡信息，不能替代宿主机取证。
- 保留策略不等于硬磁盘配额，应定期检查剩余空间。
- 默认安装 journald 持久化/容量 drop-in；已有日志策略的系统可在配置中关闭此项。

## 更多文档

- [配置、保留策略与 heartbeat](docs/CONFIGURATION.md)
- [验收、防火墙、备份、回滚和卸载](docs/OPERATIONS.md)
- [PVE 接入建议](docs/PVE.md)
- [开发与测试](CONTRIBUTING.md)

## License

[MIT](LICENSE)
