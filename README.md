<!--
 Licensed to the Apache Software Foundation (ASF) under one
 or more contributor license agreements.  See the NOTICE file
 distributed with this work for additional information
 regarding copyright ownership.  The ASF licenses this file
 to you under the Apache License, Version 2.0 (the
 "License"); you may not use this file except in compliance
 with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing,
 software distributed under the License is distributed on an
 "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 KIND, either express or implied.  See the License for the
 specific language governing permissions and limitations
 under the License.
-->

# Lattice 数据平台

> 一个完全运行在本机回环地址上的中文数据平台：十四种数据源、数据质量核查（规则模板、批量下发、自定义 SQL 与跨库比对）、
> 智能问数、Apache Polaris 元数据目录、Apache Ossie 语义模型与建立在它之上的指标平台，参照 OpenMetadata 实现的元数据管理、
> 数据血缘与面向 AI 的上下文层（含 MCP 服务），以及多用户登录、通用任务调度、查询结果缓存、可观测性和 AI 辅助，一条命令启动。

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](scripts/pyproject.toml)
[![React](https://img.shields.io/badge/React-19-61DAFB.svg)](web/package.json)
[![Polaris](https://img.shields.io/badge/Apache%20Polaris-1.7.0-F26522.svg)](integrations/polaris/README.md)

---

## 目录

- [项目简介](#项目简介)
- [核心功能能力](#核心功能能力)
- [架构与原理](#架构与原理)
- [页面功能展示](#页面功能展示)
- [安装、部署与启动](#安装部署与启动)
- [测试与验证](#测试与验证)
- [目录结构](#目录结构)
- [当前功能边界](#当前功能边界)
- [后期优化与迭代改进计划](#后期优化与迭代改进计划)
- [许可证与致谢](#许可证与致谢)

---

## 项目简介

**Lattice** 是构建在本仓库之上的数据平台层：一个同源的 FastAPI 服务、一套中文 WebUI、
本地引擎与服务的生命周期脚本，以及对 Apache Polaris 的真实集成。它把「数据源接入 →
元数据编目与血缘 → 数据质量 → 语义模型 → 查询与问数 → AI 代理」这条链路收敛到一个本机可复现的环境里。

平台的全部组件只监听回环地址（`127.0.0.1`），不依赖任何云服务即可完整运行；所有凭据、
数据与日志都写在项目内的 `.runtime/` 目录下，删除该目录即可完全重建。

### Lattice 与 Apache Ossie 的关系

这是两个不同的东西，本文档中始终分开使用：

| | 指代 | 说明 |
| --- | --- | --- |
| **Apache Ossie** | 语义模型**规范**本身 | 以及它的 JSON Schema、官方校验器与各家转换器。见 [`core-spec/`](core-spec/)、[`converters/`](converters/)、[`validation/`](validation/) |
| **Lattice** | 规范之上的**平台层** | WebUI、后端服务、启动脚本与本地集成。见 [`webapi/`](webapi/)、[`web/`](web/)、[`scripts/`](scripts/) |

Apache Ossie 保留它原有的名字、命令与行为；Lattice 只是围绕它搭建的平台。上游项目地址：
[apache/ossie](https://github.com/apache/ossie)。仓库根目录的 `./lattice` 仅仅是一个启动器，
它派发到的全部是 Apache Ossie 自己的校验器和转换器，子命令名、参数和行为都没有改变。

---

## 核心功能能力

### 1. 多引擎数据源接入

WebUI 支持十四种数据源类型，每一种都可以新增连接、测试连通性、浏览库表字段、预览数据并执行只读 SQL：

| 类型 | 分类 | 驱动 | 默认端口 |
| --- | --- | --- | --- |
| DuckDB | 本地文件 | `duckdb` | — |
| MySQL | 关系数据库 | PyMySQL | 3306 |
| PostgreSQL | 关系数据库 | psycopg 3 | 5432 |
| Oracle | 关系数据库 | python-oracledb（thin 模式） | 1521 |
| SQL Server | 关系数据库 | pymssql | 1433 |
| ClickHouse | OLAP 引擎 | clickhouse-connect | 8123 |
| StarRocks | OLAP 引擎 | PyMySQL（MySQL 协议） | 9030 |
| Apache Doris | OLAP 引擎 | PyMySQL（MySQL 协议） | 9030 |
| Apache Hive | 数据湖 / 数仓 | PyHive（Thrift + pure-sasl） | 10000 |
| Apache Iceberg | 数据湖表格式 | pyiceberg + DuckDB | — |
| Apache Paimon | 数据湖表格式 | pypaimon + DuckDB | — |
| MongoDB | 文档数据库 | pymongo + DuckDB 快照 | 27017 |
| Elasticsearch | 搜索引擎 | REST API（httpx）+ DuckDB 快照 | 9200 |
| Apache Kafka | 消息队列 | kafka-python + DuckDB 快照 | 9092 |

MongoDB、Elasticsearch 与 Kafka 没有 SQL：连接器把集合、索引、主题当作表，把抽样文档或最近的消息
（JSON 展开为字段）读入内存 DuckDB 后执行 SQL，每张表最多读取「快照行数」条记录，质量核查会标注扫描不完整。
Oracle 与 SQL Server 分别用 `FETCH FIRST` 与 `TOP` 限制返回行数，质量核查按各自方言生成语句。

项目同时提供九个**开箱即用的本地实例**，写入同样的七张示例表
（`t_lattice_orders`、`t_lattice_order_items`、`t_lattice_customers`、`t_lattice_products`、
`t_lattice_sellers`、`t_lattice_payments`，以及供 Apache Ossie 示例模型使用的 TPC-DS 风格 `store_sales`，`lattice_demo` 库，共 4490 行），
因此同一条 SQL 在九个数据源上返回一致的结果，可用来直接比较各引擎的行为差异。

机密只保存在本机，接口一律返回掩码；内置数据源由本机运行的引擎自动登记，每次启动重新生成。

### 2. 数据质量

参考 [Apache DataVines](https://github.com/datavane/datavines) 的度量模型实现：
每条规则生成一条「不合规行」查询和一条计数查询，把实际值与期望值按**结果公式、比较符和阈值**
比较得出成功或失败，并按下式计分：

```
得分 = (核查数 - 不合规数) / 核查数 × 100
```

八类核查规则：

| 核查规则类型 | 度量 | 不合规的含义 |
| --- | --- | --- |
| 唯一性校验 | 重复值检查 | 该字段出现多次的取值个数 |
| 完整性校验 | 空值检查、空字符串检查 | 字段为 NULL 或为空串的行数 |
| 准确性校验 | 正则匹配、字段长度、区间检查 | 不满足格式、长度或取值区间的行数 |
| 数据标准校验 | 枚举值检查 | 不在允许枚举内的行数 |
| 关联性校验 | 关联存在性检查 | 在关联表中找不到对应主键的行数 |
| 及时性校验 | 数据新鲜度检查 | 时间字段早于「当前时间 − 间隔」的行数 |
| 一致性校验 | 跨库比对 | 本库与参照数据源的行数 / 求和 / 平均 / 最小 / 最大值之差，参照值作为核查数量，百分比即相对差异 |
| 自定义校验 | 自定义 SQL | 一条只读 SELECT（可用 `${table}`、`${column}`、`${schema}` 占位符）返回的每一行，或它返回的单个数值 |

- **元数据存储**：规则、调度、执行日志与核查结果保存在 PostgreSQL 的 `lattice_quality` 模式中，
  表结构对应 DataVines 的 `dv_rule`、`dv_job_schedule`、`dv_job_execution`、`dv_job_execution_result`。
  该模式在服务启动时自动创建，**只新增对象，不读取也不修改库中原有的业务表**。
- **规则模板与批量下发**：模板是不带目标表的规则定义，首次启动写入 10 个内置模板（`dv_rule_template`），
  任意规则可「存为模板」；批量下发把一个模板应用到一个数据源下勾选的多张表，每个目标单独校验、单独失败。
- **AI 推荐规则**：读取表的字段与目录里已有的字段含义、标签，按主键 / 外键 / 金额 / 邮箱 / 手机号 / 时间字段等约定给出规则，
  配置模型后由模型补充；每条建议都用与保存规则相同的校验代码预检，勾选后一键创建。
- **通用任务调度**：一个后台线程驱动所有登记的任务（质量核查、元数据拾取、洞察快照、视图血缘同步、缓存清理），
  支持 Quartz 六位 cron（秒在最前，如 `0 0 12 * * ?`）与 Unix 五位写法；每个调度可设置失败重试次数与间隔，
  以及停机期间错过触发的补跑策略（跳过 / 补跑一次 / 逐次补跑），每次尝试都写入执行记录；同一任务不并发触发，
  cron 无效的任务会被停止并记录原因，不影响其他任务。
- **安全**：所有核查 SQL 都经过与 SQL 工作台相同的只读校验，标识符按方言引用，
  正则、枚举、数值等字面量单独校验后才拼入 SQL；含分号、控制字符或子查询的输入会被拒绝。
  错误数据只做只读抽样展示，**不写回任何数据源**。

### 3. 智能问数

面向任一已注册数据源提问，输出图表与数据表，并保留查询历史。

- **未配置模型**时使用内置规则：规则以 DuckDB SQL 写一次，按所选引擎的方言转换后执行，
  因此九个本地引擎都能回答内置问题，页面明确标注「本地规则」。
- **配置模型后**由模型读取该数据源的真实表结构并生成 SQL。模型未必严格遵守目标方言，
  因此引擎拒绝执行时会按目标方言转换后自动重试一次，并在思考过程中说明，转换后的 SQL 同样展示给用户。
- **查询结果缓存**：问数、SQL 工作台与指标查询的只读结果按「数据源 + 语句 + 行数上限」缓存（默认 600 秒、最多 500 条，
  超出按最近最少使用淘汰），数据源配置变更时清空、可按表名定向失效；结果旁显示「缓存 · N 秒前」并可刷新跳过缓存。

支持的模型服务商（在「智能问数 → 模型设置」中用下拉列表选择，无需手工拼参数）：

| 服务商 | 协议 | 说明 |
| --- | --- | --- |
| Anthropic 官方 API | Anthropic | 需要 API Key |
| OpenAI 官方 / 兼容接口 | OpenAI | 需要 API Key 与接口地址 |
| DeepSeek、阿里云百炼（通义千问）、月之暗面 Kimi、智谱 AI（GLM） | OpenAI 兼容 | 已预置官方接口地址，填 API Key 即可 |
| Ollama 本地模型、vLLM / 本地兼容服务 | OpenAI 兼容 | 本机服务，无需 API Key |
| 不使用模型 | — | 回到内置规则 |

「读取服务商模型列表」会调用服务商自己的模型列表接口，把账号下真实可用的模型并入下拉列表；
「测试连接」会真实调用一次模型并显示延迟与返回内容。**API Key 只保存在本机，接口一律返回掩码，
任何错误信息都不会回显密钥。** 访问外网服务商时遵循环境中的 `HTTPS_PROXY` / `NO_PROXY`。

### 4. Apache Polaris 集成

- 运行固定版本的官方 **Apache Polaris 1.7.0**（commit `4ac2f059d1cce149453d0a5f1ff1dff980ec97cc`），
  配套独立的 PostgreSQL 集群与 MinIO 对象存储。
- **API 控制台**解析固定版本的完整源规范，展示全部 **78 个接口**的参数、请求体及响应，
  覆盖 Catalog、身份、角色、授权、Iceberg 表与视图、事务、通用表、策略及映射、通知、认证和存储凭据。
- Polaris 1.7.0 自身实现其中 73 个。其余 **5 个原生语义模型接口**
  （`createSemanticModel`、`listSemanticModels`、`loadSemanticModel`、`updateSemanticModel`、`dropSemanticModel`）
  在上游仍是返回 HTTP 501 的占位实现，本项目按官方 OpenAPI 定义在同源的 Lattice 网关中实现了它们：
  请求/响应体与错误码与源规范一致；命名空间必须真实存在，否则 404；文档按仓库内的 Apache Ossie
  JSON Schema 校验，未通过返回 400（`document.version` 接受当前 schema 的 `0.2.0.dev0` 与已发布的
  `0.1.1`，Polaris 官方示例即用后者，文档按原样保存）；更新使用 `entity-version` 乐观并发，版本不匹配返回 409。
- 这些路径上的每一次上游调用都使用**调用方自己的令牌**，权限由 Polaris 按该主体判断，
  网关不会用自己的 root 身份替调用方放行。

### 5. 数据接入（Generic Table 登记）

把任意数据源中的表登记为 Polaris **Generic Table**，使其在 Catalog 中可被统一发现
（数据仍由原引擎存储），并记录来源数据源、Schema、表名与字段定义（`lattice.*` 属性）。
已登记的表可直接跳转到 SQL 工作台，针对其**原始引擎**执行查询，也可随时取消登记。

### 6. SQL 工作台与语义模型

- **SQL 工作台**：对选定数据源执行只读 `SELECT` / `WITH`，限制外部访问、查询时间与返回行数，
  截断时显示提示。
- **语义模型**：使用仓库现有的 Apache Ossie 校验器检查 YAML；既可调用 Polaris 原生语义模型接口
  发布、加载、更新和删除模型，也可将 YAML 保存为 Lattice 扩展的独立版本存档。
- **AI 生成草稿**：勾选数据源里的若干张表，生成 Ossie 语义模型草稿（字段类型映射、时间维度标记、按主键 / 同名外键推断
  relationships、按度量字段与主键生成指标），配置模型后补充描述、同义词与更有业务含义的指标，经校验器检查后载入编辑器。

### 7. 元数据管理、数据血缘与 AI 上下文层

参照 [OpenMetadata](https://github.com/open-metadata/OpenMetadata) 的实体模型与功能实现（其数据质量部分除外，平台沿用上文的数据质量模块）。
代码在 `webapi/metadata_*.py` 与 `web/src/Metadata*.tsx`，接口统一在 `/api/metadata` 下。

| 能力 | 说明 |
| --- | --- |
| 实体模型 | 数据库、消息、仪表板、工作流、机器学习模型、存储、搜索、元数据、API 九类服务及其资产（数据库 / 模式 / 数据表 / 存储过程、主题、仪表板 / 图表 / 数据模型、工作流、模型、容器、搜索索引、API 集合与端点），外加语义模型；完整名称（FQN）规则、版本历史（结构变更升主版本）、软删除与恢复、重命名级联 |
| 元数据拾取 | 十四种平台数据源、本地 Polaris（Catalog、命名空间、Iceberg 表、通用表）与语义模型可自动拾取，默认每天一次，也可手动或按计划运行；只填补空描述，人工维护的描述与字段说明不会被覆盖，源端删除的表标记为已删除。其余连接器登记服务后可手工登记资产、导入或经 MCP 写入 |
| 数据治理 | 术语库与术语（同义词、相关术语、审核流状态、互斥）、分类与标签（内置 PII、PersonalData、Tier）、数据域与数据产品、团队与用户、所有者与关注、自定义属性 |
| 数据血缘 | 表级与字段级血缘：解析 `INSERT … SELECT`、`CREATE TABLE … AS`、`CREATE VIEW`、`MERGE`，拾取时重新解析视图定义，登记 Polaris 通用表与语义模型的来源，支持手工登记与 OpenMetadata 导入；血缘图可按层展开字段映射，并提供影响分析与**根因分析**（沿上游血缘收集质量核查失败、表结构变更、资产下线、拾取失败与近期修改，按强度和距离排序） |
| 使用情况 | 智能问数、SQL 工作台与 MCP 执行的查询按所读的表计入使用率，资产页展示近期查询 |
| 洞察与告警 | 描述、所有者、分级、标签、血缘覆盖率的每日快照与趋势，KPI 目标跟踪；按对象类型、事件、变更内容订阅告警，站内通知或带 HMAC 签名的 Webhook |
| 协作 | 活动信息流、对话、任务（请求描述 / 标签，接受后自动生效）与公告 |
| AI 上下文层 | 每个资产的上下文卡片（描述、责任人、术语定义、字段、血缘、查询）；智能问数生成 SQL 时自动附带数据源的业务语义；元数据助手按目录回答问题；模型生成描述建议供人工审核 |
| MCP 服务 | `POST /api/metadata/mcp`（Streamable HTTP，JSON-RPC 2.0），提供搜索、读取资产、血缘、术语、修改资产、登记血缘、读取数据源语义、只读查询、指标目录与指标查询等 14 个工具，以及资源与提示模板；同时导出 OpenAI 与 Anthropic 的函数调用格式。登录启用后凭 API 令牌访问 |
| 存储与迁移 | 业务库 `lattice_metadata` 模式；PostgreSQL 不可达时回退到 `.runtime/webui/metadata.sqlite`。支持导出 JSON，导入 Lattice 导出文件与 OpenMetadata API 返回的 JSON |

接入 Claude Code 等 MCP 客户端（启用登录后追加 `--header "Authorization: Bearer <令牌>"`）：

```bash
claude mcp add --transport http lattice-metadata http://127.0.0.1:8787/api/metadata/mcp
```

### 8. 指标平台与语义层查询引擎

指标定义**只来自 Apache Ossie 语义模型**（内置演示模型、Lattice 模型存储与 Polaris 原生语义模型三处都会读取），
语义模型不再只是被校验和存储，而是可以直接查询：

- **指标目录**：列出每个指标的名称、含义、同义词、计算表达式、所属数据集与模型，可按关键词搜索；模型发布后自动刷新。
- **指标查询**：选择模型、指标、维度（`数据集.字段`，时间字段可按日 / 周 / 月 / 季 / 年）与筛选条件，
  引擎解析指标与字段表达式、按 relationships 以 LEFT JOIN 关联所需数据集，生成目标引擎方言的聚合 SQL，
  经与其他查询相同的只读校验、结果缓存、历史与使用率记录执行，也可以只看 SQL。
- **消费**：`/api/metrics/*` 供程序调用，MCP 工具 `list_metrics` 与 `query_metric` 让智能体按同一口径取数。

### 9. 多用户与鉴权

默认仍按单人本机使用运行；设置 `LATTICE_AUTH=1` 后首次访问创建管理员，此后所有接口需要登录会话或 API 令牌。

| 角色 | 权限 |
| --- | --- |
| 管理员 | 全部功能，包括用户与权限管理 |
| 编辑者 | 除用户管理外的全部功能 |
| 只读 | 只能浏览与执行只读查询（问数、SQL 工作台、预览、元数据搜索与助手），不能修改数据、不能使用 MCP |

每个用户还可以单独设置可见页面（只影响导航，权限边界始终是角色）。停用、改角色或重置密码都会让该用户现有会话失效，
至少保留一个可用管理员，连续输错密码会被暂时锁定。账号与会话保存在 SQLite（`LATTICE_AUTH_DB`），密码 scrypt 加盐哈希，
会话与令牌只存哈希，登录不依赖 PostgreSQL。

### 10. 可观测性

- **结构化日志**：每个 API 请求一行（默认单行文本，`LATTICE_LOG_JSON=1` 时为 JSON），含追踪 ID、路由模板、状态码、耗时、用户与内部步骤数，从不记录查询语句。
- **指标**：按路由模板统计请求次数、状态码分布与耗时直方图，引擎查询、模型调用、核查语句单独计时；`GET /api/observability/metrics` 以 Prometheus 文本格式导出。
- **链路追踪**：每个请求返回 `X-Lattice-Trace-Id`，保留最近 200 个请求的步骤耗时、属性与错误。
- **运行观测页**：请求量与错误率、内存与线程、各组件健康、最慢路由、最近错误与慢请求、日志级别切换、可点开的追踪列表。

### 11. AI 增强

三项辅助能力都先按规则工作，配置模型后再由模型补充，并且都不会自行写入，结果交给人确认：
**规则推荐**（数据质量页）、**语义模型草稿**（语义模型页）与**根因分析**（数据血缘页）。接口在 `/api/ai/*`。

---

## 架构与原理

### 整体架构

```mermaid
flowchart TB
    subgraph Browser["浏览器 127.0.0.1:8787"]
        UI["React 19 + TypeScript + Vite<br/>17 个功能页面 · ECharts 图表"]
    end

    subgraph Origin["同源 FastAPI 服务 webapi/"]
        API["REST API · 192 个路由"]
        AUTH["auth.py<br/>登录 / 角色 / 令牌"]
        OBS["observability.py<br/>日志 / 指标 / 追踪"]
        DS["datasources.py<br/>数据源注册表"]
        CONN["connectors.py<br/>十四种连接器 + 只读 SQL 校验"]
        CACHE["query_cache.py<br/>查询结果缓存"]
        Q["quality_*.py<br/>规则 / 模板 / 度量"]
        TASKS["tasks.py<br/>任务注册表与调度"]
        METRICS["semantic_query.py<br/>语义层查询 / 指标平台"]
        AI["ai_assist.py<br/>规则推荐 / 模型草稿 / 根因"]
        ING["ingest.py<br/>Generic Table 登记"]
        SEM["semantic_models.py<br/>5 个原生语义模型接口"]
        GW["polaris.py<br/>认证网关"]
        LLM["llm.py<br/>模型接入"]
        MD["metadata_*.py<br/>元数据目录 / 血缘 / 洞察 / 上下文 / MCP"]
        STATIC["静态资源<br/>web/dist"]
    end

    subgraph Engines["本地数据引擎"]
        E1["DuckDB · MySQL · PostgreSQL"]
        E2["ClickHouse · StarRocks · Doris"]
        E3["Hive · Iceberg · Paimon"]
        E4["Oracle · SQL Server<br/>MongoDB · Elasticsearch · Kafka"]
    end

    subgraph Polaris["Apache Polaris 1.7.0"]
        P["Catalog / 身份 / 策略"]
        PG[("独立 PostgreSQL<br/>:55432")]
        S3[("MinIO S3<br/>:19000")]
    end

    BIZ[("业务库 PostgreSQL :5432<br/>lattice_quality · lattice_metadata 模式")]
    EXT["外部模型服务商<br/>Anthropic / OpenAI 兼容 / 本地"]
    AGENT["AI 代理<br/>MCP 客户端"]

    UI -->|同源 fetch| API
    API --> AUTH
    API --> OBS
    API --> STATIC
    API --> DS --> CONN --> Engines
    API --> CACHE --> CONN
    API --> Q --> BIZ
    Q --> CONN
    Q --> TASKS
    API --> METRICS --> CACHE
    API --> AI
    API --> ING --> GW
    API --> SEM --> GW
    GW -->|调用方令牌| P
    P --> PG
    P --> S3
    API --> LLM -.->|可选| EXT
    API --> MD --> BIZ
    MD --> CONN
    MD --> GW
    AGENT -.->|MCP| API
```

### 请求链路

1. 浏览器加载 `web/dist` 的静态资源，全部 API 调用都是**同源**请求，不存在跨域配置。
   中间件先做本机地址与 JSON 边界检查，再由鉴权模块识别会话或令牌并按角色放行，最后为请求开一条追踪并写一行结构化日志。
2. 后端按 `source_id` 从数据源注册表取出记录，交给对应连接器；连接器负责方言引用、
   只读校验、超时与行数限制。只读结果先查缓存，未命中才执行并写入缓存。
3. 涉及 Polaris 的操作统一经过 `polaris.py` 网关；语义模型路径透传调用方令牌，
   由 Polaris 判定权限。
4. 质量规则在真实数据源上只读执行，结果写入业务库的 `lattice_quality` 模式。
5. 元数据拾取按计划调用同一套连接器读取库表结构，写入 `lattice_metadata` 模式；每次查询执行后按所读的表记录使用情况，
   智能问数调用模型前从目录读取该数据源的业务语义。
6. 指标查询把语义模型编译成目标方言的聚合 SQL 后走同一条查询链路；所有后台任务（核查、拾取、快照、血缘同步、缓存清理）
   由同一个调度器按任务注册表分发，带重试与补跑。

### 关键设计原理

| 设计 | 原理与收益 |
| --- | --- |
| **单一同源入口** | 一个 FastAPI 进程同时提供 UI 静态资源与全部 API，浏览器无需跨域，网关不把 root 密钥下发前端 |
| **连接器抽象** | 十四种引擎统一为 `Connector` 接口（连接、探测、列表、预览、查询、方言引用），新增引擎只需实现一个类 |
| **只读 SQL 校验** | 所有用户与模型产生的 SQL 都经同一套校验：仅允许 `SELECT` / `WITH`，拒绝分号、控制字符与危险构造，字面量单独校验后拼接 |
| **方言转换** | 以 `sqlglot` 在 DuckDB 方言与目标引擎方言之间转换，使一套内置规则适配九个引擎 |
| **运行时隔离** | 所有状态写入 `.runtime/`（引擎数据、Polaris、凭据、日志、Python 环境），已加入 `.gitignore`，删除即可完全重建 |
| **凭据处理** | 私有配置以 `0600` 原子写入，接口一律返回掩码，错误信息不回显密钥 |
| **进程所有权** | 启动脚本以 PID + `ps` 身份 + 所属目录三重校验进程归属，**从不终止不是自己启动的进程** |
| **构建版本检查** | `/api/health` 返回构建摘要，页面比对自身加载时的版本，重新构建后会提示刷新，避免长开标签页一直运行旧代码 |
| **任务注册表** | 后台任务不再各自开线程，而是登记到 `tasks.py` 的注册表，由一个调度器按名称分发，统一获得重试、补跑与执行记录 |
| **鉴权边界** | 登录默认关闭以兼容单人使用；开启后角色是唯一的权限边界，页面可见性只影响导航，MCP 客户端凭令牌访问 |
| **语义模型为唯一口径** | 指标不单独存储，全部从 Ossie 模型读取并编译成 SQL，问数、指标平台与智能体取到的是同一个定义 |
| **进程内可观测** | 路由模板而非具体路径作为指标维度，避免基数爆炸；日志从不记录查询语句与机密 |

### 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 19、TypeScript 5.7、Vite 6、ECharts 6、lucide-react |
| 后端 | Python 3.12、FastAPI、uvicorn、sqlglot、duckdb、pyarrow、psycopg 3、PyMySQL、clickhouse-connect、PyHive、pyiceberg、pypaimon、python-oracledb、pymssql、pymongo、kafka-python、httpx、croniter |
| 元数据 | Apache Polaris 1.7.0、PostgreSQL（`lattice_quality` / `lattice_metadata` 模式）、MinIO；账号与会话用 SQLite |
| 工具链 | uv（固定摘要下载）、pytest、prettier |

---

## 页面功能展示

WebUI 采用左侧导航 + 多标签工作区，共 17 个页面：

| 页面 | 主要能力 |
| --- | --- |
| **指标总览** | 平台整体指标与示例数据图表入口 |
| **数据地图** | 浏览已注册数据源的库、表、字段与数据量 |
| **数据源** | 卡片式列表；新增、编辑、测试、删除十四种类型连接；「环境预设」可套用本机已有数据源参数；内置数据源可隐藏并一键恢复 |
| **数据接入** | 把外部表登记为 Polaris Generic Table；已登记的表可直接跳转 SQL 工作台查询原始引擎，或取消登记 |
| **数据质量** | 五个页签：核查规则（九种内置核查 + 自定义 SQL + 跨库比对，规则模板与批量下发）、调度任务（可调度平台任意登记任务，含重试与补跑）、质量报告、统计分析、执行日志 |
| **元数据管理** | 元数据资产（资产树、分面搜索、资产详情）、数据血缘、元数据检测（告警、通知、活动信息流）、元数据洞察（趋势、应用分析、KPI）、元数据工作区（数据域与数据产品）、元数据系统（术语库、分类、自定义属性）、AI 上下文（元数据助手、MCP、工具调试、数据源语义）、元数据拾取（服务与添加向导、团队和用户、拾取记录、导入导出） |
| **标准规范** | 查看仓库内现有的 Apache Ossie 规范与模型校验结果 |
| **语义模型** | Apache Ossie YAML 校验；通过 Polaris 原生接口发布 / 加载 / 更新 / 删除，或存为独立版本存档；AI 生成草稿 |
| **指标平台** | 指标目录（来自全部语义模型）与指标查询构建器：模型、指标、维度与时间粒度、筛选，生成 SQL 并出图 |
| **SQL 工作台** | 对选定数据源执行只读 SQL，显示实际结果、耗时、缓存标记与截断提示；查询缓存面板 |
| **数据服务** | 数据服务相关视图 |
| **智能问数** | 自然语言提问，展示思考过程、生成的 SQL、图表与历史；支持模型设置与连接测试 |
| **Catalog 管理** | 展示真实 Polaris Catalog 元数据，可进入管理 API 执行变更 |
| **身份与权限** | Polaris 主体、角色与授权 |
| **API 控制台** | 全部 78 个接口的参数、请求体与响应；标注哪些由 Polaris 原生实现、哪些由 Lattice 网关实现 |
| **运行观测** | 请求指标、组件健康、慢请求与错误、日志级别、链路追踪明细，Prometheus 指标入口 |
| **用户与权限** | 平台账号、角色、页面可见性、会话与 API 令牌（登录未启用时显示说明） |

### 界面截图

以下截图取自本机启动后的实际页面（`docs/images/`）。

| 指标总览 | 数据地图 |
| --- | --- |
| ![指标总览](docs/images/overview.png) | ![数据地图](docs/images/map.png) |

| 数据源 | 数据接入 |
| --- | --- |
| ![数据源](docs/images/sources.png) | ![数据接入](docs/images/ingestion.png) |

| 数据质量 | 元数据资产 |
| --- | --- |
| ![数据质量](docs/images/quality.png) | ![元数据资产](docs/images/metadata-explore.png) |

| 数据血缘与根因分析 | 元数据洞察 |
| --- | --- |
| ![数据血缘](docs/images/metadata-lineage.png) | ![元数据洞察](docs/images/metadata-insights.png) |

| AI 上下文与 MCP | 语义模型 |
| --- | --- |
| ![AI 上下文](docs/images/metadata-context.png) | ![语义模型](docs/images/semantic.png) |

| 指标平台 | SQL 工作台 |
| --- | --- |
| ![指标平台](docs/images/metrics.png) | ![SQL 工作台](docs/images/sql.png) |

| 智能问数 | 标准规范 |
| --- | --- |
| ![智能问数](docs/images/questions.png) | ![标准规范](docs/images/standards.png) |

| 数据服务 | 运行观测 |
| --- | --- |
| ![数据服务](docs/images/services.png) | ![运行观测](docs/images/ops.png) |

| 用户与权限 | Catalog 管理 |
| --- | --- |
| ![用户与权限](docs/images/users.png) | ![Catalog 管理](docs/images/catalogs.png) |

| 身份与权限 | API 控制台 |
| --- | --- |
| ![身份与权限](docs/images/identities.png) | ![API 控制台](docs/images/explorer.png) |

---

## 安装、部署与启动

### 前置依赖

需要预先安装：

- **Go**（支持自动下载工具链）
- **JDK 21+** 与 **Maven**
- **Node.js / npm**
- **PostgreSQL 15+** 的命令行程序（本机使用 PostgreSQL 16；脚本会查找 Homebrew 等常见位置，
  其他目录可设 `LATTICE_PG_BIN`）
- **curl、OpenSSL、lsof、ps**
- **Docker**（仅 StarRocks、Doris、Hive 需要；缺少时这三个引擎保持离线，其余功能不受影响）

uv 0.9.30、Python 3.12、官方 Polaris 与 MinIO 由脚本自动准备，下载的运行时会校验固定摘要。
**脚本不会使用 sudo，也不会修改 shell 配置。**

### 一键启动

```bash
git clone https://github.com/Mdz-Bigdata/lattice.git
cd lattice
./start.sh
```

macOS 也可在 Finder 中双击根目录的 `start.command`。

默认执行完整测试：检查 Python 核心包、全部 Python 转换器、校验器、Go CLI、Java 转换器、
Web API 和运行时管理逻辑，最后启动网页及依赖服务。必要步骤失败会返回非零退出码并保留日志。
**首次运行需要网络下载工具和依赖。**

需要快速重新准备时：

```bash
./start.sh --quick     # 跳过完整测试，仍执行构建、基本运行检查和 WebUI 启动
```

### 日常启动、状态与停止

```bash
./start-web.sh            # 启动 WebUI 与全部本地示例引擎
./start-web.sh status     # 查看状态
./start-web.sh restart    # 重启
./start-web.sh stop       # 停止本项目的服务，保留数据
./start-web.sh engines    # 只启动本地示例引擎
```

macOS 日常使用可双击 `start-web.command`。重复 `start` 会检查并复用已就绪的实例；
端口冲突会明确失败。指定其他端口：`LATTICE_WEB_PORT=8788 ./start-web.sh`。

启动后访问 **<http://127.0.0.1:8787>**。

### 服务与端口

| 服务 | 本机地址 | 数据 / 日志 |
| --- | --- | --- |
| WebUI / API | `127.0.0.1:8787` | `.runtime/webui/`（`server.log`、`startup.log`） |
| Polaris | `127.0.0.1:8181` | `.runtime/polaris/` |
| Polaris 健康检查 | `127.0.0.1:8182/q/health` | readiness / liveness |
| 独立 PostgreSQL | `127.0.0.1:55432` | `.runtime/polaris/postgres/` |
| MinIO S3 | `127.0.0.1:19000` | `.runtime/polaris/object-store/` |
| PostgreSQL（业务库） | `127.0.0.1:5432/blog_converter` | 质量元数据在 `lattice_quality` 模式，元数据目录在 `lattice_metadata` 模式，示例表在 `public` 模式 |
| MCP 服务 | `127.0.0.1:8787/api/metadata/mcp` | 与 WebUI 同一进程 |
| MySQL | `127.0.0.1:33306` | `.runtime/engines/mysql/` |
| ClickHouse | `127.0.0.1:18123`（HTTP）/ `19009` | `.runtime/engines/clickhouse/` |
| StarRocks | `127.0.0.1:19030`（MySQL 协议）/ `18030` | Docker 卷 `lattice-starrocks-*` |
| Apache Doris | `127.0.0.1:29030`（MySQL 协议）/ `28030` | Docker 卷 `lattice-doris-*` |
| Apache Hive | `127.0.0.1:20000`（HiveServer2）/ `20002` | 容器 `lattice-hive` |
| Paimon 仓库 | 本地文件 | `.runtime/engines/paimon/warehouse/` |

Polaris 服务凭据位于 `.runtime/polaris/credentials.json`，MinIO 凭据位于 `local-s3.json`，权限均为 `600`。

### 环境变量

平台的环境变量一律使用 `LATTICE_` 前缀：

| 变量 | 作用 |
| --- | --- |
| `LATTICE_WEB_PORT` | WebUI / API 端口，默认 8787 |
| `LATTICE_QUALITY_DSN` | 数据质量元数据库连接串 |
| `LATTICE_METADATA_DSN` | 元数据目录的数据库连接串，默认与数据质量相同；也接受 `sqlite:///路径` |
| `LATTICE_METADATA_SCHEDULER` | 设为 `0` 时不启动元数据拾取与洞察快照线程 |
| `LATTICE_QUERY_CACHE` | `1` |
| `LATTICE_QUERY_CACHE_TTL` | `600` |
| `LATTICE_QUERY_CACHE_ENTRIES` | `500` |
| `LATTICE_AUTH` | `0` |
| `LATTICE_AUTH_DB` | `.runtime/webui/auth.sqlite` |
| `LATTICE_SESSION_HOURS` | `12` |
| `LATTICE_LOG_JSON` | `0` |
| `LATTICE_LOG_LEVEL` | `INFO` |
| `LATTICE_LLM_PROVIDER`、`LATTICE_LLM_MODEL`、`LATTICE_LLM_API_KEY`、`LATTICE_LLM_BASE_URL` | 智能问数的默认模型配置 |
| `LATTICE_PG_BIN` | PostgreSQL 命令行程序所在目录 |
| `LATTICE_MYSQLD` | 本地示例 MySQL 的 `mysqld` 路径 |
| `LATTICE_ENGINE_PULL` | 设为 `0` 时不下载容器镜像；StarRocks、Doris、Hive 将保持离线 |

### 备份与重建

所有状态都在 `.runtime/` 下。备份时先停止服务，再备份整个 `.runtime/polaris/` 与 `.runtime/webui/`；
删除 `.runtime/` 后重新运行 `./start.sh` 即可从零重建。

---

## 测试与验证

```bash
# 平台自动化测试（当前 866 项）
.runtime/envs/core/bin/python -m pytest scripts/tests webapi/tests -q

# 前端类型检查与构建
cd web && npm run build

# 真实接口验证（需先启动服务）
.runtime/envs/core/bin/python scripts/verify-webui.py
```

`verify-webui.py` 使用唯一名称创建临时目录、身份、表、视图和策略，执行后清理，
报告写入 `.runtime/webui/verification.json`。报告**区分成功操作、预期的权限拒绝及上游未实现的 501**，
不将返回错误的接口记作功能成功；它还会临时创建一个只读主体，验证网关确实按调用方权限放行。

最近一次启动日志位于 `.runtime/logs/latest.log`，失败后先查看该文件。

---

## 目录结构

```
.
├── webapi/               # FastAPI 后端：数据源与十四种连接器、质量（规则 / 模板 / 调度）、任务注册表、查询缓存、
│   │                     #   元数据目录与血缘、MCP、语义层查询与指标、鉴权、可观测性、AI 辅助、接入、语义模型、Polaris 网关
│   └── tests/            # 后端测试
├── web/                  # React + TypeScript WebUI
│   └── src/              # 17 个功能页面与共用组件
├── docs/images/          # README 使用的界面截图
├── scripts/              # 启动与运行时管理：本地引擎、WebUI 服务、验证脚本
│   └── tests/            # 脚本测试
├── integrations/
│   ├── polaris/          # 固定版本 Polaris 服务与解析后的 API 规范
│   └── engines/          # 引擎镜像与二进制的固定版本信息
├── core-spec/            # Apache Ossie 规范、JSON Schema 与文档
├── converters/           # Apache Ossie 官方转换器（dbt、GoodData、Polaris、Salesforce 等）
├── validation/           # Apache Ossie 校验工具
├── examples/             # 示例语义模型
├── docs/local-start.md   # 更详细的本地启动与功能说明
├── tasks/                # 项目计划与进度记录
├── start.sh              # 一键准备与启动
├── start-web.sh          # 日常启动 / 停止 / 状态
└── lattice               # Apache Ossie 校验器与转换器的启动器
```

---

## 当前功能边界

为避免误解，以下内容**尚未实现或依赖外部条件**：

- Apache Ossie **Go CLI** 的 `convert`、`validate`、`plugin install`、`plugin remove`
  仍是仓库中的占位实现。根目录的 `lattice` 直接调用已有的 Python 和 Java 工具；
  `cli/dist/ossie` 是另一个程序，它构建和测试通过**不代表**这些占位命令已实现。
- **Trino、Genie** 不在本仓库中。
- **Catalog federation、OIDC、OPA / Ranger、事件基础设施、云对象存储**需要对应的服务与凭据。
- **Salesforce → Apache Ossie** 导入所需 schema 会在首次运行时从 Salesforce 官方文档下载并校验。
- **Iceberg 与 Paimon** 通过 DuckDB 读取，单表最多扫描 50 万行；**MongoDB、Elasticsearch、Kafka** 按「快照行数」读取抽样文档或最近消息，
  聚合结果是快照上的结果；页面与质量核查都会标注统计可能不完整。SQL Server 不支持正则核查。
- 调度器只运行在单个服务进程内，没有跨进程的分布式锁；多副本部署时同一任务可能被各副本分别触发。
- **元数据自动拾取**覆盖平台的十四种数据源、本地 Polaris 与语义模型。OpenMetadata 连接器目录中的其他连接器（Snowflake、Kafka、Tableau、Airflow 等）
  只能登记服务，资产需要手工登记、导入 OpenMetadata JSON 或经 MCP 写入。OpenMetadata 的数据质量与数据剖析没有移植。
- **元数据助手与 AI 描述建议**需要先在智能问数中配置模型；未配置时只展示检索到的目录上下文。规则推荐、语义模型草稿与根因分析在未配置模型时按启发式规则工作。
- 登录默认关闭：未开启时所有请求以本机管理员身份执行，元数据操作记在用户 `lattice` 名下，MCP 写操作记在 `mcp-agent` 名下。
  开启后角色只有管理员 / 编辑者 / 只读三级，没有对象级授权；账号只存在本机 SQLite 中，没有接入 OIDC / LDAP。
- 可观测性是进程内实现：指标与追踪随进程重启清零，没有对接外部的指标或追踪后端。
- 元数据搜索基于数据库 `LIKE` 与内存打分，没有 Elasticsearch / OpenSearch，适合本机规模的目录。
- 测试验证的是本机环境与仓库覆盖的场景；具体输入能否转换，仍由对应转换器的支持范围决定。

---

## 后期优化与迭代改进计划

> 打勾的条目已在本仓库实现并有测试覆盖；未打勾的仍在规划中。

### 近期（工程化与易用性）

- [ ] **容器化交付**：提供 `docker compose` 一键拉起全套依赖，降低对本机 Go / JDK / Maven / PostgreSQL 的要求
- [ ] **CI 流水线**：为 `webapi`、`web`、`scripts` 建立独立的 GitHub Actions 工作流，替代继承自上游的转换器 CI
- [x] **页面截图**：界面截图已收录到 `docs/images/`（见上文「界面截图」）；端到端演示录屏待补
- [ ] **前端测试**：引入组件与端到端测试，目前前端仅有类型检查与构建校验
- [ ] **English README**：面向非中文使用者提供英文文档

### 中期（能力增强）

- [x] **多用户与鉴权**：目前平台假定单人本机使用，需要登录态、会话与基于角色的页面权限
- [x] **通用任务调度**：把调度器从仅支持 `QualityTask.run` 扩展为可注册任意任务，并补齐失败重试与补跑策略
- [x] **质量规则增强**：自定义 SQL 规则、跨库比对、规则模板与批量下发
- [x] **数据血缘**：基于 SQL 解析与 Polaris 元数据构建表级 / 字段级血缘视图
- [x] **更多数据源**：Oracle、SQL Server、MongoDB、Elasticsearch、Kafka
- [x] **查询结果缓存**：对重复问数与报表查询增加带失效策略的缓存层

### 长期（平台化）

- [x] **可观测性**：每个请求写结构化日志（可切 JSON），按路由模板统计计数与耗时，保留最近 200 条链路追踪，
      Prometheus 文本指标可直接抓取，“运行观测”页展示组件健康、慢请求、错误和追踪明细。
- [x] **语义层查询引擎**：Ossie 语义模型可直接查询，引擎按关系自动关联数据集、按目标引擎方言生成聚合 SQL，
      走统一的只读校验与缓存。数据集 `source` 里的 `main` / `public` / `default` 视为“所选数据源的默认库”，
      指标表达式中的裸列名在数据集唯一时按物理列解析，因此按 Ossie 示例手写的 `public.store_sales` 模型不用改就能跑。
- [x] **指标平台**：指标定义只来自语义模型，“指标平台”页有指标目录和查询构建器，MCP 新增两个指标工具供智能体取数。
      内置演示模型补齐了关系和六个指标；示例数据新增 TPC-DS 风格的 `store_sales`，同一个 `total_sales`
      在九个本地数据源（含业务库 PostgreSQL）上算出同一结果。
- [x] **AI 增强**：规则推荐、语义模型草稿、基于血缘的根因分析。三者无模型时按启发式规则工作，配置模型后由模型补充，
      结果都交人确认后再写入。
- [ ] **跟进 Apache Ossie 上游**：持续同步规范演进，并在上游实现原生语义模型接口后
      切换回官方实现（见 [ROADMAP.md](ROADMAP.md)）

欢迎通过 [Issues](https://github.com/Mdz-Bigdata/lattice/issues) 提出需求与问题。

---

## 许可证与致谢

本项目基于 **[Apache License 2.0](LICENSE)** 开源，派生自
[apache/ossie](https://github.com/apache/ossie)（Apache Ossie，incubating），
完整保留上游的 `LICENSE`、`NOTICE` 与 `DISCLAIMER`。

致谢以下上游项目：

- **[Apache Ossie](https://github.com/apache/ossie)** — 语义模型规范、JSON Schema、校验器与转换器
- **[Apache Polaris](https://polaris.apache.org/)** — 元数据目录服务
- **[Apache DataVines](https://github.com/datavane/datavines)** — 数据质量度量模型的设计参考
- **[OpenMetadata](https://github.com/open-metadata/OpenMetadata)** — 元数据实体模型、血缘、数据洞察与 MCP 上下文层的设计参考
- **Apache Iceberg、Apache Paimon、Apache Doris、Apache Hive、StarRocks、ClickHouse、DuckDB、PostgreSQL、MySQL**

> Apache、Apache Ossie、Apache Polaris 及相关标识是 Apache 软件基金会的商标。
> 本仓库是个人对上游项目的派生与扩展，**不隶属于 Apache 软件基金会，也不代表其立场**。

更详细的本地启动与功能说明见 [docs/local-start.md](docs/local-start.md)；
Polaris 集成细节见 [integrations/polaris/README.md](integrations/polaris/README.md)。
