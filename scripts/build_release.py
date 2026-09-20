#!/usr/bin/env python3
"""Explicit allowlist: never ship site data, credentials, caches or audit files."""
import gzip
import hashlib
import io
from pathlib import Path
import tarfile

root=Path(__file__).resolve().parents[1]
version=(root/'VERSION').read_text().strip()
files=['VERSION','README.md','LICENSE','CONTRIBUTING.md','config.example.json','manage.py','verify.py','install.sh','uninstall.sh',
       'app/netblackbox.py','app/config_tools.py','app/syslog_storage.py','app/syslog_status.py','app/log_time.py','app/simulate_failure.py','app/agent.service.in',
       'scripts/verify_remote_syslog.py','scripts/benchmark_syslog.py','tests/test_syslog.py','tests/test_timezones.py','tests/test_guard_recovery.py','tests/test_upgrade.py','tests/test_uninstall.py','tests/integration/test_rsyslog.py','tests/test_agent.py','tests/test_portable.py',
       'docs/CONFIGURATION.md','docs/OPERATIONS.md','docs/IMMORTALWRT.md','docs/PVE.md','docs/SYSLOG-DESIGN.md','docs/SYSLOG-ACCEPTANCE.md','docs/UPGRADE-v1.2.md','docs/PVE-ROADMAP.md','docs/V1.2-REPORT.md','docs/PR1-REVIEW-FIXES.md','docs/FINAL-SAFETY-REVIEW.md']
release=root/'dist';release.mkdir(exist_ok=True)
archive=release/f'netblackbox-{version}.tar.gz'
with archive.open('wb') as raw:
    with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as tar:
            for name in sorted(files):
                path=root/name
                if not path.is_file() or path.is_symlink():raise ValueError('Missing/unsafe release input: '+name)
                content=path.read_bytes()
                info=tarfile.TarInfo(f'netblackbox-{version}/{name}')
                info.size=len(content);info.mtime=0;info.uid=info.gid=0;info.uname=info.gname='root'
                info.mode=0o755 if name.endswith('.sh') or name in ('manage.py','verify.py') else 0o644
                tar.addfile(info,io.BytesIO(content))
sha=hashlib.sha256(archive.read_bytes()).hexdigest()
(archive.with_suffix(archive.suffix+'.sha256')).write_text(f'{sha}  {archive.name}\n')
print(f'{archive}\nSHA256 {sha}\n{archive.stat().st_size} bytes; {len(files)} files')
