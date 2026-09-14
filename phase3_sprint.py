"""Phase 3: ABAB fixed-length tick-sprint experiment.

The server lifecycle is deliberately injected: this module does not assume Docker,
Java, or a particular process manager.  ``RconSprintController`` supplies the
Minecraft command transport while callers provide ``restart`` for condition A.
"""
from __future__ import annotations

import itertools
import re
import socket
import struct
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
import argparse
import json
import os


class RconClient:
    """Minimal Source RCON client suitable for Minecraft's enable-rcon."""
    def __init__(self, host: str, port: int, password: str, timeout: float = 15.0):
        self.address, self.password, self.timeout = (host, int(port)), password, timeout
        self.sock: Any = None
        self._ids = itertools.count(1)

    def _receive(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            data += chunk
        return data

    def _send(self, kind: int, payload: str) -> str:
        request_id = next(self._ids)
        body = struct.pack("<ii", request_id, kind) + payload.encode() + b"\0\0"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        size = struct.unpack("<i", self._receive(4))[0]
        response = self._receive(size)
        response_id, _ = struct.unpack("<ii", response[:8])
        if response_id == -1:
            raise RuntimeError("RCON authentication failed")
        return response[8:-2].decode("utf-8", "replace")

    def connect(self) -> None:
        self.sock = socket.create_connection(self.address, self.timeout)
        self.sock.settimeout(self.timeout)
        self._send(3, self.password)

    def cmd(self, command: str) -> str:
        if self.sock is None:
            self.connect()
        try:
            return self._send(2, command.lstrip("/"))
        except (OSError, ConnectionError):
            if self.sock:
                self.sock.close()
            self.sock = None
            raise

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None



class CommandClient(Protocol):
    def cmd(self, command: str) -> str: ...


@dataclass(frozen=True)
class SprintConfig:
    ticks: int = 100_000
    repeats: int = 5
    warmup: int = 2
    discard: int = 0
    command: str = "/tick sprint {ticks}"


@dataclass
class SprintResult:
    group: str
    repeat: int
    warmup: bool
    ticks: int
    elapsed_ms: Optional[float] = None
    ms_per_tick: Optional[float] = None
    raw_response: str = ""
    error: Optional[str] = None


@dataclass
class RconSprintController:
    """Issue sprint commands through an existing RCON-like client.

    The client must expose ``cmd(str) -> str``.  Minecraft versions/plugins
    differ in response wording, so ``parser`` may be supplied; without one the
    elapsed wall-clock duration is used (and is always reported in ms/tick).
    """
    client: CommandClient
    parser: Optional[Callable[[str], Optional[float]]] = None

    def sprint(self, ticks: int, command: str = "/tick sprint {ticks}") -> tuple[float, str]:
        rendered = command.format(ticks=ticks)
        started = time.monotonic_ns()
        response = self.client.cmd(rendered)
        wall_ms = (time.monotonic_ns() - started) / 1_000_000.0
        measured = self.parser(response) if self.parser else None
        return (measured if measured is not None else wall_ms), response


_DURATION_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*ms(?:\s*/\s*tick)?")


def parse_sprint_duration(response: str) -> Optional[float]:
    """Extract a reported duration in milliseconds, or ``None`` if unavailable."""
    match = _DURATION_RE.search(response or "")
    return float(match.group(1)) if match else None


def _one(controller: Any, group: str, repeat: int, warmup: bool, cfg: SprintConfig) -> SprintResult:
    try:
        elapsed, raw = controller.sprint(cfg.ticks, cfg.command)
        if elapsed < 0:
            raise ValueError("negative sprint duration")
        return SprintResult(group, repeat, warmup, cfg.ticks, elapsed, elapsed / cfg.ticks, raw)
    except Exception as exc:  # command availability must be reported, never fabricated
        return SprintResult(group, repeat, warmup, cfg.ticks, raw_response="", error=f"{type(exc).__name__}: {exc}")


def run_abab_experiment(
    controller: Any,
    restart: Optional[Callable[[], Any]] = None,
    config: SprintConfig = SprintConfig(),
) -> list[SprintResult]:
    """Run warmups then A/B alternating measurements.

    A's restart callback is invoked immediately before every A sprint.  Warmups
    are returned and marked ``warmup`` so callers can audit/discard them; only
    ``repeats`` measured runs per group are scheduled. ``discard`` additionally
    marks the first N runs in each group as warmup (useful for post-restart JIT).
    """
    if config.ticks <= 0 or config.repeats <= 0 or config.warmup < 0 or config.discard < 0:
        raise ValueError("ticks and repeats must be positive; warmup/discard non-negative")
    if restart is None:
        restart = lambda: None
    results: list[SprintResult] = []
    # Warm up both conditions in AB order, retaining explicit records.
    for group in ("A", "B"):
        if group == "A":
            try:
                restart()
            except Exception as exc:
                results.append(SprintResult(group, 0, True, config.ticks, error=f"restart: {exc}"))
                continue
        for n in range(config.warmup):
            results.append(_one(controller, group, n + 1, True, config))
    for n in range(1, config.repeats + 1):
        for group in ("A", "B"):
            if group == "A":
                try:
                    restart()
                except Exception as exc:
                    results.append(SprintResult(group, n, n <= config.discard, config.ticks, error=f"restart: {exc}"))
                    continue
            results.append(_one(controller, group, n, n <= config.discard, config))
    return results


def results_as_dicts(results: Sequence[SprintResult]) -> list[dict[str, Any]]:
    return [asdict(item) for item in results]


__all__ = ["CommandClient", "RconClient", "SprintConfig", "SprintResult", "RconSprintController", "parse_sprint_duration", "run_abab_experiment", "results_as_dicts"]

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default=None)
    parser.add_argument("--ticks", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=2)
    args = parser.parse_args(argv)
    password = args.password or os.environ.get("RCON_PASSWORD")
    if not password:
        parser.error("RCON password required via --password or RCON_PASSWORD")
    client = RconClient(args.host, args.port, password)
    client.connect()
    try:
        results = run_abab_experiment(client, config=SprintConfig(ticks=args.ticks, repeats=args.repeats, warmup=args.warmup))
        print(json.dumps(results_as_dicts(results), ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


