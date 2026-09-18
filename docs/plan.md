# KnowMemo — 里程碑 1 实施计划

Version: 0.2
Date: 2026-09-17
Status: **M0–M2 已交付**。D1/D2/D3 已按建议实现、待你逐条确认；**D4 仍需你重新决策**（见第 5 节）。

> 本文是评审用的工作文档。决策定下来后，其结论会按设计文档 §7 的结构拆进
> `docs/architecture.md` / `data-model.md` / `wechat.md` / `weknora.md` / `roadmap.md`。

---

## 0. 本轮做了什么

只做了设计文档 §42 Rule 1 要求的事：**先核实、先出计划，不写业务代码。**

仓库现状（已实测）：

```
<checkout>/
├── .git/          (remote: git@github.com:lpeixin/knowmemo.git, 1 commit)
├── LICENSE        (MIT, Copyright (c) 2026 Peixin)
└── README.md      (1 行)
```

> 本机 checkout 的绝对路径已省略：它包含设备用户名，而本文档会随仓库一起公开。
> 下面所有涉及本机路径的地方同理。

`src/`、`docs/`、`pyproject.toml`、`tests/` **均不存在**。是一个干净的起点，没有历史包袱。

核实手段：WeKnora 官方仓库与 API 文档（`docs/api/*.md`）、macOS 微信解密工具的实现说明、本机环境实测。

---

## 1. 事实核查：设计文档的外部假设哪些站得住

### 1.1 站得住的

| 文档条目 | 核实结果 |
|---|---|
| §19 用 WeKnora 作外部 RAG 层 | ✅ 真实项目 `Tencent/WeKnora`，v0.8.0，Go，**MIT**，26.1k star，2026-09 开源 |
| §29 `weknora.base_url: http://localhost:8080` | ✅ 后端默认 `:8080`，Web UI `:80` |
| §20 专用知识库 | ✅ KB 有稳定 ID，支持多 KB 跨库检索 |
| §21 混合检索 | ✅ `/knowledge-search` 与 `/knowledge-bases/{id}/hybrid-search` |
| §33 必须用合成 fixture | ✅ 结论正确，且比文档以为的**更必要**（见 F4/F8） |
| §42 Rule 3/4 分层 | ✅ 与 WeKnora 实际接口无冲突 |

选型本身没问题。WeKnora 的定位（文档处理 / 分块 / 嵌入 / 混合检索 / 重排 / 图谱）与 §45 划的边界是一致的，没有重叠。

### 1.2 需要改的

#### F1 — WeKnora 检索**不支持元数据过滤**，这直接推翻 §22 的写法

`POST /api/v1/knowledge-search` 的请求体只有三个字段：

| 字段 | 说明 |
|---|---|
| `query` | 查询文本 |
| `knowledge_base_id` / `knowledge_base_ids` | 限定知识库（二者互斥） |
| `knowledge_ids` | 进一步限定到指定文件 |

**没有日期范围、没有参与者、没有 tag 过滤。** 文档 §22 写的这个形态在 WeKnora 侧不存在：

```
semantic query:  "我和 Alice 聊过什么"
metadata filter: participant = Alice, date >= 2025-01-01   ← 无处可传
```

**修正方案**：`knowledge_ids` 就是现成的前置过滤钩子。

```
用户提问
   ↓
KnowMemo 元数据索引解析 → {participant: Alice, 2025-01-01 ≤ t < 2026-01-01}
   ↓
得到候选 segment 集合 → 取其 weknora knowledge_id 列表
   ↓
WeKnora /knowledge-search  { query, knowledge_ids: [...] }   ← 过滤在此下推
   ↓
语义排序（只在该子集内）
```

这个修正反而**更符合 §45 的边界**——"Personal Memory Metadata"本来就该归 KnowMemo 所有。
代价：KnowMemo 需要自己维护一份元数据索引（见 M3），并注意 `knowledge_ids` 的数量上限（未在文档中给出，需实测）。

#### F2 — WeKnora **没有"客户端指定 ID 的 upsert"**，§27 的确定性 ID 传不进去

- 创建接口（`file` / `url` / `manual`）全是 `POST` **新建**，不接受外部指定 ID
- `PUT /knowledge/:id` 只更新**已存在**的记录，不会创建
- 判重靠 `file_hash`，重复上传返回 **HTTP 409** 并附带已存在知识的引用

所以 §27 那套 `sha256(...)` 确定性 ID 只能用在 **KnowMemo 内部**，不能作为 WeKnora 侧的身份。
KnowMemo 必须自己维护一张映射表：

```
segment_id  (KnowMemo 的确定性 ID)
      ↕
weknora_knowledge_id  (WeKnora 分配的 UUID)
```

**这张表在文档的数据模型里是缺失的**，已补进 M3 与 §2 的架构图（标 ★）。

#### F3 — `manual` 接口不支持 metadata，只有文件上传接口支持，且只收 `map[string]string`

| 接口 | metadata 支持 |
|---|---|
| `POST /knowledge-bases/:id/knowledge/manual` | ❌ 只有 `title` + `content` |
| `POST /knowledge-bases/:id/knowledge/file` | ✅ `metadata`（JSON 字符串 → `map[string]string`），且 `fileName` 可保留相对路径 |
| `PUT /knowledge/:id` | ✅ 但字段名叫 **`custom_metadata`**（和创建时不一致） |

**修正方案**：§16 的段文档应当**以 `.md` 文件形式 multipart 上传**，而不是走 `manual`。
这样 frontmatter 里的元数据才能真正落到 WeKnora 侧、才能在检索结果里返回（响应里有 `metadata` 字段）。

附带两条约束：
- `participants` 是列表，但 WeKnora 只收 `map[string]string` → 必须压平成逗号串
- 时间要转成 ISO 字符串

#### F4 — macOS 微信数据不是"读一个数据库"，是"先攻破加密"。§11–13 严重低估了这一步

本机实测：微信 **4.1.13**，数据位于 `~/Library/Containers/com.tencent.xinWeChat/...`。

加密方式：**WCDB（基于 SQLCipher 4）**，**每个 `.db` 一把独立的 AES-256 密钥**，密钥格式为 `x'<64hex_key><32hex_salt>'`，只存在于**运行中微信进程的内存**里。

要拿到明文，完整前置条件链是：

1. `sudo` —— 通过 Mach VM API / `task_for_pid` 读微信进程内存
2. **对 `/Applications/WeChat.app` 重新 ad-hoc 签名，去掉 Hardened Runtime** —— 否则 `task_for_pid` 被拒
   （替代方案：`csrutil disable` 关掉 SIP，但影响面更大）
3. 微信每次更新会**恢复原始签名**，第 2 步要重做

这不是"parser 的一个细节"，这是一个**独立的安全/权限子问题**，而且**每次微信升级都会坏**。
它和 §2.1「本地优先」、§28「隐私与安全」直接冲突——它要求 KnowMemo 或它的前置工具去改写系统上的微信应用包。

→ 见 **决策 D4**。这是本轮最需要你拍板的事。

#### F5 — 微信库的真实形态与 §13 的设想不同

§13 担心的是"不同**版本** schema 不同"，于是画了 `VersionAdapterA/B/C`。
实际差异轴**不是版本，是表与行**：

- 库是**分片**的：`message_0.db`、`message_1.db`、…，外加 `contact.db`、`session.db`、`message_fts.db`、`media_0.db`
- 消息**不存在一张 `message` 表里**，而是**每个联系人/群一张动态表**：`Msg_<md5(username)>`
- 部分行的 `message_content` 是 **zstd 压缩**的（`WCDB_CT_message_content=4`），必须单独解压
- 图片/文件/语音**不在消息库里**，要通过 `message_resource.db` + `xwechat_files/.../Message/` 关联

所以适配器要建在 `(wechat_version, message_content_encoding)` 上；
reader 的核心是**表发现 + 逐行内容解码**，而不是版本分支。

§13 的警告方向是对的（"不要硬编码 `table = message`"），但连它给的替代方案也不对——
因为压根没有一张叫 `message` 的表。

#### F6 — 本机**没有 Docker**，WeKnora 跑不起来。§41 的 Phase 3 目前阻塞

```
docker --version        → command not found
lsof -iTCP:8080         → 无监听
```

Phase 0–2 **不受影响**（§17 本来就要求"没有 LLM 也要能工作"）。
但 Phase 3 需要你先装 Docker Desktop，或者把 WeKnora 放到另一台机器上。

#### F7 — 本机 Ollama **没有 embedding 模型**

Ollama 在跑（`:11434`），但只有：

| 模型 | 参数 | 能力 |
|---|---|---|
| `qwen3.5:9b` | 9.7B | completion, vision, tools, thinking |
| `fauxpaslife/arch-router:1.5b` | 1.5B | tools, completion |

**没有 embedding 模型。** WeKnora 建知识库必须要 embedding，需要额外 pull 一个（`bge-m3` / `nomic-embed-text` 之类）。

另外 §29 里写的 `llm.model: qwen3`，本机实际是 `qwen3.5:9b`。

→ 已加入 `doctor` 检查项。

#### F8 — 读微信数据目录需要「完全磁盘访问权限」

```
$ ls ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files
ls: Operation not permitted
```

这是 macOS TCC。→ `doctor` 必须能检出并给出修复指引
（系统设置 → 隐私与安全性 → 完全磁盘访问权限）。

#### F9 — **微信官方不存在"导出为可读文件"的功能**（这条决定了 M1 的前提）

你提出的约束是：*只读官方导出/备份产生的合法数据，不干扰运行中的微信，符合微信协议*。
我去核实了官方说明本身，结论是这条约束在微信上**没有可编程的落地路径**。

**微信官方支持页**（`support.weixin.qq.com`，「查看备份和恢复说明」）全文只描述了一件事：

> 你可以将微信聊天记录通过微信 Windows/Mac 版备份到电脑上，并可以将之前备份的聊天记录
> **恢复到手机上**。

操作路径：PC/Mac 微信 → 左下角「更多」→「备份恢复」。**没有第三种产物形态。**

**腾讯云开发者社区的技术说明**（《微信聊天记录备份与迁移完全指南》）给出了产物的性质：

> **局限**：备份文件经过加密，**无法脱离微信直接查看**；Windows 版生成的备份在 Mac 版微信上
> 无法恢复，反之亦然。

把两条合起来：

| 需求 | 官方能力 | 结论 |
|---|---|---|
| 导出为 txt / csv / json | 不存在 | ✗ |
| 备份产物可被第三方程序读取 | 加密，且格式未公开 | ✗ |
| 备份能恢复到电脑（便于读取） | 只能恢复到**手机** | ✗ |
| 官方 API / 协议供本地读取 | 个人微信不提供 | ✗ |

**所以"官方来源"和"可被程序读取"这两个条件，微信同时不满足。**

而所有能产出明文的方法（WeChatMsg / WeChatExporter / `wechat-export-macos`）本质上都是同一件事：
解密本地 SQLCipher 库 → 需要密钥 → 密钥只存在于运行中微信进程的内存 → **读进程内存**。
这正是你排除掉的那条路。

**这不是工程难题，是能力缺失。** 微信官方没有为个人用户提供任何机器可读的会话导出，
所以任何"只读官方产物"的 WeChat connector 都没有输入。

三条出路，见决策 **D4（修订版）**。

### 1.3 文档内部自相矛盾的地方

| 位置 | 问题 | 处理建议 |
|---|---|---|
| §6 vs §42 Rule 6 | §6 的"MVP 本地部署"框图列了 PostgreSQL + Redis，但同一节又说"避免不必要的基础设施""开发期可用 SQLite"，Rule 6 也明令禁止 | **决策 D1**。那两个是 **WeKnora 自己的**依赖（它的 compose 里自带），不是 KnowMemo 的。文档把两者混在一起了 |
| §7 vs §4/§17 | §7 的目录树把 `processing/summarization.py`、`entity_extraction.py` 列为 MVP 结构，但 §4/§17 说 AI 增强可选、属 Phase 6 | 保留文件骨架但**不接入 MVP 路径**，函数体留 `NotImplementedError`，避免"看起来做完了" |
| §27 | 段 ID = `sha256(conversation_id + first_message_id + last_message_id)`，追加式导入下稳定；但**一旦改分段参数**（`inactivity_minutes` 等）边界移动 → 所有段 ID 变化 → 静默全量重建 | 哈希输入加一个 `segmentation_version`，让改参数成为**显式的重建动作** |
| §26 | 用 `last_scan_time` 做增量检查点不安全：微信按 `create_time` 排序，且**聊天记录迁移会回填更早的消息** | 检查点用 `max(create_time) − 重叠窗口`，并保留一个全量对账扫描 |
| §23 | "不要生成无依据的陈述"方向正确，但在 F1 前提下，**引用来源要 KnowMemo 自己拼**——WeKnora 返回的 `knowledge_references` 不含你的会话元数据 | M3 里由 KnowMemo 侧组装 sources |
| §19 | `WeKnoraClient` 的 `upload_document` / `update_document` / `delete_document` 可行，但缺一个"按内容哈希查已存在"的方法 | 补 `resolve_by_hash()`；配合 F2 的映射表 |

---

## 2. 修正后的架构

三处边界变化，全部标 ★：

1. **新增「解密/快照」层，且它不在 KnowMemo 进程内** —— 见 D4 方案 (a)
2. **新增 KnowMemo 侧元数据索引** —— 承担 §22 的时间/人物过滤，经 `knowledge_ids` 下推给 WeKnora
3. **新增 `segment → knowledge_id` 映射表** —— 因为 WeKnora 不给 upsert（F2）

（配图见对话中的架构图）

---

## 3. 里程碑 1 计划（目标：`knowmemo import wechat --dry-run`）

对应 §46 的第 1–11 步。**全程不需要 sudo、不需要 Docker、不需要真实数据。**

### M0 — 骨架（不碰数据）

| 产出 | 说明 |
|---|---|
| `pyproject.toml` | Python ≥3.11，hatchling；依赖 `pydantic` / `pydantic-settings` / `typer` / `sqlalchemy` / `pyyaml` / `rich` |
| `src/knowmemo/` 目录树 | 严格按 §7 |
| `domain/` | `Message` / `Conversation` / `Participant` / `Document` / `Source`，pydantic 模型，字段按 §8/§9 |
| `ingestion/interfaces.py` | `SourceConnector` ABC，四方法按 §10 |
| `config/settings.py` | §29 的配置树，YAML + env 覆盖 |
| `storage/` | SQLAlchemy + SQLite，`models.py` / `repositories.py` |
| `cli/main.py` | typer，命令按 §30 |
| 日志 | 结构化，**强制 §28 的脱敏规则**（只记 `message_id`/`conversation_id`/`status`，绝不记 content） |
| `.gitignore` / `.env.example` | 按 §42 Rule 8，`data/` 整目录忽略 |

**验收：`knowmemo doctor` 通过**，输出覆盖 F6 / F7 / F8 三项环境检查。

#### M0 完成情况（2026-09-17）

✅ **已交付并通过验收。**

```
$ knowmemo doctor
✓  Python            3.13.12
✓  Configuration     built-in defaults (no config file found)
✓  SQLite            3.50.4
✓  Data directory    data (writable)
✓  Metadata database 6 tables at sqlite:///./data/knowmemo.db
✓  LLM runtime       qwen3.5:9b present (2 models)
!  Embedding model   not configured (embedding.model is null)
!  WeKnora           http://localhost:8080 unreachable (HTTPError)
!  Docker            not installed
·  WeChat source     disabled in configuration

6 ok · 3 warning(s) · 0 failure(s)      exit 0
```

三条 warning 精确对应 F6 / F7 / F8，且**没有**触发失败——这正是 §17「没有 LLM 也要能工作」的要求。

测试与静态检查：

```
pytest        85 passed
ruff check    All checks passed
ruff format   38 files already formatted
```

**相对 §7 目录树的 3 处增补**（都是必要且有明确职责的，不是随意发挥）：

| 新增 | 理由 |
|---|---|
| `domain/ids.py` | §27 的确定性 ID 需要一个归属；散落在各 connector 里会重复实现 |
| `errors.py` | §35 的错误分类需要一个归属；这是跨层的横切关注点 |
| `health.py` | `doctor` 的检查框架；放在 `cli/` 里会让 CLI 文件膨胀且难测 |

**相对 §8/§9 模型的两处刻意增补**：

- `MessageType` 含全部 11 种类型（§8 已列出），MVP 只处理其中 5 种——schema 先支持，实现后跟上
- `KnowledgeDocument.weknora_metadata()` 把列表压平为逗号串（F3 的直接后果）

**一处与文档不同的行为约定**：未实现里程碑的命令以 **exit code 2** 退出并明确说明归属里程碑，
而不是静默成功或抛异常。这是刻意的——「没做完」和「做坏了」必须可区分。

### M1 — WeChat connector（对 fixture + 对真实快照只读）

| 模块 | 职责 |
|---|---|
| `connectors/wechat/discovery.py` | 候选路径发现与状态上报，**不硬编码任何路径**（§12） |
| `connectors/wechat/database.py` | **只读**打开明文 SQLite（`file:...?mode=ro&immutable=1`），代码层面杜绝写入 |
| `connectors/wechat/schema.py` | schema 自省 → 发现 `Msg_*` 表 → 经 `contact.db` 把 `md5(username)` 映回 `username`（按 F5 重写，不是 §13 的版本分支） |
| `connectors/wechat/decoders.py` | 内容解码：明文 / zstd / app 消息 XML（link、file、引用回复、system） |
| `connectors/wechat/parser.py` | raw row → domain 对象 |
| `normalization/` | 类型映射表、`create_time` 秒级时间戳转换、会话归一 |
| `tests/fixtures/wechat/` | **用生成脚本造合成 SQLite**（不是静态文件）—— 这样才能构造多种 schema 变体做参数化测试；配 golden JSON |

**验收：`knowmemo source scan wechat` 输出 §12 的报告。**

#### M1 完成情况（2026-09-17）

✅ **已交付。**

模块（全部按 F5 重写，未采用 §13 的 `VersionAdapter` 方案）：

| 文件 | 职责 |
|---|---|
| `connectors/wechat/database.py` | `ReadOnlyDatabase`，`mode=ro`；仅在无 `-wal` 边车文件时才用 `immutable=1`（否则会静默隐藏最新行）；**类里没有任何写接口** |
| `connectors/wechat/schema.py` | schema 自省、`Msg_*` 表发现、经 `contact.db` 反查；遇到意外形状**记录 warning 而不抛异常** |
| `connectors/wechat/decoders.py` | 明文 / zstd / app XML；纯媒体消息的 `extract_text()` 刻意返回 `None`，避免占位符污染语义检索 |
| `connectors/wechat/parser.py` | raw row → domain 对象；**群聊他人消息标 `unresolved` 且 `sender_name=None`**（`packed_info_data` 未解码，猜一个像样的名字比留空更糟） |
| `connectors/wechat/extractor.py` | 四个契约方法 + 未映射 `localType` 的显式上报 |
| `tests/fixtures/wechat/builder.py` | 合成快照生成器，参数化 `layout` / `schema_variant` / `shards` |

**一处对 §13 的实质纠正**：差异轴不是"微信版本"，是**表与行**——库是分片的，消息不在 `message` 表里而在每联系人一张 `Msg_<md5(username)>` 动态表，部分 `message_content` 是 zstd。§13 警告"不要硬编码 `table = message`"方向对，但它给的替代方案也不适用，因为压根没有一张叫 `message` 的表。

**一处诚实性约定**：`extract()` 返回 `ExtractedConversation`（会话 + 消息），不是 §10 草图的裸 `Conversation`——`Conversation` 只带计数不带载荷，而分段需要消息本身。改一个概念签名，好过让 `Conversation` 同时表示两种东西。

**一处 M2 期间发现并修掉的报告错误**：`scan` 的 `earliest`/`latest` 原本取自 `session.db` 的 `last_timestamp`，那是"会话最后活跃时间"，不是消息时间跨度——一份跨越半年的快照会被报告成"结束于某一瞬间"。已改为对每张消息表取 `MIN/MAX(createTime)`。**错误的表格比空表格危险**，所以这不算小修。

### M2 — 分段 + 知识文档

| 模块 | 职责 |
|---|---|
| `processing/segmentation.py` | 按 `create_time` 排序 → 超时切分 → 条数/token 上限（§15） |
| `knowledge/document_builder.py` | §16 的 Markdown + YAML frontmatter |
| `storage/layout.py` | 派生文件的落盘位置（**§7 目录树之外新增**，理由见下） |
| `normalization/jsonl.py` | `data/normalized/*.jsonl`（§2.3 原始数据保留） |
| `ingestion/pipeline.py` | 连接器 → 归一 → 分段 → 文档 → 段索引，落 §25 状态机与 §26 清单 |
| `cli/main.py` | `source add` / `source scan` / `import wechat [--dry-run]` 接上真实实现 |

#### M2 完成情况（2026-09-17）

✅ **已交付。**

```
$ knowmemo source scan wechat          exit 0   Earliest 2024-12-31 16:00  Latest 18:01
$ knowmemo import wechat --dry-run     exit 0   2 documents to create · 0 errors · 未落盘
$ knowmemo import wechat               exit 0   2 created
$ knowmemo import wechat               exit 0   2 unchanged        ← 幂等
```

**对上面那行验收标准的勘误（留痕，不悄悄改）**

原文写的是"`--dry-run` 产出报告，**且** `data/knowledge/` 有文档"。这两条**互斥**——会写文档的命令就不是 dry run。
现改为：`--dry-run` 证明计划（写零个文件），不带 `--dry-run` 物化它，**两条都算 M2 验收**。

**分段的三条边界**，按优先级依次判定：不活跃时长 > 条数上限 > token 估算上限。优先级被测试断言，不是靠注释保证。两条刻意的限制：

- **不做真分词**。`estimate_tokens()` 是启发式（CJK 1 字 1 token，拉丁 4 字符 1 token），因为 §17 要求没有 LLM 也要能跑；引入某家的 tokenizer 会让分段依赖一个被设计成可选的东西。
- **不切分单条消息**。超过 token 上限的单条消息自成一段；切它就得发明续接标记，而半句话的 chunk 检索质量很差。

**文档体重复 frontmatter 的原因**：WeKnora 是**先分块再嵌入**的，取自长对话中段的 chunk 不会带上 frontmatter。所以每份文档开头都有一小段上下文（会话、时间段、参与者、来源），几十个 token 的代价，换来"引用得到出处"而不是一段漂浮的碎片。

**时间戳不带时区**。微信存的是裸 Unix 时间戳，连接器拒绝为它编造时区；打印 `10:00` 是诚实的，打印 `10:00+08:00` 不是。

**相对 §7 目录树的新增**：`storage/layout.py`。派生文件的命名（`data/knowledge/<source>/<slug>__<segment_id>.md`）必须只有一个权威答案，否则"同一会话写到哪"会分裂成多份实现。文件名由**稳定标识符**派生而非时间戳或计数器——这是 §2.4 幂等性的落地点。

### M3 — WeKnora（**本轮到 M2 为止，M3 等你放行**）

- `knowledge/weknora_client.py`，接口按 F2 / F3 修正（补 `resolve_by_hash()`）
- 本地 `segment → knowledge_id` 映射表
- 元数据索引（F1 的前置过滤器）
- 知识文档以 `.md` 文件 multipart 上传（F3）

**验收：`--import` 落库，`knowmemo ask` 返回带出处的答案。**

---

## 4. 决策记录（2026-09-17）

### D1 — KnowMemo 自己的基础设施范围 → **待定**

文档 §6 内部矛盾（见 1.3）。我的建议：**KnowMemo 只用 SQLite**，
Postgres/Redis 是 WeKnora 的依赖，不引入 KnowMemo 的依赖树。

> 状态：已实现为 `storage.database_url` 单一配置项，默认 `sqlite:///./data/knowmemo.db`。
> 换成 Postgres 只需改这一行，但依赖树里目前没有装任何 Postgres 驱动。

### D2 — 时间/人物检索的实现位置 → **待定**

WeKnora 不支持元数据过滤（F1）。我的建议：**过滤留在 KnowMemo 侧**，用 `knowledge_ids` 下推。
替代方案是"先检索 top-K 再在 KnowMemo 侧过滤"，但有召回损失。

> 状态：已按建议实现 `segments` 表 + `SegmentRepository.find()`（支持 participant / 时间区间 /
> 会话 / 来源过滤，时间语义为**区间重叠**而非包含）。
> `segment_knowledge_map` 表已建好，用于把段 ID 翻译成 WeKnora 的 knowledge ID。

### D3 — 段文档的上传形态 → **待定**

`manual` 接口不支持 metadata（F3）。我的建议：**以 `.md` 文件 multipart 上传**。

> 状态：`KnowledgeDocument.weknora_metadata()` 已按 WeKnora 的 `map[string]string` 约束
> 实现（列表压平成逗号串，时间转 ISO 字符串），两条上传路径都能用。

### D4 — 微信明文数据的获取方式 → **已答复，需重新决策**

你的答复：

> 只读取用户通过官方导出/备份产生的合法数据，不要去影响正在运行的微信，要符合微信规定协议。

这个约束我完全认同，但核实后（**F9**）它把三条路**全部**排除了：

| 方案 | 是否满足你的约束 | 说明 |
|---|---|---|
| (a) 外部工具 + 明文快照 | ✗ | 那些工具做的正是读运行中微信的进程内存 |
| (b) KnowMemo 内置解密 | ✗ | 同上，且额外需要 sudo 与重签 |
| (c) 消费导出文件 | ✗ | **微信官方不产出任何导出文件** |

**结论：在"官方来源 + 不干扰运行中微信 + 合规"三重约束下，微信当前没有可编程的输入。**

### D5 — 本轮范围 → **已定：做到 M2**

M0–M2 全程不需要 Docker、不需要 sudo、不需要真实数据。

### D6 — WeKnora 运行环境 → **已定：先不管，M3 往后放**

本机无 Docker（F6）。M3 保持阻塞，等后续处理。

---

## 5. D4 修订版：微信这条路怎么走

三条出路，我倾向第 2 条。

### 5.1 换掉第一个 connector（推荐）

设计文档 §1.1 与 §2.2 的核心主张本来就是 **source-agnostic**，微信只是"第一个 connector"，
不是产品的定义。§1.1 原话：

> Do not design the system as a "WeChat RAG application".
> Design it as: A Personal Memory Ingestion and Retrieval Platform, with WeChat as the first connector.

既然微信在合规约束下没有输入，那就把"第一个 connector"换成一个**有合法明文输入**的源。
这不违反文档，恰恰是在执行它的主张。候选（都是明文、零提权、零逆向）：

| 候选 | 输入形态 | 现成解析 | 备注 |
|---|---|---|---|
| **Markdown / 本地文件夹** | 目录树 | 无需解析 | 零成本，可立即端到端跑通；能覆盖 Obsidian / 笔记 |
| **浏览器历史** | 明文 SQLite | 无依赖 | Chrome/Safari 的 History DB 未加密，读法简单 |
| **PDF / DOCX / TXT** | 文件 | 有成熟库 | 适合文档型记忆 |
| **邮件** | mbox / IMAP | 标准格式 | 记忆密度高，但接入成本略大 |
| **日历 / 通讯录** | macOS 官方 API | 官方授权接口 | 完全合规，用户授权即可读 |

其中 **Markdown/本地文件夹** 最适合当第一个：能立刻把 M0–M2 的 `--dry-run` 报告跑出真数据，
而且 §2.3（保留原始数据）、§2.4（幂等）、§33（合成 fixture）三条原则天然成立。

### 5.2 保留微信，但把提权问题显式外置

KnowMemo 不实现任何解密，只提供 `WeChatSnapshotConnector`，输入契约是"一个明文 SQLite 目录"。
**KnowMemo 本身不提权、不碰运行中的微信、不违反任何协议** —— 责任边界干净。
代价：这份明文快照由你自行合法取得，KnowMemo 不为它的来源负责。
架构上这仍然是正确的设计，只是它无法在 MVP 阶段被端到端验证。

### 5.3 停在这里，等微信开放

承认微信不是当前可做的源，把 connector 目录留空，先做别的。
最保守，但会让 §43 的 MVP 验收标准（"我以前和 Alice 聊过 AI Agent 吗？"）失去主语。

---

## 6. 本轮我**不**打算做的事

- 不写 M3 的代码
- 不碰任何真实微信数据
- 不引入 Postgres / Redis / Celery / Kafka
- 不实现 `summarization` / `entity_extraction` 的逻辑（只留骨架）
- 不改 git 配置（remote 已就绪：`git@github.com:lpeixin/knowmemo.git`）

---

## 7. 待补的核实项（不阻塞 M0–M2）

| 项 | 何时需要 |
|---|---|
| WeKnora `knowledge_ids` 的数量上限 | M3 前 |
| WeKnora Swagger（`http://localhost:8080/swagger/index.html`）逐条比对 | 需 Docker 起来后，M3 前 |
| WeKnora 的 chunk 级 metadata 是否可用于**结果后过滤** | M3 前 |
| `qwen3.5:9b` 在中文会话摘要上的实际质量 | M2 后（可选增强阶段） |
