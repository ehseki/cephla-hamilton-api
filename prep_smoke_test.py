"""
Read-only smoke test for the Hamilton Microlab Prep REST API.

Logs in, reads instrument/run state and protocol list, then logs out.
Makes NO changes and moves NO hardware.

Usage (Windows):
    pip install requests
    python prep_smoke_test.py            # uses default IP 192.168.100.101
    python prep_smoke_test.py 192.168.100.101
"""
import getpass
import json
import sys

import requests

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.100.101"
BASE = f"http://{IP}/NimbusLite/api/v1"  # the API is hosted under /NimbusLite
TIMEOUT = 10  # seconds


def show(label, resp):
    print(f"\n=== {label}  [{resp.status_code}] ===")
    try:
        print(json.dumps(resp.json(), indent=2)[:2000])
    except ValueError:
        print(resp.text[:2000])


def main():
    s = requests.Session()

    # 1. No-auth check
    show("GET /system-ready", s.get(f"{BASE}/system-ready", timeout=TIMEOUT))

    # 2. Log in with the same account you use on the touchscreen
    user = input("\nPrep username: ")
    pw = getpass.getpass("Prep password: ")
    r = s.post(f"{BASE}/authenticate", json={"username": user, "password": pw}, timeout=TIMEOUT)
    show("POST /authenticate", r)
    if r.status_code != 200:
        print("\nLogin failed; stopping.")
        return
    s.headers["Authorization"] = f"Bearer {r.json()['token']}"

    # 3. Read-only calls
    for label, path in [
        ("Auth check", "/authenticate/check-authentication"),
        ("Software versions", "/software-versions"),
        ("Instrument", "/instruments"),
        ("Connection status", "/instruments/connection-status"),
        ("Global run state", "/instruments/global-run-state"),
        ("Current run", "/protocol-run"),
        ("Protocol names", "/protocols/names"),
    ]:
        try:
            show(f"GET {path}  ({label})", s.get(f"{BASE}{path}", timeout=TIMEOUT))
        except requests.RequestException as e:
            print(f"\n=== GET {path} FAILED: {e}")

    # 4. Log out so the session doesn't linger
    show("DELETE /authenticate (logout)", s.delete(f"{BASE}/authenticate", timeout=TIMEOUT))


if __name__ == "__main__":
    main()
