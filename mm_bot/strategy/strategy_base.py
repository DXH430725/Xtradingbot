from typing import Any


class StrategyBase:
    def start(self, core: Any) -> None:  # optional
        raise NotImplementedError

    def stop(self) -> None:  # optional
        raise NotImplementedError

    async def on_tick(self, now_ms: float):
        raise NotImplementedError

