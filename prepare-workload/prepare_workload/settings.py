import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

_PREFIX = "GL_"


def _root() -> Path:
    # project root = parent of the package dir
    return Path(__file__).resolve().parents[1]


def _pkg_root() -> Path:
    return Path(__file__).resolve().parent


def _load_toml() -> dict:
    path = _root() / "settings.toml"
    return tomllib.loads(path.read_text()) if path.exists() else {}


def _deep_update(base: dict, *updates: dict) -> dict:
    out = dict(base)
    for up in updates:
        for k, v in (up or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _deep_update(out[k], v)
            else:
                out[k] = v
    return out


def _set_in(d: dict, path: list[str], value):
    cur = d
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = value


def _coerce_like(example, s: str):
    if isinstance(example, bool):
        return s.lower() in {"1", "true", "yes", "on"}
    if isinstance(example, int):
        try:
            return int(s)
        except:
            return s
    if isinstance(example, float):
        try:
            return float(s)
        except:
            return s
    return s


def _parse_env(defaults: dict) -> dict:
    """Support GL_FOO=.. and GL_NETWORK__NUM_VALIDATORS=.. for nesting."""
    out: dict = {}
    for k, v in os.environ.items():
        if not k.startswith(_PREFIX):
            continue
        key = k[len(_PREFIX):]  # e.g. NETWORK__NUM_VALIDATORS
        parts = [p.lower() for p in key.split("__") if p]
        # find example type from defaults, if any
        ex = defaults
        for p in parts:
            ex = ex.get(p) if isinstance(ex, dict) else None
        coerced = _coerce_like(ex, v) if ex is not None else v
        _set_in(out, parts, coerced)
    return out


def get_settings(**overrides):
    defaults = {
        "templates_dir": "templates",
        "testnet_dir": "testnet",
        "network_filename": "network.toml",
        "config_dir": "volumes",
        "unl_file": "unl.json",
        "compose_yml_file": "compose.yml",
        "node_config_template": "rippled.cfg.mako",
        "node_config_file": "rippled.cfg",
        "compose_template": "compose.yml.mako",
        # General network info
        "network": {
            "network_dir_name": "testnet",
            "num_validators": 5,
            "num_peers": 1,
            "use_unl": True,
            "validator_list_sites": "http://unl",
            "validator_name": "val",
            "peer_name": "rippled",
        },
        # Specific node instance configs
        "node_config": {
            "ports": {
                "rpc_admin_local": 5005,
                "peer": 2459,
                "ws_admin_local": 6006,
            },
            "voting": {
                "reference_fee": 10,
                "account_reserve": 1000000,
                "owner_reserve": 2000000,
            },
        },
        # compose.yml
        "compose_config": {
            "image": "rippleci/rippled:latest",
            "network_name": "xrpl_net",
        },
    }

    # 2) file > 3) env > 4) explicit overrides
    file_cfg = _load_toml()
    env_cfg = _parse_env(defaults)
    cfg = _deep_update(defaults, file_cfg, env_cfg, overrides)

    # figure out everything's locations
    project_root = _root()
    pkg_root = _pkg_root()
    template_dir_path = pkg_root / cfg["templates_dir"]
    network_dir_path = project_root / cfg["testnet_dir"]
    network_file = project_root / cfg["network_filename"]
    node_cfg_tmpl = template_dir_path / cfg["node_config_template"]
    compose_tmpl = template_dir_path / cfg["compose_template"]
    unl_server = pkg_root / "unl_server/app.py"

    return SimpleNamespace(
        project_root=project_root,
        template_dir_path=template_dir_path,
        network_dir_path=network_dir_path,
        network_file=network_file,
        config_dir=cfg["config_dir"],
        unl_file=cfg["unl_file"],
        compose_yml_file=cfg["compose_yml_file"],
        node_config_template=node_cfg_tmpl,
        node_config_dir_path=cfg["node_config_file"],
        node_config_file=cfg["node_config_file"],
        compose_template=compose_tmpl,
        unl_server=unl_server,

        network=SimpleNamespace(**cfg["network"]),
        node_config=SimpleNamespace(**cfg["node_config"]),
        compose_config=SimpleNamespace(**cfg["compose_config"]),
    )
