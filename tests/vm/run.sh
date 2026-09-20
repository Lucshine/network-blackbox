#!/bin/bash
# Only run on a disposable Linux CI runner. Boots a VM; no production address is used.
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
VM_DIR=$(mktemp -d)
RESULT_DIR=${VM_RESULT_DIR:-"$REPO/dist/vm-results"}
mkdir -p "$RESULT_DIR"
QEMU_PID=
cleanup() {
  if test -n "$QEMU_PID"; then kill "$QEMU_PID" 2>/dev/null || true; wait "$QEMU_PID" 2>/dev/null || true; fi
  cp "$VM_DIR/serial.log" "$RESULT_DIR/serial.log" 2>/dev/null || true
  rm -rf "$VM_DIR"
}
trap cleanup EXIT
cd "$VM_DIR"
IMAGE=debian-13-genericcloud-amd64.qcow2
BASE_URL=https://cloud.debian.org/images/cloud/trixie/latest
curl --fail --location --retry 3 --max-time 300 -o "$IMAGE" "$BASE_URL/$IMAGE"
curl --fail --location --retry 3 --max-time 60 -o SHA512SUMS "$BASE_URL/SHA512SUMS"
grep " $IMAGE$" SHA512SUMS | sha512sum --check - | tee "$RESULT_DIR/image-checksum.txt"
qemu-img resize "$IMAGE" 12G
ssh-keygen -q -t ed25519 -N '' -f "$VM_DIR/vm-key"
PUBKEY=$(cat "$VM_DIR/vm-key.pub")
cat > user-data <<EOF
#cloud-config
users:
  - name: vmtester
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $PUBKEY
ssh_pwauth: false
package_update: true
packages:
  - python3
  - rsyslog
  - curl
  - jq
  - bind9-dnsutils
  - iproute2
  - iputils-ping
  - ethtool
  - conntrack
  - sqlite3
  - ca-certificates
  - procps
  - util-linux
  - logrotate
write_files:
  - path: /etc/netblackbox-vm-fixture
    permissions: '0600'
    content: isolated-lifecycle-fixture
runcmd:
  - [mkdir, -p, /opt/vm-lifecycle]
  - [chown, vmtester:vmtester, /opt/vm-lifecycle]
EOF
printf 'instance-id: netblackbox-vm-%s\nlocal-hostname: blackbox-fixture\n' "$RANDOM" > meta-data
cloud-localds seed.img user-data meta-data
ACCEL=tcg
if test -r /dev/kvm && test -w /dev/kvm; then ACCEL=kvm; fi
printf '%s\n' "$ACCEL" > "$RESULT_DIR/accelerator.txt"
qemu-system-x86_64 -machine "accel=$ACCEL" -cpu max -m 2048 -smp 2 \
 -drive "file=$VM_DIR/$IMAGE,if=virtio,format=qcow2" \
 -drive "file=$VM_DIR/seed.img,if=virtio,format=raw" \
 -netdev user,id=n0,hostfwd=tcp:127.0.0.1:22222-:22 -device virtio-net-pci,netdev=n0 \
 -display none -serial "file:$VM_DIR/serial.log" -monitor none > qemu.log 2>&1 &
QEMU_PID=$!
SSH=(ssh -i "$VM_DIR/vm-key" -p 22222 -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$VM_DIR/known_hosts" vmtester@127.0.0.1)
SCP=(scp -i "$VM_DIR/vm-key" -P 22222 -o BatchMode=yes -o "UserKnownHostsFile=$VM_DIR/known_hosts")
ready() {
 for ((i=0;i<120;i++)); do
  if "${SSH[@]}" true 2>/dev/null; then return; fi
  kill -0 "$QEMU_PID" || { cat qemu.log; return 1; }
  sleep 5
 done
 return 1
}
ready
"${SSH[@]}" 'sudo cloud-init status --wait' | tee "$RESULT_DIR/cloud-init.txt"
git -C "$REPO" archive --format=tar f582002b547aace89c34911667bb893866efa090 > baseline.tar
git -C "$REPO" archive --format=tar HEAD > candidate.tar
"${SCP[@]}" baseline.tar candidate.tar vmtester@127.0.0.1:/opt/vm-lifecycle/
"${SSH[@]}" 'cd /opt/vm-lifecycle; mkdir v11 candidate; tar -xf baseline.tar -C v11; tar -xf candidate.tar -C candidate'
collect() {
 "${SSH[@]}" 'sudo tar -C /opt/vm-lifecycle -czf /opt/vm-lifecycle/reports.tar.gz reports logs; sudo chown vmtester:vmtester /opt/vm-lifecycle/reports.tar.gz' || true
 "${SCP[@]}" vmtester@127.0.0.1:/opt/vm-lifecycle/reports.tar.gz "$RESULT_DIR/" || true
}
for phase in baseline upgrade; do
 if ! "${SSH[@]}" "sudo python3 /opt/vm-lifecycle/candidate/tests/vm/guest.py $phase"; then collect; exit 1; fi
 collect
done
"${SSH[@]}" 'sudo systemctl reboot' || true
sleep 10
ready
"${SSH[@]}" 'sudo cloud-init status --wait'
for ((i=0;i<30;i++)); do
 if "${SSH[@]}" 'sudo systemctl is-active --quiet netblackbox.service'; then break; fi
 sleep 2
done
if ! "${SSH[@]}" 'sudo python3 /opt/vm-lifecycle/candidate/tests/vm/guest.py after-reboot'; then collect; exit 1; fi
collect
printf 'Real Debian VM lifecycle test: PASS\n'
