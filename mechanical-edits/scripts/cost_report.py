#!/usr/bin/env python3
"""汇总 DSH 会话的 token 账本：轮次、工具调用分布、缓存读取、每轮平均值。

用法：
    python3 cost_report.py ~/.dsh/sessions/<项目>/<会话>/
    python3 cost_report.py ~/.dsh/sessions/.../session.v4.jsonl.zstd
    python3 cost_report.py <会话> --json --top 5

会话文件可能是 .jsonl 或 .jsonl.zstd（自动识别、自动解压）。目录会取其中最新的会话。

看三个数就够了：
- 轮次：一条实例化指令被拆成了多少次模型调用；
- 平均每轮（cacheRead 为主）：上下文膨胀程度，> 5 万说明轮次过碎；
- 合计 / 工具调用次数：每次工具调用 > 2 万说明在逐处编辑，而不是批量改。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def find_session(path: Path) -> Path:
    if path.is_file():
        return path
    candidates = sorted(
        list(path.glob("*.jsonl")) + list(path.glob("*.jsonl.zstd")),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        candidates = sorted(
            path.glob("**/session.v4.jsonl*"), key=lambda p: p.stat().st_mtime
        )
    if not candidates:
        raise SystemExit("没有找到会话文件：%s" % path)
    return candidates[-1]


def load_events(path: Path) -> list:
    raw = path.read_bytes()
    if raw[:4] == ZSTD_MAGIC:
        raw = subprocess.run(
            ["zstd", "-dcq", str(path)], stdout=subprocess.PIPE, check=True
        ).stdout
    events = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            for item in walk(value):
                yield item
    elif isinstance(node, list):
        for value in node:
            for item in walk(value):
                yield item


def usage_of(event: dict) -> dict:
    data = event.get("data")
    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
        return data["usage"]
    for node in walk(event):
        if any(key in node for key in ("totalTokens", "cacheReadTokens", "inputTokens")):
            return node
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 DSH 会话的 token 账本")
    parser.add_argument("session", help="会话文件或目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--top", type=int, default=10, help="工具调用排行前 N 项")
    args = parser.parse_args()

    path = find_session(Path(args.session).expanduser())
    events = load_events(path)

    turns = 0
    totals = Counter()
    tools = Counter()
    seen_calls = set()
    last_context = 0

    # 工具调用优先取 tool/call 事件（每次调用一条，字段稳定）
    for event in events:
        if event.get("type") != "tool/call":
            continue
        data = event.get("data") or {}
        name = data.get("name")
        if not isinstance(name, str):
            continue
        call_id = data.get("callId") or data.get("id")
        if call_id and call_id in seen_calls:
            continue
        if call_id:
            seen_calls.add(call_id)
        tools[name] += 1

    for event in events:
        if event.get("type") != "assistant/message":
            continue
        turns += 1
        usage = usage_of(event)
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] += value
        context = (
            usage.get("inputTokens", 0)
            + usage.get("cacheReadTokens", 0)
            + usage.get("cacheWriteTokens", 0)
        )
        last_context = context or last_context
        if tools:
            continue
        # 兜底：没有 tool/call 事件时从消息块里数
        for node in walk(event):
            if node.get("type") in ("tool_use", "tool-call") and isinstance(node.get("name"), str):
                call_id = node.get("id")
                if call_id and call_id in seen_calls:
                    continue
                if call_id:
                    seen_calls.add(call_id)
                tools[node["name"]] += 1

    total = totals.get("totalTokens") or (
        totals.get("inputTokens", 0)
        + totals.get("cacheReadTokens", 0)
        + totals.get("cacheWriteTokens", 0)
        + totals.get("outputTokens", 0)
    )
    tool_calls = sum(tools.values())

    if args.json:
        print(
            json.dumps(
                {
                    "session": str(path),
                    "turns": turns,
                    "toolCalls": tool_calls,
                    "tools": dict(tools.most_common()),
                    "usage": dict(totals),
                    "total": total,
                    "avgPerTurn": total // turns if turns else 0,
                    "avgPerToolCall": total // tool_calls if tool_calls else 0,
                    "lastContext": last_context,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("会话        %s" % path)
    print("轮次        %-12d 工具调用 %d" % (turns, tool_calls))
    for name, count in tools.most_common(args.top):
        print("  %-10s %5d  (%.0f%%)" % (name, count, 100.0 * count / max(tool_calls, 1)))
    print("")
    print("缓存读取    %12s" % format(totals.get("cacheReadTokens", 0), ","))
    print("缓存写入    %12s" % format(totals.get("cacheWriteTokens", 0), ","))
    print("新输入      %12s" % format(totals.get("inputTokens", 0), ","))
    print("输出        %12s" % format(totals.get("outputTokens", 0), ","))
    print("合计        %12s" % format(total, ","))
    print("")
    print("平均每轮    %12s   （判据：> 5 万说明轮次过碎）" % format(total // turns if turns else 0, ","))
    print(
        "平均每次调用 %11s   （判据：> 2 万说明在逐处改，没有批量）"
        % format(total // tool_calls if tool_calls else 0, ",")
    )
    print("末轮上下文  %12s" % format(last_context, ","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
