from polymarket_tilt_bot.ledger import CsvLedger, SQLiteLedger
from polymarket_tilt_bot.models import Fill, Market, Position
from polymarket_tilt_bot.runner import resolve_missing_positions


def market(slug: str = "btc-updown-5m-1000", end_ts: int = 1300) -> Market:
    return Market(
        asset="BTC",
        slug=slug,
        condition_id=f"0x-{slug}",
        title="Bitcoin Up or Down",
        start_ts=end_ts - 300,
        end_ts=end_ts,
        up_token=f"up-{slug}",
        down_token=f"down-{slug}",
        accepting_orders=False,
        closed=True,
    )


class StaticResolver:
    def __init__(self, winners: dict[str, str], failing_slug: str | None = None) -> None:
        self.winners = winners
        self.failing_slug = failing_slug

    def get_winner(self, market: Market):
        if market.slug == self.failing_slug:
            raise RuntimeError("boom")
        return self.winners.get(market.slug)


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


def test_sqlite_ledger_can_lookup_recorded_market(tmp_path) -> None:
    ledger = SQLiteLedger(str(tmp_path / "paper.sqlite"))
    m = market()
    try:
        ledger.record_market(m, 1000)
        found = ledger.get_market(m.slug)
    finally:
        ledger.close()

    assert found is not None
    assert found.slug == m.slug
    assert found.end_ts == m.end_ts
    assert found.up_token == m.up_token


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


def test_csv_ledger_can_lookup_recorded_market(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    m = market()
    ledger.record_market(m, 1000)

    found = ledger.get_market(m.slug)

    assert found is not None
    assert found.slug == m.slug
    assert found.end_ts == m.end_ts
    assert found.down_token == m.down_token


def test_resolve_missing_uses_stored_fills_and_is_idempotent(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    m = market("btc-updown-5m-1000", end_ts=1300)
    ledger.record_market(m, 1000)
    ledger.record_fill(Fill(m.slug, m.condition_id, "Down", m.down_token, 0.25, 20, 5, 1000))

    first = resolve_missing_positions(ledger, object(), StaticResolver({m.slug: "Down"}), grace_seconds=20, now=1400)
    second = resolve_missing_positions(ledger, object(), StaticResolver({m.slug: "Down"}), grace_seconds=20, now=1400)

    assert first == 1
    assert second == 0
    assert ledger.is_resolved(m.slug)
    assert ledger.daily_report()["summary"]["resolved_markets"] == 1


def test_resolve_missing_keeps_going_after_one_market_fails(tmp_path) -> None:
    ledger = CsvLedger(str(tmp_path / "paper_csv"))
    bad = market("btc-updown-5m-1000", end_ts=1300)
    good = market("btc-updown-5m-1600", end_ts=1900)
    for item in [bad, good]:
        ledger.record_market(item, 1000)
        ledger.record_fill(Fill(item.slug, item.condition_id, "Up", item.up_token, 0.40, 10, 4, 1000))

    resolved = resolve_missing_positions(
        ledger,
        object(),
        StaticResolver({bad.slug: "Up", good.slug: "Up"}, failing_slug=bad.slug),
        grace_seconds=20,
        now=2000,
    )

    assert resolved == 1
    assert not ledger.is_resolved(bad.slug)
    assert ledger.is_resolved(good.slug)


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
