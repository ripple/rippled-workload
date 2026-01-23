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

echo "Starting rippled-fuzzer"
/opt/fuzzer/bin/rippled-fuzzer /etc/fuzzer/fuzzer.cfg &
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
