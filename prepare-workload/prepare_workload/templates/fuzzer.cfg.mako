# IP address for listening to connections from the isolated xrpld node.
# Uses localhost - fuzzer increments last octet for each real peer
# (127.0.0.1, 127.0.0.2, etc.)
[isolated_peer_starting_ip]
127.0.0.1

# Starting port for isolated peer connections.
# Fuzzer listens on consecutive ports: port, port+1, ..., port+N-1
[isolated_peer_starting_port]
${isolated_peer_starting_port}

# IP address the fuzzer listens on for real peer connections.
# Use 0.0.0.0 to accept connections from other containers
[real_peer_listen_ip]
0.0.0.0

# Port for real peers to connect to.
[real_peer_port]
${real_peer_port}

# Number of real peers (validators).
[num_real_peers]
${num_real_peers}

# Seed for fuzzer's own node identity.
[node_seed]
${node_seed}

# Seeds for identities presented to isolated peer (one per real peer).
[peer_seeds]
% for seed in peer_seeds:
${seed}
% endfor
