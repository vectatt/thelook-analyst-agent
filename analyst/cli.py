"""CLI chat.  Usage: python -m analyst.cli [--user NAME] [--session ID]"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import secrets
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from analyst.agent.models import describe_chain
from analyst.agent.session import Analyst, Turn
from analyst.config import settings
from analyst.golden import candidates

console = Console()

HELP = """\
Ask anything about sales, customers, products or regions — in plain English. Follow-ups work.
Ask for a report and you will be offered the chance to save it; saved reports are also queued for an
analyst to consider adding to the verified library.

Commands
  /reports [text]   list or search saved reports   /show <id>      show one
  /memory           what I've learned about you    /forget <id>    drop one observation
  /candidates       analyses awaiting promotion    /promote <id>   promote one to the golden bucket
  /sql              SQL behind the last answer     /trace [id]     spans of a turn
  /metrics          agent metrics (7 days)         /quality        judge verdicts
  /prompts          live prompt layers + versions  /quit
Deleting: just ask ("delete the reports about Nike"). You will see exactly what goes and type DELETE.
"""


def _badge(turn: Turn) -> str:
    return {"answer": "[green]✓ verified analysis[/]" if turn.verified else "[cyan]analysis[/]",
            "reports": "[yellow]reports[/]", "rejected": "[red]declined[/]",
            "error": "[red]error[/]"}.get(turn.path, turn.path)


def show_turn(turn: Turn) -> None:
    if turn.answer:
        console.print(Panel(Markdown(turn.answer), border_style="green" if turn.verified else "cyan", padding=(1, 2)))
    footer = f"{_badge(turn)} · trace [dim]{turn.trace_id}[/]"
    if turn.sql:
        footer += f" · {len(turn.sql)} quer{'y' if len(turn.sql) == 1 else 'ies'} ([dim]/sql[/])"
    console.print(footer)
    for n in turn.notes:
        console.print(f"[dim]· {n}[/]")
    if turn.awaiting_decision:
        console.print("[dim]· reply to approve it, or say what should change[/]")


def confirm_pending(analyst: Analyst, turn: Turn) -> None:
    """Resolve every pending confirmation (a resumed run can pause again)."""
    result = turn
    while result.pending:
        p = result.pending
        if not p.report_ids:
            console.print("[yellow]Nothing matched — no reports will be deleted.[/]")
            result = analyst.confirm(p, approve=False)
            continue
        t = Table(title=f"⚠  This will delete {len(p.report_ids)} report(s)", title_style="bold yellow")
        t.add_column("id", style="bold"); t.add_column("report")
        for rid in p.report_ids:
            t.add_row(rid, p.titles.get(rid, "?"))
        console.print(t)
        try:
            typed = Prompt.ask("Type [bold]DELETE[/] to confirm, anything else to cancel", default="")
        except (EOFError, KeyboardInterrupt):
            typed = ""
        result = analyst.confirm(p, approve=(typed.strip() == "DELETE"))
        show_turn(result)


def command(a: Analyst, line: str) -> bool:
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    if cmd in ("quit", "exit", "q"):
        return False
    if cmd == "help":
        console.print(HELP)
    elif cmd == "reports":
        rows = a.library.find(a.user_id, text=arg) if arg else a.library.list(a.user_id)
        for r in rows:
            console.print(f"  [{r.id}] [bold]{r.title}[/] — {r.description or r.question[:60]}  [dim]{r.created_date}[/]")
        if not rows:
            console.print("No reports yet. Ask for a report and approve it to save one.")
    elif cmd == "show":
        r = a.library.get(a.user_id, arg)
        console.print(Panel(Markdown(r.body), title=r.title) if r and not r.deleted_at else "No such report.")
    elif cmd == "memory":
        console.print(a.learned.render(a.user_id) or "Nothing learned yet.")
        for e in a.learned.for_user(a.user_id):
            console.print(f"  [dim]{e.id}[/] {e.text}")
    elif cmd == "forget":
        console.print("Forgotten." if arg.isdigit() and a.learned.forget(a.user_id, int(arg)) else "No such entry.")
    elif cmd == "candidates":
        pend = candidates.list_pending()
        for c in pend:
            console.print(f"  {c['id']}  {c['question'][:70]}  [dim]{c['owner']} {c['created_at'][:10]}[/]")
        if not pend:
            console.print("No candidates awaiting promotion.")
    elif cmd == "promote":
        try:
            trio = candidates.approve(arg, a.index)
            console.print(f"Promoted → golden/trios/{trio.id}.yaml (index rebuilt). Edit analyst_notes in the file.")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]{e}[/]")
    elif cmd == "sql":
        last = [t for t in a.tracer.recent(20) if t.get("path") == "answer"]
        spans = a.tracer.load(last[0]["trace_id"]) if last else []
        sqls = [s["sql"] for s in spans if s.get("sql")]
        for i, s in enumerate(sqls, 1):
            console.print(Panel(s, title=f"query {i}", border_style="dim"))
        if not sqls:
            console.print("No SQL yet.")
    elif cmd == "trace":
        tid = arg or (a.tracer.recent(1)[0]["trace_id"] if a.tracer.recent(1) else None)
        spans = a.tracer.load(tid) if tid else []
        if not spans:
            for t in a.tracer.recent(8):
                console.print(f"  {t['trace_id']}  {t['ts'][11:19]}  {str(t.get('path')):8s} {t['duration_ms']:6d} ms  {str(t.get('text'))[:44]}")
        for s in spans:
            extra = {k: v for k, v in s.items() if k not in ("ts", "trace_id", "span", "duration_ms", "error", "sql", "text")}
            err = f"[red]{s['error']}[/]" if s.get("error") else ""
            console.print(f"  {s['span']:22s} {s['duration_ms']:6d} ms {err} [dim]{json.dumps(extra, default=str)[:150]}[/]")
    elif cmd == "metrics":
        console.print(json.dumps(a.tracer.metrics(), indent=2))
    elif cmd == "quality":
        from analyst.judge import summary
        console.print(json.dumps(summary(), indent=2))
    elif cmd == "prompts":
        from analyst.prompts import load
        for name in ("agent", "persona", "report", "conventions", "sql"):
            p = load(name)
            console.print(f"  {name:12s} [dim]{p.version}[/]  {len(p.text):5d} chars  policy={p.policy or '-'}")
        console.print(f"[dim]edit any file in {settings.prompts_dir} — the next message uses it[/]")
    else:
        console.print("Unknown command. /help")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=getpass.getuser())
    ap.add_argument("--session", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

    problems = settings.validate()
    if problems:
        console.print("[red]" + "\n".join(problems) + "[/]"); return 1
    session = args.session or secrets.token_hex(4)
    console.print(Panel(f"[bold]TheLook analyst[/] · user [cyan]{args.user}[/] · session [dim]{session}[/]\n"
                        f"{describe_chain()}", border_style="dim"))
    try:
        analyst = Analyst(user_id=args.user, session_id=session)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Could not start: {type(e).__name__}: {str(e)[:200]}[/]\nRun `bash verify_bq.sh`.")
        return 1
    stale = " [yellow](embeddings unavailable — keyword matching only)[/]" if analyst.index.index_stale else ""
    console.print(f"[dim]{len(analyst.index.trios)} verified analyses indexed{stale} · /help for commands[/]\n")

    while True:
        try:
            line = Prompt.ask("[bold]you[/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(); return 0
        if not line:
            continue
        try:
            if line.startswith("/"):
                if not command(analyst, line):
                    return 0
                continue
            with console.status("[dim]thinking…[/]", spinner="dots"):
                turn = analyst.handle(line)
            show_turn(turn)
            if turn.pending:
                confirm_pending(analyst, turn)
        except KeyboardInterrupt:
            console.print("[dim]· cancelled[/]")
        except Exception as e:  # noqa: BLE001 - the REPL must survive anything
            console.print(f"[red]That failed ({type(e).__name__}: {str(e)[:120]}). The session is still open.[/]")


if __name__ == "__main__":
    sys.exit(main())
