from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from .config import RuntimeConfig
from .models import Fill, Market, OrderBook, OrderIntent


class LiveTradingDisabled(RuntimeError):
    pass


class LiveExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LivePreflight:
    ready: bool
    missing_env: tuple[str, ...]
    sdk_available: bool
    armed: bool


class LiveExecutor:
    """CLOB execution boundary for production mode.

    Strategy code produces OrderIntent objects. This class is responsible only
    for turning those existing intents into signed CLOB buy orders.
    """

    REQUIRED_ENV = (
        "POLYBOT_ENABLE_LIVE_TRADING",
        "POLYMARKET_PRIVATE_KEY",
    )
    API_CRED_ENV = ("CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE")

    def __init__(
        self,
        client: Any,
        sdk: Any,
        order_type: str = "FAK",
        tick_size: str = "0.01",
        price_ticks: int = 1,
    ) -> None:
        if price_ticks < 0 or price_ticks > 2:
            raise LiveExecutionError("live price ticks must be between 0 and 2")
        self.client = client
        self.sdk = sdk
        self.order_type = order_type
        self.tick_size = tick_size
        self.price_ticks = price_ticks

    @classmethod
    def preflight(cls) -> LivePreflight:
        missing = [name for name in cls.REQUIRED_ENV if not os.getenv(name)]
        partial_creds = [name for name in cls.API_CRED_ENV if os.getenv(name)]
        if partial_creds and len(partial_creds) != len(cls.API_CRED_ENV):
            missing.extend(name for name in cls.API_CRED_ENV if not os.getenv(name))
        armed = os.getenv("POLYBOT_ENABLE_LIVE_TRADING") == "YES"
        if "POLYBOT_ENABLE_LIVE_TRADING" in missing and not armed:
            missing = [name for name in missing if name != "POLYBOT_ENABLE_LIVE_TRADING"] + ["POLYBOT_ENABLE_LIVE_TRADING=YES"]
        try:
            __import__("py_clob_client_v2")
            sdk_available = True
        except ImportError:
            sdk_available = False
        return LivePreflight(
            ready=not missing and sdk_available and armed,
            missing_env=tuple(missing),
            sdk_available=sdk_available,
            armed=armed,
        )

    @classmethod
    def from_env(
        cls,
        runtime: RuntimeConfig,
        confirmed: bool,
        order_type: str = "FAK",
        tick_size: str = "0.01",
        price_ticks: int = 1,
    ) -> "LiveExecutor":
        if not confirmed:
            raise LiveTradingDisabled("live trading requires --i-understand-live-risk")
        preflight = cls.preflight()
        if not preflight.ready:
            missing = ", ".join(preflight.missing_env) or "none"
            sdk = "available" if preflight.sdk_available else "missing py-clob-client-v2"
            raise LiveTradingDisabled(f"live trading preflight failed: missing={missing}; sdk={sdk}")

        import py_clob_client_v2 as sdk

        private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
        explicit_creds = cls._api_creds_from_env(sdk)
        chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        kwargs: dict[str, Any] = {
            "host": runtime.clob_base_url,
            "chain_id": chain_id,
            "key": private_key,
        }
        if explicit_creds is not None:
            kwargs["creds"] = explicit_creds
        signature_type = os.getenv("POLYMARKET_SIGNATURE_TYPE")
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if signature_type:
            kwargs["signature_type"] = int(signature_type)
        if funder:
            kwargs["funder"] = funder
        try:
            client = sdk.ClobClient(**kwargs)
        except TypeError:
            if "funder" not in kwargs:
                raise
            kwargs["funder_address"] = kwargs.pop("funder")
            client = sdk.ClobClient(**kwargs)
        if explicit_creds is None:
            creds = client.create_or_derive_api_key()
            client.set_api_creds(creds)
        return cls(client=client, sdk=sdk, order_type=order_type, tick_size=tick_size, price_ticks=price_ticks)

    @classmethod
    def _api_creds_from_env(cls, sdk: Any):
        values = {name: os.getenv(name) for name in cls.API_CRED_ENV}
        present = {name: value for name, value in values.items() if value}
        if not present:
            return None
        if len(present) != len(cls.API_CRED_ENV):
            missing = ", ".join(name for name in cls.API_CRED_ENV if not values[name])
            raise LiveTradingDisabled(f"partial CLOB API credentials supplied; missing {missing}")
        return sdk.ApiCreds(
            api_key=values["CLOB_API_KEY"],
            api_secret=values["CLOB_SECRET"],
            api_passphrase=values["CLOB_PASS_PHRASE"],
        )

    def execute(self, market: Market, intent: OrderIntent, book: OrderBook, now: float | None = None) -> Fill | None:
        if intent.limit_price <= 0:
            raise LiveExecutionError(f"invalid live limit price for {market.slug}: {intent.limit_price}")
        limit_price = self._live_limit_price(intent, book)
        if limit_price <= 0:
            raise LiveExecutionError(f"invalid live limit price for {market.slug}: {limit_price}")
        if book.best_ask is not None and limit_price < book.best_ask:
            return None
        size = intent.max_notional / limit_price
        order_type = getattr(self.sdk.OrderType, self.order_type)
        response = self.client.create_and_post_order(
            order_args=self.sdk.OrderArgs(
                token_id=intent.token_id,
                price=limit_price,
                side=self.sdk.Side.BUY,
                size=size,
            ),
            options=self.sdk.PartialCreateOrderOptions(tick_size=self.tick_size),
            order_type=order_type,
        )
        if not self._response_success(response):
            return None
        fill_price = self._response_price(response) or limit_price
        filled_size = self._response_size(response)
        filled_notional = self._response_notional(response)
        if filled_size is None and filled_notional is None:
            filled_size = size
            filled_notional = size * fill_price
        elif filled_size is None and filled_notional is not None:
            filled_size = filled_notional / fill_price
        elif filled_size is not None and filled_notional is None:
            filled_notional = filled_size * fill_price
        if filled_size is None or filled_notional is None or filled_size <= 0 or filled_notional <= 0:
            return None
        return Fill(
            market_slug=market.slug,
            condition_id=market.condition_id,
            outcome=intent.outcome,
            token_id=intent.token_id,
            price=filled_notional / filled_size,
            size=filled_size,
            notional=filled_notional,
            timestamp=now or 0.0,
            simulated=False,
            reason=intent.reason,
        )

    def _live_limit_price(self, intent: OrderIntent, book: OrderBook) -> float:
        tick = Decimal(str(self.tick_size))
        strategy_limit = Decimal(str(intent.limit_price))
        if book.best_ask is None:
            raw_limit = strategy_limit
        else:
            ask_limit = Decimal(str(book.best_ask)) + tick * Decimal(self.price_ticks)
            raw_limit = min(strategy_limit, ask_limit)
        floored = (raw_limit / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        return float(min(floored, Decimal("0.999")))

    @staticmethod
    def _response_success(response: Any) -> bool:
        if response is None:
            return False
        if isinstance(response, dict):
            if response.get("success") is False:
                return False
            status = str(response.get("status") or response.get("state") or "").lower()
            if status in {"failed", "rejected", "cancelled", "canceled"}:
                return False
        return True

    @classmethod
    def _response_size(cls, response: Any) -> float | None:
        return cls._response_number(response, ("size_matched", "matched_size", "filled_size", "sizeFilled", "filledSize"))

    @classmethod
    def _response_notional(cls, response: Any) -> float | None:
        return cls._response_number(response, ("filled_notional", "matched_notional", "notional", "filledAmount", "takingAmount"))

    @classmethod
    def _response_price(cls, response: Any) -> float | None:
        return cls._response_number(response, ("average_price", "avg_price", "price", "filledPrice"))

    @classmethod
    def _response_number(cls, response: Any, keys: tuple[str, ...]) -> float | None:
        if not isinstance(response, dict):
            return None
        for key in keys:
            if key not in response:
                continue
            value = response[key]
            if isinstance(value, bool) or value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        for child in ("order", "data"):
            nested = response.get(child)
            if isinstance(nested, dict):
                value = cls._response_number(nested, keys)
                if value is not None:
                    return value
        return None
