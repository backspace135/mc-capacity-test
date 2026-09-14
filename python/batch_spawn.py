#!/usr/bin/env python3
"""Batch entity spawning benchmark: compare summon round trips and real datapack functions.

``summon`` sends each command separately; ``chunked`` groups the same commands
with an optional inter-batch pause (it does NOT claim fewer RCON round trips).
``function`` installs temporary .mcfunction files in an explicitly supplied
world datapacks directory, then executes one function per batch. Installation,
reload, accounting and cleanup are outside generation timing. The benchmark
assumes a loaded, safe spawn area; it does not change world gamerules or terrain.
Actual counts are observed tagged, loaded entities, never inferred from requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import time
import uuid
from dataclasses import dataclass

from capacity_gradient import PRESETS
from rcon_client import Rcon, strip_colors

STRATEGIES = ("summon", "chunked", "function")
_RESOURCE = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+\Z")
_TAG = re.compile(r"[A-Za-z0-9_.+-]+\Z")
_FAILURE = re.compile(r"\b(unable|invalid|unknown|expected|failed|exception|error)\b|not found|does not exist", re.I)


@dataclass(frozen=True)
class BatchSpawnConfig:
    count: int = 1000
    repeats: int = 3
    strategies: tuple[str, ...] = ("summon", "chunked")
    batch_size: int = 100
    preset: str = "zombie"
    entity: str | None = None
    nbt: str | None = None
    origin: tuple[float, float, float] = (0, 101, 0)
    grid_width: int = 16
    spacing: float = 1.0
    batch_pause: float = 0.0
    keep_entities: bool = False
    datapack_dir: str | None = None
    pack_format: int | None = None
    function_directory: str = "function"

    def validate(self):
        if self.count < 1 or self.repeats < 1 or self.batch_size < 1 or self.grid_width < 1:
            raise ValueError("count, repeats, batch_size and grid_width must be positive")
        if not self.strategies or any(s not in STRATEGIES for s in self.strategies):
            raise ValueError(f"strategies must be selected from {STRATEGIES}")
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("duplicate strategies would make repeat summaries ambiguous")
        if self.preset not in PRESETS:
            raise ValueError("unknown entity preset")
        if len(self.origin) != 3 or not all(math.isfinite(v) for v in self.origin):
            raise ValueError("origin must contain three finite coordinates")
        if not math.isfinite(self.spacing) or self.spacing < 0:
            raise ValueError("spacing must be finite and nonnegative")
        if not math.isfinite(self.batch_pause) or self.batch_pause < 0:
            raise ValueError("batch_pause must be finite and nonnegative")
        if self.function_directory not in ("function", "functions"):
            raise ValueError("function_directory must be function (1.21+) or functions (older)")
        if "function" in self.strategies:
            if not self.datapack_dir or self.pack_format is None or self.pack_format < 1:
                raise ValueError("function strategy requires datapack_dir and a positive server-specific pack_format")
            if not Path(self.datapack_dir).is_dir():
                raise ValueError("datapack_dir must be an existing server world's datapacks directory")
        build_summon_commands(self, "batch_spawn_validation", count=1)


def batch_commands(commands, batch_size):
    """Return ordered immutable command groups, including the final partial group."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    commands = tuple(commands)
    return tuple(commands[i:i + batch_size] for i in range(0, len(commands), batch_size))


def _tagged_nbt(nbt, tag):
    # Keeping ownership fields reserved makes cleanup reliable, including errors.
    # Passengers would create untagged children; UUID could target an existing entity.
    nbt = nbt.strip()
    if not nbt.startswith("{") or not nbt.endswith("}") or any(c in nbt for c in "\r\n\0"):
        raise ValueError("NBT must be a single-line SNBT compound")
    if re.search(r'(?:[,{]\s*)(?:[\"\']?)(Tags|Passengers|UUID)(?:[\"\']?)\s*:', nbt):
        raise ValueError("NBT must omit reserved Tags, Passengers and UUID fields")
    body = nbt[1:-1].strip()
    return "{" + body + ("," if body else "") + 'Tags:[' + json.dumps(tag) + "]}"


def build_summon_commands(config, tag, *, count=None):
    """Build reproducible grid-positioned commands; only the caller's tag varies."""
    if not _TAG.fullmatch(tag):
        raise ValueError("invalid benchmark tag")
    preset = PRESETS[config.preset]
    entity = config.entity or preset["etype"]
    if not _RESOURCE.fullmatch(entity):
        raise ValueError("entity must be a namespaced Minecraft resource identifier")
    nbt = _tagged_nbt(config.nbt if config.nbt is not None else preset["nbt"], tag)
    x, y, z = config.origin
    return tuple(
        f"summon {entity} {x + (i % config.grid_width) * config.spacing:g} {y:g} "
        f"{z + (i // config.grid_width) * config.spacing:g} {nbt}"
        for i in range(config.count if count is None else count)
    )


def parse_entity_count(response):
    """Parse vanilla execute-if feedback, preserving unknown/localized results."""
    response = strip_colors(response).strip()
    match = re.search(r"\bcount:\s*(\d+)\b", response, re.I)
    if match:
        return int(match.group(1))
    if response.lower() in ("test failed", "test failed."):
        return 0
    return None


def summarize_results(records):
    """Aggregate complete measured runs only; retain failed/unknown run counts."""
    summaries = []
    for strategy in dict.fromkeys(row["strategy"] for row in records):
        group = [row for row in records if row["strategy"] == strategy]
        successful = [row for row in group if row["status"] == "ok"]
        elapsed = sum(row["generation_seconds"] for row in successful)
        summaries.append({
            "strategy": strategy,
            "runs": len(group),
            "successful_runs": len(successful),
            "failed_runs": len(group) - len(successful),
            "unknown_count_runs": sum(row["actual_count"] is None for row in group),
            "median_generation_seconds": statistics.median(row["generation_seconds"] for row in successful) if successful else None,
            "actual_entities_per_second": sum(row["actual_count"] for row in successful) / elapsed if elapsed > 0 else None,
            "generation_commands_sent": sum(row["commands_sent"] for row in group),
        })
    return summaries


def run_batch_spawn(rc, config, *, clock=time.perf_counter, sleep=time.sleep):
    """Run against ``rc.cmd(str)->str`` and return flat, JSON-compatible records.

    ``commands_sent`` counts attempted generation API calls, not transport-level
    retries; adapters should not retry non-idempotent summons. Function failures
    that the server does not surface individually are detected by count deficits.
    Functions must finish synchronously (no schedule command is generated).
    """
    config.validate()
    records = []
    run_id = uuid.uuid4().hex
    for strategy in config.strategies:
        for repeat in range(1, config.repeats + 1):
            tag = f"batch_spawn_{run_id}_{strategy}_{repeat}"
            commands = build_summon_commands(config, tag)
            groups = batch_commands(commands, 1 if strategy == "summon" else config.batch_size)
            row = dict(strategy=strategy, repeat=repeat, tag=tag, preset=config.preset,
                       entity=config.entity or PRESETS[config.preset]["etype"],
                       nbt=config.nbt if config.nbt is not None else PRESETS[config.preset]["nbt"],
                       batch_size=1 if strategy == "summon" else config.batch_size,
                       batch_pause=config.batch_pause if strategy != "summon" else 0,
                       requested_count=config.count, actual_count=None, missing_count=None,
                       generation_seconds=0.0, commands_sent=0, total_commands_sent=0,
                       batches_attempted=0, failed_commands=0, actual_entities_per_second=None,
                       requested_entities_per_second=None, count_response="", cleanup_response="",
                       cleanup_count=None, kept_entities=config.keep_entities, errors=[], status="error")
            pack = None
            generation_started = False

            def send(command, generation=False):
                row["total_commands_sent"] += 1
                if generation:
                    row["commands_sent"] += 1
                try:
                    response = strip_colors(rc.cmd(command))
                except Exception:
                    if generation:
                        row["failed_commands"] += 1
                    raise
                if generation and _FAILURE.search(response):
                    row["failed_commands"] += 1
                    row["errors"].append(f"generation response: {response[:500]}")
                return response

            try:
                if strategy == "function":
                    # Exclusive mkdir prevents touching any pre-existing datapack.
                    candidate = Path(config.datapack_dir) / tag
                    candidate.mkdir()
                    pack = candidate
                    (pack / "pack.mcmeta").write_text(json.dumps({"pack": {
                        "pack_format": config.pack_format, "description": "Temporary batch-spawn benchmark"
                    }}), encoding="utf-8")
                    directory = pack / "data" / tag / config.function_directory
                    directory.mkdir(parents=True)
                    for index, group in enumerate(groups):
                        (directory / f"batch_{index}.mcfunction").write_text("\n".join(group) + "\n", encoding="utf-8")
                    response = send("reload")
                    if _FAILURE.search(response):
                        raise RuntimeError(f"datapack reload failed: {response}")
                generation_started = True
                start = clock()
                try:
                    for index, group in enumerate(groups):
                        row["batches_attempted"] += 1
                        if strategy == "function":
                            send(f"function {tag}:batch_{index}", generation=True)
                        else:
                            for command in group:
                                send(command, generation=True)
                        if strategy != "summon" and index + 1 < len(groups) and config.batch_pause:
                            sleep(config.batch_pause)
                finally:
                    row["generation_seconds"] = max(0.0, clock() - start)
            except Exception as exc:
                row["errors"].append(f"generation/setup: {exc}")
            finally:
                if generation_started:
                    try:
                        row["count_response"] = send(f"execute if entity @e[tag={tag}]")
                        row["actual_count"] = parse_entity_count(row["count_response"])
                        if row["actual_count"] is None:
                            row["errors"].append("actual entity count could not be parsed")
                        else:
                            row["missing_count"] = max(0, config.count - row["actual_count"])
                            if row["actual_count"] != config.count:
                                row["errors"].append("actual entity count differs from requested count")
                    except Exception as exc:
                        row["errors"].append(f"count: {exc}")
                    if not config.keep_entities:
                        try:
                            row["cleanup_response"] = send(f"kill @e[tag={tag}]")
                            response = send(f"execute if entity @e[tag={tag}]")
                            row["cleanup_count"] = parse_entity_count(response)
                            if row["cleanup_count"] != 0:
                                row["errors"].append(f"cleanup not confirmed empty: {response[:500]}")
                        except Exception as exc:
                            row["errors"].append(f"cleanup: {exc}")
                if pack is not None and pack.exists():
                    try:
                        shutil.rmtree(pack)
                        response = send("reload")
                        if _FAILURE.search(response):
                            row["errors"].append(f"cleanup reload: {response[:500]}")
                    except Exception as exc:
                        row["errors"].append(f"datapack cleanup: {exc}")
            elapsed = row["generation_seconds"]
            if elapsed > 0 and generation_started:
                row["requested_entities_per_second"] = config.count / elapsed
                if row["actual_count"] is not None:
                    row["actual_entities_per_second"] = row["actual_count"] / elapsed
            row["status"] = "ok" if not row["errors"] else "error"
            records.append(row)
    return records


def write_results(records, outdir):
    """Write deterministic field order and JSON nulls for unobservable counts."""
    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "batch_spawn.csv"
    json_path = directory / "batch_spawn.json"
    if records:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            for record in records:
                writer.writerow({**record, "errors": json.dumps(record["errors"], ensure_ascii=False)})
    else:
        csv_path.write_text("", encoding="utf-8")
    json_path.write_text(json.dumps({"records": records, "summary": summarize_results(records)},
                                    indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return {"csv_path": str(csv_path), "json_path": str(json_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default=None, help="defaults to RCON_PASSWORD")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--strategies", nargs="+", choices=STRATEGIES, default=["summon", "chunked"])
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--batch-pause", type=float, default=0, help="seconds between chunked/function batches; included in timing")
    parser.add_argument("--preset", choices=PRESETS, default="zombie")
    parser.add_argument("--entity", help="override preset entity, e.g. minecraft:pig")
    parser.add_argument("--nbt", help="override preset SNBT; omit ownership fields Tags, Passengers, UUID")
    parser.add_argument("--origin", type=float, nargs=3, default=(0, 101, 0), metavar=("X", "Y", "Z"))
    parser.add_argument("--grid-width", type=int, default=16)
    parser.add_argument("--spacing", type=float, default=1)
    parser.add_argument("--keep-entities", action="store_true")
    parser.add_argument("--datapack-dir", help="existing server world/datapacks directory; required by function strategy")
    parser.add_argument("--pack-format", type=int, help="datapack format for the target server; required by function strategy")
    parser.add_argument("--function-directory", choices=("function", "functions"), default="function", help="function for 1.21+; functions for older versions")
    parser.add_argument("--outdir", default="results/batch_spawn")
    args = parser.parse_args(argv)
    config = BatchSpawnConfig(**{name: getattr(args, name) for name in BatchSpawnConfig.__dataclass_fields__})
    try:
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    password = args.password or os.environ.get("RCON_PASSWORD")
    if not password:
        parser.error("provide --password or RCON_PASSWORD")
    # Disable shared client's retries: summon/function calls are non-idempotent.
    client = Rcon(args.host, args.port, password)

    class SingleAttemptClient:
        def cmd(self, command):
            return client.cmd(command, retries=1)

    try:
        records = run_batch_spawn(SingleAttemptClient(), config)
    finally:
        if client.sock is not None:
            client.sock.close()
    paths = write_results(records, args.outdir)
    print(json.dumps({**paths, "summary": summarize_results(records)}, indent=2, allow_nan=False))
    return 1 if any(row["status"] != "ok" for row in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
