# PVE 未来接入（不会自动执行）

pve.enabled=false，host 留空。不得根据网段猜测或扫描 PVE。

将来另行授权后建议：

1. 在 PVE 配置 persistent journald 与容量限制，修改前备份。
2. 重启后保存 `journalctl --list-boots`、`journalctl -b -1`、`journalctl -k -b -1`。
3. 保存物理 NIC / vmbr 的 `ip -s link`、`bridge link`、ethtool 和 driver stats，查找 link flap、reset、watchdog、tx timeout 等。
4. 在 PVE 创建开机 oneshot 服务，把上一 boot journal/kernel journal 导出到持久目录，命令设 timeout，stdout/stderr 均保存，缺历史 boot 时也明确记录。不要写 /tmp。
5. future pve.enabled 可启用 ping；api_health_enabled 需另行启用并配置可信证书，不猜测认证、不跳过 TLS 验证。

LXC 的虚拟网卡与 kernel journal 不等于宿主机物理网卡证据。PVE 掉电时 Debian LXC 同样停止，不能继续采样。
