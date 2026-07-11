#!/bin/bash
set -e

# Number of validators (real peers) determines how many loopback aliases we need
NUM_REAL_PEERS=${NUM_REAL_PEERS:?NUM_REAL_PEERS environment variable must be set}

echo "Setting up loopback IP aliases for $NUM_REAL_PEERS real peers..."

# Set up loopback IP aliases (127.0.0.2, 127.0.0.3, etc.)
# We need NUM_REAL_PEERS - 1 aliases (127.0.0.1 is already available)
for i in $(seq 2 $NUM_REAL_PEERS); do
    IP="127.0.0.$i"
    echo "Adding loopback alias: $IP"
    ip addr add "$IP/8" dev lo 2>/dev/null || true
done

# Verify aliases
echo "Loopback addresses configured:"
ip addr show lo | grep "inet "

# Route core dumps to the captured log volume so a crash leaves a post-mortem
# artifact that survives the container. Core dumps themselves are enabled
# image-wide via the compose `ulimits: core` (prepare-workload templates), and the
# boost::stacktrace crash handler is installed via /etc/ld.so.preload
# (Dockerfile.xrpld) — so both also cover the validators/peer, which launch xrpld
# directly and never run this script. core_pattern is a host-global (non-namespaced)
# sysctl: writing it needs a writable /proc and privilege we may not have, so this
# is best-effort; otherwise the host's pattern applies (a relative pattern writes
# the core to the process CWD).
CORE_DIR=/var/log/xrpld/cores
mkdir -p "$CORE_DIR"
if echo "$CORE_DIR/core.%e.%p.%t" > /proc/sys/kernel/core_pattern 2>/dev/null; then
    echo "core_pattern -> $CORE_DIR/core.%e.%p.%t"
else
    echo "warning: could not set core_pattern (host-managed / read-only)"
fi

echo "Starting rippled-fuzzer"
/opt/fuzzer/bin/rippled-fuzzer /etc/opt/fuzzer/fuzzer.cfg &
FUZZER_PID=$!

sleep 2

echo "Starting xrpld"
/opt/xrpld/bin/xrpld --conf /etc/opt/xrpld/xrpld.cfg &
XRPLD_PID=$!

# Wait for either process to exit
wait -n $FUZZER_PID $XRPLD_PID

# If we get here, one process died - kill the other and exit
echo "One process exited, shutting down..."
kill $FUZZER_PID $XRPLD_PID 2>/dev/null || true
exit 1
