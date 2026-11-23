#!/usr/bin/env python3
"""批量机械替换引擎：一次调用改完整批文件，替代逐条 edit 造成的几十轮往返。

用法：
    python3 apply_edits.py ops.json --dry-run     # 先看命中规模
    python3 apply_edits.py ops.json               # 实际写入

ops.json 是操作列表，按顺序应用；"file" 可以是路径或 glob（递归展开）：

    [
      {"file": "src/a.py", "old": "旧文本", "new": "新文本"},
      {"file": "src/a.py", "old": "重复片段", "new": "替换片段", "count": "all"},
      {"file": "src/*.py", "old": "本应消失的写法", "count": 0},
      {"file": "src/a.py", "old": "^def (\\\\w+)\\\\(", "new": "def \\\\1\\\\(", "regex": true},
      {"file": "**/*.py", "prepend": "# SPDX-License-Identifier: MIT\\n"}
    ]

语义：
- count 省略 = 1：old 必须恰好出现 1 次；count: "all"：至少 1 次，全部替换；
  count: 0：断言 old 不存在（不写任何东西，只做校验）。
- old == new、命中数不符、文件不存在、glob 无匹配 → 该文件整批不写入（原子性）。
- regex: true 时 old 是正则、new 是 \\1 风格替换串，在 LF 归一化后的文本上执行。
- prepend / append：文件头尾加内容；skip_if_present（默认 true）在同样内容已存在时跳过，
  所以可以重复执行而不产生重复头。
- 行尾：按文件原有风格（LF/CRLF）操作并还原；混合行尾默认拒绝，避免悄悄统一风格。
- .py 写入前 compile 检查，语法错误不落盘。
- 只报告命中数与文件，不打印 diff —— 差异交给 git diff / 校验脚本。

退出码：全部成功 0，有任何失败 1。
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def expand(pattern: str) -> list:
    """展开 glob；没有通配符时原样返回，便于报"文件不存在"。"""
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in glob.glob(pattern, recursive=True) if Path(p).is_file())
    return [pattern]


def eol_style(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "MIXED"
    return "CRLF" if crlf else "LF"


def read_text(path: Path) -> tuple:
    """返回 (LF 归一化文本, 行尾风格, 是否有 BOM)。"""
    raw = path.read_bytes()
    bom = raw.startswith(BOM)
    body = raw[len(BOM) :] if bom else raw
    text = body.decode("utf-8")
    style = eol_style(text)
    return text.replace("\r\n", "\n"), style, bom


def apply_ops(text: str, ops: list) -> tuple:
    """返回 (新文本, 成功说明, 错误列表)。任何一条出错由调用方决定整文件不写。"""
    notes = []
    errors = []

    for op in ops:
        if "prepend" in op or "append" in op:
            kind = "prepend" if "prepend" in op else "append"
            chunk = op[kind].replace("\r\n", "\n")
            if chunk and not chunk.endswith("\n"):
                chunk += "\n"
            if op.get("skip_if_present", True) and chunk.strip() and chunk.strip() in text:
                notes.append(f"{kind} 已存在，跳过")
                continue
            text = chunk + text if kind == "prepend" else text + chunk
            notes.append(f"{kind} {len(chunk)} 字符")
            continue

        old = op["old"].replace("\r\n", "\n")
        expected = op.get("count", 1)
        regex = bool(op.get("regex"))

        if "new" not in op:
            if expected != 0:
                errors.append(f"缺少 new 字段：{old[:60]!r}")
                continue
            hits = len(re.findall(old, text)) if regex else text.count(old)
            if hits:
                errors.append(f"断言不存在，但命中 {hits} 次：{old[:60]!r}")
            continue

        new = op["new"].replace("\r\n", "\n")
        if not regex and old == new:
            errors.append(f"old 与 new 相同，无法定位改动：{old[:60]!r}")
            continue

        if regex:
            hits = sum(1 for _ in re.finditer(old, text))
        else:
            hits = text.count(old)

        ok = hits >= 1 if expected == "all" else hits == expected
        if not ok:
            errors.append(f"期望命中 {expected} 次，实际 {hits} 次：{old[:60]!r}")
            continue

        if regex:
            text, hits = re.subn(old, new, text)
        else:
            text = text.replace(old, new)
        notes.append(f"{hits} 处：{old[:40]!r}")

    return text, notes, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="批量机械替换")
    parser.add_argument("ops", help="操作 JSON 文件")
    parser.add_argument("--dry-run", action="store_true", help="只报告命中数，不写文件")
    parser.add_argument("--allow-mixed-eol", action="store_true", help="允许处理混合行尾文件")
    args = parser.parse_args()

    ops = json.loads(Path(args.ops).read_text(encoding="utf-8"))
    if isinstance(ops, dict) and "ops" in ops:
        ops = ops["ops"]
    if not isinstance(ops, list):
        print("错误：ops.json 顶层必须是列表", file=sys.stderr)
        return 1

    by_file = {}
    unmatched = []
    for op in ops:
        targets = expand(op["file"])
        if not targets:
            unmatched.append(op["file"])
            continue
        for target in targets:
            by_file.setdefault(target, []).append(op)

    failures = len(unmatched)
    for pattern in unmatched:
        print(f"FAIL  {pattern}: glob 没有匹配到任何文件")

    for filename, file_ops in by_file.items():
        path = Path(filename)
        if not path.exists():
            print(f"FAIL  {filename}: 文件不存在")
            failures += 1
            continue

        try:
            text, style, bom = read_text(path)
        except UnicodeDecodeError:
            print(f"FAIL  {filename}: 不是 UTF-8 文本")
            failures += 1
            continue

        if style == "MIXED" and not args.allow_mixed_eol:
            print(f"FAIL  {filename}: 混合行尾，先统一风格再批量改")
            failures += 1
            continue
        if "\r" in text:
            print(f"FAIL  {filename}: 存在裸 CR 行尾，先统一风格")
            failures += 1
            continue

        new_text, notes, errors = apply_ops(text, file_ops)
        if errors:
            failures += 1
            print(f"FAIL  {filename}: {len(errors)} 条不匹配，文件未写入")
            for err in errors:
                print(f"        - {err}")
            continue

        if path.suffix == ".py":
            try:
                compile(new_text, str(path), "exec")
            except SyntaxError as exc:
                failures += 1
                print(f"FAIL  {filename}: 替换后语法错误（第 {exc.lineno} 行），文件未写入")
                continue

        if args.dry_run:
            print(f"DRY   {filename}: {len(notes)} 条全部匹配，行尾 {style}")
            continue

        eol = "\r\n" if style == "CRLF" else "\n"
        payload = new_text.replace("\n", eol).encode("utf-8")
        path.write_bytes((BOM if bom else b"") + payload)
        print(f"OK    {filename}: 应用 {len(notes)} 条，行尾 {style} 不变")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
