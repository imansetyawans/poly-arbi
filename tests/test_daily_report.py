from polymarket_tilt_bot.ledger import CsvLedger, SQLiteLedger
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
    assert report["summary"]["one_sided_pnl"] == 6
    assert report["summary"]["both_sided_completion_rate"] == 0
    assert report["summary"]["one_sided_average_loss"] == 0
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


def test_csv_ledger_records_and_reports(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    position = Position("m1", "0x1")
    fill = Fill("m1", "0x1", "Up", "up", 0.4, 10, 4, 1000)
    position.add_fill(fill)
    ledger.record_fill(fill)
    ledger.record_resolution(position, "Up", 1_777_176_000)

    report = ledger.daily_report("2026-04-26")

    assert report["summary"]["resolved_markets"] == 1
    assert report["summary"]["total_pnl"] == 6
    assert ledger.realized_pnl() == 6


def test_daily_report_splits_both_sided_and_one_sided_pnl(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    both = Position("btc-updown-5m-1777424100", "0x1")
    both.add_fill(Fill("btc-updown-5m-1777424100", "0x1", "Up", "up", 0.4, 10, 4, 1777424110))
    both.add_fill(Fill("btc-updown-5m-1777424100", "0x1", "Down", "down", 0.4, 10, 4, 1777424300))
    one = Position("btc-updown-5m-1777424400", "0x2")
    one.add_fill(Fill("btc-updown-5m-1777424400", "0x2", "Up", "up", 0.4, 10, 4, 1777424410))
    for fill in [
        Fill("btc-updown-5m-1777424100", "0x1", "Up", "up", 0.4, 10, 4, 1777424110),
        Fill("btc-updown-5m-1777424100", "0x1", "Down", "down", 0.4, 10, 4, 1777424300),
        Fill("btc-updown-5m-1777424400", "0x2", "Up", "up", 0.4, 10, 4, 1777424410),
    ]:
        ledger.record_fill(fill)
    ledger.record_resolution(both, "Up", 1_777_424_400)
    ledger.record_resolution(one, "Down", 1_777_424_700)

    report = ledger.daily_report()
    summary = report["summary"]

    assert summary["resolved_markets"] == 2
    assert summary["both_sided_markets"] == 1
    assert summary["one_sided_markets"] == 1
    assert summary["both_sided_pnl"] == 2
    assert summary["one_sided_pnl"] == -4
    assert summary["failed_one_sided_markets"] == 1
    assert summary["one_sided_average_loss"] == 4
    assert summary["both_sided_completion_rate"] == 0.5
    assert summary["scaled_both_sided_markets"] == 1
    assert summary["scaled_both_sided_pnl"] == 2
    assert summary["positive_pair_quality_markets"] == 1
    assert summary["expensive_both_sided_markets"] == 0
    assert summary["max_drawdown"] == 4
    assert report["markets"][0]["pair_cost"] == 0.8
    assert summary["timing_buckets"]["180-240"]["markets"] == 1
    assert summary["cost_buckets"]["6-9"]["markets"] == 1


def test_recent_completion_rate_waits_for_full_window(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    position = Position("m1", "0x1")
    position.add_fill(Fill("m1", "0x1", "Up", "up", 0.4, 10, 4, 1000))
    ledger.record_resolution(position, "Up", 1000)

    assert ledger.recent_completion_rate(20) == (1, 1.0)
