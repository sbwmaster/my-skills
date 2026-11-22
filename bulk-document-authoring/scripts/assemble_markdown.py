#!/usr/bin/env python3
"""把按单元拆分的 Markdown 片段拼装成长文档。

目录约定：
    <根目录>/_front.md          可选，封面、元信息、前言
    <根目录>/_chapters/chNN.md  正文单元，每篇以 "## ..." 开头
    <根目录>/_appendix/*.md     可选，附录
    <根目录>/<输出名>           拼装结果

目录从各单元的二级、三级标题自动生成，因此不会与正文脱节。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")
TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() + "\n"


def build_toc(units) -> str:
    lines = ["## 目录", ""]
    for _, body in units:
        for line in body.splitlines():
            if line.startswith("## "):
                lines.append("- " + line[3:].strip())
            elif line.startswith("### "):
                lines.append("  - " + line[4:].strip())
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="文档根目录")
    ap.add_argument("--out", default="output.md", help="输出文件名（位于根目录内）")
    ap.add_argument("--front", default="_front.md", help="封面文件名")
    ap.add_argument("--chapters", default="_chapters", help="正文章节所在目录名")
    ap.add_argument("--chapter-glob", default="ch*.md", help="章节文件匹配模式")
    ap.add_argument("--appendix", default="_appendix", help="附录目录名")
    ap.add_argument("--appendix-glob", default="*.md", help="附录文件匹配模式")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    ch_dir = root / args.chapters
    if not ch_dir.is_dir():
        print("错误：找不到章节目录 %s" % ch_dir, file=sys.stderr)
        return 2

    chapters = [(p, read(p)) for p in sorted(ch_dir.glob(args.chapter_glob))]
    if not chapters:
        print("错误：%s 下没有匹配 %s 的章节文件" % (ch_dir, args.chapter_glob), file=sys.stderr)
        return 2

    ap_dir = root / args.appendix
    appendices = []
    if ap_dir.is_dir():
        appendices = [(p, read(p)) for p in sorted(ap_dir.glob(args.appendix_glob))]

    front_path = root / args.front
    front = read(front_path) if front_path.is_file() else ""

    parts = []
    if front:
        parts.append(front)
    parts.append(build_toc(chapters))
    parts.extend(body for _, body in chapters)
    if appendices:
        parts.append("## 附录\n")
        parts.extend(body for _, body in appendices)

    doc = "\n\n---\n\n".join(p.strip() for p in parts) + "\n"
    out = root / args.out
    out.write_text(doc, encoding="utf-8")

    body_cjk = sum(len(CJK.findall(body)) for _, body in chapters + appendices)
    all_cjk = len(CJK.findall(doc))
    tables = sum(1 for line in doc.splitlines() if TABLE_SEP.match(line))
    h2 = sum(1 for line in doc.splitlines() if line.startswith("## "))
    h3 = sum(1 for line in doc.splitlines() if line.startswith("### "))
    print("已写入：%s" % out)
    print("单元：%d 章 + %d 篇附录" % (len(chapters), len(appendices)))
    print("正文字数：%s | 全文字数：%s | 字符数：%s" % (format(body_cjk, ","), format(all_cjk, ","), format(len(doc), ",")))
    print("行数：%s | 二级标题：%d | 三级标题：%d | 表格：%d" % (format(len(doc.splitlines()), ","), h2, h3, tables))
    print("文件大小：%s 字节" % format(out.stat().st_size, ","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
