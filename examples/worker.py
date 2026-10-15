"""Runnable agent loop for fictional row-count work; uses the public CLI only."""

import json
import subprocess
import threading
import time


def atb(*args: str) -> dict:
    completed = subprocess.run(["atb", *args, "--json"], text=True, capture_output=True)
    payload = json.loads(completed.stdout)
    if not payload["ok"]:
        raise RuntimeError(payload["error"]["code"] + ": " + payload["error"]["message"])
    return payload["data"]


def main() -> None:
    agent = "example-worker"
    while task := atb("claim", "--agent", agent, "--queue", "default", "--lease-seconds", "30"):
        lease = [task["id"], "--agent", agent, "--token", task["lease_token"]]
        stop, lost = threading.Event(), threading.Event()

        def renew() -> None:
            while not stop.wait(5):
                try:
                    atb("heartbeat", *lease, "--lease-seconds", "30")
                except RuntimeError:
                    lost.set()
                    return

        atb("heartbeat", *lease, "--lease-seconds", "30")
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            # Replace only this fixed handler with trusted, idempotent work.
            # Check lost between steps; stop external work promptly on lease loss.
            time.sleep(0.1)
            if lost.is_set():
                raise RuntimeError("Lease lost; stop work")
            result = {"processed_rows": int(task["params"].get("rows", 0))}
            atb("done", *lease, "--result", json.dumps(result))
        except Exception as error:
            if not lost.is_set():
                try:
                    atb("fail", *lease, "--error", str(error))
                except RuntimeError:
                    pass  # Another worker now owns the lease; do not overwrite it.
        finally:
            stop.set()
            thread.join()


if __name__ == "__main__":
    main()
