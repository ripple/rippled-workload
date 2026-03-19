#!/usr/bin/env python3
"""Real-time XRPL Amendment Vote Tracker
Monitors validation messages to calculate amendment support percentages
"""

import json
import threading
from collections import defaultdict
from datetime import datetime

import websocket

RIPPLED_WS = "ws://192.168.100.20:6006"


class AmendmentTracker:
    def __init__(self):
        self.validator_votes = defaultdict(set)  # validator_key -> set of amendment_ids
        self.amendment_support = defaultdict(set)  # amendment_id -> set of validator_keys
        self.total_validators = set()
        self.lock = threading.Lock()

    def update_vote(self, validator_key, amendments):
        """Update a validator's amendment votes"""
        with self.lock:
            # Clear old votes for this validator
            for amendment_id in self.validator_votes[validator_key]:
                self.amendment_support[amendment_id].discard(validator_key)

            # Add new votes
            self.validator_votes[validator_key] = set(amendments)
            self.total_validators.add(validator_key)

            for amendment_id in amendments:
                self.amendment_support[amendment_id].add(validator_key)

    def get_support_percentage(self, amendment_id):
        """Calculate support percentage for an amendment"""
        with self.lock:
            if not self.total_validators:
                return 0.0
            supporters = len(self.amendment_support[amendment_id])
            total = len(self.total_validators)
            return (supporters / total) * 100

    def get_all_amendments(self):
        """Get all amendments being voted on with their support"""
        with self.lock:
            results = []
            for amendment_id in self.amendment_support.keys():
                supporters = len(self.amendment_support[amendment_id])
                total = len(self.total_validators)
                percentage = (supporters / total) * 100 if total > 0 else 0
                results.append(
                    {
                        "amendment_id": amendment_id,
                        "supporters": supporters,
                        "total_validators": total,
                        "percentage": percentage,
                    }
                )
            return sorted(results, key=lambda x: x["percentage"], reverse=True)


def on_message(ws, message, tracker):
    """Handle incoming WebSocket messages"""
    try:
        data = json.loads(message)

        # Look for validation messages
        if data.get("type") == "validationReceived":
            validation = data.get("validation", {})

            # Extract validator public key
            validator_key = validation.get("validation_public_key")

            # Extract amendments (this is the key field!)
            amendments = validation.get("amendments", [])

            if validator_key and amendments:
                tracker.update_vote(validator_key, amendments)

                # Print update
                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                    f"Validator {validator_key[:16]}... voted for {len(amendments)} amendments"
                )

    except Exception as e:
        print(f"Error processing message: {e}")


def on_error(ws, error):
    print(f"WebSocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    print("WebSocket connection closed")


def on_open(ws):
    """Subscribe to validation stream when connection opens"""
    print("WebSocket connection opened")
    subscribe_msg = {
        "command": "subscribe",
        "streams": ["validations"],
    }
    ws.send(json.dumps(subscribe_msg))
    print("Subscribed to validations stream")


def print_status(tracker):
    """Periodically print amendment support status"""
    import time

    while True:
        time.sleep(30)  # Print every 30 seconds

        print("\n" + "=" * 80)
        print(f"Amendment Support Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

        amendments = tracker.get_all_amendments()

        if not amendments:
            print("No amendment votes detected yet...")
            continue

        print(f"\nTracking {len(tracker.total_validators)} validators")
        print(f"Amendments being voted on: {len(amendments)}\n")

        for i, amend in enumerate(amendments[:10], 1):  # Show top 10
            bar_length = 50
            filled = int(bar_length * amend["percentage"] / 100)
            bar = "█" * filled + "░" * (bar_length - filled)

            status = "✅ MAJORITY" if amend["percentage"] >= 80 else "⏳ PENDING"

            print(f"{i:2}. {amend['amendment_id'][:32]}...")
            print(
                f"    [{bar}] {amend['percentage']:.1f}% ({amend['supporters']}/{amend['total_validators']}) {status}"
            )

        if len(amendments) > 10:
            print(f"\n    ... and {len(amendments) - 10} more amendments")


def main():
    tracker = AmendmentTracker()

    # Start status printing thread
    status_thread = threading.Thread(target=print_status, args=(tracker,), daemon=True)
    status_thread.start()

    # Connect to WebSocket
    ws = websocket.WebSocketApp(
        RIPPLED_WS,
        on_message=lambda ws, msg: on_message(ws, msg, tracker),
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )

    print(f"Connecting to {RIPPLED_WS}...")
    print("This will monitor validation messages and calculate amendment support in real-time\n")

    ws.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
