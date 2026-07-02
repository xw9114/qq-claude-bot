"""命令级会话冷却工具。"""

import math
import time
from collections.abc import Callable
from typing import Any


class SessionCooldown:
    def __init__(
        self,
        seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = max(0.0, seconds)
        self._clock = clock
        self._last_run: dict[tuple[Any, ...], float] = {}

    def retry_after(self, key: tuple[Any, ...]) -> int | None:
        if self.seconds <= 0:
            return None

        now = self._clock()
        last_run = self._last_run.get(key)
        if last_run is not None:
            next_run_at = last_run + self.seconds
            if now < next_run_at:
                return max(1, math.ceil(next_run_at - now))

        self._last_run[key] = now
        return None
