from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Literal

from .models import Fill, Market, Position, Signal


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def get(self, market: Market) -> Position:
        if market.slug not in self._positions:
            self._positions[market.slug] = Position(market.slug, market.condition_id)
        return self._positions[market.slug]

    def add_fill(self, fill: Fill) -> Position:
        if fill.market_slug not in self._positions:
            self._positions[fill.market_slug] = Position(fill.market_slug, fill.condition_id)
        position = self._positions[fill.market_slug]
        position.add_fill(fill)
        return position

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())


class SQLiteLedger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def close(self) -> None:
        self.conn.close()

    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                slug TEXT PRIMARY KEY,
                condition_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                title TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                up_token TEXT NOT NULL,
                down_token TEXT NOT NULL,
                resolution_source TEXT,
                price_to_beat REAL,
                first_seen_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug TEXT NOT NULL,
                timestamp REAL NOT NULL,
                probability_up REAL NOT NULL,
                confidence REAL NOT NULL,
                direction TEXT NOT NULL,
                seconds_elapsed REAL NOT NULL,
                seconds_remaining REAL NOT NULL,
                distance_bps REAL NOT NULL,
                momentum_bps REAL NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                token_id TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                notional REAL NOT NULL,
                timestamp REAL NOT NULL,
                simulated INTEGER NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_slug TEXT NOT NULL,
                outcome TEXT NOT NULL,
                token_id TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                bid_depth REAL NOT NULL,
                ask_depth REAL NOT NULL,
                timestamp REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resolutions (
                market_slug TEXT PRIMARY KEY,
                winner TEXT NOT NULL,
                result_json TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            """
        )
        self.conn.commit()

    def record_market(self, market: Market, timestamp: float) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO markets (
                slug, condition_id, asset, title, start_ts, end_ts, up_token, down_token,
                resolution_source, price_to_beat, first_seen_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market.slug,
                market.condition_id,
                market.asset,
                market.title,
                market.start_ts,
                market.end_ts,
                market.up_token,
                market.down_token,
                market.resolution_source,
                market.price_to_beat,
                timestamp,
            ),
        )
        self.conn.commit()

    def record_signal(self, signal: Signal, timestamp: float) -> None:
        self.conn.execute(
            """
            INSERT INTO signals (
                market_slug, timestamp, probability_up, confidence, direction,
                seconds_elapsed, seconds_remaining, distance_bps, momentum_bps, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.market_slug,
                timestamp,
                signal.probability_up,
                signal.confidence,
                signal.direction,
                signal.seconds_elapsed,
                signal.seconds_remaining,
                signal.distance_bps,
                signal.momentum_bps,
                signal.reason,
            ),
        )
        self.conn.commit()

    def record_fill(self, fill: Fill) -> None:
        self.conn.execute(
            """
            INSERT INTO fills (
                market_slug, condition_id, outcome, token_id, price, size,
                notional, timestamp, simulated, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.market_slug,
                fill.condition_id,
                fill.outcome,
                fill.token_id,
                fill.price,
                fill.size,
                fill.notional,
                fill.timestamp,
                1 if fill.simulated else 0,
                fill.reason,
            ),
        )
        self.conn.commit()

    def record_orderbooks(self, market: Market, books: dict[Literal["Up", "Down"], object], timestamp: float) -> None:
        rows = []
        for outcome, book in books.items():
            rows.append(
                (
                    market.slug,
                    outcome,
                    book.token_id,
                    book.best_bid,
                    book.best_ask,
                    sum(level.size for level in book.bids),
                    sum(level.size for level in book.asks),
                    timestamp,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO orderbook_snapshots (
                market_slug, outcome, token_id, best_bid, best_ask, bid_depth, ask_depth, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()

    def record_resolution(self, position: Position, winner: Literal["Up", "Down"], timestamp: float) -> dict[str, object]:
        result = position.resolve(winner)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO resolutions (market_slug, winner, result_json, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (position.market_slug, winner, json.dumps(result), timestamp),
        )
        self.conn.commit()
        return result

    def is_resolved(self, market_slug: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM resolutions WHERE market_slug = ? LIMIT 1",
            (market_slug,),
        ).fetchone()
        return row is not None

    def get_unresolved_positions(self) -> list[Position]:
        rows = self.conn.execute(
            """
            SELECT f.market_slug, f.condition_id, f.outcome, f.price, f.size, f.notional
            FROM fills f
            LEFT JOIN resolutions r ON r.market_slug = f.market_slug
            WHERE r.market_slug IS NULL
            ORDER BY f.timestamp
            """
        ).fetchall()
        positions: dict[str, Position] = {}
        for row in rows:
            position = positions.setdefault(row["market_slug"], Position(row["market_slug"], row["condition_id"]))
            position.shares[row["outcome"]] += row["size"]
            position.cost[row["outcome"]] += row["notional"]
        return list(positions.values())

    def daily_report(self, day_prefix: str | None = None) -> dict[str, object]:
        where = ""
        params: tuple[object, ...] = ()
        if day_prefix:
            where = "WHERE datetime(r.timestamp, 'unixepoch') LIKE ?"
            params = (f"{day_prefix}%",)
        rows = self.conn.execute(
            f"""
            SELECT r.market_slug, r.winner, r.result_json, r.timestamp
            FROM resolutions r
            {where}
            ORDER BY r.timestamp
            """,
            params,
        ).fetchall()
        markets = []
        total_pnl = 0.0
        total_cost = 0.0
        wins = 0
        losses = 0
        for row in rows:
            result = json.loads(row["result_json"])
            pnl = float(result["pnl"])
            cost = float(result["total_cost"])
            total_pnl += pnl
            total_cost += cost
            wins += pnl > 0
            losses += pnl < 0
            markets.append(
                {
                    "market_slug": row["market_slug"],
                    "winner": row["winner"],
                    "pnl": pnl,
                    "cost": cost,
                    "paired_pnl": float(result["paired_pnl"]),
                    "unpaired_side": result["unpaired_side"],
                    "unpaired_pnl": float(result["unpaired_pnl"]),
                    "up_shares": float(result["up_shares"]),
                    "down_shares": float(result["down_shares"]),
                }
            )
        return {
            "markets": markets,
            "summary": {
                "resolved_markets": len(markets),
                "wins": wins,
                "losses": losses,
                "total_pnl": total_pnl,
                "total_cost": total_cost,
                "roi": total_pnl / total_cost if total_cost else 0.0,
            },
        }

    def realized_pnl(self) -> float:
        rows = self.conn.execute("SELECT result_json FROM resolutions").fetchall()
        return sum(float(json.loads(row["result_json"])["pnl"]) for row in rows)


class CsvLedger:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if self.path.suffix:
            self.path = self.path.with_suffix("")
        self.path.mkdir(parents=True, exist_ok=True)
        self.files = {
            "markets": self.path / "markets.csv",
            "signals": self.path / "signals.csv",
            "fills": self.path / "fills.csv",
            "orderbook_snapshots": self.path / "orderbook_snapshots.csv",
            "resolutions": self.path / "resolutions.csv",
        }
        self.headers = {
            "markets": ["slug", "condition_id", "asset", "title", "start_ts", "end_ts", "up_token", "down_token", "resolution_source", "price_to_beat", "first_seen_ts"],
            "signals": ["market_slug", "timestamp", "probability_up", "confidence", "direction", "seconds_elapsed", "seconds_remaining", "distance_bps", "momentum_bps", "reason"],
            "fills": ["market_slug", "condition_id", "outcome", "token_id", "price", "size", "notional", "timestamp", "simulated", "reason"],
            "orderbook_snapshots": ["market_slug", "outcome", "token_id", "best_bid", "best_ask", "bid_depth", "ask_depth", "timestamp"],
            "resolutions": ["market_slug", "winner", "result_json", "timestamp"],
        }
        for name, file in self.files.items():
            if not file.exists():
                self._append(name, [])

    def close(self) -> None:
        return None

    def _append(self, name: str, rows: list[dict[str, object]]) -> None:
        file = self.files[name]
        exists = file.exists()
        with file.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.headers[name])
            if not exists:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _read(self, name: str) -> list[dict[str, str]]:
        file = self.files[name]
        if not file.exists():
            return []
        with file.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def record_market(self, market: Market, timestamp: float) -> None:
        if any(row["slug"] == market.slug for row in self._read("markets")):
            return
        self._append("markets", [{
            "slug": market.slug,
            "condition_id": market.condition_id,
            "asset": market.asset,
            "title": market.title,
            "start_ts": market.start_ts,
            "end_ts": market.end_ts,
            "up_token": market.up_token,
            "down_token": market.down_token,
            "resolution_source": market.resolution_source,
            "price_to_beat": market.price_to_beat if market.price_to_beat is not None else "",
            "first_seen_ts": timestamp,
        }])

    def record_signal(self, signal: Signal, timestamp: float) -> None:
        self._append("signals", [{
            "market_slug": signal.market_slug,
            "timestamp": timestamp,
            "probability_up": signal.probability_up,
            "confidence": signal.confidence,
            "direction": signal.direction,
            "seconds_elapsed": signal.seconds_elapsed,
            "seconds_remaining": signal.seconds_remaining,
            "distance_bps": signal.distance_bps,
            "momentum_bps": signal.momentum_bps,
            "reason": signal.reason,
        }])

    def record_fill(self, fill: Fill) -> None:
        self._append("fills", [{
            "market_slug": fill.market_slug,
            "condition_id": fill.condition_id,
            "outcome": fill.outcome,
            "token_id": fill.token_id,
            "price": fill.price,
            "size": fill.size,
            "notional": fill.notional,
            "timestamp": fill.timestamp,
            "simulated": 1 if fill.simulated else 0,
            "reason": fill.reason,
        }])

    def record_orderbooks(self, market: Market, books: dict[Literal["Up", "Down"], object], timestamp: float) -> None:
        rows = []
        for outcome, book in books.items():
            rows.append({
                "market_slug": market.slug,
                "outcome": outcome,
                "token_id": book.token_id,
                "best_bid": book.best_bid if book.best_bid is not None else "",
                "best_ask": book.best_ask if book.best_ask is not None else "",
                "bid_depth": sum(level.size for level in book.bids),
                "ask_depth": sum(level.size for level in book.asks),
                "timestamp": timestamp,
            })
        self._append("orderbook_snapshots", rows)

    def record_resolution(self, position: Position, winner: Literal["Up", "Down"], timestamp: float) -> dict[str, object]:
        result = position.resolve(winner)
        existing = [row for row in self._read("resolutions") if row["market_slug"] != position.market_slug]
        self.files["resolutions"].unlink(missing_ok=True)
        self._append("resolutions", existing + [{
            "market_slug": position.market_slug,
            "winner": winner,
            "result_json": json.dumps(result),
            "timestamp": timestamp,
        }])
        return result

    def is_resolved(self, market_slug: str) -> bool:
        return any(row["market_slug"] == market_slug for row in self._read("resolutions"))

    def get_unresolved_positions(self) -> list[Position]:
        resolved = {row["market_slug"] for row in self._read("resolutions")}
        positions: dict[str, Position] = {}
        for row in self._read("fills"):
            if row["market_slug"] in resolved:
                continue
            position = positions.setdefault(row["market_slug"], Position(row["market_slug"], row["condition_id"]))
            outcome = row["outcome"]
            position.shares[outcome] += float(row["size"])
            position.cost[outcome] += float(row["notional"])
        return list(positions.values())

    def daily_report(self, day_prefix: str | None = None) -> dict[str, object]:
        rows = self._read("resolutions")
        markets = []
        total_pnl = 0.0
        total_cost = 0.0
        wins = 0
        losses = 0
        for row in rows:
            timestamp = float(row["timestamp"])
            if day_prefix:
                import datetime as dt
                if not dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().startswith(day_prefix):
                    continue
            result = json.loads(row["result_json"])
            pnl = float(result["pnl"])
            cost = float(result["total_cost"])
            total_pnl += pnl
            total_cost += cost
            wins += pnl > 0
            losses += pnl < 0
            markets.append({
                "market_slug": row["market_slug"],
                "winner": row["winner"],
                "pnl": pnl,
                "cost": cost,
                "paired_pnl": float(result["paired_pnl"]),
                "unpaired_side": result["unpaired_side"],
                "unpaired_pnl": float(result["unpaired_pnl"]),
                "up_shares": float(result["up_shares"]),
                "down_shares": float(result["down_shares"]),
            })
        return {
            "markets": markets,
            "summary": {
                "resolved_markets": len(markets),
                "wins": wins,
                "losses": losses,
                "total_pnl": total_pnl,
                "total_cost": total_cost,
                "roi": total_pnl / total_cost if total_cost else 0.0,
            },
        }

    def realized_pnl(self) -> float:
        return sum(float(json.loads(row["result_json"])["pnl"]) for row in self._read("resolutions"))
