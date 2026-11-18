"""
SwarmEvolve 报道核验的算据脚本

组 1 速度优势的分解：吞吐倍数 × 样本效率倍数 = 时间等价比
组 2 KV 缓存的标度：450 agents x 40 latent steps 到底要多少显存，前缀共享省了多少
组 3 多样性的来源：45 = 5 x 9 的构造，而不是「随机采了 45 个」

数据来源：
- 论文 arXiv:2611.08483v1（2026-11-05），正文与附录 A.2、D 节
- 基线在 60 分钟内评估 80 到 160 个程序（附录 A.2 原文）
- SwarmEvolve 每小时评估的程序数是基线的 5 到 13 倍（正文）
- 达到基线 60 分钟成绩的中位数 3 到 8 分钟（正文与图注）
一切结论以本脚本实跑输出为准。
"""

import numpy as np

SEP = "=" * 68


def show(t):
    print()
    print(SEP)
    print(t)
    print(SEP)


# ----------------------------------------------------------------------
# 组 1 速度优势的分解
# ----------------------------------------------------------------------
def exp1_decompose():
    show("组 1  时间等价比 = 吞吐倍数 x 样本效率倍数")
    base = (80, 160)          # 基线每小时程序数
    thr = (5, 13)             # 吞吐倍数
    mins = (3.0, 8.0)         # 达到基线 60 分钟成绩的中位分钟数
    print("  基线 60 分钟评估 %d 到 %d 个程序；SwarmEvolve 是其 %d 到 %d 倍"
          % (base[0], base[1], thr[0], thr[1]))
    print("  60 分钟成绩的等价比 = 60 / 分钟数 = %.2f 到 %.2f 倍"
          % (60 / mins[1], 60 / mins[0]))
    print()
    print("  若吞吐与时间各自独立取值，样本效率倍率 = (60/分钟) / 吞吐倍数：")
    print("  %-10s" % "吞吐倍数" + "".join("%-14s" % ("%.1f 分钟" % m) for m in (3, 5.5, 8)))
    grid = {}
    for t in (5, 9, 13):
        line = "  %-10d" % t
        for m in (3, 5.5, 8):
            v = (60.0 / m) / t
            grid[(t, m)] = v
            line += "%-14.3f" % v
        print(line)
    vals = list(grid.values())
    print("  九格区间 %.3f 到 %.3f，九格中位 %.3f" % (min(vals), max(vals), float(np.median(vals))))
    print("  判据：大于 1 说明同等程序数下 SwarmEvolve 的得分更高；小于 1 说明只是跑得多。")
    print("  九格里 5 倍吞吐那一列全部大于 1，13 倍吞吐那一列只有 3 分钟档大于 1；")
    print("  中位 %.2f，跨度 %.2f 到 %.2f —— 方向上偏正，但跨度 7 倍，"
          % (float(np.median(vals)), min(vals), max(vals)))
    print("  说明报道给的四个数不足以确定样本效率；能确定的是速度优势主要来自吞吐。")
    print()
    print("  换算成每小时程序数：%d 到 %d" % (base[0] * thr[0], base[1] * thr[1]))
    print("  每轮产出 10 个新程序（10 个 swarm 各写一个），对应每小时 %.0f 到 %.0f 轮"
          % (base[0] * thr[0] / 10, base[1] * thr[1] / 10))
    print("  每轮耗时 %.1f 到 %.1f 秒" % (3600 / (base[1] * thr[1] / 10), 3600 / (base[0] * thr[0] / 10)))


# ----------------------------------------------------------------------
# 组 2 KV 缓存的标度
# ----------------------------------------------------------------------
def exp2_kv():
    show("组 2  KV 缓存：450 个 agent x 40 步，以及前缀共享为什么是必需的")
    L, H_kv, D_head, BYTES = 32, 8, 128, 2      # 典型 GQA 配置，fp16
    per_tok = 2 * H_kv * D_head * BYTES * L
    print("  按每层 K/V 各 8 个 KV 头、头维 128、fp16、32 层估算：每 token %.0f KB"
          % (per_tok / 1024))
    print("  （此配置为估值，论文未给逐项显存账；结论看标度关系）")

    for agents, steps, label in [(3, 40, "LatentMAS 式小团队"), (45, 40, "单个 swarm"),
                                 (450, 40, "一轮全体")]:
        ntok = agents * steps
        gb = per_tok * ntok / 1024 ** 3
        print("  %-16s %4d agents x %2d 步 = %6d token 位置 %.2f GB" % (label, agents, steps, ntok, gb))

    print()
    print("  前缀（问题描述 + focus + 父代程序）若每个 agent 各存一份：")
    for pre in (1000, 2000, 4000):
        gb_noshare = per_tok * 450 * pre / 1024 ** 3
        gb_share = per_tok * 10 * pre / 1024 ** 3     # 一轮 10 个 swarm，各读一组父代
        print("    前缀 %4d token：不共享 %.1f GB，按 swarm 共享 %.2f GB，差 %.0f 倍"
              % (pre, gb_noshare, gb_share, gb_noshare / gb_share))
    print("  论文原文：Agents that read the same program attend to one shared copy of")
    print("  its KV cache through paged attention. —— 这不是优化项，是 450 个装得下的前提。")


# ----------------------------------------------------------------------
# 组 3 多样性的来源
# ----------------------------------------------------------------------
def exp3_diversity():
    show("组 3  多样性：45 = 5 x 9 的构造，不是随机采样")
    print("  每个 swarm 45 个 agent = 5 种 focus 措辞 x 9 种思考方式，各占一个组合")
    print("  一轮 10 个 swarm，共 %d 个 agent" % (10 * 45))
    print()
    d = 4096
    n = 450
    rng = np.random.default_rng(20261110)
    X = rng.normal(size=(n, d))
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    C = X @ X.T
    iu = np.triu_indices(n, 1)
    cos = C[iu]
    pairs = len(cos)
    sd = 1.0 / np.sqrt(d)
    theory_max = sd * np.sqrt(2 * np.log(pairs))
    print("  在 %d 维空间随机取 %d 个方向（%d 对）：" % (d, n, pairs))
    print("    两两余弦 均值 %.5f，标准差 %.5f（理论 1/sqrt(d) = %.5f）"
          % (cos.mean(), cos.std(), sd))
    print("    绝对值最大 %.4f（理论上限约 sqrt(2 ln 对数) x sd = %.4f）"
          % (np.abs(cos).max(), theory_max))
    print("  读法：纯随机时 450 个方向几乎两两正交，几何上完全不拥挤。")
    print("  所以新颖性过滤不是在腾空间，而是在筛掉「同父代 + 同 focus」造成的相关性。")
    print()
    print("  若两个 agent 真正相关（同父代、同思路），余弦约 0.5 以上，")
    print("  与随机噪声的 %.3f 相差 %.0f 倍 —— 过滤器的判别余量极大。" % (theory_max, 0.5 / theory_max))


if __name__ == "__main__":
    exp1_decompose()
    exp2_kv()
    exp3_diversity()
    print()
    print(SEP)
    print("全部完成")
