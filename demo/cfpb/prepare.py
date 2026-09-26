"""Build the CFPB sample: real consumer complaints with the product chosen by the consumer.

Source: the CFPB Consumer Complaint Database (public domain), via the Hugging Face copy
BEE-spoke-data/consumer-finance-complaints, config has-text, file 0.parquet (563k complaints
with a narrative, 2015 to 2024-02). Download it once, then:

    uv run --with duckdb python demo/cfpb/prepare.py path/to/0.parquet

Writes demo/cfpb/sample.jsonl.gz: 3000 complaints from 2022 onwards, stratified over nine
product classes (333 each, 336 for the first), narrative capped at 4000 characters.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import duckdb

PER_CLASS = 333
SEED = 0.42
OUT = Path(__file__).with_name("sample.jsonl.gz")

# CFPB product names changed over the years: group them into nine stable classes.
CLASSES = {
    "credit_reporting": [
        "Credit reporting, credit repair services, or other personal consumer reports",
        "Credit reporting or other personal consumer reports",
    ],
    "debt_collection": ["Debt collection"],
    "bank_account": ["Checking or savings account"],
    "credit_card": ["Credit card or prepaid card", "Credit card", "Prepaid card"],
    "mortgage": ["Mortgage"],
    "money_transfer": ["Money transfer, virtual currency, or money service"],
    "vehicle_loan": ["Vehicle loan or lease"],
    "student_loan": ["Student loan"],
    "personal_loan": [
        "Payday loan, title loan, or personal loan",
        "Payday loan, title loan, personal loan, or advance loan",
    ],
}


def main(parquet: str) -> None:
    con = duckdb.connect()
    con.execute("SELECT setseed(?)", [SEED])
    mapping = [(cls, product) for cls, products in CLASSES.items() for product in products]
    con.execute("CREATE TABLE classes (cls text, product text)")
    con.executemany("INSERT INTO classes VALUES (?, ?)", mapping)
    rows = con.execute(
        f"""
        WITH src AS (
          SELECT "Complaint ID" AS complaint_id, "Date received" AS received, c.cls AS product,
                 "Issue" AS issue, left("Consumer complaint narrative", 4000) AS narrative
          FROM read_parquet(?) p JOIN classes c ON c.product = p."Product"
          WHERE "Date received" >= '2022-01-01'
        ), ranked AS (
          SELECT *, row_number() OVER (PARTITION BY product ORDER BY random()) AS rn FROM src
        )
        SELECT complaint_id, received, product, issue, narrative FROM ranked
        WHERE rn <= {PER_CLASS} OR (product = 'credit_reporting' AND rn <= {PER_CLASS + 3})
        ORDER BY complaint_id
        """,
        [parquet],
    ).fetchall()
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        for complaint_id, received, product, issue, narrative in rows:
            record = {
                "complaint_id": int(complaint_id),
                "received": str(received),
                "product": product,
                "issue": issue,
                "narrative": narrative,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"{len(rows)} complaints -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
