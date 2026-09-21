"""Instrument definitions — DATA-002.

``config/instruments.yaml`` carries per symbol: venue symbol name including
suffix, point size, pip size, contract size, tick size, tick value, volume
min/step/max, quote currency, and the ``stops_level``/``freeze_level``
observed at GATE-011. Mutable properties are re-queried before submission
(EXEC-033) — this module is the conversion bridge, not a cache of mutable
values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import yaml

from ..core.units import D, UnitError


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    venue_symbol: str
    point_size: Decimal
    pip_size: Decimal
    contract_size: Decimal
    tick_size: Decimal
    tick_value: Decimal
    quote_currency: str
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    stops_level_points: int
    freeze_level_points: int

    def pips_to_price(self, pips: Decimal) -> Decimal:
        return D(pips) * self.pip_size

    def price_to_pips(self, distance: Decimal) -> Decimal:
        return D(distance) / self.pip_size

    def points_to_price(self, points: Decimal) -> Decimal:
        return D(points) * self.point_size

    def round_volume_down(self, volume: Decimal) -> Decimal:
        steps = (D(volume) / self.volume_step).to_integral_value(rounding=ROUND_FLOOR)
        rounded = steps * self.volume_step
        if rounded < self.volume_min:
            raise UnitError(
                f"rounded volume {rounded} below volume_min {self.volume_min} — reject, "
                "do not round up (ARCH-001)"
            )
        return rounded


def load_instruments(path: Path) -> dict[str, InstrumentSpec]:
    if not path.exists():
        raise FileNotFoundError(f"instruments config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    specs: dict[str, InstrumentSpec] = {}
    for symbol, raw in (data.get("instruments", {}) or {}).items():
        specs[symbol] = InstrumentSpec(
            symbol=symbol,
            venue_symbol=str(raw["venue_symbol"]),
            point_size=D(str(raw["point_size"])),
            pip_size=D(str(raw["pip_size"])),
            contract_size=D(str(raw["contract_size"])),
            tick_size=D(str(raw["tick_size"])),
            tick_value=D(str(raw.get("tick_value", "0"))),
            quote_currency=str(raw["quote_currency"]),
            volume_min=D(str(raw["volume_min"])),
            volume_step=D(str(raw["volume_step"])),
            volume_max=D(str(raw["volume_max"])),
            stops_level_points=int(raw.get("stops_level_points", 0)),
            freeze_level_points=int(raw.get("freeze_level_points", 0)),
        )
    return specs
