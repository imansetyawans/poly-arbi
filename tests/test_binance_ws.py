from polymarket_tilt_bot.clients import BinanceWebSocketPriceClient


def test_binance_trade_message_parses_to_tick() -> None:
    message = (
        '{"stream":"btcusdt@trade","data":{"e":"trade","E":1672515782136,'
        '"s":"BTCUSDT","t":12345,"p":"16500.50","q":"0.001","T":1672515782135}}'
    )

    tick = BinanceWebSocketPriceClient.parse_message(message)

    assert tick is not None
    assert tick.asset == "BTC"
    assert tick.price == 16500.50
    assert tick.source == "binance_ws"
