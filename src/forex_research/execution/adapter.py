"""Execution adapter contract — EXEC-001, EXEC-010.

One interface, one implementation per platform, so the platform choice changes
one module. The official MetaTrader5 package is Windows-only; a cTrader
implementation would run natively on Linux/macOS (EXEC-001).

``close(ticket)`` alone is insufficient across hedging and netting: the
contract exposes position-level closing and declares which margin mode it is
operating in (EXEC-010).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class MarginMode(StrEnum):
    HEDGING = "hedging"
    NETTING = "netting"


class FillingMode(StrEnum):
    FOK = "fok"
    IOC = "ioc"
    RETURN = "return"


class SessionStatus(StrEnum):
    TRADES_ALLOWED = "trades_allowed"
    CLOSE_ONLY = "close_only"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TerminalInfo:
    """Terminal build, version and permissions (EXEC-010)."""

    name: str
    build: int
    trade_allowed: bool  # the terminal's Algo Trading toggle
    connected: bool


@dataclass(frozen=True)
class AccountState:
    login: int
    server: str
    currency: str
    account_type: str  # demo | contest | real (platform term)
    margin_mode: MarginMode
    leverage: int
    balance: float
    equity: float
    margin_used: float
    margin_free: float


@dataclass(frozen=True)
class SymbolInfo:
    """Mutable symbol properties — re-queried before every submission (EXEC-033)."""

    name: str
    point: float
    digits: int
    contract_size: float
    tick_size: float
    tick_value: float
    volume_min: float
    volume_step: float
    volume_max: float
    stops_level_points: int
    freeze_level_points: int
    filling_mode: FillingMode
    spread_points: int


@dataclass(frozen=True)
class BrokerCapabilities:
    """Persisted probe output — EXEC-020, GATE-011 fields."""

    trade_expert: bool
    margin_mode: MarginMode
    terminal_build: int
    server: str
    account_currency: str
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Fail closed: missing or unexpected values are errors, not warnings."""
        errors: list[str] = []
        if not self.trade_expert:
            errors.append("trade_expert is false — automation disabled on this server")
        if not self.symbols:
            errors.append("no symbol capabilities captured")
        return errors


@dataclass(frozen=True)
class OrderIntent:
    """An order about to be submitted — mirrors STRAT-002 fields it needs."""

    symbol: str
    side: str  # buy | sell
    order_type: str  # market | limit | stop
    volume: float
    limit_or_stop_price: float | None
    stop_loss: float | None
    take_profit: float | None
    comment: str = ""


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    retcode: int
    comment: str
    order_ticket: int | None = None
    position_ticket: int | None = None
    price: float | None = None
    fill_volume: float | None = None


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The adapter contract — EXEC-010."""

    def connect(self) -> AccountState: ...
    def probe_capabilities(self) -> BrokerCapabilities: ...  # EXEC-020
    def terminal_info(self) -> TerminalInfo: ...

    # State
    def get_account(self) -> AccountState: ...
    def get_positions(self) -> list[dict]: ...
    def get_pending_orders(self) -> list[dict]: ...
    def find_by_correlation(self, correlation_id: str) -> dict | None: ...  # EXEC-031

    # Market
    def subscribe_quotes(self, symbols: list[str]) -> object: ...
    def symbol_session_status(self, symbol: str) -> SessionStatus: ...
    def symbol_info(self, symbol: str) -> SymbolInfo: ...  # EXEC-033 re-query

    # Calculation — cross-check, never trust blindly (EXEC-010)
    def calc_margin(self, intent: OrderIntent) -> float: ...
    def calc_profit(self, intent: OrderIntent, close_price: float) -> float: ...

    # Action
    def submit(self, intent: OrderIntent) -> OrderResult: ...
    def modify(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult: ...
    def cancel(self, ticket: int) -> OrderResult: ...
    def close_position(self, position_id: int, volume: float | None = None) -> OrderResult: ...
    def server_time(self) -> datetime: ...
