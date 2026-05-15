#!/usr/bin/env sh
# ovf/build_probe.sh
#
# Statically compile the netprobe binary for x86_64 Linux (musl libc).
#
# Output: ovf/netprobe  (stripped ELF, typically ~1.5–2 MB)
#
# Build method priority:
#   1) Docker (preferred on macOS, including Apple Silicon) — requires Docker Desktop
#   2) cargo + x86_64-linux-musl-gcc (macOS with brew musl-cross)
#   3) cargo + native musl toolchain (Linux)
#   4) cross (Docker-based, Linux x86_64 only — Apple Silicon not supported)

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROBE_DIR="${SCRIPT_DIR}/../probe"
TARGET="x86_64-unknown-linux-musl"
OUTPUT="${SCRIPT_DIR}/netprobe"

cd "$PROBE_DIR"

echo "=== Building netprobe (static musl) ==="

# -- Method 1: Docker (works on macOS ARM/Intel, Linux) ------------------
# Uses rust:alpine with --platform linux/amd64 so the container IS x86_64
# musl natively; no cross-toolchain installation needed on the host.
if [ "$(uname -s)" = "Darwin" ] && command -v docker >/dev/null 2>&1; then
    echo "  Using: Docker (rust:alpine linux/amd64)"
    docker run --rm \
        --platform linux/amd64 \
        -v "${PROBE_DIR}:/src" \
        -v "${HOME}/.cargo/registry:/root/.cargo/registry" \
        -w /src \
        rust:alpine \
        sh -c "
            apk add --no-cache musl-dev >/dev/null 2>&1
            rustup target add ${TARGET} 2>/dev/null || true
            cargo build --release --target ${TARGET}
        "

# -- Method 2: macOS with musl-cross (brew install FiloSottile/musl-cross/musl-cross) --
elif command -v cargo >/dev/null 2>&1 \
     && command -v x86_64-linux-musl-gcc >/dev/null 2>&1; then
    echo "  Using: cargo + x86_64-linux-musl-gcc (macOS musl-cross)"
    rustup target add "$TARGET" 2>/dev/null || true
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=x86_64-linux-musl-gcc \
        cargo build --release --target "$TARGET"

# -- Method 3: Linux with musl-tools -------------------------------------
elif [ "$(uname -s)" = "Linux" ] && command -v cargo >/dev/null 2>&1; then
    echo "  Using: cargo + musl toolchain (Linux)"
    rustup target add "$TARGET" 2>/dev/null || true
    if ! command -v musl-gcc >/dev/null 2>&1 && ! command -v x86_64-linux-musl-gcc >/dev/null 2>&1; then
        echo "  Installing musl-tools..."
        apt-get install -y musl-tools 2>/dev/null \
            || yum install -y musl-devel 2>/dev/null \
            || true
    fi
    cargo build --release --target "$TARGET"

# -- Method 4: cross (Linux x86_64 host only) ----------------------------
elif command -v cross >/dev/null 2>&1 && [ "$(uname -m)" = "x86_64" ]; then
    echo "  Using: cross (Docker, x86_64 host)"
    cross build --release --target "$TARGET"

else
    echo ""
    echo "ERROR: Cannot cross-compile to musl on this platform."
    echo ""
    echo "Pick one option and re-run:"
    echo ""
    echo "  A) Docker (recommended — works on Apple Silicon and Intel Mac):"
    echo "       # Just ensure Docker Desktop is running, then re-run this script."
    echo ""
    echo "  B) macOS + homebrew musl-cross:"
    echo "       brew install FiloSottile/musl-cross/musl-cross"
    echo "       rustup target add $TARGET"
    echo ""
    echo "  C) Linux:"
    echo "       apt-get install musl-tools"
    echo "       rustup target add $TARGET"
    echo ""
    exit 1
fi

# Copy and strip
cp -f "target/${TARGET}/release/netprobe" "$OUTPUT"
strip "$OUTPUT" 2>/dev/null || true

SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo ""
echo "=== Done ==="
echo "  Binary: ${OUTPUT} (${SIZE})"
echo "  Next:   run ovf/build_mini_iso.sh --skip-probe-build to embed it in the ISO"
