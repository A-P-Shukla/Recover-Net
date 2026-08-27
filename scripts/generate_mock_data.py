#!/usr/bin/env python3
"""
generate_mock_data.py

Generates 50 mock JSON objects representing FAILED webhook events
(e.g., for a payment gateway), useful as test/context data for an
LLM-based retry-decision system.

Each record includes:
    - transaction_id      (UUID v4 string)
    - user_email          (fake but realistic-looking email)
    - phone               (10-digit Indian mobile number)
    - amount              (random integer, 500-50000 INR)
    - error_code          (one of: insufficient_funds, gateway_timeout,
                            fraud_suspected, invalid_cvv)
    - past_success_rate   (float 0.0-1.0, user's historical success rate)

Output: writes a JSON array to `failed_webhooks.json` and prints
a short summary to stdout.
"""

import json
import random
import uuid
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NUM_RECORDS = 50
OUTPUT_FILE = "failed_webhooks.json"

ERROR_CODES = [
    "insufficient_funds",
    "gateway_timeout",
    "fraud_suspected",
    "invalid_cvv",
]

FIRST_NAMES = [
    "aarav", "vivaan", "aditya", "vihaan", "arjun", "sai", "reyansh",
    "ishaan", "kabir", "ansh", "ananya", "diya", "isha", "kavya",
    "myra", "priya", "riya", "saanvi", "tara", "zara",
]

LAST_NAMES = [
    "sharma", "verma", "gupta", "singh", "kumar", "patel", "reddy",
    "nair", "iyer", "mehta", "joshi", "chopra", "malhotra", "kapoor",
]

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]


def random_name() -> tuple[str, str]:
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def random_email(first: str, last: str) -> str:
    domain = random.choice(EMAIL_DOMAINS)
    separator = random.choice([".", "_", ""])
    suffix = random.choice(["", str(random.randint(1, 999))])
    return f"{first}{separator}{last}{suffix}@{domain}"


def random_phone() -> str:
    # Indian mobile numbers start with 6-9, followed by 9 more digits
    first_digit = random.choice("6789")
    remaining = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"+91{first_digit}{remaining}"


def generate_record() -> Dict[str, Any]:
    first, last = random_name()
    return {
        "transaction_id": str(uuid.uuid4()),
        "user_email": random_email(first, last),
        "phone": random_phone(),
        "amount": random.randint(500, 50000),
        "error_code": random.choice(ERROR_CODES),
        "past_success_rate": round(random.uniform(0.0, 1.0), 2),
    }


def generate_dataset(n: int = NUM_RECORDS) -> List[Dict[str, Any]]:
    return [generate_record() for _ in range(n)]


def main() -> None:
    data = generate_dataset(NUM_RECORDS)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Generated {len(data)} mock failed-webhook records.")
    print(f"Saved to: {OUTPUT_FILE}")
    print("\nSample record:")
    print(json.dumps(data[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
