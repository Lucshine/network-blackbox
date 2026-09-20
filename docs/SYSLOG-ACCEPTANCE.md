# Syslog 分层验收和隔离性能测试

所有测试报文统一使用 `NETBLACKBOX_TEST_<随机UUID>`，receiver 仍落盘，但不会令真实来源的活跃度计数增加。脚本不 SSH、不改路由或 firewall，不自动登录路由器。发送端退出码 0 仅说明 socket send 完成。

## Level 1：本机组件

```bash
sudo python3 verify.py
sudo python3 verify.py --network
sudo netblackbox syslog-status
```

输出 `level=1`，只验证本机服务、监听、SQLite、UDP/TCP 接收及文件可读。`level_2/level_3` 为 NOT_TESTED。`--network` 增加网络 probe；不等于 Syslog 的跨主机验证。

CI 独立 Linux 容器使用真实 rsyslog + logrotate，覆盖 UDP/TCP、来源拒绝、omfile 写入错误、32 次同日轮转、大小轮转后继续写、两种写入模式 benchmark。此测试不使用生产 unit、不修改现场服务。程序单元测试另覆盖保留期、锁、打开文件、磁盘阈值、配置兼容和状态机恢复。

## Level 2：跨主机 LAN

1. 在 macOS 或另一台**真正外部主机**执行（文档地址请替换）：

```bash
python3 scripts/verify_remote_syslog.py send \
  --target 192.0.2.10 --port 5514 --protocol udp --output udp-send.json
python3 scripts/verify_remote_syslog.py send \
  --target 192.0.2.10 --port 5514 --protocol tcp --output tcp-send.json
```

发送端输出唯一 id 和 socket 实际使用的 source_ip，每次默认一条、最多 10 条，receiver_result 为 NOT_TESTED。可把返回 ID 直接复制到接收端检查，不需要在脚本里保存密码。

2. 在 Debian 上检查每个返回 ID，source 使用发送结果中的实际地址：

```bash
sudo python3 scripts/verify_remote_syslog.py check \
  --source 192.0.2.20 --id NETBLACKBOX_TEST_替换为返回的32位UUID \
  --since 2026-01-01 --level 2
```

`--since` 选测试当日（服务器接收日期；跨午夜时选择前一天）。默认今天；检查限定来源、日期、最多 1024 个文件/64 MiB 解压后字节，支持 `.gz`。超过预算返回 NOT_TESTED，缩小日期范围或明确增加 `--max-bytes` 后重试。

PASS 要求每个预期 seq 在文件里恰好出现一次，来源目录和日志内 source 相符，接收时间可解析。输出实际文件名、source、received_at、内容样例、重复数。如果发送服务器与来源相同，Level 2 返回 NOT_TESTED，不冒充外部链路验收；socket 经过 NAT 时请以 Debian 实际看到的源 IP 验证并记录 NAT。

## Level 3：真实设备

先生成管理员手工命令：

```bash
python3 scripts/verify_remote_syslog.py prepare-device
```

管理员在真实路由器上手动执行返回的 `logger -t netblackbox-acceptance 'NETBLACKBOX_TEST_... seq=0 device-test'`。不要把这条命令在 Debian 上执行后声称验证路由器。

在 Debian 检查：

```bash
sudo python3 scripts/verify_remote_syslog.py check \
  --source 192.0.2.1 --id NETBLACKBOX_TEST_替换为返回的32位UUID --level 3
```

UNKNOWN/SILENT 不是验收失败，也不证明接收链路损坏。只有实际执行唯一标识测试并找到正确来源报文，才能判相应级别 PASS。本轮开发未向生产发送 Level 2/3 测试消息，这两项为 NOT_TESTED。

## Performance / durability benchmark

`scripts/benchmark_syslog.py` **只支持显式标记的 Linux loopback 测试 receiver**，拒绝 `/etc/netblackbox/config.json`、生产默认数据路径和非 loopback 目标；还验证 `/proc/<PID>/cmdline` 与隔离目录关联。

推荐直接运行完整的可销毁集成环境：

```bash
# 在安装 rsyslog/logrotate 的隔离 Debian 容器/VM 中，以 root 执行。
python3 tests/integration/test_rsyslog.py
```

测试 fixture 创建独立 `/var/lib/netblackbox-integration-*`、随机 localhost 端口、独立 daemon PID，并写入 `state/benchmark-environment.json` 的随机 environment_id。测试完成清理 fixture，不触碰系统 receiver。

需要自定义参数时，对已创建并仍运行的同类 fixture 使用：

```bash
python3 scripts/benchmark_syslog.py \
  --test-config /var/lib/测试fixture/benchmark.json \
  --environment-id 从fixture读取的随机ID \
  --count 1000 --length 1024 --protocol tcp
```

不要在生产创建隔离标记来绕过保护。最大 100000 条、单条 128–4096 字节。结果分开报告发送数、发送速率、文件中唯一序号数、重复数、整批文件可见耗时。接收器内部精确 per-test 入队计数、单条写入延迟和物理磁盘持久化条数没有可靠测量手段，返回 null/NOT_AVAILABLE；不伪造这些数字。CI 小批量 loopback 数字只用于实现对照，不代表现场吞吐上限或生产 SLA。
