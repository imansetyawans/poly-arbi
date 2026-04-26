from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests
import websocket

from .config import RuntimeConfig
from .models import BookLevel, CryptoTick, Market, OrderBook


class ApiError(RuntimeError):
    pass


@dataclass
class HttpClient:
    config: RuntimeConfig

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.config.user_agent})

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=self.config.request_timeout)
        except requests.RequestException as exc:
            raise ApiError(f"GET {url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ApiError(f"GET {response.url} -> {response.status_code}: {response.text[:300]}")
        return response.json()


class GammaClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        url = f"{self.http.config.gamma_base_url}/events/slug/{slug}"
        try:
            return self.http.get_json(url)
        except ApiError as exc:
            if "404" in str(exc):
                return None
            raise

    def parse_crypto_market(self, event: dict[str, Any], asset: Literal["BTC", "ETH"]) -> Market | None:
        markets = event.get("markets") or []
        if not markets:
            return None
        row = markets[0]
        outcomes = json.loads(row.get("outcomes") or "[]")
        token_ids = json.loads(row.get("clobTokenIds") or "[]")
        if len(outcomes) != 2 or len(token_ids) != 2:
            return None
        token_by_outcome = dict(zip(outcomes, token_ids))
        start_ts = int(event.get("slug", "").rsplit("-", 1)[-1])
        end_ts = start_ts + 300
        metadata = event.get("eventMetadata") or {}
        return Market(
            asset=asset,
            slug=event["slug"],
            condition_id=row["conditionId"],
            title=row.get("question") or event.get("title") or event["slug"],
            start_ts=start_ts,
            end_ts=end_ts,
            up_token=token_by_outcome["Up"],
            down_token=token_by_outcome["Down"],
            accepting_orders=bool(row.get("acceptingOrders")),
            closed=bool(row.get("closed")),
            resolution_source=row.get("resolutionSource") or event.get("resolutionSource") or "",
            min_order_size=float(row.get("orderMinSize") or 5.0),
            price_to_beat=float(metadata["priceToBeat"]) if metadata.get("priceToBeat") is not None else None,
        )


class ClobClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def get_book(self, token_id: str) -> OrderBook:
        url = f"{self.http.config.clob_base_url}/book"
        data = self.http.get_json(url, {"token_id": token_id})
        return OrderBook(
            token_id=token_id,
            bids=self._parse_levels(data.get("bids") or [], reverse=True),
            asks=self._parse_levels(data.get("asks") or [], reverse=False),
            timestamp_ms=int(data["timestamp"]) if data.get("timestamp") else None,
        )

    def get_books_for_market(self, market: Market) -> dict[Literal["Up", "Down"], OrderBook]:
        return {
            "Up": self.get_book(market.up_token),
            "Down": self.get_book(market.down_token),
        }

    @staticmethod
    def _parse_levels(raw: list[dict[str, str]], reverse: bool) -> list[BookLevel]:
        levels = [BookLevel(float(row["price"]), float(row["size"])) for row in raw]
        return sorted(levels, key=lambda level: level.price, reverse=reverse)


class CoinGeckoPriceClient:
    """Simple polling price client.

    Polymarket crypto 5-minute markets resolve from Chainlink data streams. This
    public client is a practical paper-trading proxy, not a perfect resolution
    feed. Swap it with a Chainlink/paid low-latency source before live trading.
    """

    ID_BY_ASSET: dict[str, str] = {"BTC": "bitcoin", "ETH": "ethereum"}

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._cache: dict[Literal["BTC", "ETH"], CryptoTick] = {}
        self._cache_ts = 0.0
        self.cache_seconds = 8.0

    def get_tick(self, asset: Literal["BTC", "ETH"]) -> CryptoTick:
        now = time.time()
        if now - self._cache_ts > self.cache_seconds or asset not in self._cache:
            self._refresh_cache(now)
        return self._cache[asset]

    def _refresh_cache(self, timestamp: float) -> None:
        url = f"{self.http.config.coingecko_base_url}/simple/price"
        data = self.http.get_json(url, {"ids": ",".join(self.ID_BY_ASSET.values()), "vs_currencies": "usd"})
        for asset, coin_id in self.ID_BY_ASSET.items():
            if coin_id in data and "usd" in data[coin_id]:
                self._cache[asset] = CryptoTick(asset=asset, price=float(data[coin_id]["usd"]), timestamp=timestamp, source="coingecko")
        self._cache_ts = timestamp


class BinanceWebSocketPriceClient:
    SYMBOL_BY_ASSET: dict[Literal["BTC", "ETH"], str] = {"BTC": "btcusdt", "ETH": "ethusdt"}

    def __init__(self, assets: tuple[Literal["BTC", "ETH"], ...]) -> None:
        self.assets = assets
        self._ticks: dict[Literal["BTC", "ETH"], CryptoTick] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        streams = "/".join(f"{self.SYMBOL_BY_ASSET[asset]}@trade" for asset in assets)
        self.url = f"wss://data-stream.binance.vision/stream?streams={streams}"
        self._thread = threading.Thread(target=self._run_forever, name="binance-price-ws", daemon=True)
        self._thread.start()

    def get_tick(self, asset: Literal["BTC", "ETH"], wait_seconds: float = 5.0) -> CryptoTick:
        if asset not in self._ticks:
            self._ready.wait(wait_seconds)
        with self._lock:
            tick = self._ticks.get(asset)
        if tick is None:
            raise ApiError(f"no Binance websocket tick received for {asset} yet")
        return tick

    def _run_forever(self) -> None:
        while True:
            ws = websocket.WebSocketApp(
                self.url,
                on_message=self._on_message,
                on_error=lambda _ws, err: None,
                on_close=lambda _ws, _code, _msg: None,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
            time.sleep(2)

    def _on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        tick = self.parse_message(message)
        if tick is None:
            return
        with self._lock:
            self._ticks[tick.asset] = tick
            if all(asset in self._ticks for asset in self.assets):
                self._ready.set()

    @classmethod
    def parse_message(cls, message: str) -> CryptoTick | None:
        payload = json.loads(message)
        stream = payload.get("stream", "")
        data = payload.get("data") or {}
        symbol = str(data.get("s") or stream.split("@", 1)[0]).lower()
        asset = next((asset for asset, stream_symbol in cls.SYMBOL_BY_ASSET.items() if stream_symbol == symbol), None)
        if asset is None or data.get("p") is None:
            return None
        timestamp_ms = data.get("T") or data.get("E")
        timestamp = float(timestamp_ms) / 1000.0 if timestamp_ms else time.time()
        return CryptoTick(asset=asset, price=float(data["p"]), timestamp=timestamp, source="binance_ws")
