#!/usr/bin/env python3
"""限流器的单元测试：两种实现同一个接口，重点验「三态」与「fail closed」。

为什么不只测内存实现：部署是多副本 + Redis，而**Redis 挂掉时的行为**才是
最容易出事的地方 —— 回退内存等于悄悄把额度放大 N 倍。所以这里用假客户端
把「连不上」这条路也压一遍，且不准它吞掉异常后装作正常。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ratelimit  # noqa: E402


class FakeRedis:
    """够用的假客户端：只实现限流用到的那几个命令。"""

    def __init__(self, fail=False):
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail
        self.calls = 0

    def incr(self, key):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, seconds):
        if self.fail:
            raise ConnectionError("redis down")
        self.ttl[key] = seconds
        return True

    def ping(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        return True


class TestMemory(unittest.TestCase):
    def test_limit_and_remaining(self):
        lim = ratelimit.MemoryRateLimiter(2)
        self.assertEqual(lim.allow("1.1.1.1"), (True, 1, "ok"))
        self.assertEqual(lim.allow("1.1.1.1"), (True, 0, "ok"))
        ok, left, state = lim.allow("1.1.1.1")
        self.assertFalse(ok)
        self.assertEqual((left, state), (0, "exceeded"))

    def test_ips_are_independent(self):
        lim = ratelimit.MemoryRateLimiter(1)
        self.assertTrue(lim.allow("1.1.1.1")[0])
        self.assertTrue(lim.allow("2.2.2.2")[0])
        self.assertFalse(lim.allow("1.1.1.1")[0])

    def test_reset(self):
        lim = ratelimit.MemoryRateLimiter(1)
        lim.allow("1.1.1.1")
        lim.reset()
        self.assertTrue(lim.allow("1.1.1.1")[0])

    def test_window_expires(self):
        fake = [1_000_000.0]
        lim = ratelimit.MemoryRateLimiter(1)
        with mock.patch.object(ratelimit, "_now", lambda: fake[0]):
            self.assertTrue(lim.allow("1.1.1.1")[0])
            self.assertFalse(lim.allow("1.1.1.1")[0])
            fake[0] += 3601
            self.assertTrue(lim.allow("1.1.1.1")[0])

    def test_memory_never_unavailable(self):
        """内存实现没有「服务不可用」这一态 —— 它只在单机模式下用。"""
        lim = ratelimit.MemoryRateLimiter(0)
        self.assertFalse(lim.allow("1.1.1.1")[0])
        self.assertNotEqual(lim.allow("1.1.1.1")[2], "unavailable")


class TestRedis(unittest.TestCase):
    def make(self, limit=2, fail=False):
        fake = FakeRedis(fail=fail)
        return ratelimit.RedisRateLimiter("redis://x/0", limit, client=fake), fake

    def test_counts_and_sets_ttl_on_first(self):
        lim, fake = self.make(limit=2)
        self.assertEqual(lim.allow("1.1.1.1"), (True, 1, "ok"))
        self.assertEqual(fake.store["dr:rl:1.1.1.1"], 1)
        self.assertEqual(fake.ttl["dr:rl:1.1.1.1"], 3600)
        self.assertEqual(lim.allow("1.1.1.1"), (True, 0, "ok"))
        self.assertEqual(lim.allow("1.1.1.1"), (False, 0, "exceeded"))

    def test_ttl_only_on_first_increment(self):
        """每次 INCR 都 EXPIRE 会把窗口不断延后，等于永不重置。"""
        lim, fake = self.make(limit=5)
        lim.allow("1.1.1.1")
        fake.ttl.clear()
        lim.allow("1.1.1.1")
        self.assertEqual(fake.ttl, {})

    def test_prefix_and_key(self):
        lim, fake = self.make()
        lim.allow("8.8.4.4")
        self.assertIn("dr:rl:8.8.4.4", fake.store)

    def test_failure_is_unavailable_not_exceeded(self):
        """连不上必须报 unavailable —— 报 exceeded 会让用户以为自己用超了。"""
        lim, _ = self.make(fail=True)
        self.assertEqual(lim.allow("1.1.1.1"), (False, 0, "unavailable"))

    def test_breaker_stops_hammering(self):
        """故障后冷却期内不再重试，否则每个请求都卡满连接超时。"""
        fake = FakeRedis(fail=True)
        lim = ratelimit.RedisRateLimiter("redis://x/0", 2, client=fake)
        lim.allow("1.1.1.1")
        first_calls = fake.calls
        for _ in range(5):
            self.assertEqual(lim.allow("1.1.1.1")[2], "unavailable")
        self.assertEqual(fake.calls, first_calls, "冷却期内不应再打 Redis")

    def test_breaker_recovers(self):
        fake = FakeRedis(fail=True)
        lim = ratelimit.RedisRateLimiter("redis://x/0", 2, client=fake)
        now = [1_000_000.0]
        with mock.patch.object(ratelimit, "_now", lambda: now[0]):
            lim.allow("1.1.1.1")          # 触发熔断
            fake.fail = False             # Redis 恢复了
            self.assertEqual(lim.allow("1.1.1.1")[2], "unavailable")   # 仍在冷却期
            now[0] += ratelimit.COOLDOWN + 1
            self.assertEqual(lim.allow("1.1.1.1")[2], "ok")            # 冷却结束，恢复

    def test_available_true_false(self):
        lim, _ = self.make()
        self.assertTrue(lim.available())
        lim2, _ = self.make(fail=True)
        self.assertFalse(lim2.available())


class TestBuild(unittest.TestCase):
    def test_no_url_means_memory(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
            lim = ratelimit.build_limiter(limit=3)
        self.assertIsInstance(lim, ratelimit.MemoryRateLimiter)
        self.assertEqual(lim.limit, 3)

    def test_url_means_redis(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": "redis://r:6379/0"}, clear=False):
            lim = ratelimit.build_limiter(limit=4)
        self.assertIsInstance(lim, ratelimit.RedisRateLimiter)
        self.assertEqual(lim.limit, 4)

    def test_limit_from_env(self):
        with mock.patch.dict(os.environ, {"REDIS_URL": "", "RATE_LIMIT_PER_HOUR": "7"}, clear=False):
            self.assertEqual(ratelimit.build_limiter().limit, 7)


if __name__ == "__main__":
    unittest.main()
