import argparse
import json
import sys
import urllib.error
import urllib.request

from workload import logger

def make_request(url: str, command: dict):
    payload = bytes(json.dumps(command), encoding="utf-8")
    try:
        response = urllib.request.urlopen(url, data=payload).read()
    except urllib.error.HTTPError:
        logger.debug("Bad response from %s", url)
    except urllib.error.URLError as e:
        logger.debug("No response from %s. Probably not running...", url)
    except ConnectionResetError:
        logger.debug("rippled is not running")
    else:
        return response


def get_server_info(url: str, params: list[str] | None = None) -> dict[str, dict]:
    response = make_request(url, {"method": "server_info"})
    if response:
        server_info_result = json.loads(response)["result"]["info"]
        server_info = {p: server_info_result[p] for p in params} if params else server_info_result
        # logger.debug(f"{params or 'Full'} server_info:\n{json.dumps(server_info, indent=2)}")
    return server_info


def is_rippled_synced(url: str) -> bool:
    synced = False
    try:
        if server_info := get_server_info(url, ["complete_ledgers", "server_state"]):
            complete_ledgers, server_state = server_info.values()
            synced = complete_ledgers != "empty" and server_state == "full"  # if not custom net, check real validator
            # TODO: get last ledger, wait a bit, do it again to insure increasing
        else:
            logger.info("Received no server_info from %s", url)
            logger.error("No server_info returned")
    except UnboundLocalError:
        logger.debug("no server_info, rippled not running yet?")
    except Exception:
        logger.exception("Couldn't get server_info")
    return synced

# TODO: get_latest_ledger_{rippled,clio}
# TODO: report if ledgers even advancing


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("rippled", nargs="?", default=None, type=str)
    # parser.add_argument("rippled_rpc", help="Other network endpoint to compare with rippled")
    parser.add_argument("-i", "--ip", default="localhost", help="rippled IP")
    parser.add_argument("-p", "--port", default="5005", help="rippled RPC port")
    parser.add_argument("--debug", "-d", action="store_true", help="debug")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    rippled = args.rippled or f"{args.ip}:{args.port}"
    print(f"{rippled=}")
    ready = is_rippled_synced(rippled)
    print(f"rippled {ready=}")
    sys.exit(not int(ready))
