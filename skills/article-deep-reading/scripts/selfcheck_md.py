#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck_md.py —— Markdown 交付前"四查"

查四样，都是本类文档最常翻车的地方：
  1) 表格行竖线一致性（裸 | 会拆行；`\\|` 是转义、不算分隔符）
  2) 粗体 ** 定界符（顺序配对 + CommonMark flanking 判定）
  3) 行内公式 $ 配对（每行 $ 个数为偶）
  4) 禁用 emoji（只允许 ★ ✓ ✗；PDF 字体栈无 emoji 字形，会静默消失）

前 3 项先对**行内代码 `...` 掩码**（代码里的 | ** $ 不是结构字符）；第 4 项走原文（代码块也会被渲染）。

用法：
    python3 selfcheck_md.py <file.md> [<file2.md> ...]
退出码：0 = 全清；1 = 有可疑项。

附：若同级往上能找到 math-doc-rigor/scripts/check_md.py，会自动一并跑（需在沙箱）。
"""
import re
import sys
import subprocess
from pathlib import Path

# 允许出现的符号（有字形）；其余 emoji 一律禁用（PDF 字体栈无字形会静默消失）
ALLOWED_SYMBOLS = set('★✓✗')
EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF'      # 各种 emoji / 象形
    '\U0001F1E6-\U0001F1FF'       # 区域指示符（国旗）
    '\u2B00-\u2BFF'               # 含 ⭐(U+2B50)
    '\u2600-\u26FF'               # 含 ⚠(U+26A0)
    '\u2700-\u27BF'               # 含 ✅(U+2705) ❌(U+274C)
    '\uFE0F'                      # 变体选择符（强制 emoji 呈现）
    '\u20E3]')                    # 键帽


def check_one(path: Path):
    lines = path.read_text(encoding='utf-8').split('\n')
    incode = False
    bad = []
    for i, l in enumerate(lines, 1):
        if l.strip().startswith('```'):
            incode = not incode
            continue
        if incode:
            continue
        # 掩码行内代码 `...`（等长占位 "x"，保位置）：代码里的 | ** $ 都不是结构字符
        l = re.sub(r'`[^`]*`', lambda m: 'x' * len(m.group(0)), l)

        # 1) 表格竖线
        s = l.strip()
        if s.startswith('|'):
            n = len(re.findall(r'(?<!\\)\|', s))
            cols = len(s.strip('|').split('|'))
            if n != cols + 1:
                bad.append(f'  L{i}: 表格竖线 {n} ≠ 期望 {cols + 1}  -> {s[:70]}')

        # 2) 粗体定界符（顺序配对）
        pos = [m.start() for m in re.finditer(r'\*\*', l)]
        if len(pos) % 2:
            bad.append(f'  L{i}: 粗体 ** 个数为奇（{len(pos)}）')

        def ws(c):
            return c.isspace()

        def punc(c):
            return (not c.isalnum()) and (not c.isspace())

        for a, b in zip(pos[0::2], pos[1::2]):
            pre = l[a - 1] if a > 0 else ' '
            po = l[a + 2] if a + 2 < len(l) else ' '
            pc = l[b - 1] if b > 0 else ' '
            p2 = l[b + 2] if b + 2 < len(l) else ' '
            left = (not ws(po)) and ((not punc(po)) or ws(pre) or punc(pre))
            right = (not ws(pc)) and ((not punc(pc)) or ws(p2) or punc(p2))
            if not (left and right):
                bad.append(f'  L{i}: 粗体断裂  ...{l[max(0, a - 14):b + 16]}...')

        # 3) 行内公式 $ 配对
        if l.count('$') % 2:
            bad.append(f'  L{i}: $ 不配对（{l.count("$")} 个）  -> {l[:70]}')

        # 4) emoji（只允许 ★ ✓ ✗）
        for c in EMOJI_RE.findall(l):
            if c not in ALLOWED_SYMBOLS:
                bad.append(f'  L{i}: 禁用 emoji U+{ord(c):04X}  -> {l[:70]}')
                break

    return bad


def find_check_md():
    """从本脚本位置向上查找 math-doc-rigor/scripts/check_md.py，再退回当前目录。

    不写死任何绝对路径：本包解压在哪，就从哪找。
    """
    here = Path(__file__).resolve().parent
    for up in [here] + list(here.parents)[:5]:
        cand = up / 'math-doc-rigor' / 'scripts' / 'check_md.py'
        if cand.exists():
            return cand
    cwd = Path.cwd()
    if cwd.exists():
        for p in cwd.rglob('check_md.py'):
            if 'math-doc-rigor' in str(p):
                return p
    return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    ok = True
    for arg in argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f'[skip] 不存在: {p}')
            continue
        bad = check_one(p)
        if bad:
            ok = False
            print(f'[!] {p.name}  {len(bad)} 处可疑：')
            for b in bad[:20]:
                print(b)
        else:
            print(f'[ok] {p.name}  四查全清')

    cm = find_check_md()
    if cm:
        print(f'--- 追加 check_md.py ({cm}) ---')
        for arg in argv[1:]:
            if Path(arg).exists():
                r = subprocess.run(['python3', str(cm), arg], capture_output=True, text=True)
                tail = (r.stdout or r.stderr).strip().split('\n')[-3:]
                print('  ' + ' | '.join(t.strip() for t in tail if t.strip()))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
