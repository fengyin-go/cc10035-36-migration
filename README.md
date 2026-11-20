# Web语义抽取与内容理解中心

面向Web结构化数据采集场景，提供页面DOM解析、实体识别、关系抽取、内容分类和知识图谱构建的语义抽取平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/migration.py 语义槽位迁移批次/交换数据/入库回执接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/slot_codec.py       v1 字典字符串 <-> v2 交换文档编解码
│   ├── app/services/slot_migration.py   迁移流水线（批次、暂停/恢复、入库、回滚）
│   ├── app/services/base.py     共享服务骨架
│   ├── app/store.py          内存数据仓库与示例数据
│   ├── scripts/slot_migration.py 离线迁移 CLI（不起服务即可演练）
│   └── tests/                离线回归测试
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| DOM解析 | `dom_parser` | 解析配置 | 配置名称、目标URI、解析引擎 |
| 实体抽取 | `entity_extract` | 实体条目 | 实体ID、实体类型、实体名称 |
| 关系图谱 | `relation_graph` | 关系边 | 边ID、源实体、目标实体 |
| 内容分类 | `content_classifier` | 分类标签 | 标签ID、页面URI、一级分类 |
| 模板规则 | `template_rule` | 规则模板 | 模板名称、适用站点、提取字段 |
| 文本分块 | `text_segment` | 文本块 | 块ID、来源页面、块类型 |
| 情感标注 | `sentiment_tag` | 情感记录 | 记录ID、文本片段、情感倾向 |
| Schema对齐 | `schema_align` | 对齐规则 | 规则名称、源Schema、目标Schema |
| 种子调度 | `feed_scheduler` | 调度任务 | 任务名称、URL种子、抓取深度 |
| 质量检查 | `quality_check` | 检查规则 | 规则名称、检查字段、检查类型 |
| 探针选择 | `probe_selector` | 探针配置 | 探针名称、目标页面、选择器列表 |
| 页面快照 | `page_snapshot` | 快照记录 | 快照ID、页面URI、DOM哈希 |
| 抽取指标 | `extract_metric` | 指标记录 | 记录时间、所属任务、成功页面数 |
| 语义结果 | `nlu_result` | 语义记录 | 记录ID、输入文本、意图标签、槽位状态 |
| 知识链接 | `knowledge_link` | 链接记录 | 链接ID、源实体、外部URI |
| 输出目标 | `output_target` | 输出配置 | 配置名称、输出类型、目标地址 |
| 抽取工坊 | `extract_studio` | 交互任务 | 任务名称、示例页面、标注区域 |
| 聚合视图 | `aggregate_view` | 视图配置 | 视图名称、统计维度、聚合字段 |

语义结果的槽位另有带版本交换结构：`槽位列表` 保留可读展示（迁移前是 v1
Python 字典字符串，迁移后改成可读的 `slot=value` 摘要），`结构化槽位` /
`槽位交换ID` / `槽位状态` / `槽位出处` / `原槽位列表` 记录 v2 交换数据的位置、
状态与原始值。情感标注通过 `片段出处` 为槽位提供出处。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 语义槽位迁移

历史语义结果的槽位是 Python 字典字符串（v1），外部读取端无法稳定解析。迁移把
它们写成带版本的交换文档（`schema=nlu.slot-exchange`, `schema_version=2`），
记录原值、转换版本、完整性校验，以及每个槽位引用的情感片段出处。

v2 交换文档的槽位形态：

```json
{"slot_type": "order_id", "value": "A-1024", "value_type": "str",
 "provenance": {"type": "sentiment_snippet", "sentiment_record_id": "sent-1002",
                "snippet": "订单 A-1024 的退款什么时候到账？", "snippet_sha256": "…"}}
```

状态与语义入库：

| 状态 | 含义 | 语义入库 |
| --- | --- | --- |
| 已结构化 `already_structured` | 原本就是 v2 结构 | 核对后接纳 |
| 已转换 `converted` | v1 无损转 v2 | 核对后接纳 |
| 待补正 `needs_review` | 有损转换或来源未核对 | 拒绝，保留原值 |
| 不可转换 `unconvertible` | 语法层面无法还原 | 拒绝，保留原值 |

- 交换ID由「语义身份 + 情感来源 + 原值哈希」确定，迁移中断后重试不会重复。
- 同一批次回滚会把语义行和情感交接清单还原到批次开始前，入库回执标记为已撤销；
  交换文档与原值保留在批次历史里，可继续核对。
- 已入库的历史记录（`status=已入库`）不会被再次接纳。

### 离线演练（部署新版读取端之前）

```bash
# 全流程演练：迁移 3 条 -> 暂停 -> 恢复 -> 迁移剩余 -> 入库 -> 重试 -> 回滚
backend/.venv/bin/python -I backend/scripts/slot_migration.py rehearse --out /tmp/rehearsal.json

# 分步演练，状态在文件之间续跑
backend/.venv/bin/python -I backend/scripts/slot_migration.py snapshot --limit 2 --out /tmp/m1.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py action pause  --state /tmp/m1.json --out /tmp/m2.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py action resume --state /tmp/m2.json --out /tmp/m3.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py migrate --state /tmp/m3.json --out /tmp/m4.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py ingest --run-id dry-run-1 --state /tmp/m4.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py legacy --semantic-id nlu-2001 --state /tmp/m4.json
backend/.venv/bin/python -I backend/scripts/slot_migration.py action rollback --state /tmp/m4.json
```

离线脚本不碰在线内存仓库，`--state` 文件可以随演练交接。回归测试：

```bash
backend/.venv/bin/python -I -m unittest discover -s backend/tests -v
```

### 在线接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/nlu-slot-migration/batch` | 开启新批次 |
| `POST /api/nlu-slot-migration/migrate` | 迁移（可 `limit` 分批） |
| `POST /api/nlu-slot-migration/action` | `开始迁移/暂停/恢复/回滚/重置` |
| `POST /api/nlu-slot-migration/ingest` | 执行一次语义入库，返回接纳/跳过回执 |
| `GET /api/nlu-slot-migration/ingest/runs` | 每次入库接纳了哪些槽位、用了哪版输入 |
| `GET /api/nlu-slot-migration/manifest` | 标注交接清单，按来源列出已转换与待补正 |
| `GET /api/nlu-slot-migration/exchanges` | 按状态/来源/版本核对交换数据 |
| `GET /api/nlu-slot-migration/exchanges/{id}/legacy` | 按旧版本读取已核对原值 |
| `GET /api/nlu-slot-migration/legacy/nlu/{id}` | 旧读取端按语义记录ID回读原值 |

前端工作台：`http://127.0.0.1:5173/slot-migration`，提供开始/暂停/恢复/回滚、
分步迁移、语义入库、交接清单、交换数据核对和旧版回读。

领域：数据工程
子方向：数据采集、爬虫与RPA
