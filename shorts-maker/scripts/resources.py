#!/usr/bin/env python3
"""Pick safe CPU limits based on whether OBS is running.

The scripts call this automatically; you never have to check OBS yourself.

NEVER stops, closes, restarts or signals OBS. It only reads /proc to detect it.
Policy (8-core laptop):
  OBS running  -> 2 cores, nice 19, ionice idle          (the stream wins)
  OBS not      -> up to 6 cores, nice 19, ionice best-effort
Never uses 100% of the machine.
"""
import os
import subprocess
from pathlib import Path

OBS_NAMES = {"obs", "obs-studio", "obs64", "obs32"}
CPUS = os.cpu_count() or 4


def obs_running():
    """True if an OBS process exists. Read-only; never touches it."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip().lower()
        except OSError:
            continue
        if name in OBS_NAMES:
            return True
    return False


def policy(obs=None):
    obs = obs_running() if obs is None else obs
    cores = 2 if obs else max(1, min(6, CPUS - 2))
    model_threads = 4 if obs else max(1, min(6, CPUS - 2))
    return {"obs": obs, "threads": cores, "model_threads": model_threads,
            "nice": 19, "ionice": 3 if obs else 2, "cpu_list": f"0-{cores - 1}"}


def apply_pid(pid, p=None):
    """Apply affinity / nice / ionice to a process and all of its threads."""
    p = p or policy()
    subprocess.run(["taskset", "-a", "-pc", p["cpu_list"], str(pid)],
                   capture_output=True)
    subprocess.run(["renice", str(p["nice"]), "-p", str(pid)],
                   capture_output=True)
    subprocess.run(["ionice", "-c", str(p["ionice"]), "-p", str(pid)],
                   capture_output=True)
    return p


def apply_self(p=None):
    """Apply the policy to the current process before loading a model."""
    p = p or policy()
    try:
        os.setpriority(os.PRIO_PROCESS, 0, p["nice"])
    except OSError:
        pass
    try:
        os.sched_setaffinity(0, set(range(p["threads"])))
    except OSError:
        pass
    return p
