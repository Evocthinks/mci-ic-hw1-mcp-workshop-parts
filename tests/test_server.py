import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import server  # noqa: E402


@pytest.fixture(autouse=True)
def temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    monkeypatch.setattr(server, "AUDIT_PATH", tmp_path / "audit.log")


def test_search_by_name_and_cheapest_first():
    rows = server.search_inventory(query="6205")
    assert [r["sku"] for r in rows] == ["BRG-6205"]
    sensors = server.search_inventory(category="sensor", max_results=10)
    prices = [r["unit_price_eur"] for r in sensors]
    assert prices == sorted(prices)


def test_search_matches_words_in_any_order():
    rows = server.search_inventory(query="M12 proximity sensor")
    assert [r["sku"] for r in rows] == ["SNS-PX12"]
    assert rows[0]["stock"] == 320 and rows[0]["location"] == "Hall B / R1"


def test_demo_question_values():
    bearing = server.search_inventory(query="6205")[0]
    assert server.calculate_tiered_price(bearing["unit_price_eur"], 120)["total_eur"] == 710.40
    cable = server.search_inventory(query="M12 cable")[0]
    assert cable["stock"] >= 300
    assert server.calculate_tiered_price(cable["unit_price_eur"], 300)["total_eur"] == 2036.80
    plc = server.search_inventory(query="input module")[0]
    assert plc["stock"] == 7
    assert server.calculate_tiered_price(plc["unit_price_eur"], 8)["total_eur"] == 1192.00


def test_search_in_stock_only_hides_empty_parts():
    all_sensors = {r["sku"] for r in server.search_inventory(category="sensor", max_results=10)}
    in_stock = {r["sku"] for r in server.search_inventory(category="sensor", in_stock_only=True, max_results=10)}
    assert "SNS-PX18" in all_sensors and "SNS-PX18" not in in_stock


def test_search_is_not_vulnerable_to_injection():
    assert server.search_inventory(query="'; DROP TABLE parts; --") == []
    assert len(server.search_inventory(max_results=50)) == len(server.SEED)


@pytest.mark.parametrize("qty, total", [
    (1, 10.00), (10, 100.00), (11, 109.50), (50, 100.00 + 40 * 9.50),
    (120, 100.00 + 40 * 9.50 + 70 * 9.00), (300, 100.00 + 40 * 9.50 + 150 * 9.00 + 100 * 8.50),
])
def test_tiered_price_with_unit_price_10(qty, total):
    assert server.calculate_tiered_price(10.0, qty)["total_eur"] == pytest.approx(total)


def test_tiered_price_breakdown_adds_up():
    result = server.calculate_tiered_price(12.75, 120)
    assert sum(t["units"] for t in result["tiers"]) == 120
    assert result["list_total_eur"] == pytest.approx(result["total_eur"] + result["discount_eur"])
    assert result["tiers"][-1]["discount_percent"] == 10


def test_tiered_price_rejects_invalid_input():
    with pytest.raises(ValueError):
        server.calculate_tiered_price(10.0, 0)
    with pytest.raises(ValueError):
        server.calculate_tiered_price(-1.0, 5)


def test_audit_log_appends_and_resource_returns_it():
    assert server.audit_log() == ""
    assert server.append_audit_event("quote", "BRG-6205 x 120")["line"] == 1
    assert server.append_audit_event("stock_check", "SNS-PX12")["line"] == 2
    lines = server.audit_log().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "quote"
