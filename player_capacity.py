"""Optional capacity test using real Minecraft client adapters.

The core never impersonates players through RCON. Supply ``adapter_factory``
returning a PlayerClient, or a command for one external client per player.
Command templates support {host}, {port}, {username}, and {index}. They are
split with shlex and executed without a shell (quote arguments containing spaces).

Command adapter protocol (UTF-8 JSON lines):
* stdin: {"event":"connect","host":...,"port":...,"username":...}
* stdout: {"event":"connected"} ONLY after the real client joins the server.
* stdin: {"event":"action","action":"look", "yaw":..., "pitch":...},
  {"event":"action","action":"move","forward":true,"sprint":false}, or
  {"event":"action","action":"jump","active":true}. Yaw is in radians;
  move/jump controls persist until the next update. Each action must be applied
  by the actual client, not translated into RCON summons or teleports.
* stdout: {"event":"disconnected"} or {"event":"error","message":...}
* stdin: {"event":"stop"}; the adapter must disconnect and exit.

Adapters must flush each output line. Other stdout lines are ignored; stderr
is inherited for diagnostics. A live process is NOT proof of a connection.
The external implementation (for example a mineflayer wrapper) and any account
credentials are the operator's responsibility. Linux CPU steal is measured on
this runner's host, not fetched remotely from the Minecraft server.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from benchmark_metrics import read_cpu_steal, sample_mspt
from rcon_client import Rcon


@dataclass(frozen=True)
class PlayerSpec:
    index: int
    username: str
    host: str
    port: int


class PlayerClient(Protocol):
    """One real client. Operations must be bounded; status reports server join.

    ``start`` allocates the client, ``connect`` joins within timeout seconds,
    ``act`` applies a scenario control, and ``stop`` releases resources even
    after a partial start. ``status`` returns False after disconnect. A factory
    callback accepting PlayerSpec is sufficient for injecting Python clients.
    """

    def start(self) -> None: ...
    def connect(self, timeout: float) -> bool: ...
    def act(self, action: dict) -> None: ...
    def status(self) -> bool: ...
    def stop(self) -> None: ...


def plan_concurrency(levels: Sequence[int]) -> tuple[int, ...]:
    """Preserve caller order (including repeats); zero is a valid baseline."""
    result = tuple(levels)
    if not result or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in result):
        raise ValueError("player levels must be a nonempty sequence of nonnegative integers")
    return result


def scenario_actions(player_index: int, step: int) -> tuple[dict, ...]:
    """Repeat five control steps: four walking, one jumping in place.

    Each cycle turns 90 degrees, producing a bounded square on flat terrain.
    Players use four deterministic initial headings; no random state is used.
    """
    if player_index < 0 or step < 0:
        raise ValueError("player index and scenario step must be nonnegative")
    jumping = step % 5 == 4
    yaw = ((player_index + step // 5) % 4) * math.pi / 2
    return (
        {"action": "look", "yaw": yaw, "pitch": 0.0},
        {"action": "move", "forward": not jumping, "sprint": False},
        {"action": "jump", "active": jumping},
    )


@dataclass(frozen=True)
class PlayerCapacityConfig:
    levels: tuple[int, ...] = (1, 5, 10, 20)
    host: str = "127.0.0.1"
    port: int = 25565
    username_prefix: str = "Bench"
    warmup_seconds: float = 30.0
    measure_seconds: float = 60.0
    sample_interval: float = 5.0
    action_interval: float = 1.0
    connect_timeout: float = 30.0

    def __post_init__(self):
        plan_concurrency(self.levels)
        for name in ("warmup_seconds", "measure_seconds", "sample_interval", "action_interval", "connect_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or (value < 0 if name == "warmup_seconds" else value <= 0):
                raise ValueError(f"invalid {name}: {value}")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        largest = max(self.levels, default=0)
        username = self.username_prefix + str(max(0, largest - 1))
        if not username.isascii() or not all(c.isalnum() or c == "_" for c in username) or len(username) > 16:
            raise ValueError("generated usernames must be 1–16 ASCII letters, digits, or underscores")


class CommandPlayerClient:
    """Subprocess JSONL bridge; process startup never counts as a joined player."""

    def __init__(self, spec: PlayerSpec, command_template: str):
        tokens = shlex.split(command_template)
        if not tokens:
            raise ValueError("adapter command is empty; configure a real player client")
        values = dict(host=spec.host, port=spec.port, username=spec.username, index=spec.index)
        self.argv = [token.format(**values) for token in tokens]
        self.spec = spec
        self.process = None
        self.reader = None
        self.ready = threading.Event()
        self.connected = False
        self.error = ""

    def start(self):
        if self.process is not None:
            raise RuntimeError("client already started")
        self.process = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.reader = threading.Thread(target=self._read_events, daemon=True)
        self.reader.start()

    def _read_events(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind == "connected":
                    self.connected = True
                    self.ready.set()
                elif kind in ("disconnected", "error"):
                    self.error = str(event.get("message") or kind)
                    self.connected = False
                    self.ready.set()
                    return
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        finally:
            self.connected = False
            self.ready.set()

    def _send(self, event):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError(self.error or "adapter process is not running")
        self.process.stdin.write(json.dumps(event, sort_keys=True) + "\n")
        self.process.stdin.flush()

    def connect(self, timeout):
        self._send(dict(event="connect", host=self.spec.host, port=self.spec.port, username=self.spec.username))
        if not self.ready.wait(timeout):
            self.error = "connection acknowledgement timed out"
        return self.status()

    def act(self, action):
        if not self.status():
            raise RuntimeError(self.error or "client disconnected")
        self._send(dict(action, event="action"))

    def status(self):
        return self.connected and self.process is not None and self.process.poll() is None

    def stop(self):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    self._send({"event": "stop"})
                except (OSError, RuntimeError):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        finally:
            self.connected = False
            if process.stdin:
                process.stdin.close()
            if self.reader:
                self.reader.join(timeout=2)
            if process.stdout:
                process.stdout.close()


def command_adapter(command_template: str) -> Callable[[PlayerSpec], PlayerClient]:
    if not command_template or not command_template.strip():
        raise ValueError("configure --adapter-command or inject a real client adapter_factory")
    return lambda spec: CommandPlayerClient(spec, command_template)


CSV_FIELDS = (
    "level_index", "player_count", "elapsed_seconds", "connected_clients", "failed_clients",
    "mspt_p50", "mspt_p95", "mspt_max", "tps", "steal_pct", "metric_source", "client_errors",
)


def run_player_capacity(
    rc, config: PlayerCapacityConfig | None = None, *,
    adapter_factory: Callable[[PlayerSpec], PlayerClient] | None = None,
    output_csv: str | Path | None = None,
    clock=time.monotonic, sleep=time.sleep,
    metric_sampler=sample_mspt, steal_reader=read_cpu_steal,
) -> list[dict]:
    """Run independent concurrency levels, disconnecting every client afterward.

    Connection handshakes execute concurrently with one shared timeout window.
    Adapters must honor their connect timeout. Failed/disconnected clients are
    not replaced. Rows retain the requested load and actual connected/failed
    counts, so partial joins cannot masquerade as successful capacity tests.
    Metric/runner errors propagate after cleanup; individual client errors are
    recorded. ``None`` metrics become empty CSV cells (never fabricated zeros).
    """
    if adapter_factory is None:
        raise ValueError("no real player adapter configured; supply adapter_factory or --adapter-command")
    cfg = config or PlayerCapacityConfig()
    rows = []
    metric_state = {"spark": True}
    output = None
    writer = None
    if output_csv is not None:
        path = Path(output_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        output = path.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
    try:
        for level_index, count in enumerate(plan_concurrency(cfg.levels)):
            clients = {}
            failures = {}
            try:
                for index in range(count):
                    try:
                        spec = PlayerSpec(index, f"{cfg.username_prefix}{index}", cfg.host, cfg.port)
                        client = adapter_factory(spec)
                        clients[index] = client
                        client.start()
                    except Exception as exc:
                        failures[index] = str(exc)

                def connect_one(index, client):
                    try:
                        if not client.connect(cfg.connect_timeout):
                            failures[index] = str(getattr(client, "error", "") or "connection failed")
                    except Exception as exc:
                        failures[index] = str(exc)

                threads = [threading.Thread(target=connect_one, args=(i, client))
                           for i, client in clients.items() if i not in failures]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                started = clock()
                measuring = started + cfg.warmup_seconds
                end = measuring + cfg.measure_seconds
                next_action = started
                next_sample = min(measuring + cfg.sample_interval, end)
                step = 0
                last_steal = None
                while True:
                    now = clock()
                    if now >= measuring and last_steal is None:
                        last_steal = steal_reader()
                    if now >= next_action and now < end:
                        for index, client in clients.items():
                            if index in failures:
                                continue
                            try:
                                if not client.status():
                                    raise RuntimeError(getattr(client, "error", "") or "client disconnected")
                                for action in scenario_actions(index, step):
                                    client.act(action)
                            except Exception as exc:
                                failures[index] = str(exc)
                        step += 1
                        next_action += cfg.action_interval
                        if next_action <= now:
                            next_action = now + cfg.action_interval
                    if now >= next_sample:
                        metrics = metric_sampler(rc, metric_state)
                        for index, client in clients.items():
                            if index not in failures:
                                try:
                                    if not client.status():
                                        raise RuntimeError(getattr(client, "error", "") or "client disconnected")
                                except Exception as exc:
                                    failures[index] = str(exc)
                        current_steal = steal_reader()
                        steal = None
                        if current_steal is not None and last_steal is not None:
                            total = current_steal[0] - last_steal[0]
                            stolen = current_steal[1] - last_steal[1]
                            if total > 0 and 0 <= stolen <= total:
                                steal = round(100 * stolen / total, 3)
                        last_steal = current_steal
                        row = dict(
                            level_index=level_index, player_count=count,
                            elapsed_seconds=round(now - measuring, 6),
                            connected_clients=count - len(failures), failed_clients=len(failures),
                            mspt_p50=metrics.get("p50"), mspt_p95=metrics.get("p95"),
                            mspt_max=metrics.get("mx"), tps=metrics.get("tps"),
                            steal_pct=steal, metric_source=metrics.get("source"),
                            client_errors=json.dumps({f"{cfg.username_prefix}{i}": failures[i]
                                                      for i in sorted(failures)}, sort_keys=True),
                        )
                        rows.append(row)
                        if writer:
                            writer.writerow(row)
                            output.flush()
                        if next_sample >= end:
                            break
                        next_sample = min(next_sample + cfg.sample_interval, end)
                    sleep(max(0.0, min(next_action if next_action < end else end, next_sample,
                                       measuring if now < measuring else end) - clock()))
            finally:
                stop_errors = []
                for index, client in clients.items():
                    try:
                        client.stop()
                    except Exception as exc:
                        stop_errors.append(f"{cfg.username_prefix}{index}: {exc}")
                if stop_errors:
                    raise RuntimeError("failed to stop client(s): " + "; ".join(stop_errors))
    finally:
        if output:
            output.close()
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adapter-command", help="one-client executable template: {host} {port} {username} {index}; JSONL protocol above")
    parser.add_argument("--host", default="127.0.0.1", help="Minecraft game host")
    parser.add_argument("--port", type=int, default=25565, help="Minecraft game port, not RCON")
    parser.add_argument("--rcon-host", help="defaults to --host")
    parser.add_argument("--rcon-port", type=int, default=25575)
    parser.add_argument("--rcon-pass", default=os.getenv("RCON_PASS"), help="or set RCON_PASS")
    parser.add_argument("--levels", default="1,5,10,20", help="comma-separated player counts; 0 permits an empty baseline")
    parser.add_argument("--username-prefix", default="Bench")
    parser.add_argument("--warmup", type=float, default=30.0, help="warmup seconds per level")
    parser.add_argument("--measure", type=float, default=60.0, help="measurement seconds per level")
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--action-interval", type=float, default=1.0)
    parser.add_argument("--connect-timeout", type=float, default=30.0)
    parser.add_argument("--output", default="results/player-capacity.csv")
    args = parser.parse_args(argv)
    try:
        factory = command_adapter(args.adapter_command)
        cfg = PlayerCapacityConfig(
            levels=tuple(int(value) for value in args.levels.split(",")),
            host=args.host, port=args.port, username_prefix=args.username_prefix,
            warmup_seconds=args.warmup, measure_seconds=args.measure,
            sample_interval=args.sample_interval, action_interval=args.action_interval,
            connect_timeout=args.connect_timeout,
        )
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if not args.rcon_pass:
        parser.error("--rcon-pass or RCON_PASS is required")
    rc = Rcon(args.rcon_host or args.host, args.rcon_port, args.rcon_pass)
    try:
        rows = run_player_capacity(rc, cfg, adapter_factory=factory, output_csv=args.output)
    finally:
        if rc.sock is not None:
            rc.sock.close()
    print(f"Wrote {len(rows)} player-capacity samples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
