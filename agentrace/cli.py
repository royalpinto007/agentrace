"""agentrace CLI.

    agentrace list                 what did my subagents do
    agentrace check               flag the suspicious results
    agentrace show <id>           read one run in full
    agentrace stats               where the time went
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .checks import analyse
from .parse import AgentRun, parse_all, parse_session

console = Console()

_SEV_COLOUR = {"high": "red", "medium": "yellow", "low": "dim"}


def _load(args) -> list[AgentRun]:
    if args.file:
        sessions = [parse_session(Path(args.file))]
    else:
        sessions = parse_all(Path(args.dir) if args.dir else None)
    runs = [r for s in sessions for r in s.runs]
    if not runs:
        console.print("[yellow]No subagent runs found.[/] Looked in ~/.claude/projects unless --dir was given.")
    return runs


def cmd_list(args) -> int:
    runs = _load(args)
    if not runs:
        return 0
    table = Table(title=f"{len(runs)} subagent runs", show_lines=False)
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("description")
    table.add_column("dur", justify="right")
    table.add_column("prompt", justify="right")
    table.add_column("result", justify="right")
    table.add_column("bg", justify="center")
    for r in runs:
        d = f"{r.duration_s:.0f}s" if r.duration_s is not None else "-"
        table.add_row(
            r.tool_use_id[-8:],
            (r.description or "(none)")[:44],
            d,
            f"{r.prompt_chars:,}",
            f"{r.result_chars:,}",
            "y" if r.background else "",
        )
    console.print(table)
    return 0


def cmd_check(args) -> int:
    runs = _load(args)
    if not runs:
        return 0

    flagged = 0
    total_findings = 0
    for r in runs:
        findings = analyse(r)
        if args.severity:
            findings = [f for f in findings if f.severity == args.severity]
        if not findings:
            continue
        flagged += 1
        total_findings += len(findings)
        console.print(f"\n[bold]{r.description or '(no description)'}[/] [dim]{r.tool_use_id[-8:]}[/]")
        for f in findings:
            colour = _SEV_COLOUR.get(f.severity, "white")
            console.print(f"  [{colour}]{f.severity:<6}[/] [bold]{f.check}[/]  {f.message}")
            if f.evidence:
                console.print(f"         [dim]{f.evidence}[/]")

    console.print(
        f"\n[bold]{flagged}/{len(runs)}[/] runs flagged, {total_findings} findings. "
        "[dim]These are hints, not verdicts: go read the run.[/]"
    )
    # Exit non-zero only on high severity, so this is usable in CI without being a nuisance.
    has_high = any(f.severity == "high" for r in runs for f in analyse(r))
    return 1 if (has_high and args.strict) else 0


def cmd_show(args) -> int:
    runs = _load(args)
    match = [r for r in runs if r.tool_use_id.endswith(args.id)]
    if not match:
        console.print(f"[red]No run matching {args.id!r}[/]")
        return 1
    r = match[0]
    console.print(f"[bold]{r.description}[/]  [dim]{r.tool_use_id}[/]")
    console.print(f"[dim]duration: {r.duration_s}s | background: {r.background}[/]\n")
    console.print("[bold cyan]PROMPT[/]")
    console.print(r.prompt[: args.max] or "[dim](empty)[/]")
    console.print("\n[bold cyan]RESULT[/]")
    console.print(r.result[: args.max] or "[dim](empty)[/]")
    findings = analyse(r)
    if findings:
        console.print("\n[bold cyan]FINDINGS[/]")
        for f in findings:
            colour = _SEV_COLOUR.get(f.severity, "white")
            console.print(f"  [{colour}]{f.severity:<6}[/] {f.check}: {f.message}")
    return 0


def cmd_stats(args) -> int:
    runs = _load(args)
    if not runs:
        return 0
    durations = [r.duration_s for r in runs if r.duration_s is not None]
    total_s = sum(durations)
    prompt_chars = sum(r.prompt_chars for r in runs)
    result_chars = sum(r.result_chars for r in runs)
    empty = sum(1 for r in runs if r.result_chars == 0)
    errors = sum(1 for r in runs if r.is_error)
    bg = sum(1 for r in runs if r.background)

    table = Table(show_header=False, box=None)
    table.add_row("subagent runs", f"{len(runs):,}")
    table.add_row("background", f"{bg:,}")
    table.add_row("errored", f"{errors:,}")
    table.add_row("empty results", f"{empty:,}")
    if durations:
        table.add_row("total agent time", f"{total_s/3600:.1f} h")
        table.add_row("median run", f"{sorted(durations)[len(durations)//2]:.0f} s")
        table.add_row("slowest run", f"{max(durations)/60:.1f} min")
    table.add_row("prompt chars written", f"{prompt_chars:,}")
    table.add_row("result chars returned", f"{result_chars:,}")
    console.print(table)

    if args.json:
        print(
            json.dumps(
                {
                    "runs": len(runs),
                    "background": bg,
                    "errored": errors,
                    "empty": empty,
                    "total_seconds": total_s,
                    "prompt_chars": prompt_chars,
                    "result_chars": result_chars,
                }
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentrace", description=__doc__.split("\n")[0])
    p.add_argument("--dir", help="transcript root (default ~/.claude/projects)")
    p.add_argument("--file", help="a single .jsonl transcript")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list subagent runs").set_defaults(func=cmd_list)

    c = sub.add_parser("check", help="flag suspicious results")
    c.add_argument("--severity", choices=["high", "medium", "low"], help="only this severity")
    c.add_argument("--strict", action="store_true", help="exit 1 if any high severity finding")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("show", help="read one run in full")
    s.add_argument("id", help="tool_use_id or its last 8 chars")
    s.add_argument("--max", type=int, default=4000, help="truncate long text")
    s.set_defaults(func=cmd_show)

    st = sub.add_parser("stats", help="aggregate stats")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
