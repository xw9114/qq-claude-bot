import asyncio
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger
from nonebot.matcher import current_event


config = get_driver().config


def _read_interval(name: str, default: float) -> float:
    value = getattr(config, name, default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        logger.warning("配置 {}={} 无效，使用默认值 {}", name.upper(), value, default)
        return default


GLOBAL_SEND_INTERVAL = _read_interval("bot_global_send_interval", 1.0)
GROUP_SEND_INTERVAL = _read_interval("bot_group_send_interval", 3.0)
USER_SEND_COOLDOWN = _read_interval("bot_user_send_cooldown", 5.0)


@dataclass(slots=True)
class SendRequest:
    operation: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    group_id: str | None
    user_id: str | None


class OutboundRateLimiter:
    """串行执行发送 API，并按全局、群和用户维度控制发送间隔。"""

    def __init__(
        self,
        global_interval: float,
        group_interval: float,
        user_cooldown: float,
    ) -> None:
        self.global_interval = max(0.0, global_interval)
        self.group_interval = max(0.0, group_interval)
        self.user_cooldown = max(0.0, user_cooldown)
        self._queue: asyncio.Queue[SendRequest] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._last_global_send = 0.0
        self._last_group_send: dict[str, float] = {}
        self._last_user_send: dict[str, float] = {}

    def calculate_delay(
        self,
        now: float,
        group_id: str | None,
        user_id: str | None,
    ) -> float:
        deadlines = [self._last_global_send + self.global_interval]
        if group_id is not None and group_id in self._last_group_send:
            deadlines.append(self._last_group_send[group_id] + self.group_interval)
        if user_id is not None and user_id in self._last_user_send:
            deadlines.append(self._last_user_send[user_id] + self.user_cooldown)
        return max(0.0, max(deadlines) - now)

    def _ensure_worker(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._worker_loop(), name="global-send-queue"
            )

    async def submit(
        self,
        operation: Callable[[], Awaitable[Any]],
        group_id: str | None,
        user_id: str | None,
    ) -> Any:
        self._ensure_worker()
        assert self._queue is not None

        future = asyncio.get_running_loop().create_future()
        await self._queue.put(SendRequest(operation, future, group_id, user_id))
        return await future

    async def _worker_loop(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()

        while True:
            request = await self._queue.get()
            try:
                if request.future.cancelled():
                    continue

                delay = self.calculate_delay(
                    loop.time(), request.group_id, request.user_id
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                sent_at = loop.time()
                self._last_global_send = sent_at
                if request.group_id is not None:
                    self._last_group_send[request.group_id] = sent_at
                if request.user_id is not None:
                    self._last_user_send[request.user_id] = sent_at

                result = await request.operation()
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as error:
                if not request.future.done():
                    request.future.set_exception(error)
            else:
                if not request.future.done():
                    request.future.set_result(result)
            finally:
                self._queue.task_done()

    async def shutdown(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None


def _normalize_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def resolve_send_context(data: dict[str, Any]) -> tuple[str | None, str | None]:
    group_id = _normalize_id(data.get("group_id"))
    user_id = _normalize_id(data.get("user_id"))
    event = current_event.get(None)

    if event is not None:
        if group_id is None:
            group_id = _normalize_id(getattr(event, "group_id", None))
        if user_id is None:
            user_id = _normalize_id(getattr(event, "user_id", None))

    return group_id, user_id


send_limiter = OutboundRateLimiter(
    GLOBAL_SEND_INTERVAL,
    GROUP_SEND_INTERVAL,
    USER_SEND_COOLDOWN,
)

_original_call_api = Bot.call_api


@wraps(_original_call_api)
async def _rate_limited_call_api(bot: Bot, api: str, **data: Any) -> Any:
    if not api.startswith("send_"):
        return await _original_call_api(bot, api, **data)

    group_id, user_id = resolve_send_context(data)

    async def operation() -> Any:
        return await _original_call_api(bot, api, **data)

    return await send_limiter.submit(operation, group_id, user_id)


Bot.call_api = _rate_limited_call_api  # type: ignore[method-assign]


@get_driver().on_startup
async def log_rate_limiter_config() -> None:
    logger.info(
        "全局发送队列已启用：全局间隔 {} 秒，同群间隔 {} 秒，同用户冷却 {} 秒",
        GLOBAL_SEND_INTERVAL,
        GROUP_SEND_INTERVAL,
        USER_SEND_COOLDOWN,
    )


@get_driver().on_shutdown
async def shutdown_rate_limiter() -> None:
    await send_limiter.shutdown()
