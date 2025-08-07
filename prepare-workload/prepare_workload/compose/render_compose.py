from mako.template import Template
from operator import itemgetter

# TODO: Move to constants and settings

service_template = "service.yml.mako"
compose_template = "compose.yml.mako"

container_config_path = "/opt/ripple/etc"

VALIDATOR_COMMAND = '["/opt/ripple/bin/rippled", "--start"]'
PEER_COMMAND = '["/opt/ripple/bin/rippled", "--start"]'

def render_peer(idx, data):
    return {
        "service_name": data["name"],
        "container_name": data["name"],
        "hostname": data["name"],
        "command": PEER_COMMAND,
        "image": data["compose_config"].image,
        "ports":  [
            f"{data['ports']['rpc_admin_local'] + idx}:{data['ports']['rpc_admin_local']}",
            f"{data['ports']['ws_admin_local'] + idx}:{data['ports']['ws_admin_local']}",
        ],
        "network_name": data["compose_config"].network_name,
        "volumes": [f"{data['volumes_path']}/{data['name']}:{container_config_path}"],
    }


def render_validator(idx, data):
    val_data = {
        "is_validator": True,
        "service_name": data["name"],
        "container_name": data["name"],
        "hostname": data["name"],
        "command": VALIDATOR_COMMAND,
        "image": data["compose_config"].image,
        "ports": [
            f"{data['ports']['rpc_admin_local'] + idx}:{data['ports']['rpc_admin_local']}",
            f"{data['ports']['ws_admin_local'] + idx}:{data['ports']['ws_admin_local']}",
        ],
        "network_name": data["compose_config"].network_name,
        "volumes": [f"{data['volumes_path']}/{data['name']}:{container_config_path}"],
    }
    # if use_ledger ... append to volumes here.
    # Assume rippled takes the default port locally...
    return val_data


def render_unl_server(unl_data):
    unl_template = Template(filename=str(unl_data["template"]))
    return unl_template.render(**unl_data)


def render_compose_data(node_config, settings):
    service_template_file_path = settings.template_dir_path / service_template
    compose_template_file_path = settings.template_dir_path / compose_template
    # compose_yml_path = settings.network_dir_path / settings.compose_yml_file
    unl_service_template = settings.template_dir_path / "unl_service.yml.mako"
    # network_dir_name = settings.network.network_dir_name
    template = Template(filename=str(service_template_file_path))

    # Settings we need from general configuration.
    s_data = {
        "node_data": settings.node_config,
        "network_dir_name": settings.network.network_dir_name,
        "volumes_path": f"./{settings.config_dir}",
        "compose_config": settings.compose_config,
        "ports": settings.node_config.ports,
    }

    # Network node specific data
    validator_data = []
    # Start enumerating validators ports from the last one of the peers. Just want the first peer node to have the defaults.
    start_index = len(node_config["peers"])
    vl = sorted(node_config["validators"], key=itemgetter('name'))
    for idx, v_data in enumerate(vl, start=start_index):
        data = {**v_data, **s_data}
        # The index is for setting the node's ports
        validator_data.append(template.render(**render_validator(idx, data)))

    peer_data = []
    pl = sorted(node_config["peers"], key=itemgetter('name'))
    for idx, p_data in enumerate(pl):
        data = {**p_data, **s_data}
        peer_data.append(template.render(**render_peer(idx, data)))

    compose_data = {
        "validators": validator_data,
        "peers": peer_data,
        "use_unl": settings.network.use_unl,
        "network_name": settings.compose_config.network_name,
    }

    if settings.network.use_unl:
        name = "unl"
        unl_data = {
            "template": unl_service_template,
            "name": name,
            "unl_file": settings.unl_file,
            "network_name": settings.compose_config.network_name,
        }
        unl_service = render_unl_server(unl_data)
        compose_data["unl_service"] = unl_service

    compose_tmpl = Template(filename=str(compose_template_file_path))
    return compose_tmpl.render(**compose_data)
