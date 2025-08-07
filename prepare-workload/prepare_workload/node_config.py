import logging
import tomllib
from pathlib import Path

from prepare_workload.formatting import pad

logger = logging.getLogger(__name__)
logger.info("parse_network")


def read_network_spec_file(spec: Path) -> dict:
    with open(spec, "rb") as f:
        return tomllib.load(f)


def get_nodes(spec):
    nodes: list[str] = []
    for u, v in spec["edges"]:
        nodes.extend((u, v))
    return sorted(set(nodes))


def get_peers(edge_list: list[list[str]]):
    nodes: list[str] = []
    for u, v in edge_list:
        nodes.extend((u, v))
    peers: dict[str, list[str]] = {n: [] for n in nodes}
    for u, v in edge_list:
        peers.setdefault(u, [])
        peers.setdefault(v, [])
        # Peers always treated as symmetric.
        # We could change this so peers can have trouble communicating one way or the other.
        # Would need to make directed.
        peers[u].append(v)
        peers[v].append(u)
    # a node must not list itself
    for n in list(peers.keys()):
        # log.warn removing self reference!
        if n in peers[n]:
            peers[n].remove(n)
    return peers


def get_node_configs(settings):
    if not settings.network_file.is_file():
        # Everyone is connected to everyone and with all default values
        private_peers = []
        n_v = settings.network.num_validators
        n_p = settings.network.num_peers
        all_v = [f"{settings.network.validator_name}{pad(i, n_v)}" for i in range(n_v)]
        if n_p > 1:
            all_p = [f"{settings.network.peer_name}{pad(i, n_p)}" for i in range(n_p)]
        else:
            all_p = [f"{settings.network.peer_name}"]
        all_ = all_v + all_p
        peers = {n: sorted([i for i in all_ if i != n]) for n in all_}
    else:
        # We need to customize the network some.
        spec = read_network_spec_file(settings.network_file)
        all_ = get_nodes(spec)
        edge_list = spec.get("edges")
        if edge_list:
            peers = get_peers(edge_list)
        private_peers = spec.get("private_peers", [])
        if len(peers) != len(all_):
            logging.exception("%s doesn't fully define the network.", settings.network_file)

    node_configs = {"validators": [], "peers": []}
    for n in peers:
        is_validator = n.startswith(settings.network.validator_name)
        cfg = {
                "name": n,
                "peers": peers[n],
                "peer_private": str(n in private_peers).lower(),
                "is_validator": is_validator,
            }
        if is_validator:
            node_configs["validators"].append(cfg)
        else:
            node_configs["peers"].append(cfg)
    pass
    return node_configs
