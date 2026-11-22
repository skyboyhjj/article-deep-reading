---
name: deepread-ops
description: article-deep-reading 服务的排障与部署手册。用于部署上线、排查读解卡住/网络错误/OOM、处理限流/SSE/内存等运维问题。不适用于读解流程本身，那用 article-deep-reading 技能。
---

# 读解服务排障与部署

## 这个技能解决什么

`article-deep-reading` 是零框架标准库 Web 服务（SSE 流式返回，五阶段读解）。它有两类「不报错但抓瞎」的运维难点：**卡住类故障**（前端停在某阶段，服务端进程已死或不输出）和**部署链路**（国内服务器直连 pypi/github 慢、SSH 引号反复踩坑）。本技能把踩过的坑和定位手法记下来，下次照着走，不重 debug。

## 排障总流程

症状只有三类，先对号入座：

| 症状 | 最可能的原因 | 第一查 |
| :-- | :-- | :-- |
| 前端停在「转写中」等某阶段 | 服务端进程被 OOM 杀，或转写后的后处理卡住 | `dmesg` 查 OOM；容器内直跑对应函数 |
| 浏览器报 `ERR_HTTP2_PROTOCOL_ERROR` / network error | SSE 用了 HTTP/2 禁用的头 | nginx 日志 + SSE 响应头 |
| 上传反复 429 | 限流额度或键配置 | health 的 `rate_limit_per_hour` + Redis 热配置 |

**核心手法：容器内直跑 vs 线上全链路，差异就是卡点。**

1. 把「疑似卡住的函数」单独写成诊断脚本，`docker cp` 进容器，`docker exec` 直跑，打印每步耗时 + `/proc/self/status` 的 VmRSS。
2. 容器内直跑正常、线上卡 → 卡点在 SSE 输出，或转写**之后**的后处理（声学画像/校对），不是 ASR/ffmpeg。
3. 进程「没了」而非「卡住」 → 查内核 `dmesg | grep -iE 'oom|killed process'`，找 `Memory cgroup out of memory: Killed process ...`。

详细命令与诊断脚本模板见 `references/playbook.md`。

## 五大已知坑

### 1. SSE + HTTP/2：禁 chunked，用 Connection: close

`Transfer-Encoding: chunked` 在 HTTP/2 下被禁止，nginx 反代会触发 `ERR_HTTP2_PROTOCOL_ERROR`。修法：SSE 响应头改 `Connection: close`，裸写响应体，靠连接关闭定界。nginx 把上游转 HTTP/1.1 时会自动加 chunked，浏览器不再看到该头。

### 2. mem_limit 256m + numpy 大数组 → OOM（视频卡「转写」的根因）

`voice.py` 的声学画像（语速/静音/基频）用 fancy indexing 拼 `(n×win)` 索引矩阵再取帧。长视频（>110s 切成多片）时 `idx`(int64) + `frames`(float32) + `frames**2` 三份临时数组峰值约 210MB，撞破容器 256MB 上限被 cgroup OOM 杀。进程一死连接即断，前端停在「转写中」。

修法：`numpy.lib.stride_tricks.as_strided` 步长视图取重叠帧（零拷贝），峰值降到约 70MB。**教训**：诊断脚本若只测 prepare+transcribe 而不测声学画像，会漏掉真正的 OOM 点。

### 3. PowerShell + SSH 引号陷阱

SSH 单引号命令内嵌套双引号会被吃掉；`python -c` 内双引号被破坏；`curl -d` 双引号转义会破坏 JSON。**最可靠**：本地写 Python 脚本文件 → scp → `docker cp` → `docker exec`。别在命令行里内联复杂引号。

### 4. 国内服务器 pip/github 直连慢

pypi.org 直连约 14 kB/s，numpy 15.7MB 会卡十几分钟。重建镜像时传 `--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`（Dockerfile 已支持该 ARG）。

### 5. 限流热配置与 compose 环境变量会不一致

`docker exec <redis容器> redis-cli SET dr:cfg:limit 20` 是**热配置**，即时生效但**不落盘到 compose**。若 compose 里 `RATE_LIMIT_PER_HOUR` 仍是 4，重建容器时热配置若失效会静默退回 4。**改限流要两处同步**：compose 环境变量 + 热配置。

## 部署速查

```bash
# 直传源码（非 git，服务器 git 可能停在旧版本且直连 github 常失败）
scp -i ~/.ssh/<server-key>.pem server/voice.py root@121.***.***.36:/opt/deepread/article-deep-reading/server/

# 重建 + 起容器（国内传阿里云镜像）
cd /opt/deepread && docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ deepread \
  && docker compose up -d deepread

# 验证（502 在容器刚重启的几秒是正常的，等 5s 再 curl）
curl -s https://deepread.hui-skill.cn/api/health
```

完整命令、SSH 引号细节、诊断脚本模板见 `references/playbook.md`。

## 参考

- `references/playbook.md` —— 诊断脚本模板、OOM/断点排查命令、SSH 引号细节、部署命令与容器拓扑
