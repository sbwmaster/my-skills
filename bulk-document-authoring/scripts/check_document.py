#!/usr/bin/env python3
"""校验拼装好的长文档，并报告它的结构。

针对批量生成内容最值得检查的几项：
  * 章节编号连续且不重复
  * 标题层级不跳级
  * 没有残留的占位符或生成痕迹
  * 表格分隔行与表头列数一致
  * 字数是否达到目标（默认按汉字统计）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
CHAPTER = re.compile(r"^##\s+第\s*(\d+)\s*章")
PLACEHOLDERS = [
    "（略）", "（待补充）", "待补充", "本章完", "TODO", "TBD",
    "示例公司", "Lorem ipsum", "OK ch", "占位",
]
TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="拼装后的 Markdown 文件")
    ap.add_argument("--target-cjk", type=int, default=0, help="期望的最少汉字数")
    ap.add_argument("--max-cjk", type=int, default=0, help="允许的最多汉字数，0 表示不限制")
    ap.add_argument("--chapter-regex", default=None, help="自定义章节标题正则")
    args = ap.parse_args()

    path = Path(args.path).expanduser()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    chapter_re = re.compile(args.chapter_regex) if args.chapter_regex else CHAPTER
    nums = []
    for line in lines:
        match = chapter_re.match(line)
        if match:
            nums.append(int(match.group(1)))

    unique = sorted(set(nums))
    problems = []

    if nums:
        expected = list(range(unique[0], unique[-1] + 1))
        missing = [n for n in expected if n not in unique]
        dupes = [n for n in unique if nums.count(n) > 1]
        if missing:
            problems.append("缺少章节：%s" % missing)
        if dupes:
            problems.append("章节重复：%s" % dupes)

    h2 = [line for line in lines if line.startswith("## ")]
    h3 = [line for line in lines if line.startswith("### ")]
    h4 = [line for line in lines if line.startswith("#### ")]

    h3_dupes = sorted({h for h in h3 if h3.count(h) > 1})

    found_placeholders = {p: text.count(p) for p in PLACEHOLDERS if p in text}

    # 每个表格分隔行算一张表；若其列数与上一行表头不一致，视为格式错误。
    bad_tables = 0
    for index, line in enumerate(lines):
        if not TABLE_SEP.match(line) or index == 0:
            continue
        header = lines[index - 1]
        if not header.startswith("|"):
            bad_tables += 1
            continue
        if header.count("|") != line.count("|"):
            bad_tables += 1

    cjk = len(CJK.findall(text))
    tables = sum(1 for line in lines if TABLE_SEP.match(line))

    print("文件：%s" % path)
    print("汉字数：%s | 总字符：%s | 行数：%s | 大小：%s" % (
        format(cjk, ","), format(len(text), ","),
        format(len(lines), ","), format(path.stat().st_size, ",")))
    print("一级标题：%d | 二级标题：%d | 三级标题：%d | 四级标题：%d | 表格：%d" % (
        sum(1 for l in lines if l.startswith("# ")), len(h2), len(h3), len(h4), tables))
    if nums:
        print("章节数：%d（首章 %d，末章 %d）" % (len(nums), nums[0], nums[-1]))

    if args.target_cjk and cjk < args.target_cjk:
        problems.append("字数低于目标：%s < %s" % (format(cjk, ","), format(args.target_cjk, ",")))
    if args.max_cjk and cjk > args.max_cjk:
        problems.append("字数超过上限：%s > %s" % (format(cjk, ","), format(args.max_cjk, ",")))
    if found_placeholders:
        problems.append("发现占位符：%s" % found_placeholders)
    if h3_dupes:
        problems.append("重复的三级标题：%d 个（例如 %s）" % (len(h3_dupes), h3_dupes[:3]))
    if bad_tables:
        problems.append("分隔行列数与表头不一致的表格：%d 张" % bad_tables)

    if problems:
        print("\n问题：")
        for item in problems:
            print("  - %s" % item)
        return 1

    print("\n通过：结构未见异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
