"""MCP server for a maintenance workshop: spare parts stock, volume pricing, audit log.

Tools: search_inventory (sqlite3), calculate_tiered_price (formula engine),
append_audit_event (appends to a local file). The file is also served as the resource audit://log.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE = Path(__file__).parent
DB_PATH = Path(os.environ.get("INVENTORY_DB", BASE / "data" / "inventory.db"))
AUDIT_PATH = Path(os.environ.get("AUDIT_LOG_PATH", BASE / "data" / "audit.log"))

# (sku, name, category, price in cents, stock, location); cents avoid float errors
SEED = [
    ("BRG-6205", "Ball bearing 6205-2RS", "bearing", 640, 480, "Hall A / R3"),
    ("BRG-6310", "Ball bearing 6310-2RS", "bearing", 1890, 95, "Hall A / R3"),
    ("BLT-HTD8", "Timing belt HTD 8M-1200", "belt", 2450, 40, "Hall A / R5"),
    ("SNS-PX12", "Inductive proximity sensor M12, PNP", "sensor", 1275, 320, "Hall B / R1"),
    ("SNS-PX18", "Inductive proximity sensor M18, PNP", "sensor", 1480, 0, "Hall B / R1"),
    ("SNS-TMP1", "PT100 temperature probe, 100 mm", "sensor", 3190, 64, "Hall B / R2"),
    ("RLY-24D", "Relay 24 V DC, 2 changeover", "relay", 890, 700, "Hall B / R4"),
    ("PLC-DI16", "PLC input module 16 DI, 24 V", "plc", 14900, 7, "Hall C / R1"),
    ("PLC-DO16", "PLC output module 16 DO, 24 V", "plc", 16200, 3, "Hall C / R1"),
    ("FSE-10A", "Fuse 10 A slow-blow, 5x20", "fuse", 45, 2500, "Hall B / R6"),
    ("MTR-AC075", "AC motor 0.75 kW, 4-pole", "motor", 21500, 5, "Hall C / R3"),
    ("CBL-M12-5", "M12 sensor cable, 5 m", "cable", 760, 410, "Hall B / R7"),
]

# Marginal tiers: (last unit of the tier or None for open end, discount in percent)
TIERS = [(10, Decimal("0")), (50, Decimal("5")), (200, Decimal("10")), (None, Decimal("15"))]

mcp = FastMCP("workshop-parts")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not DB_PATH.exists()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if new:
        con.execute(
            "CREATE TABLE parts (sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, "
            "unit_price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, location TEXT NOT NULL)"
        )
        con.executemany(
            "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?)",
            [(s, n, c, p, q, loc) for s, n, c, p, q, loc in SEED],
        )
        con.commit()
    return con


@mcp.tool()
def search_inventory(query: str = "", category: str = "", in_stock_only: bool = False,
                     max_results: int = 5) -> list[dict]:
    """Look up spare parts in the stock database.

    query: words that must all appear in the SKU or the name, in any order (empty matches everything).
    category: exact category such as bearing, sensor, relay, plc, motor (empty for all).
    in_stock_only: hide parts with stock 0.
    Returns sku, name, category, unit_price_eur, stock and location, cheapest first.
    """
    max_results = max(1, min(int(max_results), 20))
    sql = "SELECT * FROM parts WHERE 1 = 1"
    args: list = []
    for word in query.split():  # every word must appear, in any order
        sql += " AND (sku LIKE ? OR name LIKE ?)"
        args += [f"%{word}%", f"%{word}%"]
    if category:
        sql += " AND category = ?"
        args.append(category.lower())
    if in_stock_only:
        sql += " AND stock > 0"
    sql += " ORDER BY unit_price_cents, sku LIMIT ?"
    args.append(max_results)
    with _connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [
        {"sku": r["sku"], "name": r["name"], "category": r["category"],
         "unit_price_eur": r["unit_price_cents"] / 100, "stock": r["stock"], "location": r["location"]}
        for r in rows
    ]


def _eur(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@mcp.tool()
def calculate_tiered_price(unit_price_eur: float, quantity: int) -> dict:
    """Compute the order price with marginal volume discounts.

    Units 1-10 cost the full price, units 11-50 get 5 %, units 51-200 get 10 %,
    every unit above 200 gets 15 %. Returns the price per tier, list total,
    discount, final total and the effective discount in percent.
    """
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    if unit_price_eur < 0:
        raise ValueError("unit_price_eur must not be negative")
    price = Decimal(str(unit_price_eur))
    tiers, start, total = [], 1, Decimal("0")
    for last, pct in TIERS:
        end = quantity if last is None else min(last, quantity)
        if end >= start:
            units = end - start + 1
            amount = price * units * (Decimal("100") - pct) / Decimal("100")
            tiers.append({"from_unit": start, "to_unit": end, "units": units,
                          "discount_percent": float(pct), "amount_eur": _eur(amount)})
            total += amount
        if last is None or quantity <= last:
            break
        start = last + 1
    list_total = price * quantity
    return {
        "quantity": quantity,
        "tiers": tiers,
        "list_total_eur": _eur(list_total),
        "discount_eur": _eur(list_total - total),
        "total_eur": _eur(total),
        "effective_discount_percent": float((list_total - total) / list_total * 100) if list_total else 0.0,
    }


@mcp.tool()
def append_audit_event(event_type: str, message: str) -> dict:
    """Append one event (for example quote, stock_check, error) to the audit log file."""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "event": event_type[:40], "message": message[:500]}
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with AUDIT_PATH.open(encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    return {"logged": True, "line": lines}


@mcp.resource("audit://log")
def audit_log() -> str:
    """Content of the audit log file."""
    return AUDIT_PATH.read_text(encoding="utf-8") if AUDIT_PATH.exists() else ""


if __name__ == "__main__":
    mcp.run(transport="stdio")
