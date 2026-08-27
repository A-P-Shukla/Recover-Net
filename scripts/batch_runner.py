#!/usr/bin/env python3
"""
batch_runner.py

Concurrency Engine for Recover-Net.

Fires all transactions in batch_payload.json at the FastAPI recover endpoint
simultaneously using asyncio + aiohttp — no sequential waiting, no for-loop
bottleneck.

Each response is collected, aggregated, and printed as a structured report
that proves the guardrail is catching the poisoned 40% of the batch.

Usage:
    uv run python scripts/batch_runner.py
    uv run python scripts/batch_runner.py --url http://localhost:8000 --concurrency 20
"""

import argparse
import asyncio
import json
import sys
import time 
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

import aiohttp
from rich.console import Console
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
DEFAULT_CONCURRENCY = 5           # Groq free tier: ~30 req/min → 5 concurrent is safe
REQUEST_TIMEOUT     = 60          # seconds per request
BATCH_FILE          = "batch_payload.json"
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
    amount: float                 = 0.0   # original transaction amount (INR)
    final_intent: Optional[str]   = None
    final_status: Optional[str]   = None
    llm_intent: Optional[str]     = None
    guardrail_overridden: Optional[bool] = None
    rule_applied: Optional[str]   = None
    error: Optional[str]          = None
    audit_log_id: Optional[str]    = None
    action: Optional[str]          = None

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

    # Value tracking (INR)
    total_value_at_risk: float        = 0.0
    value_retried: float              = 0.0
    value_emi: float                  = 0.0
    value_escalated: float            = 0.0

    # Intent distribution (final, after guardrail)
    intent_counts: Dict[str, int]     = field(default_factory=lambda: {})

    # Guardrail stats
    guardrail_overrides: int          = 0
    fraud_blocked: int                = 0        # RULE_FRAUD_ESCALATE hits
    rule_counts: Dict[str, int]       = field(default_factory=lambda: {})

    # Per-label escalation accuracy
    label_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: {})

    # Raw results for detailed inspection
    results: List[TxResult]           = field(default_factory=lambda: [])


# ---------------------------------------------------------------------------
# Core async machinery
# ---------------------------------------------------------------------------

async def send_webhook(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    url: str,
    payload: Dict[str, Any],
) -> TxResult:
    """
    POST a single transaction to the recover endpoint.
    The semaphore caps simultaneous in-flight requests.
    Retries up to RETRY_ATTEMPTS times on 429 (rate limit) or 5xx errors.
    """
    tx_id  = payload.get("transaction_id", "unknown")
    label  = payload.get("_batch_label", "unknown")
    amount = float(payload.get("amount", 0) or 0)

    # Strip internal metadata before sending to the API
    api_payload = {k: v for k, v in payload.items() if not k.startswith("_")}

    async with semaphore:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with session.post(url, json=api_payload) as resp:
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

                    # Retry on rate-limit or transient server errors
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
                    transaction_id = tx_id,
                    batch_label    = label,
                    http_status    = 0,
                    amount         = amount,
                    error          = "Request timed out",
                )
            except aiohttp.ClientError as exc:
                return TxResult(
                    transaction_id = tx_id,
                    batch_label    = label,
                    http_status    = 0,
                    amount         = amount,
                    error          = str(exc),
                )

        return TxResult(
            transaction_id = tx_id,
            batch_label    = label,
            http_status    = 0,
            amount         = amount,
            error          = f"Failed after {RETRY_ATTEMPTS} attempts",
        )


async def run_batch(
    transactions: List[Dict[str, Any]],
    base_url: str,
    concurrency: int,
) -> BatchReport:
    """Blast all transactions concurrently and return the aggregated report."""
    url       = base_url.rstrip("/") + ENDPOINT
    semaphore = asyncio.Semaphore(concurrency)
    timeout   = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    console.print(Panel.fit(
        "[bold cyan]RECOVER-NET[/bold cyan]  [white]Concurrent Recovery Run[/white]",
        subtitle=f"{len(transactions)} transactions | concurrency {concurrency}",
        border_style="cyan",
    ))

    start = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        )
        progress_task = progress.add_task(
            "[cyan]Processing webhooks", total=len(transactions)
        )

        async def send_with_progress(transaction: Dict[str, Any]) -> TxResult:
            result = await send_webhook(session, semaphore, url, transaction)
            progress.advance(progress_task)
            return result

        with Live(progress, console=console, refresh_per_second=10):
            tasks = [send_with_progress(tx) for tx in transactions]
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

    # Total value at risk = sum of ALL transaction amounts in the batch
    report.total_value_at_risk = sum(r.amount for r in results)

    for r in results:
        if r.success:
            report.succeeded += 1
        else:
            report.failed += 1

        # Revenue value tracking (only successful responses)
        if r.success:
            if r.final_intent == "retry_now":
                report.value_retried += r.amount
            elif r.final_intent == "offer_emi":
                report.value_emi += r.amount
            elif r.final_intent == "escalate_to_human":
                report.value_escalated += r.amount

        # Intent distribution
        if r.final_intent:
            report.intent_counts[r.final_intent] = (
                report.intent_counts.get(r.final_intent, 0) + 1
            )

        # Guardrail override counts
        if r.guardrail_overridden:
            report.guardrail_overrides += 1
        if r.rule_applied:
            report.rule_counts[r.rule_applied] = (
                report.rule_counts.get(r.rule_applied, 0) + 1
            )
            if r.rule_applied == "RULE_FRAUD_ESCALATE":
                report.fraud_blocked += 1

        # Count fraud-label escalations regardless of whether guardrail had to override
        # (Groq may correctly classify fraud itself — both cases are "blocked")
        if r.success and r.batch_label == "fraud" and r.was_escalated:
            if not r.guardrail_overridden:   # LLM got it right — still counts as blocked
                report.fraud_blocked += 1

        # Per-label accuracy
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
# CFO Dashboard — terminal report
# ---------------------------------------------------------------------------

def _inr(amount: float) -> str:
    """Format a number as Indian Rupee with comma separators. e.g. ₹1,23,456"""
    # Indian numbering: last 3 digits, then groups of 2
    s = f"{int(amount):,}"          # standard comma first
    parts = s.split(",")
    if len(parts) <= 2:
        return f"₹{s}"
    # Re-group: last group of 3, rest in groups of 2
    last  = parts[-1]               # last 3 digits
    rest  = parts[:-1]
    rest_joined = ",".join(rest)    # already comma-separated in groups of 3 from Python
    return f"₹{rest_joined},{last}"


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
    summary.add_row("Processed", f"[bold]{report.succeeded}/{report.total}[/bold]")
    summary.add_row("Value at risk", f"[bold yellow]{_inr(report.total_value_at_risk)}[/bold yellow]")
    summary.add_row("Recovered", f"[bold green]{n_retry + n_emi}[/bold green]  ({_inr(value_recovered)})")
    summary.add_row("Revenue secured", f"[bold green]{pct_recovered:.1f}%[/bold green]")
    summary.add_row("Blocked", f"[bold red]{n_escalated}[/bold red]")
    summary.add_row("Guardrail overrides", str(report.guardrail_overrides))
    summary.add_row("Elapsed", f"{report.elapsed_seconds:.2f}s  [dim]~{avg_latency_ms:.0f}ms/request[/dim]")
    console.print(Panel(summary, title="[bold]Run summary[/bold]", border_style="green"))

    table = Table(title="Decision ledger", header_style="bold cyan", expand=False)
    table.add_column("Outcome", justify="center")
    table.add_column("Transaction")
    table.add_column("Intent")
    table.add_column("Rule / action")
    table.add_column("Audit ref", style="dim")
    table.add_column("Amount", justify="right")
    for result in report.results:
        outcome_style = {"RECOVERED": "green", "BLOCKED": "red", "FAILED": "yellow"}[result.outcome]
        outcome = Text(result.outcome, style=f"bold {outcome_style}")
        transaction_id = result.transaction_id[:12]
        audit_ref = result.audit_log_id[:12] if result.audit_log_id else "-"
        rule = result.rule_applied or result.action or "-"
        table.add_row(outcome, transaction_id, result.final_intent or "-", rule, audit_ref, _inr(result.amount))
    console.print(table)

    if report.rule_counts:
        rules = Table(title="Guardrail activity", header_style="bold magenta")
        rules.add_column("Rule")
        rules.add_column("Hits", justify="right")
        for rule, count in sorted(report.rule_counts.items()):
            rules.add_row(rule, str(count))
        console.print(rules)


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

    report = asyncio.run(run_batch(transactions, args.url, args.concurrency))
    print_report(report)

    if args.report_json:
        out: Dict[str, Any] = {
            "total": report.total,
            "succeeded": report.succeeded,
            "failed": report.failed,
            "elapsed_seconds": round(report.elapsed_seconds, 3),
            "intent_counts": report.intent_counts,
            "guardrail_overrides": report.guardrail_overrides,
            "rule_counts": report.rule_counts,
            "label_stats": report.label_stats,
            "results": [
                {
                    "transaction_id":      r.transaction_id,
                    "batch_label":         r.batch_label,
                    "http_status":         r.http_status,
                    "final_intent":        r.final_intent,
                    "final_status":        r.final_status,
                    "llm_intent":          r.llm_intent,
                    "guardrail_overridden":r.guardrail_overridden,
                    "rule_applied":        r.rule_applied,
                    "audit_log_id":        r.audit_log_id,
                    "action":              r.action,
                    "error":               r.error,
                }
                for r in report.results
            ],
        }
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"Full results written to: {args.report_json}")


if __name__ == "__main__":
    main()
