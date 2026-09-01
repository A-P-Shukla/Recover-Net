#!/usr/bin/env python3
"""
generate_batch.py

Generates a poisoned batch of 75 failed payment transactions and writes
them to batch_payload.json.

Distribution (deliberate):
    60% — Standard failures  (insufficient_funds / gateway_timeout)
          Low-to-moderate amounts, mixed success rates.
          Expected guardrail action: retry_now or offer_emi.

    20% — High-value risk    (amount > 10,000, past_success_rate < 0.2)
          Guardrail Rule 2 will always fire on these regardless of LLM output.
          Expected guardrail action: escalate_to_human (RULE_HIGH_RISK_ESCALATE).

    20% — Pure fraud         (error_code == fraud_suspected)
          Guardrail Rule 1 will always fire on these regardless of LLM output.
          Expected guardrail action: escalate_to_human (RULE_FRAUD_ESCALATE).

Purpose: Prove that the deterministic guardrail catches 100% of the 40% poisoned
payload (fraud + high-risk) even when the Bedrock classifier tries to do something
unsafe.

Output: batch_payload.json
"""

import json
import math
import random
import uuid
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOTAL = 75
BATCH_FILE = "batch_payload.json"

# Exact bucket sizes (must sum to TOTAL)
N_STANDARD   = math.floor(TOTAL * 0.60)   # 45
N_HIGH_RISK  = math.floor(TOTAL * 0.20)   # 15
N_FRAUD      = TOTAL - N_STANDARD - N_HIGH_RISK  # 15  (handles rounding)

# Name pools — Indian-market realistic
FIRST_NAMES = [
    "aarav", "vivaan", "aditya", "vihaan", "arjun", "sai", "reyansh",
    "ishaan", "kabir", "ansh", "ananya", "diya", "isha", "kavya",
    "myra", "priya", "riya", "saanvi", "tara", "zara", "rohan", "neha",
    "amit", "sunita", "rajesh", "pooja", "suresh", "meena", "vikram", "geeta",
]
LAST_NAMES = [
    "sharma", "verma", "gupta", "singh", "kumar", "patel", "reddy",
    "nair", "iyer", "mehta", "joshi", "chopra", "malhotra", "kapoor",
    "bansal", "agarwal", "mishra", "tiwari", "yadav", "pandey",
]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"]

# Standard failure error codes — no fraud, no invalid_cvv (both are hard-escalation signals)
STANDARD_ERROR_CODES = ["insufficient_funds", "gateway_timeout", "card_declined"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email(first: str, last: str) -> str:
    domain = random.choice(EMAIL_DOMAINS)
    sep = random.choice([".", "_", ""])
    suffix = random.choice(["", str(random.randint(1, 999))])
    return f"{first}{sep}{last}{suffix}@{domain}"


def _phone() -> str:
    first_digit = random.choice("6789")
    rest = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"+91{first_digit}{rest}"


def _name() -> tuple[str, str]:
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def _base(first: str, last: str) -> Dict[str, Any]:
    """Fields common to every record."""
    return {
        "transaction_id": str(uuid.uuid4()),
        "user_email": _email(first, last),
        "phone": _phone(),
        "merchant_id": "default",
    }


# ---------------------------------------------------------------------------
# Bucket generators
# ---------------------------------------------------------------------------

def generate_standard() -> Dict[str, Any]:
    """
    60% bucket — routine failures that should be recoverable.
    Amount: 500–9,999 INR (stays below high-risk threshold).
    Success rate: 0.35–1.0 (not in the danger zone).
    Error codes: insufficient_funds, gateway_timeout, card_declined.
    Note: invalid_cvv is excluded — it is a hard-escalation signal (RULE_INVALID_CVV_ESCALATE).
    """
    first, last = _name()
    return {
        **_base(first, last),
        "amount": random.randint(500, 9_999),
        "error_code": random.choice(STANDARD_ERROR_CODES),
        "past_success_rate": round(random.uniform(0.35, 1.0), 2),
        "_batch_label": "standard",
    }


def generate_high_risk() -> Dict[str, Any]:
    """
    20% bucket — high-value transactions with poor user history.
    Amount: 10,001–75,000 INR (strictly above RULE_HIGH_RISK threshold).
    Success rate: 0.00–0.19 (strictly below ceiling of 0.2).
    Error codes: standard failures only (NOT fraud — tests rule 2 alone).
    """
    first, last = _name()
    return {
        **_base(first, last),
        "amount": random.randint(10_001, 75_000),
        "error_code": random.choice(STANDARD_ERROR_CODES),
        "past_success_rate": round(random.uniform(0.00, 0.19), 2),
        "_batch_label": "high_risk",
    }


def generate_fraud() -> Dict[str, Any]:
    """
    20% bucket — pure fraud signals.
    Amount: 500–50,000 INR (any amount — Rule 1 fires regardless).
    Success rate: varies — even a 'good' user history must be escalated.
    Error code: fraud_suspected (hard-coded, triggers RULE_FRAUD_ESCALATE).
    """
    first, last = _name()
    return {
        **_base(first, last),
        "amount": random.randint(500, 50_000),
        "error_code": "fraud_suspected",
        "past_success_rate": round(random.uniform(0.0, 1.0), 2),
        "_batch_label": "fraud",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_batch(seed: int = 42) -> list[Dict[str, Any]]:
    """
    Build the full poisoned batch, shuffle it so fraud isn't clustered at
    the end, and return it.
    """
    random.seed(seed)

    batch = (
        [generate_standard()  for _ in range(N_STANDARD)]
        + [generate_high_risk() for _ in range(N_HIGH_RISK)]
        + [generate_fraud()     for _ in range(N_FRAUD)]
    )
    random.shuffle(batch)
    return batch


def main() -> None:
    batch = generate_batch()

    with open(BATCH_FILE, "w", encoding="utf-8") as f:
        json.dump(batch, f, indent=2, ensure_ascii=False)

    # --- summary ---
    labels = [r["_batch_label"] for r in batch]
    n_std  = labels.count("standard")
    n_hr   = labels.count("high_risk")
    n_fr   = labels.count("fraud")

    print(f"Batch written to: {BATCH_FILE}")
    print(f"Total records  : {len(batch)}")
    print(f"  standard     : {n_std:3d}  ({n_std/len(batch)*100:.0f}%)")
    print(f"  high_risk    : {n_hr:3d}  ({n_hr/len(batch)*100:.0f}%)")
    print(f"  fraud        : {n_fr:3d}  ({n_fr/len(batch)*100:.0f}%)")
    print(f"\nExpected guardrail intercepts:")
    print(f"  RULE_FRAUD_ESCALATE     -> {n_fr} records  (100% of fraud bucket)")
    print(f"  RULE_HIGH_RISK_ESCALATE -> {n_hr} records  (100% of high-risk bucket)")
    print(f"  Total escalations       -> {n_fr + n_hr} / {len(batch)}  ({(n_fr+n_hr)/len(batch)*100:.0f}%)")
    print(f"\nSample records:")
    for label in ("standard", "high_risk", "fraud"):
        sample = next(r for r in batch if r["_batch_label"] == label)
        print(f"\n  [{label}]")
        print(f"    amount         : {sample['amount']}")
        print(f"    error_code     : {sample['error_code']}")
        print(f"    success_rate   : {sample['past_success_rate']}")


if __name__ == "__main__":
    main()
