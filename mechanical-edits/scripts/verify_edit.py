#!/usr/bin/env python3
"""按"不变量档位"校验机械改动：证明除了该改的，其它都没动。

用法：
    python3 verify_edit.py --profile comment 'src/**/*.py'
    python3 verify_edit.py --profile text 'config/*.yaml'
    python3 verify_edit.py --profile rename --rename old_name=new_name 'src/**/*.py'
    python3 verify_edit.py --profile message --summary 'src/**/*.py'
    python3 verify_edit.py --profile format --base 01f8db3 'src/**/*.py'

档位（profile）就是"这一类改动必须保住的不变量"：

  comment  注释 / docstring / 文案本地化
           去掉 docstring 的 AST 一致（代码、标识符、表达式没动）、docstring 数量一致、
           非 docstring 字符串多重集一致、指令注释原样、行尾一致；行数变化只警告
  message  日志 / 错误文案统一
           AST 结构一致（字符串内容折叠成占位符）、字符串字面量个数一致、行尾一致
  format   格式化 / import 重排
           完整 AST 一致（含 docstring）、非空白 token 序列一致、行尾一致
  rename   批量重命名
           基线 AST 按 --rename 映射改写后与改后 AST 完全一致；字符串里的旧名一并映射
  text     配置 / 文档 / 其它文本
           能解析的格式（json / yaml / toml / ini / env）解析结果必须相等，
           所以改注释、改缩进安全，改键值会被拦下；行尾一致
  syntax   最低档：只要求还能解析 / 编译

基线：
    --base REF       git 提交（默认 HEAD）
    --base-dir DIR   快照目录（优先于 --base）
    --allow-new      基线里没有的新文件只做语法检查

放宽：--allow string,comments,docstrings,lines,eol 里的某一项（改了才用，别默认开）。
--strict-lines  行数变化算失败（默认只警告，因为注释重排会并行）。
--require-structure  text 档位找不到解析器时算失败，而不是警告。
--summary       只打印失败项与统计，适合最后汇报。

退出码：全部通过 0，否则 1。
"""

from __future__ import annotations

import argparse
import ast
import configparser
import glob
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

PROFILES = ("comment", "message", "format", "rename", "text", "syntax")
PYTHON_PROFILES = ("comment", "message", "format", "rename")

DIRECTIVE_RE = re.compile(
    r"#\s*(type:\s*ignore|noqa|pragma:|doctest:|fmt:|isort:|pylint:|pyright:|mypy:)"
)
DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
WRAPPERS = (".example", ".sample", ".template", ".dist", ".tmpl", ".orig")
TOKEN_NOISE = {
    tokenize.NEWLINE,
    tokenize.NL,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.ENCODING,
}


# --------------------------------------------------------------------------- 通用


def expand(pattern: str) -> list:
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in glob.glob(pattern, recursive=True) if Path(p).is_file())
    return [pattern]


def eol_style(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "MIXED"
    return "CRLF" if crlf else "LF"


def strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, DOC_OWNERS) and node.body:
            if ast.get_docstring(node, clean=False) is not None:
                node.body.pop(0)
    return tree


def docstring_texts(tree: ast.AST) -> list:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, DOC_OWNERS):
            text = ast.get_docstring(node, clean=False)
            if text is not None:
                found.append(text)
    return found


def docstring_count(tree: ast.AST) -> int:
    return len(docstring_texts(tree))


def _str_value(node: ast.AST):
    """兼容 3.7（ast.Str）与 3.8+（ast.Constant）的字符串节点。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):
        return node.s
    return None


def _set_str(node: ast.AST, value: str) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        node.value = value
    elif isinstance(node, ast.Str):
        node.s = value


def string_literals(tree: ast.AST) -> Counter:
    """非 docstring 的字符串字面量多重集：日志、异常信息、prompt、配置键都在这里。"""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, DOC_OWNERS) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and _str_value(first.value) is not None:
                docs.add(id(first.value))
    counter = Counter()
    for node in ast.walk(tree):
        value = _str_value(node)
        if value is not None and id(node) not in docs:
            counter[value] += 1
    return counter


def comments(source: str) -> list:
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            found.append(tok.string)
    return found


def directives(comment_list: list) -> Counter:
    counter = Counter()
    for text in comment_list:
        match = DIRECTIVE_RE.search(text)
        if match:
            counter[match.group(1)] += 1
    return counter


def tokens(source: str) -> list:
    return [
        (tok.type, tok.string)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type not in TOKEN_NOISE
    ]


def fold_strings(tree: ast.AST) -> ast.AST:
    """把字符串内容折叠成占位符，留下结构：日志文案可改，f-string 占位符不能动。"""
    for node in ast.walk(tree):
        if _str_value(node) is not None:
            _set_str(node, "\u00a7")
    return tree


def count_strings(tree: ast.AST) -> int:
    return sum(1 for node in ast.walk(tree) if _str_value(node) is not None)


# --------------------------------------------------------------------------- rename


def rename_ast(tree: ast.AST, mapping: dict) -> ast.AST:
    def apply(name):
        if name is None:
            return None
        if name in mapping:
            return mapping[name]
        for old, new in mapping.items():
            if name.startswith(old + "."):
                return new + name[len(old) :]
        return name

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.name = apply(node.name)
        elif isinstance(node, ast.Name):
            node.id = apply(node.id)
        elif isinstance(node, ast.Attribute):
            node.attr = apply(node.attr)
        elif isinstance(node, ast.arg):
            node.arg = apply(node.arg)
        elif isinstance(node, ast.keyword):
            node.arg = apply(node.arg)
        elif isinstance(node, ast.ExceptHandler):
            node.name = apply(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            node.names = [apply(n) for n in node.names]
        elif isinstance(node, ast.alias):
            node.name = apply(node.name)
            node.asname = apply(node.asname)
        elif isinstance(node, ast.ImportFrom):
            node.module = apply(node.module)
        value = _str_value(node)
        if value is not None:
            _set_str(node, rename_text(value, mapping))
    return tree


def rename_text(text: str, mapping: dict) -> str:
    for old, new in mapping.items():
        if re.fullmatch(r"[A-Za-z_]\w*", old):
            text = re.sub(r"\b%s\b" % re.escape(old), new.replace("\\", r"\\"), text)
        else:
            text = text.replace(old, new)
    return text


# --------------------------------------------------------------------------- 结构化文本


def parse_structured(path: Path, text: str):
    """返回 (类型, 解析结果)；结果 None 表示没有可用解析器；抛 ValueError 表示解析失败。"""
    name = path.name.lower()
    for wrapper in WRAPPERS:
        if name.endswith(wrapper):
            name = name[: -len(wrapper)]
            break

    if name.endswith(".json"):
        return "json", json.loads(text)
    if name.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            return "yaml", None
        return "yaml", yaml.safe_load(text)
    if name.endswith(".toml"):
        try:
            import tomllib as reader
        except ImportError:
            try:
                import tomli as reader
            except ImportError:
                try:
                    import toml as reader
                except ImportError:
                    return "toml", None
        return "toml", reader.loads(text)
    if name.endswith((".ini", ".cfg")):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(text)
        return "ini", {section: dict(parser[section]) for section in parser.sections()}
    if name.endswith(".env") or name in (".env", "env"):
        data = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([A-Za-z_][\w.]*)\s*[=:]\s*(.*)$", line)
            if not match:
                raise ValueError(f"无法解析的行：{line[:60]!r}")
            data[match.group(1)] = match.group(2).strip()
        return "env", data
    return None, None


# --------------------------------------------------------------------------- 校验


def check_python(before: str, after: str, profile: str, mapping: dict, allow: set):
    problems, warnings = [], []
    before_tree = ast.parse(before)
    after_tree = ast.parse(after)

    if profile == "comment":
        if ast.dump(strip_docstrings(before_tree)) != ast.dump(strip_docstrings(after_tree)):
            problems.append("去掉 docstring 后 AST 不一致：代码、标识符或表达式被改动")
        if "docstrings" not in allow:
            if docstring_count(before_tree) != docstring_count(after_tree):
                problems.append(
                    "docstring 数量变化：%d -> %d"
                    % (docstring_count(before_tree), docstring_count(after_tree))
                )
        if "string" not in allow:
            old_strings = string_literals(before_tree)
            new_strings = string_literals(after_tree)
            if old_strings != new_strings:
                lost = list((old_strings - new_strings).items())[:3]
                extra = list((new_strings - old_strings).items())[:3]
                problems.append("非 docstring 字符串被改动：少了 %r 多了 %r" % (lost, extra))
        if "comments" not in allow:
            old_d = directives(comments(before))
            new_d = directives(comments(after))
            if old_d != new_d:
                problems.append("指令注释被改动：%s -> %s" % (dict(old_d), dict(new_d)))
        old_comments = len(comments(before))
        new_comments = len(comments(after))
        if old_comments >= 5 and new_comments < old_comments * 0.8:
            warnings.append("注释条数明显减少：%d -> %d，确认不是整段被删" % (old_comments, new_comments))
        old_chars = sum(len(text) for text in docstring_texts(before_tree))
        new_chars = sum(len(text) for text in docstring_texts(after_tree))
        if old_chars >= 200 and new_chars < old_chars * 0.5:
            warnings.append("docstring 总长度减少过半：%d -> %d 字符" % (old_chars, new_chars))
    elif profile == "message":
        if ast.dump(fold_strings(before_tree)) != ast.dump(fold_strings(after_tree)):
            problems.append("去文案后的 AST 结构不一致：表达式、f-string 占位符或分支被改动")
        if count_strings(before_tree) != count_strings(after_tree):
            problems.append(
                "字符串字面量个数变化：%d -> %d" % (count_strings(before_tree), count_strings(after_tree))
            )
    elif profile == "format":
        if ast.dump(before_tree) != ast.dump(after_tree):
            problems.append("完整 AST 不一致：格式化不该改变代码或 docstring")
        if tokens(before) != tokens(after):
            problems.append("token 序列不一致：除了空白，还有实质内容变化")
    elif profile == "rename":
        if not mapping:
            problems.append("rename 档位需要至少一个 --rename OLD=NEW")
        else:
            expected = ast.dump(rename_ast(before_tree, mapping))
            if expected != ast.dump(after_tree):
                problems.append("按映射改写基线后 AST 仍不一致：有调用点没改到，或改了不该改的")
    return problems, warnings


def check_text(path: Path, before: str, after: str, require_structure: bool):
    problems, warnings = [], []
    try:
        kind, old_data = parse_structured(path, before)
        _, new_data = parse_structured(path, after)
    except ValueError as exc:
        return ["内容无法解析，无法做结构校验：%s" % exc], warnings

    if kind is None:
        warnings.append("没有结构校验（未知格式），只验证了行尾与行数")
    elif old_data is None:
        message = "%s 解析器未安装，跳过结构校验" % kind
        (problems if require_structure else warnings).append(message)
    elif old_data != new_data:
        problems.append("%s 解析结果不一致：键、值或结构被改动" % kind)
    return problems, warnings


def load_baseline(path: Path, base_ref: str, base_dir):
    if base_dir is not None:
        relative = path
        if relative.is_absolute():
            try:
                relative = relative.relative_to(Path.cwd())
            except ValueError:
                relative = Path(relative.name)
        with (base_dir / relative).open(encoding="utf-8", newline="") as handle:
            return handle.read()
    root = Path(
        subprocess.check_output(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"])
        .decode()
        .strip()
    )
    relative = path.resolve().relative_to(root.resolve())
    return subprocess.check_output(
        ["git", "-C", str(root), "show", "%s:%s" % (base_ref, relative.as_posix())]
    ).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="按不变量档位校验机械改动")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--base", default="HEAD", help="git 基线（默认 HEAD）")
    parser.add_argument("--base-dir", default=None, help="快照目录基线，优先于 --base")
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--allow", default="", help="string,comments,docstrings,lines,eol")
    parser.add_argument("--strict-lines", action="store_true")
    parser.add_argument("--allow-new", action="store_true")
    parser.add_argument("--require-structure", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    allow = {item.strip() for item in args.allow.split(",") if item.strip()}
    mapping = {}
    for item in args.rename:
        if "=" not in item:
            print("错误：--rename 需要 OLD=NEW 形式", file=sys.stderr)
            return 1
        old, new = item.split("=", 1)
        mapping[old] = new

    base_dir = Path(args.base_dir) if args.base_dir else None
    targets = []
    for pattern in args.files:
        targets.extend(expand(pattern))

    passed = 0
    failures = 0
    warning_count = 0

    for name in targets:
        path = Path(name)
        if not path.exists():
            failures += 1
            print("FAIL  %s: 文件不存在" % name)
            continue

        with path.open(encoding="utf-8", newline="") as handle:
            after_raw = handle.read()
        try:
            before_raw = load_baseline(path, args.base, base_dir)
        except (subprocess.CalledProcessError, FileNotFoundError):
            if not args.allow_new:
                failures += 1
                print("FAIL  %s: 基线里没有这个文件（新文件请加 --allow-new）" % name)
                continue
            before_raw = ""

        after = after_raw.replace("\r\n", "\n")
        before = before_raw.replace("\r\n", "\n")
        problems = []
        file_warnings = []

        if path.suffix != ".py" and args.profile in PYTHON_PROFILES:
            problems.append("档位 %s 只适用于 .py，文本文件请用 --profile text" % args.profile)
        elif path.suffix == ".py" and args.profile == "text":
            problems.append("文本档位不用于 .py：改注释用 comment，格式化用 format")
        elif path.suffix == ".py" and args.profile == "syntax":
            try:
                compile(after, str(path), "exec")
            except SyntaxError as exc:
                problems.append("语法错误：第 %s 行 %s" % (exc.lineno, exc.msg))
        elif path.suffix == ".py":
            try:
                parse_problems, parse_warnings = check_python(
                    before, after, args.profile, mapping, allow
                )
            except SyntaxError as exc:
                parse_problems = ["语法错误：第 %s 行 %s" % (exc.lineno, exc.msg)]
                parse_warnings = []
            problems.extend(parse_problems)
            file_warnings.extend(parse_warnings)
        else:
            try:
                text_problems, text_warnings = check_text(
                    path, before, after, args.require_structure
                )
            except (SyntaxError, ValueError) as exc:
                text_problems, text_warnings = ["解析失败：%s" % exc], []
            problems.extend(text_problems)
            file_warnings.extend(text_warnings)

        if "eol" not in allow and before_raw and eol_style(before_raw) != eol_style(after_raw):
            problems.append("行尾风格变化：%s -> %s" % (eol_style(before_raw), eol_style(after_raw)))
        if before_raw and before_raw.endswith("\n") != after_raw.endswith("\n"):
            problems.append("文件末尾换行状态变化")
        if before.count("\n") != after.count("\n") and "lines" not in allow:
            message = "行数变化：%d -> %d" % (before.count("\n") + 1, after.count("\n") + 1)
            if args.strict_lines:
                problems.append(message)
            else:
                file_warnings.append(message)

        warning_count += len(file_warnings)
        if problems:
            failures += 1
            print("FAIL  %s" % name)
            for problem in problems:
                print("        - %s" % problem)
        else:
            passed += 1
            if not args.summary:
                extra = ""
                if path.suffix == ".py":
                    extra = "（注释 %d, docstring %d, %s）" % (
                        len(comments(after)),
                        docstring_count(ast.parse(after)),
                        eol_style(after_raw),
                    )
                print("PASS  %s%s" % (name, extra))
        for warning in file_warnings:
            print("        ~ %s: %s" % (path.name, warning))

    print("\n通过 %d / %d，失败 %d，警告 %d" % (passed, passed + failures, failures, warning_count))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
