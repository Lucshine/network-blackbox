# v1.2 Syslog 可靠性设计

## 1. 时间保留与容量保护

选择方案 B：仍按日期/大小切分，`rotate -1` 禁止按轮转数量淘汰客户日志。旧 `retention.syslog_rotate` 字段保留、仍接受旧配置，但从 v1.2 起不用于客户日志删除。`syslog_days=30` 才是时间保留的依据。

唯一 Syslog 生命周期管理者是 `syslog_storage.py`，由原 `netblackbox-logrotate.timer` 每 30 秒触发，正常每 300 秒执行一次 logrotate。Agent 原有 maintenance 不再删除 syslog。维护程序持有 `<data_dir>/state/syslog.lock`，覆盖轮转、HUP、压缩与清理；验收读取也使用同一锁。不要绕过管理器直接运行 `logrotate -f`。

只管理 canonical IPv4 来源目录中的受支持名称：`YYYY-MM-DD.log`、`YYYY-MM-DD.log-YYYYMMDD[-HHMMSS][.gz]` 和旧数字后缀 `.log.N[.gz]`。保留日期始终取文件名最前面的接收日期，不用 mtime，也不用发送设备自报时间。为兼容不同本地时区的旧文件，删除阈值多留一天；保留边界内的数据即使一天切分 80 份也不会提前删除。

活跃 `.log` 永不由清理程序 unlink/compress。旧的 `.log` 先由 logrotate rename，HUP 令 rsyslog 重新开文件。Python 只压缩/删除归档，先检查 `/proc/*/fd`，已打开文件一律跳过；无法完整检查时保守跳过删除/压缩。归档名称不再被 receiver 的 dynafile 模板打开。压缩时分块、有时间预算、先 fsync 临时 gzip，再以不覆盖方式发布，最后移除原文件；已经存在的 `.gz` 不覆盖。当天和前一天归档暂不压缩，以方便当前诊断。

符号链接、硬链接、非 IPv4 目录和不符合命名的文件不清理。升级不重命名、不迁移历史证据，也不清空 SQLite。logrotate 执行前将 glob 转为经过上述校验的文件清单，避免顺带轮转其他文件。

## 2. 磁盘压力策略

优先级：**保护系统继续工作 > 持续接收新日志；保留已收集的近期证据优先于腾空间继续接收。**

- warning：剩余空间低于 `min_free_mb`（默认 1024 MiB）或受管理 syslog 达 `syslog_budget_mb` 的 80%（预算默认 1024 MiB）。产生 STORAGE_PRESSURE；暂停压缩与大 snapshot，继续轻量指标。
- critical：剩余空间低于 `syslog_stop_free_mb`（默认 256 MiB）或 syslog 达预算。持久化暂停标记并停止 **netblackbox-syslog.service**，不停止 Agent、Docker 或网络，不删除保留期内日志。
- 恢复：仅对维护器自行暂停的 receiver，在空闲超过 `min_free_mb+128 MiB` 且 syslog 小于预算 75% 时自动启动。管理员手动停止的服务不由维护器误启动。
- 暂停状态在 `state/syslog-storage.json`，API `/syslog` 可见。ExecCondition 防止已标记的 receiver 因重启再次写满磁盘。空间完全耗尽导致标记写失败时仍执行 stop 并输出 STORAGE_PRESSURE 至 journal；清理空间后需检查标记/服务，必要时由管理员启动。
- 维护器直接将压力变化写入 journald；Agent 会将采样到的压力变化写入 SQLite events。SQLite 无法写入时 API `/health` 为降级，主循环回滚未提交事务并重试，不伪称证据已保存。

这是每 30 秒抽样控制，**不是文件系统硬配额**。突发流量可能在两次检查之间越过预算，其他软件也能耗尽磁盘；不能保证任何负载下绝不填盘。强容量隔离需单独文件系统/quota，并另行审批。目录统计超出 10 秒预算或不可读取时也保守暂停 receiver 并报告 inventory_complete=false，防止把未知占用当健康；此时需要检查磁盘权限/目录规模。接收暂停期间 UDP 会丢失、TCP 连接失败；这比静默删除最近证据更明确。syslog 预算仅统计管理范围内日志；exports、旧备份、其他程序空间由总空闲阈值保护，不自动删除。

## 3. 写入策略

`syslog.write_mode`：

| 模式 | sync | flushOnTXEnd | asyncWriting | 语义 |
|---|---|---|---|---|
| performance（默认） | off | on | off | 每个 transaction 结束刷新应用缓冲到内核，减少强制同步成本 |
| durability | on | on | off | 在输出事务后请求同步文件/目录，成本更高 |

不用另一个 asynchronous writer，也不叠加额外 action queue：omfile 保持 Direct，主队列 FixedArray 4096 条、单 worker、batch 上限 128、最大消息 8 KiB。receiver 有 MemoryMax 128 MiB、CPUQuota 30%、TasksMax 64，避免无限内存或占满系统。UDP 保留原 2000/s 输入限速；达到队列/限速上限可能丢失。write failure 采用可观测失败计数，不声称所有收到的包有无限可靠的重试或磁盘队列。

UDP send 成功 → 内核收包 → rsyslog 接受并入队 → omfile 写入应用/文件系统缓存 → 文件系统与块设备提交，是不同阶段。`flushOnTXEnd` 不是 fsync；文件可读也不是突然断电后必然存在。durability 的同步请求仍受 guest 文件系统、虚拟块设备缓存、PVE 宿主 fsync/缓存策略、控制器和磁盘掉电保护约束。两种模式都不保证未进入 rsyslog/未写文件的消息能在崩溃后重现。

HUP 用于重开已轮转的文件，不是重新解析全部配置，也不是独立的断电保证。修改 receiver 配置仍需 `rsyslogd -N1` 后按升级流程重启。

参考官方行为说明：[omfile](https://docs.rsyslog.com/doc/configuration/modules/omfile.html)、[sync](https://docs.rsyslog.com/doc/reference/parameters/omfile-sync.html)、[flushOnTXEnd](https://docs.rsyslog.com/doc/reference/parameters/omfile-flushontxend.html)、[impstats](https://docs.rsyslog.com/doc/configuration/modules/impstats.html)、[统计计数语义](https://docs.rsyslog.com/doc/configuration/rsyslog_statistic_counter.html)。实际 Debian 12/13 所带版本由 CI 用真实 daemon 验证，不依赖新版本才有的统计持久化选项。

## 4. 可观测性

`GET /syslog` / `netblackbox syslog-status` 显示 receiver active、匹配 receiver PID 的 UDP/TCP listener、omfile failure/suspension 和本地写入错误状态。`write_healthy` 为观察到的本地输出状态，不是端到端/物理磁盘持久化证明；无数据时可为 null。

impstats 每 10 秒输出到专属统计文件，读取最多 256 KiB 尾部，不每 10 秒递归扫描历史日志。统计文件以 1 MiB、2 份控制大小；它是遥测而非客户日志，不采用 30 天证据策略。source dynstats 最大 256 个活动计数器，86400 秒未使用可移除；字段明确标注 **receiver 进程/动态 counter 生存期的快照**，不是跨 reboot 精确累计计数。缺失/过期数据返回 null，不填假 0。write_failure_count 为 null（完整写入失败次数不可得）；action_failure_count 是不完整的 action failed 计数，不能当成所有 dynafile 写失败或丢失消息数量。另读取该 receiver PID 最近两分钟的有界 journal 错误，弥补某些 omfile 错误不增加计数的情况。

可选 `expected_sources`，默认空数组。来源首次观测无证据为 UNKNOWN；计数变化时尝试读取当前/前一天文件最多各 64 KiB 的真实接收时间；若不能读到则使用统计变化的观测上界，并标明精度。不能因为启动后看见一个旧累计值就断言正在接收。跨 observer/receiver 重启保留 last seen 元数据但不把旧 counter 跨进程拼接为精确总数。统计容量限制/淘汰时标记计数范围，不影响原始日志接收。

RECEIVING：最近有真实消息证据；SILENT：过去收到、现在安静；UNKNOWN：没有足够证据；RECEIVER_ERROR：服务/监听/输出出现明确异常。**SILENT 不等于 DOWN**。所有验收流量带 `NETBLACKBOX_TEST_` 前缀，仍保存日志，但从真实设备活跃度计数中排除。历史 `NETBLACKBOX_VERIFY_` 同样排除。
