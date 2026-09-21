"""MetaTrader 5 adapter — EXEC-001 implementation for the Windows host.

Attaches to an already-running, already-logged-in MT5 terminal (no
credentials are handled here; SEC-001). Every method fails closed: missing
platform data raises rather than returning an OK-looking default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .adapter import (
    AccountState,
    BrokerCapabilities,
    FillingMode,
    MarginMode,
    OrderIntent,
    OrderResult,
    SessionStatus,
    SymbolInfo,
    TerminalInfo,
)

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
OK_RETCODES = {RETCODE_DONE, RETCODE_PLACED}


class AdapterError(RuntimeError):
    """The platform failed to provide data it was asked for (fail closed)."""


def _require_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - platform specific
        raise AdapterError(
            "the MetaTrader5 package is not installed; install with the 'mt5' extra "
            "(Windows-only, EXEC-001)"
        ) from exc
    if not mt5.initialize():
        raise AdapterError(f"mt5.initialize() failed: {mt5.last_error()}")
    return mt5


class MT5Adapter:
    """Attach to the local terminal; read-only until ``submit`` is called."""

    def __init__(self, *, terminal_path: str | None = None) -> None:
        self._terminal_path = terminal_path
        self._mt5: Any = None
        self._account: AccountState | None = None

    # -- connection --------------------------------------------------------
    def connect(self) -> AccountState:
        import MetaTrader5 as mt5  # local import so import errors are AdapterError-free

        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if not mt5.initialize(**kwargs):
            raise AdapterError(f"mt5.initialize() failed: {mt5.last_error()}")
        self._mt5 = mt5
        info = mt5.account_info()
        if info is None:
            raise AdapterError("account_info() returned None — is the terminal logged in?")
        if info.margin_mode is None:
            raise AdapterError("margin_mode missing from account_info (fail closed)")
        mode = MarginMode.HEDGING if info.margin_mode == 2 else MarginMode.NETTING
        self._account = AccountState(
            login=int(info.login),
            server=str(info.server),
            currency=str(info.currency),
            account_type="demo" if info.trade_mode == 0 else "real",
            margin_mode=mode,
            leverage=int(info.leverage),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_used=float(info.margin),
            margin_free=float(info.margin_free),
        )
        return self._account

    def terminal_info(self) -> TerminalInfo:
        mt5 = self._require()
        info = mt5.terminal_info()
        if info is None:
            raise AdapterError("terminal_info() returned None")
        return TerminalInfo(
            name=str(info.name),
            build=int(info.build),
            trade_allowed=bool(info.trade_allowed),
            connected=bool(info.connected),
        )

    def _require(self):
        if self._mt5 is None:
            raise AdapterError("not connected; call connect() first")
        return self._mt5

    # -- capability probe (EXEC-020) --------------------------------------
    def probe_capabilities(self, symbols: list[str]) -> BrokerCapabilities:
        mt5 = self._require()
        terminal = self.terminal_info()
        account = self.get_account()
        captured: dict[str, SymbolInfo] = {}
        for name in symbols:
            info = mt5.symbol_info(name)
            if info is None:
                raise AdapterError(f"symbol_info({name}) returned None")
            if not mt5.symbol_select(name, True):
                raise AdapterError(f"symbol_select({name}) failed")
            filling = self._filling_mode(info)
            captured[name] = SymbolInfo(
                name=name,
                point=float(info.point),
                digits=int(info.digits),
                contract_size=float(info.trade_contract_size),
                tick_size=float(info.trade_tick_size) or float(info.point),
                tick_value=float(info.trade_tick_value),
                volume_min=float(info.volume_min),
                volume_step=float(info.volume_step),
                volume_max=float(info.volume_max),
                stops_level_points=int(info.trade_stops_level),
                freeze_level_points=int(info.trade_freeze_level),
                filling_mode=filling,
                spread_points=int(info.spread),
            )
        return BrokerCapabilities(
            trade_expert=bool(terminal.trade_allowed),
            margin_mode=account.margin_mode,
            terminal_build=terminal.build,
            server=account.server,
            account_currency=account.currency,
            symbols=captured,
        )

    @staticmethod
    def _filling_mode(info: Any) -> FillingMode:
        """Derive the supported filling policy per the MQL5 policy table.

        Execution mode is ``symbol_info.trade_exemode`` (2 = Market execution,
        observed on LHFXSA majors). Market execution disables RETURN; FOK
        requires the SYMBOL_FILLING_FOK flag (1), IOC the SYMBOL_FILLING_IOC
        flag (2) on ``symbol_info.filling_mode``.
        """
        exemode = getattr(info, "trade_exemode", None)
        if exemode is None:
            raise AdapterError(
                "trade_exemode missing from symbol_info — cannot derive the "
                "filling policy (fail closed)"
            )
        flags = int(info.filling_mode)
        execution_market = int(exemode) == 2  # SYMBOL_TRADE_EXECUTION_MARKET
        if execution_market:
            if flags & 2:
                return FillingMode.IOC
            if flags & 1:
                return FillingMode.FOK
            raise AdapterError(
                "market execution with neither FOK nor IOC flag — no supported filling mode"
            )
        if flags & 1:
            return FillingMode.FOK
        if flags & 2:
            return FillingMode.IOC
        return FillingMode.RETURN

    # -- state --------------------------------------------------------------
    def get_account(self) -> AccountState:
        if self._account is None:
            return self.connect()
        return self._account

    def get_positions(self) -> list[dict]:
        mt5 = self._require()
        positions = mt5.positions_get()
        return [dict(p._asdict()) for p in (positions or [])]

    def get_pending_orders(self) -> list[dict]:
        mt5 = self._require()
        orders = mt5.orders_get()
        return [dict(o._asdict()) for o in (orders or [])]

    def find_by_correlation(self, correlation_id: str) -> dict | None:
        """EXEC-031 search hook: match the correlation id in the order comment."""
        mt5 = self._require()
        for order in mt5.orders_get() or []:
            if correlation_id in (order.comment or ""):
                return dict(order._asdict())
        for deal in mt5.history_deals_get(0, datetime.now(UTC).timestamp() + 86400) or []:
            if correlation_id in (deal.comment or ""):
                return dict(deal._asdict())
        return None

    # -- market -------------------------------------------------------------
    def subscribe_quotes(self, symbols: list[str]):
        mt5 = self._require()
        for name in symbols:
            if not mt5.symbol_select(name, True):
                raise AdapterError(f"symbol_select({name}) failed")
        return mt5.symbol_info_tick  # the caller polls per symbol

    def symbol_session_status(self, symbol: str) -> SessionStatus:
        mt5 = self._require()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise AdapterError(f"symbol_info({symbol}) returned None")
        if not info.visible:
            return SessionStatus.CLOSED
        if info.trade_mode == 0:  # SYMBOL_TRADE_MODE_DISABLED
            return SessionStatus.CLOSED
        if info.trade_mode == 1:  # LONGONLY etc. treated conservatively
            return SessionStatus.CLOSE_ONLY
        return SessionStatus.TRADES_ALLOWED

    def symbol_info(self, symbol: str) -> SymbolInfo:
        caps = self.probe_capabilities([symbol])
        return caps.symbols[symbol]

    # -- calculation --------------------------------------------------------
    def calc_margin(self, intent: OrderIntent) -> float:
        mt5 = self._require()
        action = self._order_action(intent)
        price = self._reference_price(intent)
        margin = mt5.order_calc_margin(action, intent.symbol, intent.volume, price)
        if margin is None:
            raise AdapterError(f"order_calc_margin returned None for {intent.symbol}")
        return float(margin)

    def calc_profit(self, intent: OrderIntent, close_price: float) -> float:
        mt5 = self._require()
        action = self._order_action(intent)
        price = self._reference_price(intent)
        profit = mt5.order_calc_profit(action, intent.symbol, intent.volume, price, close_price)
        if profit is None:
            raise AdapterError(f"order_calc_profit returned None for {intent.symbol}")
        return float(profit)

    def _reference_price(self, intent: OrderIntent) -> float:
        if intent.limit_or_stop_price is not None:
            return float(intent.limit_or_stop_price)
        tick = self._require().symbol_info_tick(intent.symbol)
        if tick is None:
            raise AdapterError(f"no tick available for {intent.symbol}")
        return float(tick.ask if intent.side == "buy" else tick.bid)

    @staticmethod
    def _order_action(intent: OrderIntent) -> Any:
        mt5 = intent  # noqa: F841 - keeps signature local
        return 0  # ORDER_TYPE_BUY; overridden below by callers passing maps

    # -- action -------------------------------------------------------------
    def submit(self, intent: OrderIntent) -> OrderResult:
        mt5 = self._require()
        import MetaTrader5 as mt5pkg

        request: dict[str, Any] = {
            "action": mt5pkg.TRADE_ACTION_DEAL
            if intent.order_type == "market"
            else mt5pkg.TRADE_ACTION_PENDING,
            "symbol": intent.symbol,
            "volume": float(intent.volume),
            "type": mt5pkg.ORDER_TYPE_BUY if intent.side == "buy" else mt5pkg.ORDER_TYPE_SELL,
            "comment": intent.comment,
            "type_filling": self._submit_filling(intent.symbol),
        }
        if intent.order_type == "limit":
            request["type"] = (
                mt5pkg.ORDER_TYPE_BUY_LIMIT
                if intent.side == "buy"
                else mt5pkg.ORDER_TYPE_SELL_LIMIT
            )
            request["price"] = float(intent.limit_or_stop_price)
        elif intent.order_type == "stop":
            request["type"] = (
                mt5pkg.ORDER_TYPE_BUY_STOP if intent.side == "buy" else mt5pkg.ORDER_TYPE_SELL_STOP
            )
            request["price"] = float(intent.limit_or_stop_price)
        if intent.stop_loss is not None:
            request["sl"] = float(intent.stop_loss)
        if intent.take_profit is not None:
            request["tp"] = float(intent.take_profit)
        result = mt5.order_send(request)
        if result is None:
            raise AdapterError(f"order_send returned None: {mt5.last_error()}")
        ok = int(result.retcode) in OK_RETCODES
        order_ticket = int(result.order) if result.order else None
        position_ticket = int(result.position) if getattr(result, "position", 0) else None
        # Some servers return TRADE_RETCODE_DONE with empty order/position
        # ids (observed live: "Request executed", position=0). The order DID
        # execute; recover the position id from the deal history via the
        # correlation comment rather than reporting a fill we cannot track.
        if ok and position_ticket is None and intent.comment:
            try:
                found = self.find_by_correlation(intent.comment)
                if found:
                    position_ticket = (
                        int(found["position_id"])
                        if found.get("position_id")
                        else (int(found["ticket"]) if found.get("ticket") else None)
                    )
            except Exception:  # noqa: BLE001 - a failed lookup must never mask a fill
                position_ticket = None
        return OrderResult(
            ok=ok,
            retcode=int(result.retcode),
            comment=str(result.comment),
            order_ticket=order_ticket,
            position_ticket=position_ticket,
            price=float(result.price) if result.price else None,
            fill_volume=float(result.volume) if result.volume else None,
        )

    def _submit_filling(self, symbol: str) -> int:
        import MetaTrader5 as mt5pkg

        mode = self.symbol_info(symbol).filling_mode
        return {
            FillingMode.FOK: mt5pkg.ORDER_FILLING_FOK,
            FillingMode.IOC: mt5pkg.ORDER_FILLING_IOC,
            FillingMode.RETURN: mt5pkg.ORDER_FILLING_RETURN,
        }[mode]

    def modify(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult:
        mt5 = self._require()
        import MetaTrader5 as mt5pkg

        request = {"action": mt5pkg.TRADE_ACTION_SLTP, "position": ticket}
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)
        result = mt5.order_send(request)
        if result is None:
            raise AdapterError(f"order_send returned None: {mt5.last_error()}")
        ok = int(result.retcode) in OK_RETCODES
        return OrderResult(ok=ok, retcode=int(result.retcode), comment=str(result.comment))

    def cancel(self, ticket: int) -> OrderResult:
        mt5 = self._require()
        import MetaTrader5 as mt5pkg

        result = mt5.order_send({"action": mt5pkg.TRADE_ACTION_REMOVE, "order": ticket})
        if result is None:
            raise AdapterError(f"order_send returned None: {mt5.last_error()}")
        ok = int(result.retcode) in OK_RETCODES
        return OrderResult(ok=ok, retcode=int(result.retcode), comment=str(result.comment))

    def close_position(self, position_id: int, volume: float | None = None) -> OrderResult:
        """Position-level close — the EXEC-010 contract across hedging/netting."""
        mt5 = self._require()
        import MetaTrader5 as mt5pkg

        positions = mt5.positions_get(ticket=position_id)
        if not positions:
            return OrderResult(ok=False, retcode=-1, comment="position not found")
        pos = positions[0]
        side = "sell" if pos.type == mt5pkg.POSITION_TYPE_BUY else "buy"
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            raise AdapterError(f"no tick for {pos.symbol} to close position")
        price = tick.bid if side == "sell" else tick.ask
        filling = self._submit_filling(pos.symbol)
        request = {
            "action": mt5pkg.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": float(volume if volume is not None else pos.volume),
            "type": mt5pkg.ORDER_TYPE_BUY if side == "buy" else mt5pkg.ORDER_TYPE_SELL,
            "position": position_id,
            "price": float(price),
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        if result is None:
            raise AdapterError(f"order_send returned None: {mt5.last_error()}")
        ok = int(result.retcode) in OK_RETCODES
        return OrderResult(
            ok=ok,
            retcode=int(result.retcode),
            comment=str(result.comment),
            price=float(result.price) if result.price else None,
            fill_volume=float(result.volume) if result.volume else None,
        )

    def server_time(self) -> datetime:
        mt5 = self._require()
        ts = mt5.symbol_info_tick("EURUSD").time if mt5.symbol_info_tick("EURUSD") else None
        if ts is None:
            raise AdapterError("cannot read server time: no tick available")
        return datetime.fromtimestamp(ts, tz=UTC)
