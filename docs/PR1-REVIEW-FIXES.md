# PR #1 审阅修复报告

审阅基线：`734e5670df69479c4e822e5c99156d1f772d2a1f`。工作分支：`feature/v1.2-syslog-reliability`。只修改该分支；不合并、不部署生产。

## 1. P1：隔离存储压力域

原实现把 receiver guard 的 Syslog 预警 OR 进全局 storage_pressure，快照调度将待执行任务永久标为 skipped。现在 `maintenance()` 返回 `domains.disk`、`domains.incident`、`domains.syslog`，分别存储状态并产生带 component 的变化事件。兼容汇总字段只包含 disk/incident；`jobs()` 不再读取旧汇总值，只检查 incident 自身压力和实时磁盘余量。

回归测试用临时稀疏日志达到 Syslog 预算 85%，总盘模拟空闲 8 GiB，运行真实 maintenance、数据库状态处理与快照调度。只有诊断快照内容执行器替换为测试 stub（避免外部探测）；任务最终为 done、无 skipped，Syslog 预警仍被记录。另覆盖持久化旧汇总标志为 true、真正磁盘/incident 压力仍会保护快照、按域事件不会重复。

不自动改写或重放此前已 skipped 的历史任务；修复后的待执行任务不再受 Syslog 独立预警抑制。

## 2. P2：监听未知时不得报告 HEALTHY

HEALTHY 要求 service_active、udp_listening、tcp_listening、write_healthy 全部明确 true。任何明确 false 仍为 RECEIVER_ERROR；没有明确错误但存在未知项时为 UNKNOWN。

`ss` 非零退出、超时、stdout 截断或不可解析时不能证明监听状态。监听记录缺 PID 所属信息，或 receiver PID 无效时，也返回未知。成功、完整且可解析的 `ss` 输出中明确缺少相应 listener 才返回 false。来源 SILENT/RECEIVING 继续依据消息证据，不把未知检查变成设备 DOWN。

新增回归覆盖失败/超时（包括残留正常输出）、截断、格式异常、缺 PID、部分未知、明确故障优先级及正常正反例。Debian 隔离集成继续用真实 `ss` 检查真实 receiver PID。

## 3. P1：升级容量预检

`manage.py preflight()` 复用 `syslog_storage.inventory()` 的受管理文件范围和字节口径，在 APT、安装审计、配置替换、timer/service 操作前统计已有 Syslog。达到或超过候选 `syslog_budget_mb` 即阻止升级（等于预算也会触发运行时暂停，因此同样拒绝）。

错误提示包含已用 bytes/MiB、预算、调整 `retention.syslog_budget_mb` 并重新执行 `--config ... --replace-config --check` 的命令，以及管理员批准后归档到独立存储的选项。不会自动删除证据、轮转或暂停当前服务。不可读取/超时统计不会按 0 处理，而是拒绝继续。新空目录可通过且保持不创建；正常预检报告包括用量与预算。

预检不取得写锁、不写状态文件；这是当前时刻的检查，不冻结实时收件或预留磁盘。运行时 guard 继续负责后续增长。

## 4. 测试报告

本地执行：

| 测试 | 数量 | 结果 |
|---|---:|---|
| Agent 单元 | 14 | PASS |
| 安装器回归 | 21 | PASS |
| Syslog 单元 | 25 | PASS |
| Python 编译、shell 语法、Git 历史隐私检查 | — | PASS |

合计 60 项单元测试；新增 12 项覆盖三个审阅问题。安装容量测试使用真实临时文件/压缩历史文件和真实 inventory；仅替换系统环境/服务命令，验证失败前没有调用服务/APT/事务/审计写入，文件指纹不变。

Debian 12/13 使用现有 CI：完整 60 项单元测试、5 项真实 rsyslog/logrotate 集成测试（含 benchmark）、配置语法校验、隐私检查与打包。最终远端运行状态及修复 SHA 将附于 PR 描述/交付回复；未通过的运行不能标记为 PASS。

生产 Debian、ImmortalWrt、PVE 与现有服务：NOT_TESTED / 未访问、未修改；本次不执行现场验收或部署。

## 5. 文件范围

- `app/netblackbox.py`：压力分域、按域事件、隔离快照决策。
- `app/syslog_status.py`：监听检查三态与健康聚合。
- `manage.py`：只读升级容量预检及可操作错误。
- `tests/test_agent.py`、`tests/test_syslog.py`、`tests/test_portable.py`：新增回归。
- `docs/SYSLOG-DESIGN.md`、`docs/CONFIGURATION.md`、`docs/UPGRADE-v1.2.md`：同步状态、预检和边界。
- `docs/V1.2-REPORT.md`：链接本报告，保留先前实测记录。
- `docs/PR1-REVIEW-FIXES.md`：本报告。
- `scripts/build_release.py`：把新增报告加入发布包，避免文档链接缺失。

SQLite schema、现有日志格式、receiver 写入参数、监听地址与生产服务配置不变。升级/回滚仍按现有 manifest 备份机制执行，本次不执行该流程。
