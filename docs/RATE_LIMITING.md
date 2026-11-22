# 读解服务 · 额度配置方案（滑动窗口 + Retry-After + 热配置）

> 变更文件：`server/ratelimit.py`、`server/app.py`、`web/index.html`、`server/test_ratelimit.py`、`server/test_app.py`
> 落地日期：2026-11-19 · 已线上验证（deepread.hui-skill.cn）

---

## 1. 背景与问题

原限流实现是 **Redis 固定窗口**：`INCR` 计数 + 首次 `EXPIRE 3600`。这带来两个体验问题：

1. **恢复体验差**：额度打满后，必须硬等「首次请求 + 1 小时」那个固定窗口结束，才一次性恢复全部额度。客户端经常出现「额度确实还没恢复（11:52 才到期）」这种提示——用户既不知道还要等多久，也感觉额度被"锁死"了。
2. **无恢复倒计时**：超限只回一句「每小时限 N 次」，用户无法预判何时能再用，只能反复重试碰运气。

本次方案用「滑动窗口 + Retry-After + 热配置」三件套解决，核心目标是 **让额度恢复平滑、可预期、可运维调整**。

---

## 2. 方案设计

### ① 滑动窗口（替代固定窗口）

- 每个 IP 一个 Redis **ZSET**，`score = 请求时间戳`（Unix 秒），成员为 `时间戳:uuid` 保证唯一。
- 每次判定用一段 **Lua 原子脚本** 完成：清窗口外旧记录 → 记本次 → `ZCARD` 计数 → 超限则回滚本次并返回最早一笔的剩余窗口秒数。
- 效果：**每有一笔请求掉出 1 小时窗口，就自动释放 1 次额度**，而非整点一次性释放。额度恢复呈"逐笔平滑"曲线。

### ② Retry-After 头 + retry_in_seconds 字段

- 超限（429）时，响应带 `Retry-After` 头 + JSON 里的 `retry_in_seconds` 字段，值 = 最早一笔记录的剩余窗口秒数。
- 前端据此展示「约 X 分钟后恢复」，用户明确知道何时能再用，不再盲试。

### ③ 额度热配置（dr:cfg:limit）

- 运维可直接 `SET dr:cfg:limit N`（N 为每窗口限额）**即时生效**，无需重启容器；`DEL dr:cfg:limit` 恢复默认。
- 实现：限流器本地缓存配置 5 秒（`CONFIG_TTL`），每次 `allow()` 前解析当前额度；读不到或 Redis 未连接时回退构造默认值（配置非关键，不 fail closed）。
- 429 文案里的次数也读动态额度（`current_limit()`），保证提示与实际限额一致。

---

## 3. 实现细节

### 3.1 限流接口升级为四元组

`allow(ip) -> (是否放行, 剩余次数, 状态, 恢复还需秒数)`

| 状态 | 含义 | retry_after |
|------|------|-------------|
| `ok` | 放行 | 恒为 `None` |
| `exceeded` | 额度用尽 → 429 | 最早一笔的剩余窗口秒数 |
| `unavailable` | 限流服务不可用 → 503 | 恒为 `None` |

### 3.2 Redis 滑动窗口 Lua 脚本（`ratelimit.py` 核心）

```lua
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
```

### 3.3 热配置解析（`ratelimit.py`）

```python
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
```

> 关键坑：`_resolve_limit` 不能有 `if self._client is None: return self.limit` 之类的提前返回——否则冷启动副本的第一次 `allow()` 会跳过配置读取，热配置首次不生效。

### 3.4 各文件改动清单

| 文件 | 改动 |
|------|------|
| `server/ratelimit.py` | 固定窗口 → 滑动窗口（ZSET+Lua）；`allow` 返回四元组；新增 `_resolve_limit`/`current_limit` 热配置 |
| `server/app.py` | `_send`/`_json` 支持透传响应头；新增 `_exceeded`（429 + Retry-After + 动态额度文案）；health 的 `rate_limit_per_hour` 也读动态额度；两处限流调用改为四元组 |
| `web/index.html` | 新增 `fmtWait`；上传/分析错误提示追加「约 X 分钟后恢复」 |
| `server/test_ratelimit.py` | `FakeRedis` 用 Python 复刻 Lua 语义；补滑动窗口/Retry-After/热配置测试 |
| `server/test_app.py` | `Down.allow` 返回四元组 |

---

## 4. 验证结果

- **单元测试**：`python -m unittest server.test_ratelimit server.test_app` → 52 tests OK（含 `test_sliding_window_recovers_gradually`、`test_retry_after_is_oldest_remaining`、`test_hot_config_override`）。
- **线上验证**（deepread.hui-skill.cn，双副本）：

| 操作 | 结果 |
|------|------|
| `SET dr:cfg:limit 0` → analyze | **429**，`retry-after: 3600` 头 + `retry_in_seconds: 3600` 字段 |
| `SET dr:cfg:limit 0`（动态文案） | 文案「每小时限 **0** 次」（此前写死 4） |
| `SET dr:cfg:limit 20` → health | `rate_limit_per_hour: 20`（跟随热配置，`DEL` 后回默认） |
| `DEL dr:cfg:limit` → analyze | **200**，`quota` 事件「本小时剩余 3 次」 |

---

## 5. 部署与运维

### 部署（源码 scp 直传非 git，公网地址已脱敏）

```bash
# 同步变更文件
scp -i ~/.ssh/<server-key>.pem server/ratelimit.py server/app.py root@121.***.***.36:/opt/deepread/article-deep-reading/server/
scp -i ~/.ssh/<server-key>.pem web/index.html root@121.***.***.36:/opt/deepread/article-deep-reading/web/

# 重建（不用 --no-cache，走缓存）
cd /opt/deepread && docker compose -f docker-compose.yml build deepread \
  && docker compose -f docker-compose.yml up -d --scale deepread=2
```

### 运维热配置

```bash
# 调限额为 N（即时生效，无需重启）
docker exec <redis容器> redis-cli SET dr:cfg:limit N

# 恢复默认（删除键）
docker exec <redis容器> redis-cli DEL dr:cfg:limit
```

### 分享场景建议额度

站点面向「分享给同事/群」使用，每 IP 额度不宜低于 **10 次/小时**（一人试两篇 + 分享 2~3 人各试一篇就接近打满）。代码默认值已是 10；线上曾以 `RATE_LIMIT_PER_HOUR=4` 启动，偏紧，可用热配置即时放宽（无需重建容器）：

```bash
docker exec <redis容器> redis-cli SET dr:cfg:limit 20
```

> 注意：热配置即时生效，但**不落盘到 compose**。若用热配置放宽额度，也要同步把 `docker-compose.yml` 的 `RATE_LIMIT_PER_HOUR` 改成同样值——否则重建容器时热配置若失效（如 Redis 键被清/过期），会静默退回 compose 里写死的旧值（如 4），限流又变紧。改额度记住「两处同步」。

### 键与盐（v2.20）

Redis 里的键形如 `dr:rl:<32 位 hex>`，内容是 `sha256(RL_SALT + IP)` 的前 32 位，**不留明文 IP**。`RL_SALT` 由环境变量给出，默认 `deepread-v1`。

轮换盐等于让所有旧键作废，效果等同于「全员额度重置」，所以：

- 平时不用换；
- 真要换，挑低峰，并提前说明「本小时额度会重置，不是故障」。

内存实现同样用哈希做键 —— 崩溃转储和调试打印都可能把内存里的字典带出来。

### 行为语义（重要）

- 校验层（空体 / 超限 / 错扩展名 / 空 payload）在限流**之前**返回，不耗额度。
- 超 50MB 的 JSON 413 也在限流前，不耗额度。
- 上传与 analyze 各计一次额度。
- Redis 连不上 → fail closed 回 **503**（非 429），30 秒冷却后重试，避免每个请求都卡满连接超时。
