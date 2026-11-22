#!/usr/bin/env python3
"""从 DeepSeek Harness 的会话日志中统计 token 消耗。

每个会话是一个 zstd 压缩的 JSONL 文件。一条 `assistant/message` 记录就是一次
模型调用，带 `usage` 字段：
    inputTokens / outputTokens / cacheReadTokens / cacheWriteTokens

总 token = 输入 + 输出 + 缓存读取 + 缓存写入。缓存读取通常占大头，它按输入
token 的一小部分计费，所以总 token 会高估实际费用；应当比较不同阶段，而不是
绝对数值。

用法：
    python token_report.py                       # 最近 6 小时，按会话列出
    python token_report.py --since 2026-11-22T16:00:00+08:00
    python token_report.py --top 10 --quiet      # 只看最贵的前 10 个会话的汇总
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def parse_time(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


def decompress(path):
    try:
        return subprocess.run(["zstd", "-dc", path], capture_output=True, check=True).stdout
    except FileNotFoundError:
        import zstandard  # type: ignore

        with open(path, "rb") as fh:
            return zstandard.ZstdDecompressor().stream_reader(fh).read()


def scan(path):
    raw = decompress(path)
    info = {
        "session": os.path.basename(os.path.dirname(path)),
        "started": None,
        "calls": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "model": None,
    }
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "session":
            info["started"] = event.get("createdAt")
            info["session"] = event.get("id") or info["session"]
        elif kind == "assistant/message":
            data = event.get("data") or {}
            usage = data.get("usage") or {}
            if not usage:
                continue
            info["calls"] += 1
            info["input"] += usage.get("inputTokens", 0)
            info["output"] += usage.get("outputTokens", 0)
            info["cache_read"] += usage.get("cacheReadTokens", 0)
            info["cache_write"] += usage.get("cacheWriteTokens", 0)
            source = (data.get("message") or {}).get("source") or {}
            info["model"] = source.get("model") or info["model"]
    info["total"] = info["input"] + info["output"] + info["cache_read"] + info["cache_write"]
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=os.environ.get("DSH_HOME", "~/.dsh"),
                    help="工作台主目录，默认取 $DSH_HOME 或 ~/.dsh")
    ap.add_argument("--since", default=None, help="ISO 时间戳，只看该时刻之后开始的会话")
    ap.add_argument("--hours", type=float, default=6.0, help="不指定 --since 时的回看小时数")
    ap.add_argument("--top", type=int, default=0, help="只显示最贵的前 N 个会话")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = ap.parse_args()

    home = os.path.expanduser(args.home)
    cutoff = parse_time(args.since) if args.since else datetime.now(timezone.utc) - timedelta(hours=args.hours)
    cutoff_ms = cutoff.timestamp() * 1000

    pattern = os.path.join(home, "sessions", "**", "session.v4.jsonl.zstd")
    sessions = []
    for path in glob.glob(pattern, recursive=True):
        info = scan(path)
        if info["started"] and info["started"] >= cutoff_ms and info["calls"]:
            sessions.append(info)

    if not sessions:
        print("时间窗口内没有带用量记录的会话")
        return 0

    sessions.sort(key=lambda s: s["total"], reverse=True)
    if args.top:
        sessions = sessions[: args.top]

    if not args.quiet:
        print("%19s %6s %13s %10s %13s  %s" % ("开始时间", "调用数", "总token", "输出", "缓存读取", "会话"))
        for s in sessions:
            started = datetime.fromtimestamp(s["started"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            print("%19s %6d %13s %10s %13s  %s" % (
                started, s["calls"], format(s["total"], ","),
                format(s["output"], ","), format(s["cache_read"], ","), s["session"][:20]))
        print()

    totals = {k: sum(s[k] for s in sessions) for k in ("calls", "input", "output", "cache_read", "cache_write", "total")}
    print("会话数：%d | 模型调用：%s 次 | 总 token：%s" % (
        len(sessions), format(totals["calls"], ","), format(totals["total"], ",")))
    print("  输入：%s | 输出：%s | 缓存读取：%s | 缓存写入：%s" % (
        format(totals["input"], ","), format(totals["output"], ","),
        format(totals["cache_read"], ","), format(totals["cache_write"], ",")))
    if totals["calls"]:
        print("  平均每次调用：%s token" % format(totals["total"] // totals["calls"], ","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
