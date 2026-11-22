# 读解服务运维手册（详细）

## 服务器与容器拓扑

- 服务器：`root@121.***.***.36`，SSH 密钥 `~/.ssh/<server-key>.pem`
- 部署目录：`/opt/deepread/`（`docker-compose.yml` + `article-deep-reading/` 源码）
- 容器：`deepread-deepread-1`（应用，代码在 `/app/server/`）、`deepread-deepread-redis-1`（限流/缓存）、`ms-nginx`（反代 + TLS）
- 域名：`deepread.hui-skill.cn`
- 上传文件落在容器内 `/tmp/deepread_uploads/`（bind mount 到宿主机卷）

## 诊断脚本模板（卡住类故障）

卡住类故障的定位关键是「容器内直跑核心函数」，打印每步耗时与内存（VmRSS）：

```python
import os, sys, time, shutil, tempfile
sys.path.insert(0, "/app/server")
import media, asr, voice

def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return -1.0

media_path = sys.argv[1]
workdir = tempfile.mkdtemp(prefix="diag_")
try:
    spec = asr.audio_spec()
    t = time.time()
    prep = media.prepare(media_path, workdir, max_sec=spec["max_sec"], fmt=spec["fmt"])
    print(f"prepare={time.time()-t:.1f}s parts={len(prep['parts'])} rss={rss_mb():.1f}MB", flush=True)

    t = time.time()
    out = asr.transcribe_parts(prep["parts"], prep["seg_sec"])
    print(f"transcribe={time.time()-t:.1f}s chars={len(out['text'])} rss={rss_mb():.1f}MB", flush=True)

    t = time.time()
    ac = voice._acoustic_many(prep["parts"])   # 最容易 OOM 的一步
    print(f"acoustic={time.time()-t:.1f}s ok={ac.get('ok')} rss={rss_mb():.1f}MB", flush=True)
finally:
    shutil.rmtree(workdir, ignore_errors=True)
```

投放方式：`scp` 到 `/tmp/` → `docker cp /tmp/_diag.py deepread-deepread-1:/app/server/` → `docker exec deepread-deepread-1 python3 /app/server/_diag.py <视频路径>`。

**注意**：诊断脚本必须覆盖「疑似 OOM 的那一步」（本例是 `voice._acoustic_many`），只测 ASR/ffmpeg 会漏掉真凶。

## 查 OOM 与请求断点

```bash
# 内核 OOM 记录（进程被 cgroup 杀掉会在这里留痕）
dmesg | grep -iE 'oom|killed process' | tail

# nginx 请求在哪一步断（740 字节 = 只输出了开头几个 SSE 事件）
docker exec ms-nginx sh -c 'grep analyze /var/log/nginx/access.log | tail'

# 容器是否被 OOM/重启
docker inspect deepread-deepread-1 --format 'OOMKilled={{.State.OOMKilled}} Restarts={{.RestartCount}}'
```

## SSH/PowerShell 引号细节

- SSH 单引号命令内**禁用双引号**（会被 PowerShell 吃掉，导致远端 bash 语法错）。
- 外层双引号、内层单引号（`ssh ... "docker ps; echo '----'"`）可行。
- `python -c '...'` 内嵌双引号会被破坏 → 改用脚本文件。
- `curl -d '{"url":"..."}'` 的双引号转义会破坏 JSON → 用单引号或数据文件。
- `*` 通配符在 SSH 里不展开。
- **最可靠**：本地写脚本 → `scp` → `docker cp` → `docker exec`。

## 部署命令

```bash
# 1. 直传源码（服务器 git 可能停在旧版本，且直连 github 常失败）
scp -i ~/.ssh/<server-key>.pem server/voice.py root@121.***.***.36:/opt/deepread/article-deep-reading/server/

# 2. 重建镜像（国内必须传阿里云镜像，否则 pypi 14 kB/s 卡死）
ssh -i ~/.ssh/<server-key>.pem root@121.***.***.36 \
  "cd /opt/deepread && docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ deepread && docker compose up -d deepread"

# 3. 验证（502 在容器刚重启的几秒是正常的，等 5s 再 curl）
curl -s https://deepread.hui-skill.cn/api/health
```

## 限流一致性

- 热配置（即时生效，不重启）：`docker exec deepread-deepread-redis-1 redis-cli SET dr:cfg:limit 20`
- 恢复默认：`docker exec deepread-deepread-redis-1 redis-cli DEL dr:cfg:limit`
- **改额度要两处同步**：热配置 + `docker-compose.yml` 的 `RATE_LIMIT_PER_HOUR`，否则重建容器时热配置若失效会退回 compose 旧值。
