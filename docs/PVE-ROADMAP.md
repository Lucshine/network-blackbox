# PVE 只读采集路线图

## 当前实现审查

`app/netblackbox.py:probes` 仅在 `pve.enabled=true` 时增加 PVE ping；`api_health_enabled=true` 才用现有 HTTPS 探测函数 GET `/api2/json/version`。结果保存在 probe 的 `detail.pve_ping/pve_api`；它们不参与核心 gateway/internet/DNS/HTTPS incident 分类。

当前默认 `enabled=false`、`host=""`、`port=8006`、API health 关闭。没有配置或猜测现场 PVE 地址，也没有在本轮任务访问 PVE。

| 能力 | 现在的状态 | 下一阶段建议 |
|---|---|---|
| 主机 ping | 已实现，可选 | 保留独立状态/时间，明确 ICMP 不等于节点健康 |
| API endpoint health | 已实现最小无认证 HTTPS 探测 | 可信 CA、超时、明确 HTTP/认证错误分类 |
| API token/权限配置 | 未实现 | 单独 root-only secret 文件，禁止日志输出 token |
| VM/LXC 运行状态 | 未实现 | 只读 status/current 与资源列表，保存节点/guest ID/状态 |
| 物理 NIC 与 bridge | 未实现 | 宿主本地 collector 读取 sysfs、ip/bridge、ethtool |
| 宿主 CPU/RAM | 未实现 | API 节点状态与本地 proc/cgroup 交叉校验 |
| persistent journal | 只有操作建议 | 获批后创建受限 journald drop-in，容量配置先审查 |
| 上一个 boot journal | 只有操作建议 | 开机 oneshot 在本地持久盘保存上一 boot kernel/system journal |

## 权限与只读边界

API 集成需要专用账号和最小只读权限。候选角色为 PVEAuditor，按节点、VM 和存储资源范围裁剪；实施时针对实际 PVE 版本核对 API viewer 的每个 endpoint 权限，不能把“只读”当作可以读取一切敏感配置。推荐独立且有有效期的 API token，权限分离，TLS 使用可信 CA；不要猜密码或默认关闭证书验证。

节点状态、guest 运行状态、计数器和 journal 读取不需要写入虚拟机配置。物理驱动统计可能需要宿主 root/CAP_NET_ADMIN；不应因此向远程 token 授予管理权限。建议宿主独立 collector，以受限 systemd 单元执行明确的只读命令，然后输出本地持久 JSON。内核 journal 可通过 systemd-journal 组读取，ethtool 权限按实际驱动验证。

配置 persistent journal、安装 collector/oneshot 和建立只读账号本身是**部署变更**，需要下一阶段明确批准，本次只形成方案。

## 故障域与存储设计

Debian 如果是同一 PVE 上的 guest，PVE 整机故障时两者会同时停止。单靠该 guest 不能证明宿主掉电期间发生了什么：

1. PVE 本地保存 journal、NIC/bridge 计数器和 boot ID，重启后立即导出上一 boot 日志；禁止仅写 /tmp。
2. 使用不同物理故障域的外部观察者（独立硬件或其他站点），低流量检测 PVE/现场可达性。云 heartbeat 只提供最后可达时间，不能代替宿主日志。
3. 将宿主日志异步复制到独立存储，断链时本地缓冲且容量有界。复制应有认证与加密；不要经隧道大量传输所有原始日志。
4. 对齐 UTC、boot ID、collector identity、观测时间与采集空窗。远端观察者失联只是故障域线索，不能直接认定 PVE 根因。
5. 验证 API token 撤销、宿主重启、上一 boot 缺失、网络隔离、磁盘不足等条件；破坏性用例只能在测试节点执行。

下一阶段交付应包含专用 collector、endpoint/权限表、secret 管理、持久化/retention、跨故障域设计和独立验收报告，不与本次 Syslog 可靠性变更混合部署。
