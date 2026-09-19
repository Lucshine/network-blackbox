"""Validated site configuration and deterministic config generation, no host writes."""
import ipaddress
import json
from pathlib import Path
import re
import urllib.parse


def apply_defaults(c):
    """Additive v1.1 -> v1.2 migration; data paths and legacy keys are retained."""
    sy=c['syslog']
    for k,v in {'write_mode':'performance','expected_sources':[], 'silent_seconds':300}.items():
        sy.setdefault(k,v)
    r=c['retention']
    r.setdefault('syslog_budget_mb',1024)
    r.setdefault('syslog_stop_free_mb',min(256,max(32,r['min_free_mb']//4)))
    return c


def validate(c):
    apply_defaults(c)
    def number(v,low,high,name):
        if type(v) is not int or not low <= v <= high:
            raise ValueError(f'{name}: expected integer {low}..{high}')
    if not isinstance(c['site'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',c['site']):
        raise ValueError('site: use 1..64 letters, numbers, dot, underscore or dash')
    path=c['data_dir']
    if not re.fullmatch(r'/(srv|var/lib)/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*',path):
        raise ValueError('data_dir must be a dedicated path below /srv or /var/lib, without spaces or ..')
    if c['api']['host'] != '127.0.0.1':
        raise ValueError('API must stay on 127.0.0.1')
    number(c['api']['port'],1024,65535,'api.port')
    sy=c['syslog']
    if sy['write_mode'] not in ('performance','durability'): raise ValueError('syslog.write_mode must be performance or durability')
    number(sy['silent_seconds'],30,86400,'syslog.silent_seconds')
    if not isinstance(sy['expected_sources'],list) or len(sy['expected_sources'])>256:raise ValueError('expected_sources requires at most 256 IPv4 addresses')
    for source in sy['expected_sources']:ipaddress.IPv4Address(source)
    listen=ipaddress.IPv4Address(sy['listen_address'])
    if listen.is_unspecified or listen.is_loopback or listen.is_multicast or listen.is_reserved:
        raise ValueError('syslog.listen_address must be a specific unicast LAN IPv4 address')
    number(sy['port'],1024,65535,'syslog.port')
    if sy['port']==c['api']['port']:
        raise ValueError('Syslog and API ports must differ')
    if not sy['allowed_networks'] or len(sy['allowed_networks'])>16:
        raise ValueError('syslog.allowed_networks needs 1..16 IPv4 CIDRs')
    nets=[ipaddress.IPv4Network(n,strict=True) for n in sy['allowed_networks']]
    if any(n.prefixlen==0 for n in nets) or not any(listen in n for n in nets):
        raise ValueError('CIDRs must include server IP and must not allow 0.0.0.0/0')
    for k in ('gateway','router_dns'):
        ipaddress.ip_address(c[k])
    for k,min_count in [('public_ips',2),('public_dns',1)]:
        if not min_count <= len(c[k]) <= 8:
            raise ValueError(f'{k}: expected {min_count}..8 targets')
        for a in c[k]: ipaddress.ip_address(a)
    if not 2 <= len(c['https_urls']) <= 8: raise ValueError('Need 2..8 HTTPS targets')
    for url in c['https_urls']:
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or any(ch.isspace() for ch in url):
            raise ValueError('Probe URL must be HTTPS without userinfo/whitespace')
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',c['dns_name']): raise ValueError('Invalid dns_name')
    number(c['probe_interval_seconds'],5,20,'probe_interval_seconds')
    number(c['host_interval_seconds'],10,300,'host_interval_seconds')
    for k,v in c['timeouts'].items(): number(v,1,15,'timeouts.'+k)
    for k in ('ping','dns','https','snapshot_command'): c['timeouts'][k]
    for k in ('failure_threshold','recovery_threshold'): number(c['incident'][k],2,30,'incident.'+k)
    if c['incident']['snapshot_offsets_seconds'] != [0,15,60]:
        raise ValueError('snapshot_offsets_seconds must be [0,15,60] in this release')
    for k in ('nic_error_delta','nic_drop_delta','nic_anomaly_hold_seconds'): number(c['incident'][k],1,1000000,'incident.'+k)
    r=c['retention']
    for k,low,high in [('metrics_days',7,365),('events_days',90,3650),('incident_days',90,3650),('syslog_days',30,3650),('maintenance_seconds',30,3600),('min_free_mb',128,1048576),('snapshot_budget_mb',128,1048576),('snapshot_command_max_bytes',8192,1048576),('syslog_maxsize_mb',1,1024),('syslog_rotate',30,10000),('journal_max_use_mb',32,65536),('journal_days',1,3650)]:
        number(r[k],low,high,'retention.'+k)
    number(r['syslog_budget_mb'],32,1048576,'retention.syslog_budget_mb')
    number(r['syslog_stop_free_mb'],16,r['min_free_mb']-1,'retention.syslog_stop_free_mb')
    for section,key in [('cloud','enabled'),('pve','enabled'),('journald','configure_persistent')]:
        if type(c[section][key]) is not bool: raise ValueError(f'{section}.{key} must be boolean')
    cloud=c['cloud']
    number(cloud['interval_seconds'],60,86400,'cloud.interval_seconds')
    number(cloud['timeout_seconds'],1,10,'cloud.timeout_seconds')
    if cloud['mode'] not in ('json','kuma'): raise ValueError('cloud.mode: json or kuma')
    if cloud['push_url']:
        u=urllib.parse.urlsplit(cloud['push_url'])
        if u.scheme!='https' or not u.hostname or u.username or u.password or any(x.isspace() for x in cloud['push_url']):
            raise ValueError('Invalid HTTPS cloud URL')
    if cloud['enabled'] and not cloud['push_url']: raise ValueError('cloud.push_url required when enabled')
    if c['pve']['enabled']: ipaddress.ip_address(c['pve']['host'])
    number(c['pve']['port'],1,65535,'pve.port')
    return c


def render(c,app_dir):
    validate(c)
    root=c['data_dir'];sy=c['syslog'];r=c['retention']
    rules=[]
    for text in sy['allowed_networks']:
        n=ipaddress.IPv4Network(text)
        rules.append(f'($.source >= {int(n.network_address)} and $.source <= {int(n.broadcast_address)})')
    acl=' or '.join(rules)
    syslog=f'''# Network Blackbox: independent rsyslog instance, no local inputs/includes.
global(workDirectory="{root}/state/rsyslog" maxMessageSize="8k")
main_queue(queue.type="FixedArray" queue.size="4096" queue.workerThreads="1" queue.dequeueBatchSize="128")
module(load="impstats" interval="10" severity="7" resetCounters="off"
       log.syslog="off" log.file="{root}/state/rsyslog/stats.log" format="json")
dyn_stats(name="netblackbox_sources" resettable="off" maxCardinality="256" unusedMetricLife="86400")
module(load="imudp")
module(load="imtcp")
$AllowedSender UDP, {', '.join(sy['allowed_networks'])}
$AllowedSender TCP, {', '.join(sy['allowed_networks'])}
template(name="NetBlackboxPath" type="string" string="{root}/syslog/%fromhost-ip%/%timegenerated:1:10:date-rfc3339%.log")
template(name="NetBlackboxLine" type="string" string="%timegenerated:::date-rfc3339% source=%fromhost-ip% hostname=%hostname% %syslogtag% %msg:::drop-last-lf%\\n")
ruleset(name="NetBlackboxLAN") {{
    set $.source = ipv42num($fromhost-ip);
    if not ({acl}) then {{ stop }}
    # Count accepted source messages independently of successful file output.
    # Test traffic is intentionally excluded from real-device activity status.
    if not ($msg contains "NETBLACKBOX_TEST_" or $msg contains "NETBLACKBOX_VERIFY_") then {{
        set $.counter = dyn_inc("netblackbox_sources", $fromhost-ip);
    }}
    action(name="netblackbox_write" type="omfile" dynaFile="NetBlackboxPath" template="NetBlackboxLine"
           dirCreateMode="0700" fileCreateMode="0600" dynaFileCacheSize="32"
           queue.type="Direct" asyncWriting="off" flushOnTXEnd="on"
           sync="{'on' if sy['write_mode']=='durability' else 'off'}"
           action.resumeRetryCount="0")
    stop
}}
input(type="imudp" address="{sy['listen_address']}" port="{sy['port']}" ruleset="NetBlackboxLAN" ratelimit.interval="1" ratelimit.burst="2000")
input(type="imtcp" address="{sy['listen_address']}" port="{sy['port']}" ruleset="NetBlackboxLAN" MaxSessions="32")
'''
    agent=(Path(app_dir)/'agent.service.in').read_text().replace('ReadWritePaths=/srv/netblackbox','ReadWritePaths='+root)
    agent=agent.replace('TimeoutStopSec=60','TimeoutStartSec=90\nTimeoutStopSec=90')
    receiver=f'''[Unit]
Description=Network Blackbox isolated LAN syslog receiver
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecCondition=/usr/bin/python3 /opt/netblackbox/syslog_storage.py --receiver-allowed
ExecStartPre=/usr/sbin/rsyslogd -N1 -f /etc/netblackbox/rsyslog.conf
ExecStart=/usr/sbin/rsyslogd -n -f /etc/netblackbox/rsyslog.conf -i /run/netblackbox-syslog/pid
ExecReload=/bin/sh -c '/usr/sbin/rsyslogd -N1 -f /etc/netblackbox/rsyslog.conf && /bin/kill -HUP "$MAINPID"'
RuntimeDirectory=netblackbox-syslog
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={root}
ProtectHome=true
PrivateTmp=true
MemoryMax=128M
TasksMax=64
CPUQuota=30%
Nice=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''
    rotate=f'''{root}/syslog/*/*.log {{
    daily
    rotate -1
    maxsize {r['syslog_maxsize_mb']}M
    nocompress
    missingok
    notifempty
    nocreate
    dateext
    dateformat -%Y%m%d-%H%M%S
    sharedscripts
    su root root
    postrotate
        /usr/bin/systemctl kill -s HUP --kill-who=main netblackbox-syslog.service
    endscript
}}
{root}/state/rsyslog/stats.log {{
    size 1M
    rotate 2
    nocompress
    missingok
    notifempty
    nocreate
    su root root
    postrotate
        /usr/bin/systemctl kill -s HUP --kill-who=main netblackbox-syslog.service
    endscript
}}
'''
    rotate_unit=f'''[Unit]
Description=Rotate Network Blackbox syslog

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/netblackbox/syslog_storage.py
TimeoutStartSec=60
MemoryMax=128M
CPUQuota=20%
Nice=15
'''
    timer='''[Unit]
Description=Guard Network Blackbox storage every 30 seconds

[Timer]
OnBootSec=15s
OnUnitActiveSec=30s
RandomizedDelaySec=2

[Install]
WantedBy=timers.target
'''
    files={
        '/etc/netblackbox/config.json':(json.dumps(c,indent=2)+'\n',0o600),
        '/etc/netblackbox/rsyslog.conf':(syslog,0o600),
        '/etc/netblackbox/logrotate.conf':(rotate,0o600),
        '/etc/systemd/system/netblackbox.service':(agent,0o644),
        '/etc/systemd/system/netblackbox-syslog.service':(receiver,0o644),
        '/etc/systemd/system/netblackbox-logrotate.service':(rotate_unit,0o644),
        '/etc/systemd/system/netblackbox-logrotate.timer':(timer,0o644),
        '/usr/local/bin/netblackbox':('#!/bin/sh\nexec /usr/bin/python3 /opt/netblackbox/netblackbox.py "$@"\n',0o755),
    }
    if c['journald']['configure_persistent']:
        files['/etc/systemd/journald.conf.d/60-netblackbox.conf']=(f'[Journal]\nStorage=persistent\nSystemMaxUse={r["journal_max_use_mb"]}M\nSystemKeepFree=1G\nMaxRetentionSec={r["journal_days"]}d\nSyncIntervalSec=30s\n',0o644)
    return files
