#!/usr/bin/env python3
"""
batch_runner.py

Concurrency Engine for Recover-Net.

Fires all transactions in batch_payload.json at the FastAPI recover endpoint
simultaneously using asyncio + aiohttp — no sequential waiting, no for-loop
bottleneck.

The decision ledger updates in near-real-time as each response arrives.
Final results are written to CSV automatically.

Usage:
    uv run python scripts/batch_runner.py
    uv run python scripts/batch_runner.py --url http://localhost:8000 --concurrency 20
    uv run python scripts/batch_runner.py --report-csv results.csv --report-json results.json
"""

import argparse
import asyncio
import csv
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from dotenv import load_dotenv

load_dotenv()

import aiohttp
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_URL         = "http://localhost:8000"
ENDPOINT            = "/webhook/payment-failure/recover"
DEFAULT_CONCURRENCY = 20          # Bedrock on-demand: ~10,000 RPM — no need to throttle
REQUEST_TIMEOUT     = 60          # seconds per request
BATCH_FILE          = "batch_payload.json"
DEFAULT_CSV_FILE    = "batch_results.csv"
RETRY_ATTEMPTS      = 3           # retry on 429/5xx before giving up
RETRY_BACKOFF       = 2.0         # seconds to wait before each retry
console = Console()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TxResult:
    """Outcome for a single transaction."""
    transaction_id: str
    batch_label: str              # from _batch_label metadata
    http_status: int
    amount: float                 = 0.0
    final_intent: Optional[str]   = None
    final_status: Optional[str]   = None
    llm_intent: Optional[str]     = None
    guardrail_overridden: Optional[bool] = None
    rule_applied: Optional[str]   = None
    error: Optional[str]          = None
    audit_log_id: Optional[str]   = None
    action: Optional[str]         = None

    @property
    def success(self) -> bool:
        return self.http_status == 201

    @property
    def was_escalated(self) -> bool:
        return self.final_intent == "escalate_to_human"

    @property
    def outcome(self) -> str:
        if not self.success:
            return "FAILED"
        return "BLOCKED" if self.was_escalated else "RECOVERED"


@dataclass
class BatchReport:
    """Aggregated stats across all transactions."""
    total: int                        = 0
    succeeded: int                    = 0
    failed: int                       = 0
    elapsed_seconds: float            = 0.0

    total_value_at_risk: float        = 0.0
    value_retried: float              = 0.0
    value_emi: float                  = 0.0
    value_escalated: float            = 0.0

    intent_counts: Dict[str, int]     = field(default_factory=lambda: {})
    guardrail_overrides: int          = 0
    fraud_blocked: int                = 0
    rule_counts: Dict[str, int]       = field(default_factory=lambda: {})
    label_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: {})
    results: List[TxResult]           = field(default_factory=lambda: [])


# ---------------------------------------------------------------------------
# INR formatter
# ---------------------------------------------------------------------------

def _inr(amount: float) -> str:
    """Format as Indian Rupee e.g. ₹1,23,456"""
    s = f"{int(amount):,}"
    parts = s.split(",")
    if len(parts) <= 2:
        return f"₹{s}"
    last = parts[-1]
    rest_joined = ",".join(parts[:-1])
    return f"₹{rest_joined},{last}"


# ---------------------------------------------------------------------------
# Live display builders
# ---------------------------------------------------------------------------

def _build_live_table(results: List[TxResult], total: int) -> Table:
    """Build the live-updating decision ledger table from completed results."""
    n_done      = len(results)
    n_recovered = sum(1 for r in results if r.outcome == "RECOVERED")
    n_blocked   = sum(1 for r in results if r.outcome == "BLOCKED")
    n_failed    = sum(1 for r in results if r.outcome == "FAILED")

    table = Table(
        title=f"[bold cyan]Decision Ledger[/bold cyan]  "
              f"[dim]{n_done}/{total} processed — "
              f"[green]{n_recovered} recovered[/green]  "
              f"[red]{n_blocked} blocked[/red]  "
              f"[yellow]{n_failed} failed[/yellow][/dim]",
        header_style="bold cyan",
        expand=True,
        show_lines=False,
    )
    table.add_column("Outcome",      justify="center", width=11)
    table.add_column("Transaction",  width=14)
    table.add_column("Intent",       width=19)
    table.add_column("Rule / action",width=27)
    table.add_column("Amount",       justify="right", width=10)

    # Most recent results at the top so new arrivals are immediately visible
    for r in reversed(results):
        outcome_style = {"RECOVERED": "green", "BLOCKED": "red", "FAILED": "yellow"}[r.outcome]
        outcome_text  = Text(r.outcome, style=f"bold {outcome_style}")
        rule          = r.rule_applied or r.action or "-"
        table.add_row(
            outcome_text,
            r.transaction_id[:13],
            r.final_intent or ("-" if r.success else r.error or "ERR"),
            rule,
            _inr(r.amount),
        )

    return table


def _build_live_scoreboard(results: List[TxResult], total: int, start: float) -> Table:
    """Compact running stats panel shown beside the progress bar."""
    n_done      = len(results)
    n_recovered = sum(1 for r in results if r.outcome == "RECOVERED")
    n_blocked   = sum(1 for r in results if r.outcome == "BLOCKED")
    n_failed    = sum(1 for r in results if r.outcome == "FAILED")
    val_recovered = sum(r.amount for r in results if r.outcome == "RECOVERED")
    val_total     = sum(r.amount for r in results)
    pct = val_recovered / val_total * 100 if val_total > 0 else 0.0
    elapsed = time.perf_counter() - start

    sb = Table.grid(padding=(0, 2))
    sb.add_column(style="dim", width=18)
    sb.add_column(justify="right")
    sb.add_row("Processed",      f"[bold]{n_done}/{total}[/bold]")
    sb.add_row("Recovered",      f"[bold green]{n_recovered}[/bold green]")
    sb.add_row("Blocked",        f"[bold red]{n_blocked}[/bold red]")
    sb.add_row("Failed",         f"[bold yellow]{n_failed}[/bold yellow]" if n_failed else "0")
    sb.add_row("Revenue secured",f"[bold green]{pct:.1f}%[/bold green]")
    sb.add_row("Val recovered",  f"[green]{_inr(val_recovered)}[/green]")
    sb.add_row("Elapsed",        f"{elapsed:.1f}s")
    return sb


# ---------------------------------------------------------------------------
# Core async machinery
# ---------------------------------------------------------------------------

async def send_webhook(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    url: str,
    payload: Dict[str, Any],
    webhook_secret: Optional[str] = None,
) -> TxResult:
    """
    POST a single transaction to the recover endpoint.
    Retries up to RETRY_ATTEMPTS times on 429/5xx.
    Signs the payload with HMAC-SHA256 if webhook_secret is provided.
    """
    tx_id  = payload.get("transaction_id", "unknown")
    label  = payload.get("_batch_label", "unknown")
    amount = float(payload.get("amount", 0) or 0)

    api_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    raw_bytes   = json.dumps(api_payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        sig = hmac.new(
            webhook_secret.encode("utf-8"),
            raw_bytes,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    async with semaphore:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.post(url, data=raw_bytes, headers=headers) as resp:
                    http_status = resp.status
                    try:
                        body: Dict[str, Any] = await resp.json(content_type=None)
                    except Exception:
                        body = {}

                    if http_status == 201:
                        return TxResult(
                            transaction_id       = body.get("transaction_id", tx_id),
                            batch_label          = label,
                            http_status          = http_status,
                            amount               = amount,
                            final_intent         = body.get("final_intent"),
                            final_status         = body.get("final_status"),
                            llm_intent           = body.get("llm_proposed_intent"),
                            guardrail_overridden = body.get("guardrail_overridden"),
                            rule_applied         = body.get("rule_applied"),
                            audit_log_id         = body.get("audit_log_id"),
                            action               = body.get("action"),
                        )

                    if http_status in (429, 500, 502, 503, 504) and attempt < RETRY_ATTEMPTS:
                        await asyncio.sleep(RETRY_BACKOFF * attempt)
                        continue

                    detail = body.get("detail", str(body))
                    return TxResult(
                        transaction_id = tx_id,
                        batch_label    = label,
                        http_status    = http_status,
                        amount         = amount,
                        error          = detail,
                    )

            except asyncio.TimeoutError:
                if attempt < RETRY_ATTEMPTS:
                    await asyncio.sleep(RETRY_BACKOFF * attempt)
                    continue
                return TxResult(
                    transaction_id = tx_id, batch_label = label,
                    http_status = 0, amount = amount, error = "Request timed out",
                )
            except aiohttp.ClientError as exc:
                return TxResult(
                    transaction_id = tx_id, batch_label = label,
                    http_status = 0, amount = amount, error = str(exc),
                )

        return TxResult(
            transaction_id = tx_id, batch_label = label,
            http_status = 0, amount = amount,
            error = f"Failed after {RETRY_ATTEMPTS} attempts",
        )


async def run_batch(
    transactions: List[Dict[str, Any]],
    base_url: str,
    concurrency: int,
    webhook_secret: Optional[str] = None,
) -> BatchReport:
    """
    Blast all transactions concurrently.
    The decision ledger updates live as each result arrives.
    Returns the aggregated BatchReport.
    """
    url       = base_url.rstrip("/") + ENDPOINT
    semaphore = asyncio.Semaphore(concurrency)
    timeout   = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    total     = len(transactions)

    console.print(Panel.fit(
        "[bold cyan]RECOVER-NET[/bold cyan]  [white]Concurrent Recovery Run[/white]",
        subtitle=f"{total} transactions | concurrency {concurrency}",
        border_style="cyan",
    ))

    # Shared mutable state — appended to by each completed coroutine
    live_results: List[TxResult] = []
    start = time.perf_counter()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        expand=True,
    )
    progress_task = progress.add_task("[cyan]Processing webhooks", total=total)

    def _render_live() -> Layout:
        """Compose the live display: progress bar on top, ledger + scoreboard below."""
        layout = Layout()
        layout.split_column(
            Layout(progress, name="progress", size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(_build_live_table(live_results, total), name="ledger", ratio=3),
            Layout(
                Panel(_build_live_scoreboard(live_results, total, start),
                      title="[bold]Live stats[/bold]", border_style="green"),
                name="stats",
                ratio=1,
            ),
        )
        return layout

    async with aiohttp.ClientSession(timeout=timeout) as session:
        with Live(_render_live(), console=console, refresh_per_second=4) as live:

            async def send_and_record(tx: Dict[str, Any]) -> TxResult:
                result = await send_webhook(
                    session, semaphore, url, tx, webhook_secret=webhook_secret
                )
                live_results.append(result)
                progress.advance(progress_task)
                live.update(_render_live())
                return result

            tasks = [send_and_record(tx) for tx in transactions]
            results: List[TxResult] = await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - start
    return _aggregate(results, elapsed)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate(results: List[TxResult], elapsed: float) -> BatchReport:
    report = BatchReport(
        total           = len(results),
        elapsed_seconds = elapsed,
        results         = results,
    )
    report.total_value_at_risk = sum(r.amount for r in results)

    for r in results:
        if r.success:
            report.succeeded += 1
        else:
            report.failed += 1

        if r.success:
            if r.final_intent == "retry_now":
                report.value_retried += r.amount
            elif r.final_intent == "offer_emi":
                report.value_emi += r.amount
            elif r.final_intent == "escalate_to_human":
                report.value_escalated += r.amount

        if r.final_intent:
            report.intent_counts[r.final_intent] = (
                report.intent_counts.get(r.final_intent, 0) + 1
            )

        if r.guardrail_overridden:
            report.guardrail_overrides += 1
        if r.rule_applied:
            report.rule_counts[r.rule_applied] = (
                report.rule_counts.get(r.rule_applied, 0) + 1
            )
            if r.rule_applied == "RULE_FRAUD_ESCALATE":
                report.fraud_blocked += 1

        if r.success and r.batch_label == "fraud" and r.was_escalated:
            if not r.guardrail_overridden:
                report.fraud_blocked += 1

        lbl = r.batch_label
        if lbl not in report.label_stats:
            report.label_stats[lbl] = {"total": 0, "escalated": 0, "errors": 0}
        report.label_stats[lbl]["total"] += 1
        if r.was_escalated:
            report.label_stats[lbl]["escalated"] += 1
        if not r.success:
            report.label_stats[lbl]["errors"] += 1

    return report


# ---------------------------------------------------------------------------
# Final static report (printed after live run completes)
# ---------------------------------------------------------------------------

def print_report(report: BatchReport) -> None:
    n_retry     = report.intent_counts.get("retry_now", 0)
    n_emi       = report.intent_counts.get("offer_emi", 0)
    n_escalated = report.intent_counts.get("escalate_to_human", 0)

    value_recovered = report.value_retried + report.value_emi
    pct_recovered   = (
        value_recovered / report.total_value_at_risk * 100
        if report.total_value_at_risk > 0 else 0.0
    )
    avg_latency_ms = (
        report.elapsed_seconds / report.succeeded * 1000
        if report.succeeded > 0 else 0.0
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column(justify="right")
    summary.add_row("Processed",       f"[bold]{report.succeeded}/{report.total}[/bold]")
    summary.add_row("Value at risk",   f"[bold yellow]{_inr(report.total_value_at_risk)}[/bold yellow]")
    summary.add_row("Recovered",       f"[bold green]{n_retry + n_emi}[/bold green]  ({_inr(value_recovered)})")
    summary.add_row("Revenue secured", f"[bold green]{pct_recovered:.1f}%[/bold green]")
    summary.add_row("Blocked",         f"[bold red]{n_escalated}[/bold red]")
    summary.add_row("Guardrail overrides", str(report.guardrail_overrides))
    summary.add_row("Elapsed",         f"{report.elapsed_seconds:.2f}s  [dim]~{avg_latency_ms:.0f}ms/request[/dim]")
    console.print(Panel(summary, title="[bold]Run summary[/bold]", border_style="green"))

    # Final ledger (static, sorted: RECOVERED first, then BLOCKED, then FAILED)
    order = {"RECOVERED": 0, "BLOCKED": 1, "FAILED": 2}
    sorted_results = sorted(report.results, key=lambda r: order[r.outcome])

    table = Table(title="Final decision ledger", header_style="bold cyan", expand=False)
    table.add_column("Outcome",       justify="center")
    table.add_column("Transaction")
    table.add_column("Intent")
    table.add_column("Rule / action")
    table.add_column("Audit ref",     style="dim")
    table.add_column("Amount",        justify="right")
    for r in sorted_results:
        outcome_style = {"RECOVERED": "green", "BLOCKED": "red", "FAILED": "yellow"}[r.outcome]
        rule          = r.rule_applied or r.action or "-"
        table.add_row(
            Text(r.outcome, style=f"bold {outcome_style}"),
            r.transaction_id[:12],
            r.final_intent or "-",
            rule,
            r.audit_log_id[:12] if r.audit_log_id else "-",
            _inr(r.amount),
        )
    console.print(table)

    if report.rule_counts:
        rules = Table(title="Guardrail activity", header_style="bold magenta")
        rules.add_column("Rule")
        rules.add_column("Hits", justify="right")
        for rule, count in sorted(report.rule_counts.items()):
            rules.add_row(rule, str(count))
        console.print(rules)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "timestamp", "outcome", "transaction_id", "batch_label", "http_status",
    "amount", "final_intent", "final_status", "llm_intent",
    "guardrail_overridden", "rule_applied", "action", "audit_log_id", "error",
]


def write_csv(report: BatchReport, path: str) -> None:
    """Write all results to a CSV file with a run-timestamp column."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in report.results:
            writer.writerow({
                "timestamp":          ts,
                "outcome":            r.outcome,
                "transaction_id":     r.transaction_id,
                "batch_label":        r.batch_label,
                "http_status":        r.http_status,
                "amount":             r.amount,
                "final_intent":       r.final_intent or "",
                "final_status":       r.final_status or "",
                "llm_intent":         r.llm_intent or "",
                "guardrail_overridden": r.guardrail_overridden,
                "rule_applied":       r.rule_applied or "",
                "action":             r.action or "",
                "audit_log_id":       r.audit_log_id or "",
                "error":              r.error or "",
            })
    console.print(f"[dim]CSV results written to:[/dim] [bold]{path}[/bold]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recover-Net concurrent batch runner")
    p.add_argument("--url",         default=DEFAULT_URL,
                   help=f"Base URL of the FastAPI server (default: {DEFAULT_URL})")
    p.add_argument("--batch",       default=BATCH_FILE,
                   help=f"Path to batch JSON file (default: {BATCH_FILE})")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Max simultaneous requests (default: {DEFAULT_CONCURRENCY})")
    p.add_argument("--secret",      default=os.getenv("WEBHOOK_SECRET", ""),
                   help="HMAC-SHA256 secret key for request signing (default: $WEBHOOK_SECRET)")
    p.add_argument("--report-csv",  metavar="FILE", default=DEFAULT_CSV_FILE,
                   help=f"Path to write CSV results (default: {DEFAULT_CSV_FILE})")
    p.add_argument("--report-json", metavar="FILE",
                   help="Also write full results to a JSON file")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    try:
        with open(args.batch, "r", encoding="utf-8") as f:
            raw_transactions: Any = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: batch file not found: {args.batch}")
        print("Run  `uv run python scripts/generate_batch.py`  first.")
        sys.exit(1)

    if not isinstance(raw_transactions, list) or not raw_transactions:
        print("ERROR: batch file must contain a non-empty JSON array.")
        sys.exit(1)

    transactions: List[Dict[str, Any]] = []
    for raw_transaction in cast(List[Any], raw_transactions):
        if not isinstance(raw_transaction, dict):
            print("ERROR: every batch transaction must be a JSON object.")
            sys.exit(1)
        transactions.append(cast(Dict[str, Any], raw_transaction))

    report = asyncio.run(
        run_batch(transactions, args.url, args.concurrency, webhook_secret=args.secret)
    )
    print_report(report)

    # CSV — always written (default: batch_results.csv)
    write_csv(report, args.report_csv)

    # JSON — optional
    if args.report_json:
        out: Dict[str, Any] = {
            "total":              report.total,
            "succeeded":          report.succeeded,
            "failed":             report.failed,
            "elapsed_seconds":    round(report.elapsed_seconds, 3),
            "intent_counts":      report.intent_counts,
            "guardrail_overrides":report.guardrail_overrides,
            "rule_counts":        report.rule_counts,
            "label_stats":        report.label_stats,
            "results": [
                {
                    "transaction_id":       r.transaction_id,
                    "batch_label":          r.batch_label,
                    "http_status":          r.http_status,
                    "final_intent":         r.final_intent,
                    "final_status":         r.final_status,
                    "llm_intent":           r.llm_intent,
                    "guardrail_overridden": r.guardrail_overridden,
                    "rule_applied":         r.rule_applied,
                    "audit_log_id":         r.audit_log_id,
                    "action":               r.action,
                    "error":                r.error,
                }
                for r in report.results
            ],
        }
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        console.print(f"[dim]JSON results written to:[/dim] [bold]{args.report_json}[/bold]")


if __name__ == "__main__":
    main()
