#!/usr/bin/env python3
"""
Quick live test for the agent server.

Takes a screenshot of your screen, sends it to POST /plan with a goal,
and prints the returned step plan. No frontend needed.

Usage:
    # 1. Start the server (in another terminal):
    #    cd agent-server && source .venv/bin/activate
    #    uvicorn app.main:app --reload
    #
    # 2. Run this script:
    #    python test_live.py
    #    python test_live.py "Open System Settings and go to Wi-Fi"
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

SERVER_URL = "http://localhost:8000"
DEFAULT_GOAL = "Find and open the Downloads folder in Finder"


def take_screenshot() -> tuple[bytes, int, int]:
    """Capture the main display using macOS screencapture. Returns (png_bytes, width, height)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    # -x = no sound, -C = capture cursor, -m = main display only
    result = subprocess.run(
        ["screencapture", "-x", "-C", "-m", tmp_path],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"screencapture failed: {result.stderr.decode()}")

    png_bytes = Path(tmp_path).read_bytes()
    Path(tmp_path).unlink()

    # Get screen dimensions via system_profiler
    try:
        sp = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in sp.stdout.splitlines():
            if "Resolution" in line:
                parts = line.split(":")[1].strip().split()
                w, h = int(parts[0]), int(parts[2])
                if "Retina" in line:
                    w, h = w // 2, h // 2
                return png_bytes, w, h
    except Exception:
        pass

    return png_bytes, 1920, 1080


def test_plan(goal: str) -> dict | None:
    """Send a screenshot + goal to POST /plan and print the result."""
    print(f"\n{'='*60}")
    print(f"  GOAL: {goal}")
    print(f"{'='*60}\n")

    # Health check
    print("[1/3] Checking server health...")
    try:
        resp = httpx.get(f"{SERVER_URL}/health", timeout=5)
        health = resp.json()
        print(f"  Server: {health['status']}, mock_mode={health['mock_mode']}, model={health['model']}")
    except httpx.ConnectError:
        print("  ERROR: Cannot connect to server. Is it running?")
        print("  Start it with:")
        print("    cd agent-server && source .venv/bin/activate")
        print("    uvicorn app.main:app --reload")
        sys.exit(1)

    # Screenshot
    print("[2/3] Taking screenshot...")
    start = time.time()
    png_bytes, w, h = take_screenshot()
    elapsed = round((time.time() - start) * 1000)
    print(f"  Captured: {len(png_bytes):,} bytes, {w}x{h} ({elapsed}ms)")

    # Send to /plan
    print("[3/3] Sending to /plan...")
    start = time.time()
    resp = httpx.post(
        f"{SERVER_URL}/plan",
        data={
            "goal": goal,
            "image_size": f'{{"w":{w},"h":{h}}}',
        },
        files={
            "screenshot": ("screenshot.png", png_bytes, "image/png"),
        },
        headers={"X-Request-ID": "test-live"},
        timeout=30,
    )
    elapsed = round((time.time() - start) * 1000)

    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return None

    plan = resp.json()
    print(f"  Response: {resp.status_code} ({elapsed}ms)")
    print_plan(plan)
    return plan


def test_next_step(goal: str, original_plan: dict | None):
    """Test the /next endpoint with a fresh screenshot after a fake step 1."""
    if original_plan is None:
        print("\n  Skipping /next test (no plan from /plan)")
        return

    num_steps = len(original_plan["steps"])
    first_step = original_plan["steps"][0]

    print(f"\n{'='*60}")
    print(f"  TESTING /next  (pretending step 1 is done)")
    print(f"{'='*60}\n")

    print("[1/2] Taking fresh screenshot...")
    start = time.time()
    png_bytes, w, h = take_screenshot()
    elapsed = round((time.time() - start) * 1000)
    print(f"  Captured: {len(png_bytes):,} bytes ({elapsed}ms)")

    import json
    completed = json.dumps([{"id": first_step["id"], "instruction": first_step["instruction"]}])

    print(f"[2/2] Sending to /next (completed: [{first_step['id']}], total: {num_steps})...")
    start = time.time()
    resp = httpx.post(
        f"{SERVER_URL}/next",
        data={
            "goal": goal,
            "image_size": f'{{"w":{w},"h":{h}}}',
            "completed_steps": completed,
            "total_steps": str(num_steps),
        },
        files={
            "screenshot": ("screenshot.png", png_bytes, "image/png"),
        },
        headers={"X-Request-ID": "test-live-next"},
        timeout=30,
    )
    elapsed = round((time.time() - start) * 1000)

    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return

    plan = resp.json()
    print(f"  Response: {resp.status_code} ({elapsed}ms)")
    print_plan(plan, label="NEXT STEPS")


def print_plan(plan: dict, label: str = "PLAN"):
    """Pretty-print a step plan."""
    print(f"\n{'─'*60}")
    print(f"  {label}: {plan['goal']}")
    print(f"  Version: {plan['version']}  |  Image: {plan['image_size']['w']}x{plan['image_size']['h']}")
    print(f"  Steps: {len(plan['steps'])}")
    print(f"{'─'*60}")

    for i, step in enumerate(plan["steps"]):
        targets = step["targets"]
        t = targets[0]
        conf = t.get("confidence", "?")
        label_txt = t.get("label", "")
        advance = step["advance"]["type"]
        print(f"\n  Step {step['id']}  [{advance}]")
        print(f"    {step['instruction']}")
        print(f"    Target: ({t['x']:.3f}, {t['y']:.3f}) {t['w']:.3f}x{t['h']:.3f}  conf={conf}  \"{label_txt}\"")
        if len(targets) > 1:
            print(f"    + {len(targets)-1} more target(s)")

    print()


if __name__ == "__main__":
    goal = DEFAULT_GOAL
    args = sys.argv[1:]
    if args:
        goal = " ".join(args)

    plan = test_plan(goal)
    test_next_step(goal, plan)

    print("=" * 60)
    print("  All tests passed!")
    print("=" * 60)
