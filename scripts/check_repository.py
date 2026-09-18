#!/usr/bin/env python3
"""Check versioned text and optional Git history for private deployment data."""
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
DOCUMENTATION=[ipaddress.ip_network(n) for n in ('192.0.2.0/24','198.51.100.0/24','203.0.113.0/24')]
PUBLIC_PROBES={'1.1.1.1','223.5.5.5'}
PATTERNS={
    'private key':r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
    'GitHub token':r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
    'access key':r'\bAKIA[0-9A-Z]{16}\b',
    'personal home path':r'(?:/Users/|/home/)[A-Za-z0-9_.-]+',
}
BAD_NAMES={'site.json','config.json','install-state.json','audit-before.txt','audit-detail.txt'}


def scan(label,data):
    issues=[]
    for name,pattern in PATTERNS.items():
        if re.search(pattern,data):issues.append((label,name))
    for value in set(re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])',data)):
        try:a=ipaddress.ip_address(value)
        except ValueError:continue
        if a.is_loopback or a.is_unspecified or a.is_multicast or str(a) in PUBLIC_PROBES or any(a in n for n in DOCUMENTATION):continue
        issues.append((label,'non-documentation network address'))
    return issues


def git(*args):
    return subprocess.run(['git',*args],cwd=ROOT,capture_output=True,check=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--history',action='store_true');args=p.parse_args()
    listing=git('ls-files','-z')
    if listing.returncode==0 and listing.stdout:
        paths=[ROOT/x.decode() for x in listing.stdout.split(b'\0') if x]
    else:
        paths=[p for p in ROOT.rglob('*') if p.is_file() and not any(x in p.relative_to(ROOT).parts for x in ('.git','dist','__pycache__'))]
    findings=[]
    for path in paths:
        label=str(path.relative_to(ROOT))
        if path.name in BAD_NAMES or path.suffix in ('.sqlite3','.db','.log','.gz','.zip'):
            findings.append((label,'runtime/config/archive file must not be versioned'))
        try:text=path.read_text()
        except UnicodeDecodeError:
            findings.append((label,'unexpected binary file'));continue
        findings.extend(scan(label,text))
    if args.history:
        objects=git('rev-list','--objects','--all')
        if objects.returncode:
            raise SystemExit('Git history is unavailable: '+objects.stderr.decode(errors='replace').strip())
        for row in objects.stdout.decode().splitlines():
            oid,_,name=row.partition(' ')
            kind=git('cat-file','-t',oid).stdout.strip()
            if kind not in (b'blob',b'commit'):continue
            text=git('cat-file','-p',oid).stdout.decode(errors='replace')
            findings.extend(scan('history:'+oid[:12]+(':'+name if name else ''),text))
    if findings:
        for label,reason in findings:print(f'FAIL {label}: {reason}')
        raise SystemExit(1)
    print(f'PASS: {len(paths)} files checked'+('; all reachable Git history checked' if args.history else ''))

if __name__=='__main__':main()
