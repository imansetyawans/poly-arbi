from polymarket_tilt_bot.models import Fill, Position


def test_position_decomposes_paired_and_unpaired_pnl() -> None:
    position = Position("m", "0x")
    position.add_fill(Fill("m", "0x", "Up", "up", price=0.55, size=100, notional=55, timestamp=1))
    position.add_fill(Fill("m", "0x", "Down", "down", price=0.48, size=60, notional=28.8, timestamp=1))

    result = position.resolve("Up")

    assert result["pnl"] == 16.200000000000003
    assert result["paired_shares"] == 60
    assert round(result["paired_pnl"], 6) == -1.8
    assert result["unpaired_side"] == "Up"
    assert round(result["unpaired_pnl"], 6) == 18.0
