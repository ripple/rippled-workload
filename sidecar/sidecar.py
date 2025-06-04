import json
import sys
import time
import argparse

from typing import ClassVar
from urllib import request
from urllib.error import HTTPError, URLError
from threading import Thread
from antithesis import lifecycle, assertions

# Opcodes
OP_LEDGER = "ledger"

# Convert a server name to a URL to request against
to_url = lambda ip: f"http://{ip}:5005"

#
#
class LedgerClosedThread(Thread):
    """Define a thread to make a ledger call to a validator"""

    def __init__(self, validator):
        super().__init__(None, None, validator, None, None)
        self._return = None
        self.validator = validator

    def run(self):
        """Get the status of the ledger"""
        req = request.Request(to_url(self.validator), data=json.dumps({"method": OP_LEDGER, "params": [{"ledger_index": "validated"}]}).encode())
        try:
            data = json.loads(request.urlopen(req, timeout=2).read())
            result = data["result"]
            # print(f"--> {self.validator} returning {result}", file=sys.stderr)
        except (HTTPError, URLError, TimeoutError) as err:
            result = {"exception": str(err), "status": f"node not running"}
        except Exception as err:
            result = {"exception": str(err), "status": f"exception"}

        self._return = result

#
#
if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("-v", "--validator", action="extend", nargs="+", type=str, help="Hostname(s) of validators to poll", default=[])
    parser.add_argument("-i", "--interval", type=int, help="Seconds between checking the validators", default=15 )
    parser.add_argument("-t", "--tolerance", type=int, help="Number of times a validator can freeze without alarming", default=1)
    parser.add_argument("--min", type=int, help="Minimum index number to alarm", default=0)
    parser.add_argument("--stop", type=int, help="Stop after this number of cycles", default=0)

    args = parser.parse_args()

    print(f"Sidecar args: {str(args)}")
    lifecycle.send_event("sidecar_start", {"args": str(args)})

    CHECK_INTERVAL_SECS = args.interval
    ALLOWED_MISSED_INTERVALS = args.tolerance
    MIN_INDEX_FOR_ALERT = args.min

    # Servers to collect status from
    servers = [x for x in args.validator]
    print(f'Server list: {servers}')

    # Tallys for state between passes
    last_index_per_validator = {}  # validator: (index, last_update)
    update_num = 0

    for v in servers:
        last_index_per_validator[v] = (0,0)

    stop_num = args.stop   # 0 to run forever

    while True:

        pass_start = time.time()
        update_num += 1

        num_stalled = 0
        num_reporting = 0
        stalled_validators = []

        # Make each validator call on a separate thread
        threads = {v: LedgerClosedThread(v) for v in servers}

        for v in threads:
            threads[v].start()

        for v in threads:
            threads[v].join()

        # Process the results of this pass
        for v in threads:
            results = threads[v]._return
            if results["status"] != "success":
                # Error getting results for this validator
                # Leave the current state as it was. We could do something else here
                # but for now just float through the error. Due to faults in effect it
                # is possible we temporarily lose connections with the validator
                 pass

            else: # Success getting the status
                details = {'index': results['ledger_index'], 'hash': results['ledger_hash'], 'status': results['status'] }

                num_reporting += 1

                # Check if the index is updating
                old_state = last_index_per_validator[v]

                if (old_state[0] != details['index']):
                    # Index changed
                    last_index_per_validator[v] = (details['index'], update_num)

                else: # Possibly stalled
                    # Check that enough intervals have happened
                    if ((old_state[1] + ALLOWED_MISSED_INTERVALS) < update_num) :
                        # This is stalled. Report it if we are deep enough in the run (to avoid alerting during start-up)
                        if old_state[0] >= MIN_INDEX_FOR_ALERT:
                            print(f"STALLED VALIDATOR: {v} Index: {details['index']} Checks missed: {update_num - old_state[1]}")
                            lifecycle.send_event("validator_stall", {"validator": v, "index": details['index'], "missed": update_num - old_state[1], "details": details})
                            num_stalled += 1
                            stalled_validators.append(v)

        to_log = {"healthcheck_seq": update_num, "validator_status" : {v: threads[v]._return for v in servers}, "stalled_validators": stalled_validators}
        lifecycle.send_event("val_health", to_log)

        if num_reporting > 0:
            assertions.always(num_stalled == 0, "Validators are never stalled", to_log)
            assertions.always(num_stalled < len(servers), "ALL validators are never stalled", to_log)

        print(f"Done with healthcheck pass {update_num}")

        # Wait for next pass
        if update_num == stop_num:
            print("Healthcheck exited normally")
            break

        time.sleep(max(1, (pass_start + CHECK_INTERVAL_SECS) - time.time()))
