#!/usr/bin/env python3
"""Send finite heart batches, waiting five minutes on server rate limits."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Public browser configuration, not an admin key.
API = "https://caytaxksqrhrdvontdca.supabase.co/rest/v1"
KEY = "sb_publishable_7lKXZ7SPyRBR0uSNUqbt1g_qJlg_cY1"
CAT_ID = "3be94441-c4e0-4445-8685-e29dcbe04045"
CAT_NAME = "Not quiet a dog"
RETRY_SECONDS = 300
IDENTITY_FILE = Path(__file__).resolve().with_name("vote_cat.identity.json")


class RateLimited(RuntimeError):
    pass


class Rejected(RuntimeError):
    """Explicit server rejection, rather than an unknown transport outcome."""


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(API + path, data=data, headers={
        "apikey": KEY, "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=20) as response:
            result = json.load(response)
        if isinstance(result, dict) and result.get("hint") == "rate_limited":
            raise RateLimited("Network allowance exhausted")
        return result
    except HTTPError as exc:
        with exc:
            detail = exc.read(4096).decode(errors="replace")
        try:
            error = json.loads(detail)
        except ValueError:
            error = {}
        if exc.code == 429 or (isinstance(error, dict)
                               and error.get("hint") == "rate_limited"):
            raise RateLimited("Network allowance exhausted") from exc
        if 400 <= exc.code < 500:
            raise Rejected(f"HTTP {exc.code}: {detail}") from exc
        raise RuntimeError(f"HTTP {exc.code}: {detail}; outcome may be unknown") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Request outcome unknown; not retried: {exc}") from exc


def save(path, state):
    """Replace a single small snapshot; no growing request history."""
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load(path, target):
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("version") != 1 or state.get("dog_id") != CAT_ID:
            raise RuntimeError("Unrecognized progress file.")
        if state.get("target") != target:
            raise RuntimeError("Use the same --hearts total to resume, or a different --state file for a new run.")
        if state.get("pending"):
            raise RuntimeError(
                "The previous run ended during a vote; it may have counted. "
                "Automatic resume is stopped to avoid duplicating it. Keep the progress file.")
        if not 0 <= state["confirmed"] <= target:
            raise RuntimeError("Invalid confirmed count in progress file.")
        return state
    return dict(version=1, dog_id=CAT_ID, target=target, confirmed=0,
                device=None, left=0, batch_done=0, wait_until=0, pending=False)


def wait_until(deadline):
    # Small sleep chunks remain responsive to Ctrl+C, with negligible idle CPU.
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def stable_device(args, state):
    """Keep one identity across restarts and different progress files."""
    with IDENTITY_FILE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = json.loads(IDENTITY_FILE.read_text())["device"] if IDENTITY_FILE.exists() else None
        identities = [value for value in (stored, state.get("device"), getattr(args, "device", None))
                      if value is not None]
        if any(not isinstance(value, str) or not value.strip() for value in identities):
            raise RuntimeError("Invalid saved device identity.")
        if len(set(identities)) > 1:
            raise RuntimeError("The device must match the saved identity.")
        device = identities[0] if identities else secrets.token_urlsafe(18)
        if stored is None:
            save(IDENTITY_FILE, {"device": device})
        return device


def run(args, state):
    device = stable_device(args, state)
    state["device"] = device
    state["left"] = 0  # Recheck the server allowance on every restart.
    save(args.state, state)
    verified = False
    print(f"Progress: {state['confirmed']}/{args.hearts}; batches of {args.batch_size}; "
          f"rate-limit retry every {RETRY_SECONDS}s.", flush=True)
    while state["confirmed"] < args.hearts:
        wait_until(state["wait_until"])
        if state["batch_done"] >= args.batch_size:
            state["batch_done"] = 0
            state["wait_until"] = time.time() + args.batch_pause
            save(args.state, state)
            print(f"Batch complete. Pausing {args.batch_pause}s.", flush=True)
            continue
        voting = False
        try:
            if not verified:
                query = urlencode({"select": "id,name", "id": f"eq.{CAT_ID}",
                                   "status": "eq.approved"})
                dogs = request("/dogs?" + query)
                if len(dogs) != 1 or dogs[0]["name"] != CAT_NAME:
                    raise Rejected("Expected cat entry missing or renamed.")
                verified = True
            if state["left"] == 0:
                status = request("/rpc/heart_status", {"p_device": state["device"]})
                if status.get("open") is not True:
                    raise Rejected("Voting is paused or closed.")
                left = status.get("left")
                if type(left) is not int or left < 0:
                    raise RuntimeError(f"Unexpected allowance: {status}")
                if left == 0:
                    raise RateLimited("This device's allowance is exhausted")
                state["left"] = left
            state["pending"] = True
            save(args.state, state)
            voting = True
            result = request("/rpc/give_heart", {"p_device": state["device"], "p_dog": CAT_ID})
            if result.get("ok") is not True:
                raise Rejected(f"Vote rejected: {result}")
            state["confirmed"] += 1
            state["batch_done"] += 1
            state["pending"] = False
            left = result.get("left")
            if type(left) is not int or left < 0:
                # Preserve the confirmed vote before stopping on a changed API.
                save(args.state, state)
                raise RuntimeError(f"Unexpected remaining allowance: {result}")
            state["left"] = left
            state["wait_until"] = 0
            save(args.state, state)
            if state["confirmed"] % 100 == 0 or state["confirmed"] == args.hearts:
                print(f"Confirmed {state['confirmed']}/{args.hearts} hearts.", flush=True)
            if args.delay and state["confirmed"] < args.hearts:
                wait_until(time.time() + args.delay)
        except RateLimited:
            state["pending"] = False  # Explicit rejection: safe to retry this vote.
            state["left"] = 0  # Check allowance before attempting another vote.
            state["wait_until"] = time.time() + RETRY_SECONDS
            save(args.state, state)
            print(f"Rate limited at {state['confirmed']}/{args.hearts}. "
                  f"Waiting {RETRY_SECONDS}s before one retry.", flush=True)
        except Rejected:
            if voting:
                state["pending"] = False
                save(args.state, state)
            raise
    print(f"Done: {state['confirmed']} hearts confirmed for this run.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hearts", type=int, default=100, help="total for this run, including resumed progress")
    parser.add_argument("--device", help="optional existing identity for first setup; otherwise one is generated and permanently reused")
    parser.add_argument("--batch-size", type=int, default=1200, help="hearts per batch, 1–1200 (default: 1200)")
    parser.add_argument("--batch-pause", type=float, default=300, help="seconds between full batches, 0–86400 (default: 300)")
    parser.add_argument("--delay", type=float, default=0, help="seconds between successful votes, 0–60 (default: 0)")
    parser.add_argument("--workers", type=int, choices=[1], default=1, help="one sequential worker for resumable counts")
    parser.add_argument("--state", type=Path, default=Path("vote_cat1.progress.json"), help="small progress file; reused on restart")
    parser.add_argument("--dry-run", action="store_true", help="check target and print plan without votes or state writes")
    args = parser.parse_args()
    if args.hearts <= 0:
        parser.error("--hearts must be positive")
    if not 1 <= args.batch_size <= 1200:
        parser.error("--batch-size must be between 1 and 1200")
    for name, maximum in [("delay", 60), ("batch_pause", 86400)]:
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 <= value <= maximum:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and {maximum}")
    args.state = args.state.resolve()
    try:
        if args.dry_run:
            state = load(args.state, args.hearts)
            query = urlencode({"select": "id,name,hearts", "id": f"eq.{CAT_ID}", "status": "eq.approved"})
            dogs = request("/dogs?" + query)
            if len(dogs) != 1 or dogs[0]["name"] != CAT_NAME:
                raise RuntimeError("Expected cat entry missing or renamed.")
            print(f"{CAT_NAME}: {dogs[0]['hearts']} hearts currently. "
                  f"Would send {args.hearts - state['confirmed']} remaining hearts; "
                  f"batch size {args.batch_size}, batch pause {args.batch_pause}s, delay {args.delay}s.")
            return 0
        # A file lock prevents two instances from sharing/overwriting progress.
        with args.state.with_name(args.state.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another instance is using this progress file.") from exc
            state = load(args.state, args.hearts)
            try:
                run(args, state)
            finally:
                print(f"Saved progress: {state['confirmed']}/{args.hearts}. File: {args.state}", flush=True)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped by user. Re-run the same command to resume; an interrupted vote may need review.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
