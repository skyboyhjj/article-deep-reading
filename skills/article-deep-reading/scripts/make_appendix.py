#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_appendix.py —— 通用"算据附录"装配器

把一份算据脚本**逐字节**嵌入 Markdown 附录（绝不手抄），并内嵌其实跑输出，
最后**抽回代码块与源文件比对**做自校验（不一致就报错退出）。

用法：
    python3 make_appendix.py \
        --src  verify_xxx.py \
        --out  附录_主题_算据脚本.md \
        --title "附录：主题 · 算据脚本" \
        --doc   "读解_主题.md" \
        --deps  "Python 3（仅标准库）" \
        --intro "一句话说明这份算据在做什么、核验哪几组" \
        [--pitfalls "本次踩到的坑与正确做法"]

生成的四节：一、在做什么 / 二、脚本原文 / 三、实跑输出 / 四、已知的坑
"""
import argparse
import datetime
import hashlib
import subprocess
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True, help='算据脚本路径')
    ap.add_argument('--out', required=True, help='输出的附录 md 路径')
    ap.add_argument('--title', required=True)
    ap.add_argument('--doc', required=True, help='配套读解文档名')
    ap.add_argument('--deps', default='Python 3（仅标准库）', help='运行环境/依赖（非纯标准库必须写明）')
    ap.add_argument('--intro', default='（说明这份算据在做什么、核验哪几组。）')
    ap.add_argument('--pitfalls', default='（本次踩到的坑与正确做法。）')
    ap.add_argument('--runner', default='python3')
    ap.add_argument('--timeout', type=int, default=1800)
    a = ap.parse_args()

    src_path = Path(a.src)
    src = src_path.read_text(encoding='utf-8')
    md5 = hashlib.md5(src_path.read_bytes()).hexdigest()

    run = subprocess.run([a.runner, str(src_path)], capture_output=True, text=True, timeout=a.timeout)
    if run.returncode != 0:
        raise SystemExit(f'脚本运行失败（rc={run.returncode}）:\n{run.stderr}')
    out_text = run.stdout

    today = datetime.date.today().isoformat()
    doc_md = f"""# {a.title}

> **配套文档**：`{a.doc}`
> **生成日期**：{today}
> **运行环境**：{a.deps}
> **运行方式**：`{a.runner} {src_path.name}`
> **源文件 md5**：`{md5}`

---

## 一、这份算据在做什么

{a.intro}

---

## 二、脚本原文

```python
{src.strip()}
```

---

## 三、实跑输出

```text
{out_text.strip()}
```

---

## 四、已知的坑（写给将来的自己）

{a.pitfalls}

---

## 五、自校验

- 内嵌脚本与源文件 `{src_path.name}` **逐字节一致**（装配时断言，md5 见上）。
- 实跑输出为脚本 stdout 原文，未人工改写。

---

*附录版本 v1.0（{today}）*
*配套文档：`{a.doc}`*
"""

    out_path = Path(a.out)
    out_path.write_text(doc_md, encoding='utf-8')

    # 自校验：抽回代码块与源文件比对
    text = out_path.read_text(encoding='utf-8')
    block = text.split('```python\n')[1].split('\n```')[0]
    assert block == src.strip(), '嵌入代码块与源文件不一致（可能有转义被吃掉）！'

    print(f'OK 附录已生成: {out_path}')
    print(f'   嵌入代码块 == 源文件 : {block == src.strip()}')
    print(f'   源文件 md5          : {md5}')


if __name__ == '__main__':
    main()
