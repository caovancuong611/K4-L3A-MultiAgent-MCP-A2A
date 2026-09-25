from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient_error(item) for item in exc.exceptions)
    if isinstance(exc, ValueError):
        return False
    if isinstance(exc, (ConnectionError, OSError, RuntimeError, TimeoutError)):
        return True
    if type(exc).__name__ == "MCPError":
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "connection closed",
                "sse stream ended",
                "all connection attempts failed",
                "timed out",
            )
        )
    return type(exc).__name__ in {
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "WriteError",
        "WriteTimeout",
    }


def _completed_cases(
    output_root: Path, trace_path: Path, contracts: Contracts
) -> set[str]:
    finalized: set[str] = set()
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            contracts.validate_trace(event, "resume trace")
            if event.get("event_type") == "case_finalized":
                finalized.add(str(event.get("case_id")))

    completed: set[str] = set()
    for path in output_root.glob("*.json"):
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        case_id = value.get("case_id")
        if isinstance(case_id, str) and case_id == path.stem and case_id in finalized:
            contracts.validate_output(value, f"outputs/{path.name}")
            completed.add(case_id)
    return completed


async def _solve_and_commit_case(
    *,
    root: Path,
    case_id: str,
    case: dict[str, Any],
    gateway: Any,
    contracts: Contracts,
    output_root: Path,
    trace_path: Path,
) -> None:
    temporary_trace = root / "traces" / f".{case_id}.trace.tmp"
    temporary_trace.unlink(missing_ok=True)
    case_trace = TraceWriter(temporary_trace, contracts)
    try:
        case_trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, case_trace)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        case_trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

        target = output_root / f"{case_id}.json"
        temporary_output = target.with_suffix(".json.tmp")
        temporary_output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary_output.replace(target)
        with trace_path.open("a", encoding="utf-8") as destination:
            destination.write(temporary_trace.read_text(encoding="utf-8"))
    finally:
        temporary_trace.unlink(missing_ok=True)


async def _run(root: Path, *, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        completed = _completed_cases(output_root, trace_path, contracts)
    else:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
        completed = set()

    pending = [case_id for case_id in case_set.case_ids if case_id not in completed]
    if not pending:
        print(f"OK: all {len(case_set.case_ids)} cases already completed")
        return

    index = 0
    failures = 0
    discovered = False
    batch_size = 5
    max_failures = 3
    while index < len(pending):
        batch_start = index
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not discovered:
                    discovered_tools = await gateway.list_tools()
                    if not discovered_tools:
                        raise RuntimeError("MCP Gateway returned no tools")
                    discovered = True
                batch_end = min(index + batch_size, len(pending))
                while index < batch_end:
                    case_id = pending[index]
                    await _solve_and_commit_case(
                        root=root,
                        case_id=case_id,
                        case=case_set.cases[case_id],
                        gateway=gateway,
                        contracts=contracts,
                        output_root=output_root,
                        trace_path=trace_path,
                    )
                    index += 1
                    failures = 0
                    print(
                        f"OK: {case_id} ({len(completed) + index}/{len(case_set.case_ids)})",
                        flush=True,
                    )
        except Exception as exc:
            if not _is_transient_error(exc):
                raise
            if index > batch_start:
                failures = 0
            failures += 1
            if failures > max_failures:
                current = pending[index] if index < len(pending) else "connection shutdown"
                raise RuntimeError(
                    f"MCP failed repeatedly near {current}; rerun with 'day09 run --resume'"
                ) from exc
            await asyncio.sleep(2 ** (failures - 1))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="continue from validated outputs and finalized traces"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
