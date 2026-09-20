# 验收、维护和回滚

## 安装依赖与离线模式

Debian 12/13、systemd、root、Python >=3.9。依赖：rsyslog、curl、jq、bind9-dnsutils、iproute2、iputils-ping、ethtool、conntrack、sqlite3、python3、ca-certificates、procps、util-linux、logrotate。无需 Docker/pip/nftables。

`--offline` 不执行 apt；缺包会在写入前失败并列出包名。请用组织自己的 Debian 镜像/离线 deb 仓库提供依赖，本包没有内置操作系统软件包。线上模式只使用目标机现有 apt 源，升级/新增包记录在安装审计，不自动删除包。

## 验收与故障演练

```bash
sudo python3 verify.py              # 服务/API/监听/WAL/持续写库/UDP TCP真实落盘
sudo python3 verify.py --network    # 额外 Gateway/公网IP/DNS/HTTPS
sudo python3 /opt/netblackbox/simulate_failure.py  # 约70–120秒，独立模拟目录
sudo systemctl status netblackbox netblackbox-syslog --no-pager
sudo journalctl -u netblackbox -u netblackbox-syslog --since '1 hour ago'
```

verify 发送可识别的测试 syslog；它不重启服务、不轮转既有日志、不修改网络。公网被隔离时 --network 可以失败，这也是正常采集的故障证据。模拟测试使用自身 SQLite，在 exports/selftest_* 中验证事件去重、T+0/15/60/恢复快照、重启续接、pre-fault 数据，不断网。

从其他 LAN 机器验证 TCP/UDP 到实际 `syslog.listen_address:port`，再确认对应来源 IP 目录出现日志；安装器的本机发送测试不能证明上游交换机、防火墙和路由器发送配置正常。

## 防火墙

安装器不修改 nftables、ufw 或 iptables。专用 rsyslog 已使用 LAN CIDR ACL，不记录范围外消息，但完整的网络层放行/拒绝由现有 firewall 管理。

如果没有防火墙，通常不需要额外配置。若 ufw 已启用，管理员按站点参数手动加入规则，例如：

```bash
sudo ufw allow from 192.0.2.0/24 to 192.0.2.10 port 5514 proto udp
sudo ufw allow from 192.0.2.0/24 to 192.0.2.10 port 5514 proto tcp
```

如果 nftables 已有 input drop/reject，在最终拒绝规则之前加入限定源 CIDR、目的 IP 和端口的 TCP/UDP accept 规则，并用现有运维方式持久化。先检查规则所属表/链，不能机械假定叫 filter/input。**不要 flush ruleset，不要直接重载含 flush ruleset 的配置，尤其是有 Docker 的主机。** 不要开放 API 9911 到 LAN。

## 备份与查询

实际数据目录取自 /etc/netblackbox/config.json。以下假定默认 /srv/netblackbox：

```bash
sudo sqlite3 /srv/netblackbox/db/netblackbox.sqlite3 ".backup '/srv/netblackbox/exports/backup.sqlite3'"
sudo sqlite3 /srv/netblackbox/db/netblackbox.sqlite3 \
  "SELECT datetime(ts,'unixepoch'),type,data FROM events ORDER BY id DESC LIMIT 30;"
sudo sqlite3 /srv/netblackbox/db/netblackbox.sqlite3 \
  "SELECT datetime(ts,'unixepoch'),json_extract(data,'$.gateway'),json_extract(data,'$.internet') FROM metrics WHERE kind='probe' ORDER BY ts DESC LIMIT 20;"
```

运行中不要只复制 .sqlite3 而漏掉 WAL；用 SQLite backup API。incident/syslog/config 另行归档。数据库内含 incident 绝对路径，因此本版不提供任意数据目录在线迁移；搬旧数据时保持相同 data_dir，停服务并保留整套目录。把旧数据复制到新机器不是部署必要步骤，通常新站点应从空数据目录开始。

## 修改配置/重复安装

原配置默认保留，安装器自己管理的文件相同则不会重启 Agent。重装可能再次执行本地验收并产生少量测试日志和审计目录。明确修改时：

```bash
sudo cp /etc/netblackbox/config.json ./edited-site.json
sudo chmod 600 ./edited-site.json
sudo nano ./edited-site.json
sudo ./install.sh --config ./edited-site.json --replace-config --check
sudo ./install.sh --config ./edited-site.json --replace-config
```

每次操作记录在 `<data_dir>/state/installations/<UTC时间戳_随机ID>/`：audit-before.json、preflight.json、apt 日志、package-changes.json、before/（被覆盖文件的原始路径）、manifest.json、生成配置的校验输出、health.json、verification.json。

## 回滚

安装失败自动恢复项目文件及原有项目服务的 active/enabled 状态；不删除采集数据、不卸载 apt 包。若配置校验失败，仍保留诊断材料。不要用其他机器的防火墙 dump 覆盖当前 firewall。

手动回滚最新一次成功安装（manifest 路径替换为安装输出）：

```bash
sudo python3 manage.py rollback /srv/netblackbox/state/installations/时间戳_ID/manifest.json
```

只允许回滚最新安装，避免覆盖后续变更。回滚不撤销 apt 软件包动作；如果系统已发生其他修改，应先审阅 manifest 和备份。回滚成功后用 systemctl/netblackbox 验证服务；回滚第一次安装相当于撤掉项目文件，仍保留证据和包。

## 卸载（默认永久保留数据）

```bash
# v1.2.1+ 的推荐入口；无需源码 checkout，也不会下载任何项目。
sudo python3 /opt/netblackbox/manage.py uninstall --yes

# 当前版本 wrapper 会优先调用已安装的对应管理器。
sudo ./uninstall.sh --yes
```

只移除本安装器管理的文件、停用项目三个服务和 timer；保留数据、安装审计、软件包和现有系统 rsyslog，不改防火墙。若已手工修改文件，会拒绝移除，审阅后可用 `--yes --force`；即使 --force，也先备份这些文件。不提供自动清空数据的选项。只有确认完整归档、不再需要取证时才由管理员自行删除数据目录。

如果旧源码目录的 VERSION 为 v1.1，而已安装版本为 v1.2，旧管理器可能报 `Unknown managed path: /opt/netblackbox/syslog_storage.py`。这是旧文件白名单拒绝新版路径，尚未删除程序。不要通过放开任意路径校验来绕过。v1.2.1 起安装器会保存对应管理器到 `/opt/netblackbox/manage.py`；早于此版本且没有该文件的安装，应使用原升级时保留的匹配 `manage.py uninstall --yes`，或仅取得匹配管理工具后卸载，不需要先安装或拉取完整新项目。原先 v1.1 的 wrapper 本身不会自动获得新代码。

卸载会先备份全部受管理文件，按 timer→guard→receiver→Agent 顺序停止并确认，再移除文件。停止失败不会在仍运行的进程下删除程序；删除后失败会尝试恢复文件和服务状态。证据、卸载备份、原源码目录和通用 apt 包保留；不会 git pull、clone 或重新安装程序。

若启用了 journald drop-in，卸载移除该 drop-in 并重启 journald，恢复其他原有配置策略，不删除 journal。安装器拒绝覆盖任何非本项目拥有的同名配置，避免卸载误删其他人的文件。

## 与其他安装方式共存

安装器遇到已有但不受其管理的同名配置、服务或旧式 syslog snippet 时会拒绝覆盖。先归档数据并检查现有安装方式，按其卸载步骤释放相关端口和服务，再安装。不会自动迁移未知版本、修改全局 firewall 或更改数据目录。跨机器迁移已有数据库时应保留原绝对数据路径，并在停服务后迁移完整数据目录。
