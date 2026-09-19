# v1.2 生产升级与回滚（需管理员批准后执行）

本轮开发不部署生产，不修改现有正常工作的 Syslog 链路。以下命令由维护人员在批准的窗口执行。升级会短暂重启本项目 Agent/receiver，因此 UDP 在窗口内可能丢失。

## 升级前

1. 确认当前工作区干净、旧版本 commit/安装包、运行单元及数据目录。确认剩余磁盘足够容纳独立备份。先保留当前代码和安装 manifest，禁止清空旧日志/incident。
2. 确认 `/syslog` 新配置默认预算 1 GiB 对站点是否合适。安装器在执行任何安装动作前，按 guard 相同口径统计已有受管理 syslog（含日期/数字后缀与 `.gz` 的实际文件大小）。若已达到或超过候选 syslog_budget_mb，会阻止升级并输出占用量/预算；统计失败或超时同样阻止升级。请在候选配置中合理增大预算、留出磁盘余量后重跑 `--check`，或由管理员批准归档计划。预检不会停止服务、轮转、删除日志或写入 lock 文件。实时收件可能在预检后继续增长，因此预检不是容量预留，运行时 guard 仍必要。
3. 原配置和一致 SQLite 备份：

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/srv/netblackbox-upgrade-backups/$stamp
sudo mkdir -p "$backup"
sudo chmod 700 "$backup"
sudo cp -a /etc/netblackbox "$backup/etc-netblackbox"
sudo cp -a /opt/netblackbox "$backup/opt-netblackbox"
sudo cp -a /etc/systemd/system/netblackbox* "$backup/"
sudo sqlite3 /srv/netblackbox/db/netblackbox.sqlite3 ".backup '$backup/netblackbox.sqlite3'"
```

示例假定 data_dir 默认值，自定义路径请以配置为准。备份可能包含 cloud URL，保持 root-only，不上传 GitHub。在线复制 SQLite 使用 `.backup`，不能只 cp 主文件而漏掉 WAL。syslog/incident 另行归档；不要删除源目录。需要一致完整文件快照时安排停写或文件系统快照。

## 安装候选版本

在独立源码目录 checkout 已审阅的 v1.2 commit，核对 SHA，不直接在生产源码目录逐行编辑：

```bash
# 在已经取到候选代码的目录中执行
sudo ./install.sh --check
sudo ./install.sh
sudo netblackbox health
sudo netblackbox status
sudo netblackbox syslog-status
sudo python3 verify.py --network
sudo systemctl status netblackbox netblackbox-syslog netblackbox-logrotate.timer
```

`./install.sh` 自动给 v1.1 配置补齐 write_mode、expected_sources、silent_seconds、syslog_budget_mb、syslog_stop_free_mb，保留 data_dir 和原有参数/证据。旧 `syslog_rotate` 保留但已弃用为客户日志的删除依据。配置在覆盖前由 installer 备份；也可先复制旧 JSON 到工作文件，按需求添加新参数，然后 `--config edited-site.json --replace-config`。

安装前校验新 rsyslog、logrotate 和 systemd；配置写入前停止旧轮转 timer/oneshot，避免旧 rotate-30 在替换期间执行。幂等安装不改变已有数据目录、不清空日志、没有数据库 schema 迁移。本版仅增加独立 version=1 的 observer JSON，不改已有 SQLite 表；未知 observer JSON 版本忽略后重新建立，不破坏原始日志。

安装输出 `state/installations/<timestamp>/manifest.json`，记录每个新建/替换文件。失败时恢复项目文件/服务；已安装 apt 包和证据保留。安装验证仅 Level 1；必须根据 SYSLOG-ACCEPTANCE.md 从外部主机和真实设备分别做 Level 2/3 才能宣称现场验收通过。

## 回滚

升级成功但需要回退时，使用**此次 v1.2 源码的**管理器，指定此次安装的 manifest：

```bash
sudo python3 manage.py rollback /srv/netblackbox/state/installations/此次时间戳_ID/manifest.json
```

该命令恢复 v1.1 原配置/程序/unit 并恢复之前 active/enabled 状态；原始日志/SQLite/incident 不删。旧代码恢复后原 `rotate 30` 风险也恢复，因此不要在回退版本长时间高流量运行；应暂停旧轮转 timer 并由维护人员安排后续升级，不能让它清掉保留期内证据。

若处于磁盘告急，先清理**非证据**数据或扩容，不能为了启动 receiver 擅自删暂停标记并忽视磁盘阈值。回滚期间保留 v1.2 暂停/observer JSON 不影响旧 schema，但回退 receiver 不理解暂停标记，必须先确认磁盘安全。

升级/回滚不改 Docker、SSH、firewall、路由、DNS，不 reboot Debian/PVE。卸载仍默认保留全部证据；`--force` 仅用于管理员确认过的本项目文件，不删除 data_dir。
