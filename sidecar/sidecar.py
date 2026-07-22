import argparse
import json
import time
from threading import Thread
from urllib import request
from urllib.error import HTTPError, URLError

from antithesis import assertions, lifecycle

# Opcodes
OP_LEDGER = "ledger"
OP_SERVER_INFO = "server_info"

# Ripple-epoch (2000-01-01 UTC) seconds offset from Unix epoch.
RIPPLE_EPOCH_OFFSET = 946684800

# Max acceptable |network close_time - sidecar wall clock|.
MAX_TIME_SKEW_SECS = 60


def complete_ledgers_floor(cl: str | None) -> int | None:
    """Lower bound of a complete_ledgers range like '256-512' or '256-400,410-512'."""
    if not cl or cl in ("empty", "unknown"):
        return None
    try:
        return int(cl.split(",")[0].split("-")[0])
    except (ValueError, AttributeError):
        return None


def to_url(ip: str) -> str:
    """Convert a server name to a URL to request against."""
    return f"http://{ip}:5005"


#
#
class LedgerClosedThread(Thread):
    """Define a thread to make a ledger call to a validator"""

    def __init__(self, validator: str) -> None:
        super().__init__(None, None, validator, None, None)
        self._return = None
        self._complete_ledgers = None
        self.validator = validator

    def run(self) -> None:
        """Get the status of the ledger"""
        req = request.Request(
            to_url(self.validator),
            data=json.dumps(
                {"method": OP_LEDGER, "params": [{"ledger_index": "validated"}]}
            ).encode(),
        )
        try:
            data = json.loads(request.urlopen(req, timeout=2).read())
            result = data["result"]
            # print(f"--> {self.validator} returning {result}", file=sys.stderr)
        except (HTTPError, URLError, TimeoutError) as err:
            result = {"exception": str(err), "status": "node not running"}
        except Exception as err:
            result = {"exception": str(err), "status": "exception"}

        self._return = result
        self._complete_ledgers = self._fetch_complete_ledgers()

    def _fetch_complete_ledgers(self) -> str | None:
        """Read the node's retained-ledger range to observe online_delete pruning."""
        req = request.Request(
            to_url(self.validator),
            data=json.dumps({"method": OP_SERVER_INFO, "params": [{}]}).encode(),
        )
        try:
            data = json.loads(request.urlopen(req, timeout=2).read())
            return data["result"]["info"].get("complete_ledgers")
        except Exception:
            return None


#
#
if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-v",
        "--validator",
        action="extend",
        nargs="+",
        type=str,
        help="Hostname(s) of validators to poll",
        default=[],
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        help="Seconds between checking the validators",
        default=15,
    )
    parser.add_argument(
        "-t",
        "--tolerance",
        type=int,
        help="Missed intervals before a frozen validator is reported as a (soft) stall. "
        "This is telemetry only and does NOT fail an assertion -- a fault-induced halt "
        "that later recovers is expected.",
        default=1,
    )
    parser.add_argument(
        "--max-stall",
        dest="max_stall",
        type=int,
        help="Recovery budget: missed intervals a validator may stay frozen before the "
        "stall is treated as non-recovering and fails the assertion. Must exceed --tolerance "
        "and the longest legitimate fault-induced halt; tune to your fault-injection window. "
        "Default 20 (~5 min at 15s) is ~3x the longest fault-driven freeze observed in runs.",
        default=20,
    )
    parser.add_argument("--min", type=int, help="Minimum index number to alarm", default=0)
    parser.add_argument("--stop", type=int, help="Stop after this number of cycles", default=0)

    args = parser.parse_args()

    print(f"Sidecar args: {args!s}")
    lifecycle.send_event("sidecar_start", {"args": str(args)})

    CHECK_INTERVAL_SECS = args.interval
    ALLOWED_MISSED_INTERVALS = args.tolerance
    MAX_STALL_INTERVALS = args.max_stall
    MIN_INDEX_FOR_ALERT = args.min

    # Servers to collect status from
    servers = list(args.validator)
    print(f"Server list: {servers}")

    # Tallys for state between passes
    last_index_per_validator = {}  # validator: (index, update_num_of_last_change)
    stall_episode = {}  # validator: bool -- currently inside a (soft) stall episode
    last_total_coins = {}  # validator: (index, total_coins) at the highest index seen
    baseline_floor = {}  # validator: first complete_ledgers lower bound seen
    max_floor = {}  # validator: highest lower bound seen (rotation-event dedup)
    update_num = 0

    for v in servers:
        last_index_per_validator[v] = (0, 0)
        stall_episode[v] = False
        last_total_coins[v] = None
        baseline_floor[v] = None
        max_floor[v] = None

    stop_num = args.stop  # 0 to run forever

    while True:
        pass_start = time.time()
        update_num += 1

        num_reporting = 0
        # Soft stalls: frozen past --tolerance. Telemetry only, expected under faults.
        num_soft_stalled = 0
        soft_stalled_validators = []
        # Unrecovered stalls: frozen past --max-stall. This is the real failure condition.
        num_unrecovered = 0
        unrecovered_validators = []
        # Validators that closed a new ledger this pass -- the only ones with a fresh
        # close_time to judge against wall clock.
        advanced_validators: set[str] = set()

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
                # Error getting results for this validator. Leave the current state as it
                # was -- due to faults in effect it is possible we temporarily lose the
                # connection with the validator.
                continue

            # Success getting the status
            details = {
                "index": results["ledger_index"],
                "hash": results["ledger_hash"],
                "status": results["status"],
            }

            num_reporting += 1

            last_idx, last_upd = last_index_per_validator[v]

            if last_idx != details["index"]:
                # Index advanced -> the validator is making progress.
                advanced_validators.add(v)
                if stall_episode[v]:
                    # It was frozen and has now recovered. A recovered halt is expected
                    # under fault injection and must NOT fail any assertion.
                    stalled_for = update_num - last_upd
                    print(
                        f"RECOVERED VALIDATOR: {v} "
                        f"Index: {details['index']} "
                        f"Stalled intervals: {stalled_for}"
                    )
                    lifecycle.send_event(
                        "validator_recovered",
                        {
                            "validator": v,
                            "index": details["index"],
                            "stalled_intervals": stalled_for,
                            "details": details,
                        },
                    )
                    stall_episode[v] = False
                last_index_per_validator[v] = (details["index"], update_num)

            elif last_idx >= MIN_INDEX_FOR_ALERT:
                # Index unchanged -> possibly stalled.
                missed = update_num - last_upd

                if missed > ALLOWED_MISSED_INTERVALS:
                    # Soft stall: telemetry only. Expected while faults are active.
                    stall_episode[v] = True
                    print(
                        f"STALLED VALIDATOR: {v} Index: {details['index']} Checks missed: {missed}"
                    )
                    lifecycle.send_event(
                        "validator_stall",
                        {
                            "validator": v,
                            "index": details["index"],
                            "missed": missed,
                            "details": details,
                        },
                    )
                    num_soft_stalled += 1
                    soft_stalled_validators.append(v)

                if missed > MAX_STALL_INTERVALS:
                    # Frozen past the recovery budget: treat as a genuine, non-recovering
                    # stall. This is what fails the assertions below.
                    print(
                        f"UNRECOVERED STALL: {v} "
                        f"Index: {details['index']} "
                        f"Checks missed: {missed} "
                        f"Budget: {MAX_STALL_INTERVALS}"
                    )
                    lifecycle.send_event(
                        "validator_stall_unrecovered",
                        {
                            "validator": v,
                            "index": details["index"],
                            "missed": missed,
                            "budget": MAX_STALL_INTERVALS,
                            "details": details,
                        },
                    )
                    num_unrecovered += 1
                    unrecovered_validators.append(v)

        to_log = {
            "healthcheck_seq": update_num,
            "validator_status": {v: threads[v]._return for v in servers},
            "stalled_validators": soft_stalled_validators,
            "unrecovered_validators": unrecovered_validators,
            "max_stall_intervals": MAX_STALL_INTERVALS,
        }
        lifecycle.send_event("val_health", to_log)

        # online_delete: a full-history node keeps genesis forever, so its
        # complete_ledgers lower bound never rises. Any rise above the first value seen
        # proves NuDB rotation ran and pruned -- reachable at rotation 1 (~ledger 258),
        # no threshold. sometimes(), not always(): a short run may never rotate; only a
        # whole (long) run that never observes it means the config broke.
        rotated_any = False
        rot_evt: dict = {}
        for v in servers:
            floor = complete_ledgers_floor(threads[v]._complete_ledgers)
            if floor is None:
                continue
            if baseline_floor[v] is None:
                baseline_floor[v] = floor
                max_floor[v] = floor
            elif floor > max_floor[v]:
                lifecycle.send_event(
                    "online_delete_rotation",
                    {"validator": v, "ledger_floor": floor, "baseline_floor": baseline_floor[v]},
                )
                max_floor[v] = floor
            if floor > baseline_floor[v]:
                rotated_any = True
                rot_evt = {
                    "validator": v,
                    "ledger_floor": floor,
                    "baseline_floor": baseline_floor[v],
                }
        assertions.sometimes(rotated_any, "online_delete rotation observed", rot_evt)

        if num_reporting > 0:
            # A fault-induced halt that recovers within the budget never trips these.
            # Only a validator (or the whole set) that stays frozen past --max-stall does.
            assertions.always(num_unrecovered == 0, "Validators are never stalled", to_log)
            assertions.always(
                num_unrecovered < len(servers), "ALL validators are never stalled", to_log
            )

            # A ledger's close_time carries a fresh, consensus-set value only when the
            # ledger closes. Judge each close_time once, when its ledger appears: a
            # freshly closed ledger must be stamped within skew of wall clock, faults or
            # not -- a manipulated close_time (malicious validator) is caught immediately.
            # A halt closes no new ledger, so it re-presents an already-judged close_time
            # that drifts from wall clock purely because time passes; skip it (the stall
            # checks cover halts) instead of re-judging it, which is what made this fire
            # on fault-induced outages.
            now_unix = int(time.time())
            for v, thr in threads.items():
                if v not in advanced_validators:
                    continue
                r = thr._return
                ct = r.get("ledger", {}).get("close_time")
                if ct is None:
                    continue
                ct_unix = int(ct) + RIPPLE_EPOCH_OFFSET
                skew = abs(ct_unix - now_unix)
                evt = {"validator": v, "close_time": ct, "skew_secs": skew}
                if skew > MAX_TIME_SKEW_SECS:
                    lifecycle.send_event("validator_time_skew", evt)
                assertions.always(
                    skew <= MAX_TIME_SKEW_SECS,
                    "Validator close_time tracks wall clock",
                    evt,
                )

            # XRP supply is fixed and only burns (fees), so total_coins must never rise
            # as a validator's ledger advances. Any increase means XRP was minted -- a
            # safety bug, so this is a hard always(). Compared only when the index
            # strictly advances (a stale/rewound read during faults isn't a violation).
            for v, thr in threads.items():
                r = thr._return
                if r.get("status") != "success":
                    continue
                idx = r.get("ledger_index")
                tc_raw = r.get("ledger", {}).get("total_coins")
                if idx is None or tc_raw is None:
                    continue
                total_coins = int(tc_raw)
                prev = last_total_coins[v]
                if prev is not None and idx > prev[0]:
                    evt = {
                        "validator": v,
                        "index": idx,
                        "prev_index": prev[0],
                        "total_coins": total_coins,
                        "prev_total_coins": prev[1],
                    }
                    assertions.always(
                        total_coins <= prev[1],
                        "XRP total supply never increases",
                        evt,
                    )
                if prev is None or idx >= prev[0]:
                    last_total_coins[v] = (idx, total_coins)

            # Validators on the same ledger_index must agree on its hash. This is a safety
            # invariant -- divergence is always a bug, so it stays a hard always().
            by_index = {}
            for v, thr in threads.items():
                r = thr._return
                if r.get("status") != "success":
                    continue
                idx = r.get("ledger_index")
                h = r.get("ledger_hash")
                if idx is None or h is None:
                    continue
                by_index.setdefault(idx, {})[v] = h
            for idx, hashes in by_index.items():
                unique = set(hashes.values())
                evt = {"index": idx, "hashes": hashes}
                assertions.always(
                    len(unique) <= 1,
                    "Validators agree on ledger hash for a given index",
                    evt,
                )

        print(f"Done with healthcheck pass {update_num}")

        # Wait for next pass
        if update_num == stop_num:
            print("Healthcheck exited normally")
            break

        time.sleep(max(1, (pass_start + CHECK_INTERVAL_SECS) - time.time()))
