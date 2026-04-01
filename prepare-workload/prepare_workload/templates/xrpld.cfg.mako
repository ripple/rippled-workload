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
{ "command": "log_level", "severity": "info" }

[ssl_verify]
0

[compression]
0

[tx_reduce_relay_enable]
1

[ledger_replay]
1

[peer_private]
${peer_private}

[signing_support]
${signing_support}

[ips_fixed]
${ips_fixed}

[validators]
${validator_public_keys}

% if use_unl:
[validator_list_sites]
${validator_list_sites}

[validator_list_keys]
${validator_list_keys}
% endif

% if is_validator:
[validation_seed]
${validation_seed}

[voting]
reference_fee = ${voting["reference_fee"]}
account_reserve = ${voting["account_reserve"]}
owner_reserve = ${voting["owner_reserve"]}
% endif

## Amendments are pre-enabled in the genesis ledger (--ledgerfile).
## No [features] section needed.
