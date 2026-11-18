"""限流：进程内存或 Redis 固定窗口，Redis 不可用时 fail closed。

为什么多副本必须 Redis：进程内存各算各的，实际额度是 N × 限额。
为什么不可用要 fail closed：回退内存等于悄悄放宽，与「降级必须显式」相反。
为什么要区分「额度用尽」与「服务不可用」：都回 429 会让用户以为自己用超了。

两种实现共用一个接口：

    allow(ip) -> (是否放行, 剩余次数, 状态)

状态只有三个值：
    "ok"           放行
    "exceeded"     额度用尽，上层回 429
    "unavailable"  限流服务本身不可用，上层回 503

选哪个实现由环境变量 `REDIS_URL` 决定：没设就用内存（本地开发、CI 无需 Redis），
设了就用 Redis（多副本部署）。注意「没设 Redis」与「Redis 连不上」是两件事：
前者是模式，后者是故障。
"""
from __future__ import annotations

import os
import threading
import time

WINDOW = 3600
# Redis 故障后不再重试的冷却秒数：否则每个请求都卡满连接超时
COOLDOWN = 30


class RateLimiterError(RuntimeError):
    """限流服务不可用。"""


def _now() -> float:
    return time.time()


class MemoryRateLimiter:
    """单进程内存固定窗口。只用于本地开发与测试 —— 多副本下额度会翻 N 倍。"""

    def __init__(self, limit: int, window: int = WINDOW):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, ip: str) -> tuple[bool, int, str]:
        now = _now()
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[ip] = hits
                return False, 0, "exceeded"
            hits.append(now)
            self._hits[ip] = hits
            return True, self.limit - len(hits), "ok"


class RedisRateLimiter:
    """Redis 固定窗口：INCR 计数 + 首次 EXPIRE。

    - 全部键都带 TTL，配合独立实例的 noeviction，不会被淘汰归零
    - 任何异常（连不上、超时、协议错）→ fail closed，返回 unavailable
    - 故障后冷却 COOLDOWN 秒不再尝试，避免每个请求都卡满超时
    """

    def __init__(self, url: str, limit: int, window: int = WINDOW,
                 prefix: str = "dr:rl:", client=None, connect_timeout: float = 1.5):
        self.url = url
        self.limit = limit
        self.window = window
        self.prefix = prefix
        self.connect_timeout = connect_timeout
        self._client = client
        # 注入的客户端由调用方负责生命周期，故障后不清空（否则测试里换掉 fail 也恢复不了）
        self._injected = client is not None
        self._lock = threading.Lock()
        self._down_until = 0.0

    # ---- 连接 ----
    def _conn(self):
        if self._client is not None:
            return self._client
        import redis  # 延迟导入：本地没装 redis 也不影响内存模式
        self._client = redis.Redis.from_url(
            self.url,
            socket_connect_timeout=self.connect_timeout,
            socket_timeout=self.connect_timeout,
            decode_responses=True,
        )
        return self._client

    def _trip(self) -> None:
        with self._lock:
            self._down_until = _now() + COOLDOWN

    def _tripped(self) -> bool:
        with self._lock:
            return _now() < self._down_until

    # ---- 计数 ----
    def allow(self, ip: str) -> tuple[bool, int, str]:
        if self._tripped():
            return False, 0, "unavailable"
        key = f"{self.prefix}{ip}"
        try:
            c = self._conn()
            n = int(c.incr(key))
            if n == 1:
                c.expire(key, self.window)
            if n > self.limit:
                return False, 0, "exceeded"
            return True, max(0, self.limit - n), "ok"
        except Exception:      # noqa: BLE001  连接/超时/协议错一律当不可用
            if not self._injected:
                self._client = None
            self._trip()
            return False, 0, "unavailable"

    def available(self) -> bool:
        """探活：给 /api/health 与运维脚本用。"""
        if self._tripped():
            return False
        try:
            self._conn().ping()
            return True
        except Exception:      # noqa: BLE001
            if not self._injected:
                self._client = None
            self._trip()
            return False


def build_limiter(limit: int | None = None, url: str | None = None):
    """按环境变量选实现。REDIS_URL 为空 = 内存模式（单机），非空 = Redis 模式。"""
    if limit is None:
        limit = int(os.environ.get("RATE_LIMIT_PER_HOUR", "10"))
    if url is None:
        url = os.environ.get("REDIS_URL", "").strip()
    if url:
        return RedisRateLimiter(url, limit)
    return MemoryRateLimiter(limit)
