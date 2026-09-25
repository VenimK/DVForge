#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

clear
cat <<'EOF'
 ____  _     __ _____
|  _ \| |   / _|  ___|__  _ __ __ _  ___
| | | | |  | |_| |_ / _ \| '__/ _` |/ _ \
| |_| | |__|  _|  _| (_) | | | (_| |  __/
|____/|____|_| |_|  \___/|_|  \__, |\___|
                              |___/

DVForge ARM64 worker LXC installer
Creates an amd64 LXC containing an emulated aarch64 build container.
EOF

if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
    fail "This installer expects an x86_64/amd64 Proxmox host."
fi

NEXTID=$(pvesh get /cluster/nextid)

CTID="$NEXTID"
HOSTNAME="dvforge-arm64"
DISK_SIZE="100"
CORES="6"
MEMORY="12288"
BRIDGE="vmbr0"
VLAN_TAG=""
OSTYPE="ubuntu"
OSVERSION="24.04"
INNER_NAME="dvforge-arm64"
DVFORGE_REPO="https://github.com/VenimK/DVForge.git"
DVFORGE_REF="main"
QUEUE_URL="https://api.nas86.eu"
WORKER_NAME="proxmox-arm64-qemu"
DATA_DIR="/opt/dvforge-arm64"

echo
echo -e "${CYAN}DVForge ARM64 emulated worker${NC}"
echo
echo -e "${YELLOW}This creates a privileged amd64 LXC, then an ARM64 container inside it.${NC}"
echo -e "${YELLOW}It is QEMU emulation, not native ARM64 hardware.${NC}"
echo

read -r -p "Container ID [${CTID}]: " INPUT_CTID
if [[ -n "${INPUT_CTID}" ]]; then
    [[ "${INPUT_CTID}" =~ ^[0-9]+$ ]] || fail "Invalid container ID."
    CTID="${INPUT_CTID}"
fi

pct status "${CTID}" >/dev/null 2>&1 && fail "Container ${CTID} already exists."

read -r -p "Hostname [${HOSTNAME}]: " INPUT_HOST
[[ -n "${INPUT_HOST}" ]] && HOSTNAME="${INPUT_HOST}"

read -r -p "Disk size GB [${DISK_SIZE}]: " INPUT_DISK
[[ "${INPUT_DISK}" =~ ^[0-9]+$ && "${INPUT_DISK}" -gt 0 ]] && DISK_SIZE="${INPUT_DISK}"

read -r -p "CPU cores [${CORES}]: " INPUT_CORES
[[ "${INPUT_CORES}" =~ ^[0-9]+$ && "${INPUT_CORES}" -gt 0 ]] && CORES="${INPUT_CORES}"

read -r -p "RAM MB [${MEMORY}]: " INPUT_MEM
[[ "${INPUT_MEM}" =~ ^[0-9]+$ && "${INPUT_MEM}" -gt 0 ]] && MEMORY="${INPUT_MEM}"

read -r -p "Bridge [${BRIDGE}]: " INPUT_BRIDGE
[[ -n "${INPUT_BRIDGE}" ]] && BRIDGE="${INPUT_BRIDGE}"

read -r -p "VLAN tag [none]: " INPUT_VLAN
if [[ -n "${INPUT_VLAN}" ]]; then
    [[ "${INPUT_VLAN}" =~ ^[0-9]+$ && "${INPUT_VLAN}" -ge 1 && "${INPUT_VLAN}" -le 4094 ]] \
        || fail "Invalid VLAN tag."
    VLAN_TAG="${INPUT_VLAN}"
fi

read -r -p "Worker name [${WORKER_NAME}]: " INPUT_WORKER
[[ -n "${INPUT_WORKER}" ]] && WORKER_NAME="${INPUT_WORKER}"

read -r -p "Queue URL [${QUEUE_URL}]: " INPUT_QUEUE
[[ -n "${INPUT_QUEUE}" ]] && QUEUE_URL="${INPUT_QUEUE}"

read -r -s -p "Farm token: " QUEUE_TOKEN
echo
[[ -n "${QUEUE_TOKEN}" ]] || fail "Farm token is required."

echo
log "Detecting container storage"
mapfile -t STORAGE_OPTIONS < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 {print $1}')
[[ ${#STORAGE_OPTIONS[@]} -gt 0 ]] || fail "No rootdir-capable storage found."

idx=1
for store in "${STORAGE_OPTIONS[@]}"; do
    echo "  ${idx}) ${store}"
    idx=$((idx + 1))
done

read -r -p "Storage [1]: " STORAGE_CHOICE
STORAGE_CHOICE="${STORAGE_CHOICE:-1}"
[[ "${STORAGE_CHOICE}" =~ ^[0-9]+$ && "${STORAGE_CHOICE}" -ge 1 && "${STORAGE_CHOICE}" -le ${#STORAGE_OPTIONS[@]} ]] \
    || fail "Invalid storage selection."

STORAGE="${STORAGE_OPTIONS[$((STORAGE_CHOICE - 1))]}"
ok "Using storage ${STORAGE}"

echo
log "Finding ${OSTYPE} ${OSVERSION} amd64 template"
pveam update

HOST_ARCH="$(dpkg --print-architecture)"
TEMPLATE=$(pveam available | awk \
    -v version="${OSTYPE}-${OSVERSION}-standard" \
    -v arch="_${HOST_ARCH}" \
    '$2 ~ version && index($2, arch) {template=$2} END {print template}')

[[ -n "${TEMPLATE}" ]] || {
    warn "Available templates:"
    pveam available | grep -E 'ubuntu|debian' || true
    fail "Could not find an amd64 ${OSTYPE} ${OSVERSION} template."
}

ok "Template: ${TEMPLATE}"
TEMPLATE_PATH="/var/lib/vz/template/cache/${TEMPLATE}"

if [[ ! -f "${TEMPLATE_PATH}" ]]; then
    log "Downloading template"
    pveam download local "${TEMPLATE}"
fi

NET0="name=eth0,bridge=${BRIDGE},ip=dhcp"
[[ -n "${VLAN_TAG}" ]] && NET0="${NET0},tag=${VLAN_TAG}"

echo
log "Creating privileged LXC ${CTID}"
warn "Privileged LXC is used because nested Podman/QEMU/binfmt is unreliable in unprivileged containers."

pct create "${CTID}" "${TEMPLATE_PATH}" \
    --hostname "${HOSTNAME}" \
    --cores "${CORES}" \
    --memory "${MEMORY}" \
    --rootfs "${STORAGE}:${DISK_SIZE}" \
    --net0 "${NET0}" \
    --unprivileged 0 \
    --features nesting=1,keyctl=1,fuse=1 \
    --onboot 1

cat >> "/etc/pve/lxc/${CTID}.conf" <<'LXC_OPTS'
lxc.apparmor.profile: unconfined
lxc.cap.drop:
lxc.cgroup2.devices.allow: a
lxc.mount.auto: proc:rw sys:rw
LXC_OPTS

ok "Container created"

pct start "${CTID}"

log "Waiting for container startup"
for _ in {1..20}; do
    pct status "${CTID}" | grep -qi running && break
    sleep 2
done
pct status "${CTID}" | grep -qi running || fail "Container did not start."

log "Waiting for network"
for _ in {1..40}; do
    if pct exec "${CTID}" -- ping -c 1 1.1.1.1 >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

pct exec "${CTID}" -- ping -c 1 1.1.1.1 >/dev/null 2>&1 \
    || fail "Container has no network."

IP="$(pct exec "${CTID}" -- hostname -I | awk '{print $1}')"
ok "Container IP: ${IP}"

echo
log "Installing DVForge ARM64 worker stack inside LXC"

pct exec "${CTID}" -- env \
    INNER_NAME="${INNER_NAME}" \
    DATA_DIR="${DATA_DIR}" \
    DVFORGE_REPO="${DVFORGE_REPO}" \
    DVFORGE_REF="${DVFORGE_REF}" \
    QUEUE_URL="${QUEUE_URL}" \
    QUEUE_TOKEN="${QUEUE_TOKEN}" \
    WORKER_NAME="${WORKER_NAME}" \
    bash -s <<'INNER_SETUP'
set -euo pipefail

log() { echo -e "\n==> $1"; }

log "Installing outer-container packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y
apt-get install -y \
    podman \
    qemu-user-static \
    binfmt-support \
    fuse-overlayfs \
    uidmap \
    slirp4netns \
    git \
    curl \
    wget \
    ca-certificates \
    xz-utils \
    python3

mkdir -p /etc/containers
cat > /etc/containers/containers.conf <<'PODMAN_CONF'
[containers]
apparmor_profile = "unconfined"
seccomp_profile = "unconfined"
PODMAN_CONF

mount | grep -q binfmt_misc || \
    mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

if [[ -x /usr/lib/systemd/systemd-binfmt ]]; then
    /usr/lib/systemd/systemd-binfmt /usr/lib/binfmt.d/qemu-aarch64.conf
elif [[ -x /lib/systemd/systemd-binfmt ]]; then
    /lib/systemd/systemd-binfmt /usr/lib/binfmt.d/qemu-aarch64.conf
else
    echo "systemd-binfmt not found"
    exit 1
fi

grep -q '^enabled' /proc/sys/fs/binfmt_misc/qemu-aarch64

log "Verifying ARM64 emulation"
ARCH="$(podman run --rm \
    --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined \
    --platform linux/arm64 \
    alpine:3.20 uname -m)"
if [[ "${ARCH}" != "aarch64" ]]; then
    echo "ARM64 emulation test returned: ${ARCH}"
    exit 1
fi
echo "ARM64 emulation OK (${ARCH})"

mkdir -p "${DATA_DIR}"

log "Creating inner ARM64 container"
podman rm -f "${INNER_NAME}" >/dev/null 2>&1 || true
podman pull --platform linux/arm64 ubuntu:22.04

podman run -d \
    --name "${INNER_NAME}" \
    --platform linux/arm64 \
    --security-opt apparmor=unconfined \
    --security-opt seccomp=unconfined \
    -v "${DATA_DIR}:/opt/DVForge" \
    -w /opt/DVForge \
    ubuntu:22.04 \
    sleep infinity

podman exec "${INNER_NAME}" uname -m | grep -qx aarch64

log "Installing packages inside ARM64 container"
podman exec -i "${INNER_NAME}" bash -s <<'ARM_PACKAGES'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
    sudo \
    git \
    curl \
    wget \
    unzip \
    zip \
    tar \
    xz-utils \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates
ARM_PACKAGES

log "Cloning DVForge"
podman exec -i "${INNER_NAME}" bash -s <<ARM_CLONE
set -euo pipefail
cd /opt/DVForge
if [[ ! -d .git ]]; then
    git clone --depth 1 --branch "${DVFORGE_REF}" "${DVFORGE_REPO}" .
else
    git fetch origin "${DVFORGE_REF}"
    git checkout "${DVFORGE_REF}"
    git pull --ff-only origin "${DVFORGE_REF}"
fi
ARM_CLONE

log "Installing DVForge system dependencies"
podman exec -i "${INNER_NAME}" bash -s <<'ARM_DVFORGE_DEPS'
set -euo pipefail
cd /opt/DVForge
chmod +x Setup-DVForge-Ubuntu.sh
sed -i '/lib32z1 lib32ncurses6 lib32stdc++6/d' Setup-DVForge-Ubuntu.sh
./Setup-DVForge-Ubuntu.sh --skip-toolchains
ARM_DVFORGE_DEPS

log "Installing supported ARM64 toolchains"
podman exec -i "${INNER_NAME}" bash -s <<'ARM_TOOLCHAINS'
set -euo pipefail
cd /opt/DVForge
python3 builder/toolchains.py rust llvm vcpkg sccache
ARM_TOOLCHAINS

log "Installing flutter-elinux"
podman exec -i "${INNER_NAME}" bash -s <<'ARM_FLUTTER'
set -euo pipefail

if [[ ! -d /opt/flutter-elinux/.git ]]; then
    git clone https://github.com/sony/flutter-elinux.git /opt/flutter-elinux
fi

cd /opt/flutter-elinux
git fetch --tags --force
git reset --hard 3.24.5

bin/flutter-elinux doctor -v
bin/flutter-elinux precache --linux

# RustDesk CI copies the x64 shader_lib into the arm64 engine cache.
rm -rf /tmp/flutter-x64 /tmp/flutter.tar.xz
wget -q \
  https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.24.5-stable.tar.xz \
  -O /tmp/flutter.tar.xz
mkdir -p /tmp/flutter-x64
tar -xf /tmp/flutter.tar.xz -C /tmp/flutter-x64

mkdir -p flutter/bin/cache/artifacts/engine/linux-arm64
cp -R /tmp/flutter-x64/flutter/bin/cache/artifacts/engine/linux-x64/shader_lib \
      flutter/bin/cache/artifacts/engine/linux-arm64/

rm -rf /tmp/flutter-x64 /tmp/flutter.tar.xz

if [[ -f /opt/DVForge/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff ]]; then
    if git -C flutter apply --check /opt/DVForge/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff; then
        git -C flutter apply /opt/DVForge/patches/flutter_3.24.4_dropdown_menu_enableFilter.diff
    fi
fi

cat > /usr/local/bin/flutter <<'FLUTTER_WRAPPER'
#!/usr/bin/env bash
exec /opt/flutter-elinux/bin/flutter-elinux "$@"
FLUTTER_WRAPPER

chmod +x /usr/local/bin/flutter
flutter --version
ARM_FLUTTER

log "Creating worker launcher"
mkdir -p "${DATA_DIR}/farm"
cat > "${DATA_DIR}/farm/run-worker.sh" <<EOF
#!/usr/bin/env bash
set -e
cd /opt/DVForge/farm
export DVFORGE_WORKER="${WORKER_NAME}"
exec /usr/bin/python3 worker.py --with-app \\
    --queue "${QUEUE_URL}" \\
    --token "${QUEUE_TOKEN}"
EOF
chmod 700 "${DATA_DIR}/farm/run-worker.sh"

log "Creating binfmt service"
cat > /etc/systemd/system/dvforge-binfmt.service <<'BINFMT_SERVICE'
[Unit]
Description=Register QEMU aarch64 binfmt
DefaultDependencies=no
After=proc-sys-fs-binfmt_misc.mount
ConditionPathExists=/usr/lib/binfmt.d/qemu-aarch64.conf

[Service]
Type=oneshot
ExecStartPre=-/bin/mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
ExecStart=/usr/lib/systemd/systemd-binfmt /usr/lib/binfmt.d/qemu-aarch64.conf

[Install]
WantedBy=multi-user.target
BINFMT_SERVICE

systemctl daemon-reload
systemctl enable dvforge-binfmt.service

log "Creating worker service"
cat > /etc/systemd/system/dvforge-arm64-worker.service <<EOF
[Unit]
Description=DVForge emulated ARM64 worker
After=network-online.target dvforge-binfmt.service
Wants=network-online.target
Requires=dvforge-binfmt.service

[Service]
Type=simple
ExecStartPre=-/usr/bin/podman start ${INNER_NAME}
ExecStart=/usr/bin/podman exec ${INNER_NAME} /opt/DVForge/farm/run-worker.sh
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dvforge-arm64-worker.service

sleep 5
systemctl --no-pager --full status dvforge-arm64-worker.service || true

echo
echo "Worker installed."
echo "Inner architecture: $(podman exec "${INNER_NAME}" uname -m)"
echo "Service: dvforge-arm64-worker.service"
INNER_SETUP

echo
echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        DVForge ARM64 worker installation complete          ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo
echo "Outer LXC ID:      ${CTID}"
echo "Outer LXC IP:      ${IP}"
echo "Inner container:   ${INNER_NAME}"
echo "Worker name:       ${WORKER_NAME}"
echo
echo "Checks:"
echo "  pct exec ${CTID} -- podman exec ${INNER_NAME} uname -m"
echo "  pct exec ${CTID} -- systemctl status dvforge-arm64-worker"
echo "  pct exec ${CTID} -- journalctl -u dvforge-arm64-worker -f"
echo
echo "Expected inner architecture: aarch64"
echo
warn "This is QEMU emulation. First builds can be slow."
warn "If the build reaches Flutter and still looks for build/linux/x64, DVForge needs a small orchestrator patch to use the arm64 bundle path."