#!/usr/bin/env python3
"""Default command-line entry point used by the platform launchers.

Runs the capacity-gradient test from ``capacity_gradient.py`` and keeps the
``--ping`` / ``--stop`` server-management switches.  The remaining tools have
dedicated modules: ``soak_test.py``, ``sprint_experiment.py``,
``sprint_stats.py`` and ``batch_spawn.py``.
"""
import argparse
import os
import sys

from capacity_gradient import PRESETS, SETUP_CMDS, PEN, run_capacity
from capacity_gradient import spawn
from benchmark_metrics import count_entities, linfit, read_cpu_steal, sample_mspt, warmup_stable
from rcon_client import Rcon, strip_colors


def _password(server_dir, explicit=None):
    password = explicit or os.environ.get("RCON_PASSWORD")
    if password:
        return password
    try:
        with open(os.path.join(server_dir, "server.properties"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("rcon.password="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=PRESETS, default="zombie")
    parser.add_argument("--step", type=int, default=1000)
    parser.add_argument("--max-levels", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=60)
    parser.add_argument("--measure", type=int, default=60)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=50.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default=None)
    parser.add_argument("--server-dir", default="/opt/purpur-test")
    parser.add_argument("--outdir", default=None)
    parser.add_argument("--ping", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--keep-entities", action="store_true")
    args = parser.parse_args(argv)
    password = _password(args.server_dir, args.password)
    if not password:
        parser.error("找不到 RCON 密码: --password / $RCON_PASSWORD / server.properties")
    rc = Rcon(args.host, args.port, password)
    rc.connect()
    if args.ping:
        print(strip_colors(rc.cmd("list")).strip())
        return 0
    if args.stop:
        try:
            rc.cmd("stop", retries=1)
        except Exception:
            pass
        print("已发送 stop")
        return 0
    result = run_capacity(args, rc)
    print(f"\n明细: {result['samples_path']}\n汇总: {result['summary_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
