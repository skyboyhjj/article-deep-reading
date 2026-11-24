"""限流：进程内存或 Redis 滑动窗口，Redis 不可用时 fail closed。

为什么多副本必须 Redis：进程内存各算各的，实际额度是 N × 限额。
为什么不可用要 fail closed：回退内存等于悄悄放宽，与「降级必须显式」相反。
为什么要区分「额度用尽」与「服务不可用」：都回 429 会让用户以为自己用超了。
为什么用滑动窗口而非固定窗口：固定窗口（首次请求 + 1 小时一次性恢复）会导致
「打满后硬等一整点才恢复」；滑动窗口每有一笔掉出窗口就回 1 次额度，体验平滑。

两种实现共用一个接口：

    allow(ip) -> (是否放行, 剩余次数, 状态, 恢复还需秒数)

状态只有三个值：
    "ok"           放行（retry_after 恒为 None）
    "exceeded"     额度用尽，上层回 429（retry_after = 最早一笔的剩余窗口秒数）
    "unavailable"  限流服务本身不可用，上层回 503（retry_after 恒为 None）

选哪个实现由环境变量 `REDIS_URL` 决定：没设就用内存（本地开发、CI 无需 Redis），
设了就用 Redis（多副本部署）。注意「没设 Redis」与「Redis 连不上」是两件事：
前者是模式，后者是故障。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid

WINDOW = 3600
# Redis 故障后不再重试的冷却秒数：否则每个请求都卡满连接超时
COOLDOWN = 30
# 额度热配置：运维 SET dr:cfg:limit N 即时生效，无需重启；本地缓存 CONFIG_TTL 秒
CONFIG_KEY = "dr:cfg:limit"
CONFIG_TTL = 5.0
# 键里的 IP 一律先哈希：Redis 里不该留明文地址。换盐 = 旧键全部作废，
# 效果等同于「全员额度重置」，这一点写进 docs/RATE_LIMITING.md 的运维须知。
RL_SALT = os.environ.get("RL_SALT", "deepread-v1")


def _hash_ip(ip: str) -> str:
    """IP → 不可反推的稳定键。同一 IP 同键（限流才有效），不同 IP 不同键。"""
    return hashlib.sha256((RL_SALT + str(ip)).encode()).hexdigest()[:32]


class RateLimiterError(RuntimeError):
    """限流服务不可用。"""
    code = "DR-3003"


def _now() -> float:
    return time.time()


class MemoryRateLimiter:
    """单进程内存滑动窗口。只用于本地开发与测试 —— 多副本下额度会翻 N 倍。"""

    def __init__(self, limit: int, window: int = WINDOW):
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, ip: str) -> tuple[bool, int, str, float | None]:
        now = _now()
        k = _hash_ip(ip)        # 内存里也不留明文 IP（崩溃转储、调试打印都可能带出来）
        with self._lock:
            hits = [t for t in self._hits.get(k, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[k] = hits
                retry = (hits[0] + self.window - now) if hits else self.window
                return False, 0, "exceeded", retry
            hits.append(now)
            self._hits[k] = hits
            return True, self.limit - len(hits), "ok", None

    def current_limit(self) -> int:
        """当前生效额度（内存模式即构造值），给 429 文案用。"""
        return self.limit


class RedisRateLimiter:
    """Redis 滑动窗口：ZSET 记时间戳 + Lua 原子判定。

    - 每个 IP 一个 ZSET，score = 请求时间戳；每次调用先清窗口外、再记本次、
      再 ZCARD 计数；超限则回滚本次并返回最早一笔的剩余窗口秒数（Retry-After）
    - 全部键都带 TTL，配合独立实例的 noeviction，不会被淘汰归零
    - 任何异常（连不上、超时、协议错）→ fail closed，返回 unavailable
    - 故障后冷却 COOLDOWN 秒不再尝试，避免每个请求都卡满超时
    - 额度可热配置：SET dr:cfg:limit N 即时生效（本地缓存 CONFIG_TTL 秒，配置非关键不 fail closed）
    """

    _LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
redis.call('ZADD', key, now, member)
local count = redis.call('ZCARD', key)
if count > limit then
    redis.call('ZREM', key, member)
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry = 0
    if oldest[2] then retry = tonumber(oldest[2]) + window - now end
    redis.call('PEXPIRE', key, window * 1000)
    return {0, 0, retry}
end
redis.call('PEXPIRE', key, window * 1000)
return {1, limit - count, 0}
"""

    def __init__(self, url: str, limit: int, window: int = WINDOW,
                 prefix: str = "dr:rl:", config_key: str = CONFIG_KEY,
                 config_ttl: float = CONFIG_TTL, client=None, connect_timeout: float = 1.5):
        self.url = url
        self.limit = limit
        self.window = window
        self.prefix = prefix
        self.config_key = config_key
        self.config_ttl = config_ttl
        self.connect_timeout = connect_timeout
        self._client = client
        # 注入的客户端由调用方负责生命周期，故障后不清空（否则测试里换掉 fail 也恢复不了）
        self._injected = client is not None
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._cfg_limit: int | None = None
        self._cfg_at = 0.0

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

    # ---- 热配置 ----
    def _resolve_limit(self) -> int:
        """读 Redis 里的额度配置；读不到/未连接就用构造值（配置非关键，不 fail closed）。"""
        now = _now()
        if self._cfg_at and now - self._cfg_at < self.config_ttl:
            return self._cfg_limit if self._cfg_limit is not None else self.limit
        try:
            v = self._conn().get(self.config_key)
            self._cfg_limit = int(v) if v is not None else None
        except Exception:      # noqa: BLE001
            self._cfg_limit = None
        self._cfg_at = now
        return self._cfg_limit if self._cfg_limit is not None else self.limit

    def current_limit(self) -> int:
        """当前生效额度（含热配置覆盖值），给 429 文案用。"""
        return self._resolve_limit()

    # ---- 计数 ----
    def allow(self, ip: str) -> tuple[bool, int, str, float | None]:
        if self._tripped():
            return False, 0, "unavailable", None
        now = _now()
        key = f"{self.prefix}{_hash_ip(ip)}"
        limit = self._resolve_limit()
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        try:
            c = self._conn()
            res = c.eval(self._LUA, 1, key, now, self.window, limit, member)
            allowed, remaining, retry = int(res[0]), int(res[1]), float(res[2])
            if not allowed:
                return False, 0, "exceeded", retry if retry > 0 else self.window
            return True, remaining, "ok", None
        except Exception:      # noqa: BLE001  连接/超时/协议错一律当不可用
            if not self._injected:
                self._client = None
            self._trip()
            return False, 0, "unavailable", None

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
