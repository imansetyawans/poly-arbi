from polymarket_tilt_bot.clients import GammaClient


def test_parse_crypto_market_maps_outcomes_to_token_ids() -> None:
    event = {
        "slug": "btc-updown-5m-1777131600",
        "title": "Bitcoin Up or Down",
        "resolutionSource": "chainlink",
        "eventMetadata": {"priceToBeat": 100.0},
        "markets": [
            {
                "conditionId": "0xabc",
                "question": "Bitcoin Up or Down",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
                "acceptingOrders": True,
                "closed": False,
                "orderMinSize": 5,
            }
        ],
    }

    market = GammaClient.__new__(GammaClient).parse_crypto_market(event, "BTC")

    assert market is not None
    assert market.start_ts == 1777131600
    assert market.up_token == "up-token"
    assert market.down_token == "down-token"
    assert market.price_to_beat == 100.0
