from polymarket_tilt_bot.ledger import SQLiteLedger
from polymarket_tilt_bot.models import Fill, Position


def test_daily_report_summarizes_resolved_positions(tmp_path) -> None:
    db = tmp_path / "paper.sqlite"
    ledger = SQLiteLedger(str(db))
    try:
        position = Position("m1", "0x1")
        fill = Fill("m1", "0x1", "Up", "up", 0.4, 10, 4, 1000)
        position.add_fill(fill)
        ledger.record_fill(fill)
        ledger.record_resolution(position, "Up", 1_777_176_000)

        report = ledger.daily_report("2026-04-26")
        resolved = ledger.is_resolved("m1")
        realized = ledger.realized_pnl()
    finally:
        ledger.close()

    assert report["summary"]["resolved_markets"] == 1
    assert resolved is True
    assert report["summary"]["wins"] == 1
    assert report["summary"]["total_pnl"] == 6
    assert realized == 6
    assert report["markets"][0]["winner"] == "Up"


def test_unresolved_positions_can_be_rebuilt_from_fills(tmp_path) -> None:
    db = tmp_path / "paper.sqlite"
    ledger = SQLiteLedger(str(db))
    try:
        ledger.record_fill(Fill("m1", "0x1", "Down", "down", 0.25, 20, 5, 1000))
        positions = ledger.get_unresolved_positions()
    finally:
        ledger.close()

    assert len(positions) == 1
    assert positions[0].shares["Down"] == 20
    assert positions[0].total_cost == 5
