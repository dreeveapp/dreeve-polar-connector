"""Weight sync's policy: which Polar weights reach Dreeve, under which date, and when.

Polar only ever reports the current weight, so this is a change detector, not a history copy. It runs
once per cycle, after the exercises, and costs one Polar request. The rules, each tested on its own:

- A default or estimated weight is not a reading and is never sent.
- A weight is sent only when it differs from the last one Dreeve accepted, at Polar's 0.1 kg precision.
  The date never decides that on its own: editing resting heart rate in Polar bumps `modified`, and
  filing the unchanged weight under that new date would add an entry nobody measured.
- The date is when the value took effect in Polar, in the connector's local time zone (`TZ`), because
  Dreeve files a weight under a calendar day and applies it from that day on.
- Dreeve failing never fails the cycle: the weight is simply sent again next time. Polar's cycle-fatal
  errors (rate limit, rejected token, missing consent) stay cycle-fatal, exactly as for exercises.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from enum import StrEnum
from typing import Protocol

from dreeve_polar_connector.config import Config, WeightUnit
from dreeve_polar_connector.dreeve import DreeveError
from dreeve_polar_connector.polar import ConnectionFailed, PolarClient, UnexpectedResponse
from dreeve_polar_connector.weight_state import PushedWeight, WeightState

logger = logging.getLogger(__name__)

# Polar's way of saying "we have no real reading": a profile default, or a value it derived itself.
NOT_A_MEASUREMENT = frozenset({"SOURCE_DEFAULT", "SOURCE_ESTIMATE"})
POUNDS_PER_KILOGRAM = 2.20462262185


class WeightOutcome(StrEnum):
    PUSHED = "pushed"
    UNCHANGED = "unchanged"
    NOT_MEASURED = "not-measured"
    NO_DATA = "no-data"
    FAILED = "failed"
    DRY_RUN = "dry-run"


@dataclass(frozen=True)
class WeightResult:
    outcome: WeightOutcome
    on: date | None = None
    # In DREEVE_WEIGHT_UNIT, as Dreeve stores it.
    value: float | None = None
    error: str | None = None


class WeightRecorder(Protocol):
    """The sliver of Dreeve weight sync depends on. `DreeveClient` is the real one."""

    async def record_weight(self, on: date, weight: float) -> None: ...


class WeightSync:
    def __init__(
        self,
        config: Config,
        polar: PolarClient,
        dreeve: WeightRecorder,
        state: WeightState,
        zone: tzinfo | None = None,
    ) -> None:
        self._config = config
        self._polar = polar
        self._dreeve = dreeve
        self._state = state
        # None is the process's local zone, which `TZ` sets in the container.
        self._zone = zone
        self._last_error: str | None = None

    async def run_once(self, started_at: datetime) -> WeightResult:
        """Raises only Polar's cycle-fatal errors; every other failure is a `FAILED` result."""
        try:
            weight = await self._polar.current_weight()
        except (ConnectionFailed, UnexpectedResponse) as exception:
            return self._failed(f"Could not read the weight from Polar: {exception}")

        if weight is None:
            return WeightResult(WeightOutcome.NO_DATA)

        if weight.source in NOT_A_MEASUREMENT:
            logger.debug("Polar's weight is %s, not a reading; not sending it to Dreeve", weight.source)
            return WeightResult(WeightOutcome.NOT_MEASURED)

        kilograms = round(weight.kilograms, 1)
        last = self._state.last_pushed
        if last is not None and round(last.kilograms, 1) == kilograms:
            return WeightResult(WeightOutcome.UNCHANGED, on=last.on, value=self._for_dreeve(kilograms))

        on = weight.effective_at.astimezone(self._zone).date()
        value = self._for_dreeve(kilograms)
        unit = self._config.dreeve_weight_unit.value

        if self._config.dry_run:
            logger.info("Would record weight %s %s for %s in Dreeve", value, unit, on)
            return WeightResult(WeightOutcome.DRY_RUN, on=on, value=value)

        try:
            await self._dreeve.record_weight(on, value)
        except DreeveError as exception:
            return self._failed(str(exception))

        self._state.record_push(PushedWeight(on=on, kilograms=kilograms, source=weight.source, pushed_at=started_at))
        await self._state.flush()
        self._last_error = None
        logger.info("Recorded weight %s %s for %s in Dreeve", value, unit, on)

        return WeightResult(WeightOutcome.PUSHED, on=on, value=value)

    def _for_dreeve(self, kilograms: float) -> float:
        """Dreeve reads the number in its Appearance unit system, which DREEVE_WEIGHT_UNIT mirrors."""
        if self._config.dreeve_weight_unit is WeightUnit.LB:
            return round(kilograms * POUNDS_PER_KILOGRAM, 1)

        return kilograms

    def _failed(self, message: str) -> WeightResult:
        # Once per distinct problem: a Dreeve that is down for a day must not log every cycle.
        if message != self._last_error:
            logger.warning("Weight not recorded in Dreeve; trying again next cycle: %s", message)
            self._last_error = message
        else:
            logger.debug("Weight still not recorded in Dreeve: %s", message)

        return WeightResult(WeightOutcome.FAILED, error=message)
