# PR #1 最终复审：时区与生产升级安全

## 基线与交付定位

获取远端 PR 后确认 HEAD 与上一轮 `b00dadf87a1fc0df7ebe320ceb973d01f085c1fd` 相同；没有覆盖其他新增提交。所有修改在 `feature/v1.2-syslog-reliability`。本报告随代码提交，因此不嵌入自身未来 commit hash；最终 HEAD SHA、最后 CI URL 和验收结果在 PR 最新交付记录中精确给出。

本轮未访问/修改生产 Debian、ImmortalWrt 或 PVE，未部署、未重启生产服务、未合并 main、未删除生产证据。

## A. 时区根因与处理

- 模板 `%timegenerated:1:10:date-rfc3339%` 使用 rsyslog 接收时间，不是设备报文头的 timereported。
- 没有使用 `date-utc`，文件名日期随接收服务的本地时区；正文 `%timegenerated:::date-rfc3339%` 带时区偏移。
- 旧 `recent_file_time()` 用 UTC 当天/前一天，上海本地进入次日时会漏找唯一的当天文件。retention/压缩日期也曾用 UTC，语义不一致。
- 新 `app/log_time.py:receive_day()` 使用服务器本地日历日期；recent_file_time、归档保留/压缩，以及验收脚本的默认搜索日起点与之统一。内部 event/incident UTC 时间不改。
- Receiver/系统重启后 counter 为 0 的首次采样，也会对预期来源执行一次有界文件基线读取；Observer 保存 baseline 标记，安静时不反复扫描。
- 不改 rsyslog 模板格式，不重命名/迁移历史文件；已有多留一天的保守保留余量不变。要求 receiver、Agent、guard 使用同一主机时区，不配置独立 TZ override。

官方说明：[timegenerated](https://docs.rsyslog.com/doc/reference/properties/message-timegenerated.html)、[property replacer/date-utc](https://docs.rsyslog.com/doc/configuration/property_replacer.html)。隔离真实 rsyslog 测试另以 UTC/Asia/Shanghai 验证收到的文件名和正文偏移，并发送旧的设备时间，证明取的是接收时间。

## B. 发现并修复的升级阻塞问题

| 风险 | 修复 | 验证边界 |
|---|---|---|
| 替换配置时旧 Agent/receiver 仍运行 | 先停止 timer→guard→receiver→Agent，确认停止后才替换 | 文件与顺序模型测试；真实 systemd 生命周期待演练 |
| 预检后收件增长导致超预算 | 初始预检、收件排空后、启动前、验收后复检容量 | 临时文件 TOCTOU 注入；不宣称消除其他共享盘写入 |
| 缺失/损坏旧安装 manifest 或备份 | 校验 owner/data_dir、必需文件、rollback 入口、阶段、服务状态与备份路径/可选 SHA | legacy manifest 接受、缺失/错误备份拒绝 |
| 回滚忽略停止失败 | 停止失败则不覆写运行中的新程序；恢复/daemon-reload 失败不盲目启动 | 失败注入模型 |
| static oneshot 被误 enable/disable | 单独处理可安装单元，保留 enabled/disabled/enabled-runtime 与原 active | 状态模型回归 |
| 只启动 timer 未验证新 guard 能运行 | 先启动真实 guard oneshot 的“升级仅校验”路径；不轮转/不清理证据 | guard 逻辑测试；systemd 启动调用模型 |
| 新 Agent 验收期间清理历史证据 | 事务标记存在时 Agent maintenance 只报告容量，不删除旧 metrics/events/incident | 真实 SQLite/incident 文件保留测试 |
| 新暂停标记污染回滚旧版本 | 停止后备份 pause/error 控制元数据；回滚先归档新状态再恢复旧值 | 真文件内容测试，证据不删 |
| Python 异常退出无法定位半成品 | 全量 preimage/校验值先 fsync，再持久化阶段标记；SIGTERM/普通异常回滚；不可捕获退出留手工恢复入口 | 子进程 os._exit 后真实文件恢复；不是断电测试 |
| 并行安装/回滚竞争 | Linux 部署排他锁；read-only --check 不创建锁 | 不支持外部管理员绕过锁同时改文件 |
| 暂停标记 JSON 损坏被当作无标记 | 严格校验、不覆盖原坏文件，生成 manual_intervention 错误；receiver 启动拒绝 | 损坏/错误类型/NaN 单元测试 |
| 暂停后归档过期容量下降 | 删除允许的关闭过期归档后再判断恢复，可同周期 resume | 真实 rsyslog 进程暂停/再启动/UDP/TCP 收件 |
| 管理员原先停服务仍被自动 start | 不对原先 inactive 的 receiver 取得自动启动权 | 状态模型测试 |
| 统计/恢复异常频繁 stop/start | 不确定状态要求人工介入；resume 失败保留原因并 300秒 backoff | 状态模型测试 |

前一轮压力域隔离与 UNKNOWN 健康语义保留全部测试。快照完成标记/未完成调度恢复、SQLite WAL 及正常证据保留机制未破坏。

## C. 升级状态与回滚

详见 [可执行 SOP](UPGRADE-v1.2.md)，包含 commit/版本/预算检查、独立存储备份与校验、服务顺序图、UDP 中断窗口、逐阶段回滚和恢复命令。

新流程：预检 → 暂存语法校验 → 完整备份与事务标记 → 停全部项目服务 → 复检容量 → 写程序/配置/unit → daemon-reload → 新 guard 仅校验 → 再查容量 → receiver → Agent → 验收与容量复检 → timer → 完成。

发生错误恢复旧程序、配置、unit、active/enabled；不回退/删除已有 SQLite/syslog/incident，也不改 Docker/网络。已经执行的 APT 包动作不自动降级。

SIGKILL/主机掉电不能由 Python 异常处理捕获。备份与阶段标记用于手工恢复；候选启动检查拒绝无活跃安装器的升级标记。**旧 v1.1 单元尚未替换的窗口不能保证任意掉电后原子恢复**，这不是完整 A/B 系统更新器。真实 systemd 演练和批准窗口仍是生产前置条件。

## D. 测试清单和状态

| 类型 | 执行方式 | 状态 |
|---|---|---|
| 既有 Agent、Syslog、安装器单元 | 本地及 Debian CI，临时目录 | PASS；原三组加新增保护共61项（Agent15、安装器21、Syslog25） |
| 跨时区/跨午夜 | 独立 TZ 子进程：UTC、Asia/Shanghai、America/Los_Angeles；每个验证真实文件、解析时间、来源状态、重启/首采与 retention | PASS |
| 升级顺序/失败回滚 | 真文件和备份 + 模拟服务执行器，覆盖写入后、receiver/Agent/guard/timer/验收失败、KeyboardInterrupt、容量二次检查失败、停止失败 | PASS；升级专项9项，其中阶段失败包含9个子场景；不能当作真实 systemd 验证 |
| 不可捕获进程退出 | 独立子进程 os._exit，检查留下的 manifest/备份，手动恢复文件与控制状态 | PASS；不是整机断电 |
| 真实 rsyslog 集成 | Debian 12/13 容器，UDP/TCP、ACL、omfile 失败、32次轮转、真实FD保护、双模式 benchmark | CI 以最终 SHA 为准 |
| 真实 rsyslog 本地时区与暂停/恢复 | 子进程 receiver，TZ 环境与旧报文时间；预算暂停、过期归档清理、恢复后 UDP/TCP | CI 以最终 SHA 为准 |
| 发布包/配置语法/历史隐私 | build、解压入口、rsyslogd -N1、logrotate -d、systemd-analyze verify | CI 以最终 SHA 为准 |
| 真 systemd v1.1→v1.2 安装/自动回滚/开机恢复 | 当前 CI 容器没有运行 systemd PID1；未提供隔离 VM | **NOT_TESTED** |
| 生产跨主机/真实设备/磁盘耗尽/断电 | 本轮禁止生产修改和破坏性测试 | **NOT_TESTED** |

没有将 mock/systemd-analyze 的通过包装成真实 systemd active/enabled 生命周期通过，也没有在生产构造高流量或填盘。

单元测试总数为80：基础回归61项 + 时区3项 + guard恢复7项 + 升级专项9项。隔离真实 rsyslog 集成为7项。最终 SHA 上的 CI 仍必须重新运行，结果由 PR 精确链接记录。

## E. 变更文件

- `app/log_time.py`：统一本地接收日期。
- `app/netblackbox.py`：升级验证期间冻结历史 retention，继续报告压力。
- `app/syslog_status.py`：本地日期查找和 guard 人工介入状态。
- `app/syslog_storage.py`：统一日期、严格标记、暂停归属、清理后恢复、退避和升级校验模式。
- `app/config_tools.py`：Agent 候选升级启动保护。
- `manage.py`：manifest/权限/容量安全检查，排他事务、quiesce、完整备份、明确阶段与回滚。
- `scripts/verify_remote_syslog.py`：默认搜索日期与 receiver 一致。
- `scripts/build_release.py`：包含新模块/测试/文档。
- `tests/test_timezones.py`、`tests/test_guard_recovery.py`、`tests/test_upgrade.py`：新增回归。
- `tests/test_syslog.py`、`tests/test_portable.py`：适配显式测试环境、保持原回归。
- `tests/integration/test_rsyslog.py`：真实本地时区与暂停恢复。
- `.github/workflows/ci.yml`：上述测试及 tzdata。
- `docs/UPGRADE-v1.2.md`、`docs/SYSLOG-DESIGN.md`、`docs/CONFIGURATION.md`、`docs/V1.2-REPORT.md`、本报告：SOP 与准确边界。

## F. 合并与生产结论

- **代码审查**：本报告发现的代码阻塞已修复并增加回归；需维护者审阅 PR，未自动合并。
- **CI**：最终提交必须重新通过 Debian 12/13，精确 HEAD/URL 在 PR 交付记录中；历史成功不代替最终 SHA。
- **生产升级验证**：**未通过/未执行**。真实 systemd 升级/回滚/重启演练、现场新日志验收和备份确认尚未完成，不应把 CI 通过当作立即生产部署授权。

在最终 CI 通过且维护者认可这些已声明边界后，可进入代码合并评审；生产部署仍需隔离 VM 演练、审批与 SOP 验收。主机突然掉电、硬盘故障和日志保护硬配额仍属于系统级设计，不能由本版本作绝对保证。
