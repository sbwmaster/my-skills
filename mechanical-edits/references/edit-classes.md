# 机械改动分类：每一类怎么改、拿什么证明

十类最常见的机械改动。每类给：识别信号 → 首选工具 → 必须保住的不变量 → 用哪个校验档位 → 典型坑。

---

## 1. 注释与 docstring 本地化

**识别**：只动 `#` 注释和 docstring，代码、标识符、字符串一个字不改。

**工具**：改动密度高、每处都要重新组织语言 → 每个文件一次 `write` 整文件重写；纯替换（术语统一、固定句式）→ `apply_edits.py`。

**不变量**：去掉 docstring 后的 AST 完全一致；docstring 数量不变；非 docstring 字符串（日志、异常、prompt、配置键、URL）逐字不变；`# noqa`、`# type: ignore`、`# pragma:`、`# doctest:` 原样；行尾风格不变。

**档位**：`--profile comment`（行数变化只警告：翻译常把两行并一行）。

**可翻译 / 不可翻译**：

| 可翻译 | 不可翻译 |
|---|---|
| `#` 注释、模块/类/函数 docstring、`Args:/Returns:/Raises:` 等小节标题 | 代码、标识符、字符串字面量、日志与异常文案、prompt、JSON Schema 的 `description`、URL、文件路径、doctest、指令注释 |

小节标题的常规译法：`Args:`→`参数：`，`Returns:`→`返回：`，`Raises:`→`异常：`，`Attributes:`→`属性：`，`Yields:`→`产出：`。

**术语**：保留英文原文（首次出现可加中文）—— RAG、embedding、token、prompt、rerank、chunk、query、retriever、fixture、mock、patch、HTTP、API、MCP、CRLF。其余按项目既有译法统一，建立一张表放仓库里，别每次重新决定。

**坑**：把长英文注释整段删掉重写 → 信息丢失；docstring 里的空行带尾随空格导致多行匹配失败；`"""` 缩进被改；把字符串当注释翻译。

---

## 2. 日志与错误文案统一

**识别**：`logger.info(...)`、`raise ValueError("...")`、CLI 输出、用户可见提示里的话术统一（措辞、大小写、句号、错误码前缀）。

**工具**：`apply_edits.py` 逐条精确替换（旧文案必须唯一，不唯一就带上下文行）。

**不变量**：AST 结构不变（表达式、分支、参数）；字符串字面量**个数**不变；f-string 的占位符个数、顺序、表达式不变；异常类型、错误码、日志级别、结构化字段名不变。

**档位**：`--profile message`。它把字符串内容折叠成占位符后比结构，所以"换措辞"通过、"少一个 `{}`"失败。

**坑**：顺手"改进"了文案里的变量顺序；把 `f"{a}/{b}"` 写成 `f"{b}/{a}"`；把 `logger.warning` 降成 `info`；多语言 key 拼错导致 i18n 缺词；**文案改动和逻辑改动混在一个 commit** —— 档位只能证明结构没变，证明不了语义，所以必须分开提交、分开 review。

---

## 3. 批量重命名

**识别**：一个名字（函数/类/变量/模块/配置键/环境变量/CLI 参数/URL 段）在多处出现，要统一换掉。

**工具**：`apply_edits.py` + `"count": "all"`；对 Python，跨语言的调用面（文档、CI、脚本、`.env`）用 grep 兜底列表。

**不变量**：把基线 AST 按 `--rename 旧=新` 映射改写后，与改后 AST 完全一致（既证明没漏，也证明没多改）；字符串里的旧名一并映射；行尾不变。

**档位**：`--profile rename --rename old=new`（可重复给多条）。

**流程**：
1. `grep -rn '\bold\b'` 数出旧名出现次数（含注释、字符串、文档）；
2. 一次性替换所有出现位置；
3. 加一条 `{"old": "old", "count": 0}` 断言旧名零残留；
4. `--profile rename` 确认调用点全改、没有改到别的同名符号。

**坑**：`str.replace` 命中子串（`user` → `account` 会顺手改掉 `username`、`user_id`）→ 用词边界或先列出所有命中逐条确认；漏掉配置键、环境变量、文档、测试 fixture；重命名 `__all__`、`__init__.py` 的 re-export、字符串里的 `getattr(obj, "old")` 动态引用（AST 看不见，只能 grep）；改了公共 API 却不加弃用别名。

---

## 4. 许可证头 / 文件头 / 编码行

**识别**：给一批文件加同样的头，或替换公司名、年份、SPDX 标识。

**工具**：`apply_edits.py` 的 `prepend`（`skip_if_present` 默认开，可重复执行不重复插入）。

**不变量**：只加不改；已有头的文件不重复插入；`# -*- coding:` 与 shebang 的位置不被破坏。

**档位**：`--profile comment`（`--allow lines` 免掉行数告警）。

**坑**：shebang 必须第一行 → 有 shebang 的文件不能用 `prepend`，改成 `old=<shebang 行>`、`new=<shebang 行> + 头` 的替换；`# -*- coding: utf-8 -*-` 必须在第一或第二行；某些语言的头要放在 `namespace`/`package` 之后。

---

## 5. import 重排与路径更新

**识别**：迁移到绝对导入、按字母排序、移动模块后改引用。

**工具**：`isort`、`ruff check --fix`、`pre-commit`；路径改名配合 `git mv`。**不要手写排序**。

**不变量**：完整 AST 只允许 import 顺序变化 —— 用 `--profile format` 更合适的主张是"token 序列不变"，而 import 重排会变 token，所以改用：先跑工具，再 `--profile syntax` + 全量测试/import smoke。

**档位**：`--profile syntax` + `python -c "import pkg"` 或测试；顺序本身由 isort 保证。

**坑**：相对导入改成绝对导入后包名写错；`__init__.py` 的 re-export 顺序影响循环导入；只改了 `.py`，漏了 `pyproject.toml` 的 `packages`、`MANIFEST.in`、Dockerfile 的 `COPY`、CI 的路径。

---

## 6. 格式化

**识别**：`black` / `ruff format` / `prettier` / `gofmt` / `rustfmt` 之类。

**工具**：直接跑工具，全仓库一次。**永远不要用模型手写格式化结果**：既慢，又会在无关行制造 diff。

**不变量**：完整 AST 完全一致（含 docstring）；非空白 token 序列一致。

**档位**：`--profile format`。

**坑**：格式化改动和功能改动混在一个 commit → diff 无法 review；仓库没有 formatter 配置，跑出来的风格与现状冲突，产生上万行 diff → 先只格式化"本次要改的文件"，或单独一个 commit 全仓库格式化；`--check` 进 CI 之前先统一一次，否则每个人都在本地制造噪声。

---

## 7. 废弃 API 迁移（codemod）

**识别**：`old_api(...)` → `new_api(...)`，参数改名、参数改为关键字、返回值结构变化。

**工具**：单行且形态固定 → 正则 + `apply_edits.py`（`"regex": true`）；跨行、需要类型信息、需要区分同名不同物 → `libcst`（Python）、`jscodeshift`/`ts-morph`（JS/TS）、`rope`（重命名）。不要逐文件手改。

**不变量**：调用点数量不变（改用 `grep -c` 或 AST 计数）；参数名与顺序符合新签名；未被改动的文件 AST 完全一致。

**档位**：`--profile rename`（若只是改名）或 `--profile syntax` + 测试。

**坑**：正则跨行匹配吃掉括号；把同名但不同来源的函数一起改了；漏掉 `**kwargs` 透传、`getattr` 动态调用、文档示例、type stub（`.pyi`）、mock 里的断言字符串。

---

## 8. 配置键改名与样例同步

**识别**：`settings.yaml`、`.env`、`pyproject.toml`、CI 配置里的键改名或改层级。

**工具**：`apply_edits.py`（YAML/TOML 都是文本）；同时改读取端代码。

**不变量**：解析结果（除目标键外）完全相等；`.env.example` 与真实配置的键集合一致；代码里的默认值与样例值不冲突。

**档位**：`--profile text`（json/yaml/toml/ini/env 都会解析后比对，所以改注释、改缩进安全，改值会被拦下）。

**坑**：只改样例没改代码；只改代码没改样例；CI secret 名称与 `.env` 键不一致；YAML 里 `on`/`yes`/`no` 被解析成布尔；嵌套键改名后旧键残留（加 `count: 0` 断言）。

---

## 9. 目录 / 文件搬迁

**识别**：包拆分、目录重命名、文件从 `utils/` 挪到 `core/`。

**工具**：`git mv`（保留历史）；引用更新用 `apply_edits.py`。

**不变量**：旧路径零引用；`import` 全部可解析；打包配置、CI、文档、Dockerfile、脚本里的路径全部更新。

**档位**：`--profile syntax` + `python -c "import <pkg>"` + 全量测试。

**坑**：漏掉非 Python 引用（`Makefile`、`Dockerfile`、`.github/workflows`、`pyproject.toml` 的 `packages`/`package-data`、`MANIFEST.in`）；相对导入层级算错；`__pycache__` 里的旧模块被误 import。

---

## 10. 删除废弃代码与文件

**识别**：删掉不再使用的模块、函数、开关、测试。

**工具**：`git rm`；删除前 `grep -rn` 确认零引用（包括字符串里的动态 import、文档、配置）。

**不变量**：import 图无悬空；测试全绿；覆盖率不出现新的未覆盖行。

**档位**：`--profile syntax` + 全量测试 + `python -c "import <pkg>"`。

**坑**：删了实现没删导出；删了导出没删测试；文档与示例还在引用；发布包里的入口点（`entry_points`、`console_scripts`）指向已删函数。
