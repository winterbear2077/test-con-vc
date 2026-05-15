#!/usr/bin/env sh
# ovf/build_mini_iso.sh
#
# Build a minimal bootable ISO (~10–12 MB) containing only:
#   - Alpine virt kernel  (vmlinuz-virt, ~7 MB)
#   - Custom initramfs    (busybox-static + netprobe + vmci/vsock modules, ~3 MB)
#   - isolinux bootloader (~1 MB)
#
# No Alpine rootfs, no modloop, no apkovl — the VM boots directly into netprobe.
# This is ~6× smaller than the old full Alpine memboot ISO (~65 MB).
#
# Usage:
#   ./ovf/build_mini_iso.sh [output.iso] [--skip-probe-build]
#
# Defaults:
#   output.iso   ovf/nettest-mini.iso
#
# Requirements:
#   - Docker Desktop running  (handles cross-compile + ISO tools automatically)
#   - ovf/netprobe must exist (built by ovf/build_probe.sh, or pass --skip-probe-build)
#
# The resulting ISO is used with --boot-method memboot --memboot-iso-path <output.iso>.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_ISO="${1:-${SCRIPT_DIR}/nettest-mini.iso}"
SKIP_PROBE_BUILD=0

for arg in "$@"; do
    case "$arg" in --skip-probe-build) SKIP_PROBE_BUILD=1 ;; esac
done

BINARY="${SCRIPT_DIR}/netprobe"
INIT_SCRIPT="${SCRIPT_DIR}/init"

echo "=== Mini ISO builder (kernel + busybox + netprobe) ==="
echo "  Output: ${OUTPUT_ISO}"

# ── Step 1: Build the Rust netprobe binary ────────────────────────────────────
if [ "$SKIP_PROBE_BUILD" -eq 1 ] && [ -f "$BINARY" ]; then
    echo "  Skipping probe build (--skip-probe-build), using: ${BINARY}"
else
    echo "  Building Rust netprobe binary..."
    sh "${SCRIPT_DIR}/build_probe.sh"
fi

if [ ! -f "$BINARY" ]; then
    echo "ERROR: ${BINARY} not found after build_probe.sh"
    exit 1
fi

if [ ! -f "$INIT_SCRIPT" ]; then
    echo "ERROR: ${INIT_SCRIPT} not found (expected ovf/init)"
    exit 1
fi

# ── Step 2: Build initramfs + ISO inside Docker ───────────────────────────────
# All ISO tooling (syslinux, xorriso, linux-virt) runs in an alpine:latest
# container so there are no host dependencies beyond Docker.
echo "  Building ISO inside Docker (alpine:latest linux/amd64)..."

docker run --rm -i \
    --platform linux/amd64 \
    -v "${SCRIPT_DIR}:/repo" \
    alpine:latest sh << 'DOCKER_EOF'
set -e

echo "  [docker] Installing packages..."
apk add --no-cache syslinux linux-virt busybox-static xorriso >/dev/null 2>&1

KVER=$(ls /lib/modules/ | head -1)
echo "  [docker] Kernel version: ${KVER}"

# ── initramfs directory tree ──────────────────────────────────────────────────
INITRD=/tmp/initrd-root
mkdir -p \
    "${INITRD}/bin" \
    "${INITRD}/dev" \
    "${INITRD}/proc" \
    "${INITRD}/sys" \
    "${INITRD}/tmp" \
    "${INITRD}/usr/local/bin" \
    "${INITRD}/lib/modules"

# busybox static binary + essential symlinks
BUSYBOX=$(ls /bin/busybox.static /usr/bin/busybox.static 2>/dev/null | head -1 || command -v busybox 2>/dev/null || true)
if [ -z "${BUSYBOX}" ]; then
    echo "ERROR: busybox-static not found in container"; exit 1
fi
cp "${BUSYBOX}" "${INITRD}/bin/busybox"
chmod 755 "${INITRD}/bin/busybox"
for cmd in sh mount mkdir mknod echo insmod sleep cat; do
    ln -sf busybox "${INITRD}/bin/${cmd}"
done

# netprobe binary
cp /repo/netprobe "${INITRD}/usr/local/bin/netprobe"
chmod 755 "${INITRD}/usr/local/bin/netprobe"

# VMware vmci/vsock kernel modules — copy flat and decompress for insmod
for mod in vmw_vmci vsock vmw_vsock_vmci_transport; do
    src=$(find "/lib/modules/${KVER}" -name "${mod}.ko*" 2>/dev/null | head -1 || true)
    if [ -n "${src}" ]; then
        dest="${INITRD}/lib/modules/${mod}.ko"
        case "${src}" in
            *.gz) gunzip -c "${src}" > "${dest}" ;;
            *)    cp "${src}" "${dest}" ;;
        esac
        echo "  [docker] Module: ${mod}.ko ($(du -sh "${dest}" | cut -f1))"
    fi
done

# /init script (from repo)
cp /repo/init "${INITRD}/init"
chmod 755 "${INITRD}/init"

# Pack initramfs
( cd "${INITRD}" && find . | cpio -H newc -o 2>/dev/null | gzip -9 > /tmp/initramfs.gz )
INITRD_SIZE=$(du -sh /tmp/initramfs.gz | cut -f1)
echo "  [docker] initramfs: ${INITRD_SIZE}"

# ── ISO directory structure ───────────────────────────────────────────────────
ISOROOT=/tmp/isoroot
mkdir -p "${ISOROOT}/boot/isolinux"

cp "/boot/vmlinuz-virt"                 "${ISOROOT}/boot/vmlinuz"
cp /tmp/initramfs.gz                    "${ISOROOT}/boot/initramfs.gz"
cp /usr/share/syslinux/isolinux.bin     "${ISOROOT}/boot/isolinux/isolinux.bin"
cp /usr/share/syslinux/ldlinux.c32      "${ISOROOT}/boot/isolinux/ldlinux.c32"

# Boot params:
#   console=tty0   — kernel output to VGA (visible in vSphere console viewer)
#   loglevel=0     — suppress all but panic messages so /dev/ttyS0 is clean
#   quiet          — suppress initcall output
{
    echo 'DEFAULT nettest'
    echo 'LABEL nettest'
    echo '  KERNEL /boot/vmlinuz'
    echo '  INITRD /boot/initramfs.gz'
    echo '  APPEND console=tty0 loglevel=0 quiet'
} > "${ISOROOT}/boot/isolinux/isolinux.cfg"

# Build ISO
xorriso -as mkisofs \
    -o /repo/nettest-mini.iso \
    -b boot/isolinux/isolinux.bin \
    -c boot/isolinux/boot.cat \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
    "${ISOROOT}" 2>&1 | grep -v '^$' || true

ISO_SIZE=$(du -sh /repo/nettest-mini.iso | cut -f1)
echo "  [docker] ISO size: ${ISO_SIZE}"
DOCKER_EOF

# ── Move to final output path if it differs from SCRIPT_DIR/nettest-mini.iso ──
BUILT="${SCRIPT_DIR}/nettest-mini.iso"
if [ "${OUTPUT_ISO}" != "${BUILT}" ]; then
    mv -f "${BUILT}" "${OUTPUT_ISO}"
fi

SIZE=$(du -sh "${OUTPUT_ISO}" | cut -f1)
echo ""
echo "=== Done ==="
echo "  ISO: ${OUTPUT_ISO} (${SIZE})"
echo ""
echo "Next steps:"
echo "  1. Upload ISO to vCenter once per cluster:"
echo "       --memboot-iso-path ${OUTPUT_ISO}"
echo "  2. Run tests:"
echo "       --boot-method memboot --memboot-iso-path ${OUTPUT_ISO}"
