# ImmortalWrt / OpenWrt 手工接入

安装器不会连接或修改路由器。服务器配置完成后，请管理员在 LuCI 系统日志设置中填写：

- Remote log server：site.json 的 syslog.listen_address（新 Debian 的 LAN IP）。
- Remote log port：syslog.port（默认 5514）。
- Protocol：UDP。

菜单位置、字段名随版本/主题/翻译不同，请先查看实际界面。参考 UCI 命令（下面是示例，替换地址/端口；只由管理员手动在路由器执行）：

```sh
uci set system.@system[0].log_ip='192.0.2.10'
uci set system.@system[0].log_port='5514'
uci set system.@system[0].log_proto='udp'
uci set system.@system[0].log_remote='1'
uci commit system
/etc/init.d/log restart
```

先备份路由器自身 /etc/config/system。配置后手动产生一条 logger 日志，在 Debian 的 `<data_dir>/syslog/<路由器源IP>/` 验证收到。只看到 Debian 自测日志并不代表路由器已经接入。

UDP 不保证送达；路由器至 Debian 的 LAN 路径中断时可能丢失远端 syslog，本地主动采集仍继续。今后可自行评估 TCP。

官方参考：https://openwrt.org/docs/guide-user/base-system/log.essentials
