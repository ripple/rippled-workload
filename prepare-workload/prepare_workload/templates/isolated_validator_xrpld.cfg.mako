# Isolated validator xrpld configuration (runs inside fuzzer container)
# Connects to fuzzer on localhost addresses (127.0.0.x)

[server]
port_rpc_admin_local
port_peer
port_ws_admin_local

[port_rpc_admin_local]
port = ${ports["rpc_admin_local"]}
ip = 0.0.0.0
admin = [0.0.0.0]
protocol = http

[port_peer]
port = ${ports["peer"]}
ip = 0.0.0.0
protocol = peer

[port_ws_admin_local]
port = ${ports["ws_admin_local"]}
ip = 0.0.0.0
admin = [0.0.0.0]
protocol = ws

[node_db]
type = NuDB
path = /var/lib/xrpld/db/nudb

[ledger_history]
full

[database_path]
/var/lib/xrpld/db

[debug_logfile]
/var/log/xrpld/debug.log

[node_size]
huge

[beta_rpc_api]
1

[rpc_startup]
{ "command": "log_level", "severity": "warning" }

[ssl_verify]
0

[compression]
0

[tx_reduce_relay_enable]
1

[ledger_replay]
1

[peer_private]
1

[signing_support]
true

# Connect to fuzzer on localhost addresses
# Each connection uses a different IP (127.0.0.1, 127.0.0.2, etc.)
[ips_fixed]
% for i in range(num_real_peers):
127.0.0.${i + 1} ${isolated_peer_starting_port + i}
% endfor

[validators]
${validator_public_keys}

[validation_seed]
${validation_seed}

[voting]
reference_fee = ${voting["reference_fee"]}
account_reserve = ${voting["account_reserve"]}
owner_reserve = ${voting["owner_reserve"]}

## Amendments are pre-enabled in the genesis ledger (--ledgerfile).
## No [features] section needed.
