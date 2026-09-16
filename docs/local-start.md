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

# 一键启动 Lattice WebUI 和 Apache Polaris

项目现在包含按参考截图制作的中文 WebUI —— **Lattice 数据平台**，以及真实 Apache Polaris 1.7.0 服务。启动后访问 **[http://127.0.0.1:8787](http://127.0.0.1:8787)**。仓库原有的 Apache Ossie 语义规范、校验器和转换器不受影响，通过根目录的 `./lattice` 命令继续使用。

本文中的 **Lattice** 指本仓库之上的数据平台（WebUI、后端服务、启动脚本与本地集成），**Apache Ossie** 始终指语义模型规范本身及其官方校验器、转换器和 JSON Schema。两者是不同的东西。

## 一键启动

在项目根目录运行：

```bash
./start.sh
```

macOS 也可在 Finder 中双击根目录的 `start.command`。启动结束后窗口保留结果，按回车关闭。

需要预先安装 Go（支持自动下载工具链）、JDK 21+、Maven、Node.js/npm、PostgreSQL 15+ 的命令行程序，以及 curl、OpenSSL、lsof 和 ps。本机使用 PostgreSQL 16；脚本会查找 Homebrew 等常见位置。PostgreSQL 在其他目录时可设置 `LATTICE_PG_BIN`。uv 0.9.30、Python 3.12、官方 Polaris 和 MinIO 由脚本自动准备；下载的运行时会校验固定摘要。脚本不会使用 sudo 或修改 shell 配置。

默认执行完整测试，检查 Python 核心包、全部 Python 转换器、校验器、Go CLI、Java 转换器、Web API 和运行时管理逻辑，最后启动网页及依赖服务。必要步骤失败会返回非零退出码并保留日志。首次运行需要网络下载工具和依赖。

需要快速重新准备时：

```bash
./start.sh --quick
```

`--quick` 跳过完整测试，仍执行构建、基本运行检查和 WebUI 启动。启动脚本退出后，后台服务继续运行。

日常启动、查看状态和停止：

```bash
./start-web.sh
./start-web.sh status
./start-web.sh restart
./start-web.sh stop
```

macOS 日常使用可双击 `start-web.command`。`stop` 停止本项目的 WebUI、Polaris、PostgreSQL、MinIO 和本地示例引擎，保留数据。重复 `start` 会检查并复用已就绪的实例。端口冲突会明确失败；进程管理检查命令、启动时间及所属目录，不终止无关进程。`LATTICE_WEB_PORT=8788 ./start-web.sh` 可指定其他网页端口。

| 服务 | 本机地址 | 数据 / 日志 |
| --- | --- | --- |
| WebUI / API | `127.0.0.1:8787` | `.runtime/webui/`，`server.log`、`startup.log` |
| Polaris | `127.0.0.1:8181` | `.runtime/polaris/`，`server.log`、`polaris.log` |
| 健康检查 | `127.0.0.1:8182/q/health` | Polaris readiness / liveness |
| 独立 PostgreSQL | `127.0.0.1:55432` | `.runtime/polaris/postgres/` |
| MinIO S3 | `127.0.0.1:19000` | `.runtime/polaris/object-store/` |
| PostgreSQL（业务库） | `127.0.0.1:5432/blog_converter` | 本机已有的库，质量元数据在 `lattice_quality` 模式，七张示例表在 `public` 模式 |
| MySQL | `127.0.0.1:33306` | `.runtime/engines/mysql/` |
| ClickHouse | `127.0.0.1:18123`（HTTP）/ `19009` | `.runtime/engines/clickhouse/` |
| StarRocks | `127.0.0.1:19030`（MySQL 协议）/ `18030` | Docker 卷 `lattice-starrocks-*` |
| Apache Doris | `127.0.0.1:29030`（MySQL 协议）/ `28030` | Docker 卷 `lattice-doris-*` |
| Apache Hive | `127.0.0.1:20000`（HiveServer2）/ `20002` | 容器 `lattice-hive` |
| Paimon 仓库 | 本地文件 | `.runtime/engines/paimon/warehouse/` |

Polaris 服务凭据位于 `.runtime/polaris/credentials.json`，MinIO 凭据位于 `local-s3.json`，权限为 600。浏览器通过本机的 Lattice 网关访问 Polaris，网关不发送 root 密钥给前端。通过 API 控制台轮换或重置 root 凭据时，新值写回本机私有配置。需要备份时，先停止服务，再备份整个 `.runtime/polaris/` 和 `.runtime/webui/`。

### 环境变量

平台的环境变量一律使用 `LATTICE_` 前缀。早期版本的旧前缀变量已经不再被识别，请把 shell、脚本和 launchd 配置中的旧变量改成下表中的名字。

| 变量 | 作用 |
| --- | --- |
| `LATTICE_WEB_PORT` | WebUI / API 端口，默认 8787 |
| `LATTICE_QUALITY_DSN` | 数据质量元数据库连接串 |
| `LATTICE_LLM_PROVIDER`、`LATTICE_LLM_MODEL`、`LATTICE_LLM_API_KEY`、`LATTICE_LLM_BASE_URL` | 智能问数的默认模型配置 |
| `LATTICE_PG_BIN` | PostgreSQL 命令行程序所在目录 |
| `LATTICE_MYSQLD` | 本地示例 MySQL 的 `mysqld` 路径 |
| `LATTICE_ENGINE_PULL` | 设为 `0` 时不下载容器镜像；StarRocks、Doris、Hive 将保持离线 |

平台只传给自己子进程的内部变量（`LATTICE_WEB_INSTANCE_ID`、`LATTICE_WEB_RUNTIME`、`LATTICE_PYTHON`）由脚本自行设置和读取，不需要手工配置。

## 本地示例引擎与数据源

WebUI 的数据源页面支持十四种类型：DuckDB、MySQL、PostgreSQL、ClickHouse、StarRocks、Apache Doris、Apache Hive、Apache Iceberg、Apache Paimon、Oracle、SQL Server、MongoDB、Elasticsearch 和 Apache Kafka。每种类型都可以在页面中新增连接、测试连通性、浏览库表字段、预览数据并执行只读 SQL。

Oracle（python-oracledb thin 模式）与 SQL Server（pymssql）是普通的 SQL 数据源，分别以 `FETCH FIRST` 和 `TOP` 限制返回行数，质量核查按各自方言生成语句（SQL Server 不支持正则核查）。MongoDB、Elasticsearch 与 Kafka 没有 SQL：连接器把集合 / 索引 / 主题当作表，把抽样文档或最近的消息（JSON 展开为字段，嵌套一层用点号命名）读入内存 DuckDB 后执行 SQL，每张表最多读取“快照行数”条记录，因此聚合结果是快照上的结果；这三类在质量核查中会标注扫描不完整。驱动 `oracledb`、`pymssql`、`pymongo`、`kafka-python` 已列入 `scripts/pyproject.toml`，缺失时数据源测试会给出安装提示。

项目同时提供九个开箱即用的本地实例，写入同样的七张示例表 `t_lattice_orders`、`t_lattice_order_items`、`t_lattice_customers`、`t_lattice_products`、`t_lattice_sellers`、`t_lattice_payments`，以及供 Apache Ossie 示例模型使用的 TPC-DS 风格 `store_sales`（`lattice_demo` 库，共 4490 行），因此同一条 SQL 在九个数据源上返回一致结果：

```bash
./start.sh                  # 首次：准备全部组件并启动九个数据源（含镜像下载）
./start-web.sh              # 日常：启动 WebUI 与全部本地示例引擎
./start-web.sh engines      # 只启动本地示例引擎
.runtime/envs/core/bin/python scripts/local-engines.py status   # 查看每个引擎的版本、表数与行数
```

| 引擎 | 运行方式 | 说明 |
| --- | --- | --- |
| DuckDB | 项目文件 | 启动时生成的 `.runtime/webui/sample.duckdb` |
| PostgreSQL | 本机业务库 | `localhost:5432/blog_converter`，数据质量模块的元数据也在其中；启动时把七张示例表写入其 `public` 模式（只新建缺失的表） |
| MySQL | 本机 `mysqld` | 需要已安装 MySQL（`brew install mysql`），数据目录在项目内 |
| ClickHouse | 官方二进制 | 按 `integrations/engines/clickhouse.json` 固定版本与校验和下载 |
| Iceberg | Apache Polaris | 通过本项目的 Polaris REST Catalog 读写 `lattice.demo` |
| Paimon | pypaimon | 本地文件系统 warehouse，无独立进程 |
| StarRocks | 官方容器 | `starrocks/allin1-ubuntu` |
| Apache Doris | 官方容器 | `apache/doris` FE + BE，使用私有 Docker 网络 `lattice-engines` |
| Apache Hive | 官方容器 | `apache/hive` HiveServer2，内置 Derby 元数据库 |

后三者没有可用的 macOS 原生版本，改用 `integrations/engines/docker.json` 中固定的官方镜像（合计约 13 GB），需要本机运行 Docker。容器是 `lattice-starrocks`、`lattice-doris-fe`、`lattice-doris-be` 和 `lattice-hive`，数据分别存放在命名卷 `lattice-starrocks-fe-meta`、`lattice-starrocks-be-storage`、`lattice-doris-fe-meta`、`lattice-doris-be-storage`、`lattice-hive-warehouse`、`lattice-hive-metastore` 中。所有容器都带 `lattice.project` 标签并只监听回环地址（`docker ps --filter label=lattice.project` 可以列出它们）；脚本只操作带该标签的容器，不会启动、停止或删除本项目之外的任何容器、网络或卷。`./start.sh` 和 `./start-web.sh start` 会在镜像缺失时下载它们，并在终端显示下载进度；镜像只下载一次，之后每次启动都直接复用。不想下载时设 `LATTICE_ENGINE_PULL=0`，这三个引擎会标记为未就绪，其余功能照常可用。容器使用 `--restart unless-stopped`，Docker 或 macOS 重启后会自动恢复运行，因此数据源页面不会因为重启而变成离线；`./start-web.sh stop` 停止后不会被自动拉起。

三个引擎同时运行实测占用约 4.3 GB 内存（StarRocks 1.3 GB、Doris FE 0.9 GB、Doris BE 1.3 GB、Hive 0.8 GB），Docker 至少要有 6.5 GB 可用内存，Docker Desktop 的默认 8 GB 即可。低于该值时脚本会在启动前提示，容器被内存杀死时也会直接说明原因，可在 Docker Desktop → Settings → Resources → Memory 调大后重试。镜像解压后在 Docker 磁盘上约占 22 GB。

没有 Docker 时，StarRocks、Doris 和 Hive 的连接器仍然可用，可在数据源页面填写外部服务地址后测试与查询。

## 数据质量

Lattice 的数据质量模块参考 [Apache DataVines](https://github.com/datavane/datavines) 的度量模型：每条规则生成一条“不合规行”查询和一条计数查询，把实际值与期望值按结果公式、比较符和阈值比较，得出成功或失败，并按 `(核查数 - 不合规数) / 核查数 × 100` 计算得分。

六类核查规则与对应度量：

| 核查规则类型 | 度量 | 不合规的含义 |
| --- | --- | --- |
| 唯一性校验 | 重复值检查 | 该字段出现多次的取值个数 |
| 完整性校验 | 空值检查、空字符串检查 | 字段为 NULL 或为空串的行数 |
| 准确性校验 | 正则匹配检查、字段长度检查、区间检查 | 不满足格式、长度或取值区间的行数 |
| 数据标准校验 | 枚举值检查 | 不在允许枚举内的行数 |
| 关联性校验 | 关联存在性检查 | 在关联表中找不到对应主键的行数 |
| 及时性校验 | 数据新鲜度检查 | 时间字段早于「当前时间 - 间隔」的行数 |

DataVines 把正则、长度、区间归入 COMPLETENESS，把跨表核查归入 ACCURACY；本模块按上面的六类界面重新分组，其余语义保持一致。及时性校验没有照搬上游的 `DATE_FORMAT` 脚本（该写法在 PostgreSQL 上不是合法 SQL），改为按方言直接比较时间戳。

### 元数据存储

规则、调度、执行日志和核查结果保存在本机业务库 `blog_converter` 的 `lattice_quality` 模式中，表结构对应 DataVines 的 `dv_rule`、`dv_job_schedule`、`dv_job_execution` 和 `dv_job_execution_result`，另有记录每次任务尝试的 `dv_task_run`。已有的 `dv_job_schedule` 会在启动时以 `ADD COLUMN IF NOT EXISTS` 补上重试与补跑相关的列。该模式由服务启动时自动创建，只新增对象，不读取也不修改库中原有的业务表。连接串默认取自 `LATTICE_QUALITY_DSN`，未设置时使用 `postgresql://postgres:postgres@localhost:5432/blog_converter`；SQLAlchemy 风格的 `+asyncpg` 等驱动后缀会被自动去掉，后端使用 psycopg 3 同步连接。

平台的运行时标识符统一使用 `lattice` 前缀：质量元数据模式 `lattice_quality`、示例库 `lattice_demo`、Polaris 元数据库与角色 `lattice_polaris`、示例表 `t_lattice_*`、Polaris 目录 `lattice`（realm `LATTICE`）与存储桶 `lattice-warehouse`、Docker 标签 `lattice.project`、网络 `lattice-engines` 以及 `lattice-` 开头的容器和卷名。这些对象都由启动脚本创建和写入，重建运行时即可重新生成。

同一个库同时注册为内置数据源「业务库 PostgreSQL（数据质量）」，因此规则可以直接核查其中的真实业务表。元数据库不可用时，Lattice 其余功能照常启动，质量页面会显示明确的中文提示。

### 自定义 SQL、跨库比对、模板与批量下发

除九种内置核查外，规则可以选择两种由自己生成全部语句的核查类型：

- **自定义 SQL**（分类“自定义校验”）：填写一条只读 SELECT，可用 `${table}`、`${column}`、`${schema}` 占位符引用规则目标；“返回的每一行都是不合规记录”模式把语句结果计数作为不合规数，“返回的单个数值作为实际值”模式直接用语句返回的数值（请给该列取别名 `actual_value`）。语句不允许注释、分号或多条语句，并经过与 SQL 工作台相同的只读校验。
- **跨库比对**（分类“一致性校验”）：在两个数据源上分别计算行数、求和、平均、最小或最大值，差异作为不合规数量，参照值作为核查数量，因此“百分比”计算方式得到的就是相对差异。参照侧的 SQL 按参照数据源自己的方言生成。

**规则模板**保存不带目标表的规则定义，首次启动时写入 10 个内置模板（`dv_rule_template`），也可以从任意规则“存为模板”。**批量下发**把一个模板应用到一个数据源下勾选的多张表（字段级核查对所有表使用同一字段名），每个目标单独校验、单独失败，接口为 `POST /api/quality/rules/batch`。

### 规则执行与调度

手动执行在请求内同步完成，受连接器的查询超时限制。调度由服务内的一个后台线程驱动，cron 支持界面所示的 Quartz 6 位写法（秒在最前，`?` 等同 `*`，如 `0 0 12 * * ?`），也支持 5 位 Unix 写法。同一任务不会并发触发；cron 无效的任务会被停止并记录原因，不会影响其他任务。

调度器按任务名称分发：`webapi/tasks.py` 里的注册表在服务启动时登记 `QualityTask.run`（质量核查全量执行）、`MetadataTask.ingest`（元数据拾取，参数为服务 FQN，留空则拾取到期服务）、`MetadataTask.snapshot`（洞察快照，参数可为日期）与 `MetadataTask.syncViews`（视图血缘同步）。新增的后台任务只需调用 `registry.add(bean, method, label, callable)` 登记，无需再开线程。每个调度可设置**失败重试次数**（0–10）与**重试间隔**（秒），以及**错过触发时**的策略：`skip` 从现在重新计算下一次（默认）、`once` 立即补跑一次、`all` 按错过的每个时间点逐次补跑（受**补跑上限**限制）。手动“执行”不会自动重试，失败直接返回错误。每一次尝试都写入 `dv_task_run`，在“执行记录”抽屉里可按调度与状态查看；相关接口为 `GET /api/quality/tasks`、`GET /api/quality/task-runs`。

所有核查 SQL 都经过与 SQL 工作台相同的只读校验，标识符按方言引用，正则、枚举、数值等字面量单独校验后才拼入 SQL；含分号、控制字符或子查询的输入会被拒绝。错误数据只做只读抽样展示，不写回任何数据源。Iceberg 与 Paimon 通过 DuckDB 读取，单表最多扫描 50 万行，页面会对这两类数据源标注统计可能不完整。

## AI 增强

三项辅助能力都先按规则工作，配置模型后再由模型补充，且都不会自行写入，结果交给人确认：

- **规则推荐**（数据质量 → 核查规则 → “AI 推荐规则”）：读取所选表的字段与目录里已有的字段含义、标签，按主键 / 外键 / 金额 / 邮箱 / 手机号 / 时间字段等命名与类型约定给出核查规则，模型可再补充；每条建议都用与保存规则相同的校验代码预检，勾选后一键创建。
- **语义模型草稿**（语义模型 → “AI 生成草稿”）：把勾选的表生成 Ossie 语义模型：字段类型映射、时间维度标记、按主键 / 同名外键推断 relationships、按度量字段与主键生成指标；模型可补充描述、同义词与更有业务含义的指标（只保留引用了真实数据集与字段的结果）；草稿经 Ossie 校验器检查后可载入编辑器。
- **根因分析**（元数据管理 → 数据血缘 → 影响分析旁）：沿上游血缘最多三层收集信号——质量核查未通过、表结构变更（删字段、改类型）、资产下线、拾取失败、近期修改——按信号强度与距离打分排序并给出结论；配置模型时由模型撰写分析文字，否则用模板生成。

接口：`GET /api/ai/status`、`POST /api/ai/quality-rules/suggest|apply`、`POST /api/ai/semantic-model/suggest`、`POST /api/ai/root-cause`。

## 语义层查询引擎与指标平台

语义模型不再只是被校验和存储：`webapi/semantic_query.py` 把 Ossie 模型直接变成可查询的语义层。

- **指标目录**：指标定义只来自语义模型（内置演示模型、Lattice 模型存储与 Polaris 原生语义模型三处都会读取），“指标平台”页列出每个指标的名称、含义、同义词、计算表达式、所属数据集与模型，可按关键词搜索；模型保存或发布后 60 秒内自动刷新，也可手动“重新读取模型”。
- **指标查询**：选择模型、指标、维度（`数据集.字段`，时间字段可按日 / 周 / 月 / 季 / 年）与筛选条件（维度字段作 WHERE，指标作 HAVING），引擎解析指标与字段表达式、按 relationships 以 LEFT JOIN 关联所需数据集（以指标所在的数据集为根），生成目标引擎方言的聚合 SQL，经与其他查询相同的只读校验、结果缓存、历史与使用率记录执行，结果用现有的图表组件展示，也可以“只看 SQL”。数据集的 `source` 按 `库.表` 解析，也可以是一段 SELECT；`main`、`public`、`default` 这三个“默认库”写法会换成所选数据源自己的默认库（本地引擎上是 `lattice_demo`，本地示例数据上直接省略），只有在该方言里它本来就是真实模式时才原样保留（PostgreSQL 的 `public`、ClickHouse 的 `default`），因此按 Ossie 示例写成 `public.store_sales` 的模型在九个数据源上都能执行。指标表达式里未在 `fields` 中声明的裸列名，会在数据集唯一（模型只有一个数据集，或表达式只点名了一个数据集）时按该数据集的物理列处理，手写的 `SUM(ss_ext_sales_price)` 无需先补字段定义。
- **消费**：`POST /api/metrics/query`、`/api/metrics/compile`、`GET /api/metrics/catalog|models|model` 供程序调用；MCP 上下文层新增 `list_metrics` 与 `query_metric` 两个工具，智能体按同一口径取数。

内置演示模型补充了六个数据集之间的关系与 `total_sales`、`order_count`、`item_count`、`avg_order_value`、`total_payment`、`customer_count` 六个指标，可在本地示例数据上直接计算。示例数据还带有一张 TPC-DS 风格的 `store_sales`（`ss_sold_date_sk`、`ss_item_sk`、`ss_customer_sk`、`ss_store_sk`、`ss_ticket_number`、`ss_quantity`、`ss_sales_price`、`ss_ext_sales_price`、`ss_net_paid`，由订单明细派生，`SUM(ss_ext_sales_price)` 与 `total_sales` 相等），Apache Ossie 规范中的 `store_sales` 示例模型无需改动即可在全部九个数据源上算出同一结果。

## 可观测性

服务内置进程内的可观测能力，不依赖外部组件：

- **结构化日志**：每个 API 请求在服务标准输出写一行日志（默认单行文本；`LATTICE_LOG_JSON=1` 时为 JSON），字段包含追踪 ID、路由模板（如 `/api/datasources/{source_id}/query`，不是具体 ID）、状态码、耗时、用户与内部步骤数，从不记录查询语句；启动、就绪、关闭以及各模块的关键事件也会写入。级别由 `LATTICE_LOG_LEVEL` 决定，运行时可在“运行观测”页切换。
- **指标**：按路由模板统计请求次数、按状态码的分布与耗时直方图（p95、最大值），内部步骤（引擎查询、模型调用、核查语句）单独计时；`GET /api/observability/metrics` 以 Prometheus 文本格式导出，可直接被抓取。
- **链路追踪**：每个请求返回 `X-Lattice-Trace-Id`，服务保留最近 200 个请求的追踪，包含各步骤的耗时、属性与错误；`GET /api/observability/traces` 支持按路由、耗时、状态码筛选，`/traces/{id}` 查看单条。
- **运行观测页**：请求量与错误率、内存与线程、各组件健康（质量元数据库、元数据目录、调度器、拾取调度、查询缓存、Polaris、模型）、最慢路由、最近的错误与慢请求、日志事件计数与级别切换，以及可点开查看步骤的追踪列表。

## 多用户与鉴权

平台默认仍按单人本机使用运行：不设置 `LATTICE_AUTH` 时没有登录页，所有请求以“本机管理员”身份执行，“用户与权限”页面只显示说明。设置 `LATTICE_AUTH=1` 后重启，首次打开页面会引导创建第一个管理员；之后所有 `/api/*` 接口（健康检查与登录接口除外）都需要登录会话（HttpOnly Cookie，默认 12 小时，使用时顺延）或 `Authorization: Bearer <令牌>`。

角色决定接口权限：**管理员**可以做一切并管理用户；**编辑者**除用户管理外都可以；**只读**只能发 GET 请求与只读查询（智能问数、SQL 工作台、表预览、元数据搜索与助手），不能使用 MCP 写工具。每个用户还可以单独设置**可见页面**，它只影响左侧导航，权限边界始终是角色。管理员可以停用、删除用户或重置密码；停用、改角色、重置密码都会让该用户现有的登录会话失效；至少要保留一个可用的管理员。同一账号连续 5 次输错密码后会被暂时锁定 5 分钟。

MCP 客户端和脚本使用 **API 令牌**：管理员或编辑者在“用户与权限”里创建（只显示一次，默认 90 天），例如

```bash
claude mcp add --transport http lattice-metadata http://127.0.0.1:8787/api/metadata/mcp --header "Authorization: Bearer <令牌>"
```

账号与会话保存在 `LATTICE_AUTH_DB`（默认 `.runtime/webui/auth.sqlite`），密码以 scrypt 加盐哈希存储，会话与令牌只保存哈希；登录后对元数据的修改会记录为该用户。相关接口在 `/api/auth/*`。

## 查询结果缓存

智能问数、SQL 工作台与报表执行的只读查询，其结果会按“数据源 + 语句（忽略字面量外的空白）+ 行数上限”缓存在服务内存中，由 `webapi/query_cache.py` 实现。失效策略有三类：**到期**（默认 600 秒，可用 `LATTICE_QUERY_CACHE_TTL` 或在 SQL 工作台的“查询缓存”面板修改）、**容量**（默认最多 500 条，超出按最近最少使用淘汰；单条结果超过 5000 行不缓存）与**定向失效**（数据源配置更新或删除时清空该数据源；`POST /api/query/cache/invalidate` 可按数据源、按表名或按条目清理；调度任务 `QueryCacheTask.sweep` 清理过期条目，`QueryCacheTask.clear` 清空）。任何查询请求都可带 `refresh: true` 跳过缓存重新执行，结果里的 `cached` 与 `cache_age_seconds` 说明它是否来自缓存以及有多旧；界面在结果标题旁显示“缓存 · N 秒前”并提供“刷新”。`LATTICE_QUERY_CACHE=0` 可整体关闭。缓存本身不落盘，重启即清空；只有开关与有效期设置保存在 `.runtime/webui/query-cache.json`。

## 元数据管理

「元数据管理」页面参照 OpenMetadata 实现元数据目录、数据血缘与 AI 上下文层（OpenMetadata 的数据质量部分不在其中，平台使用上文的数据质量模块）。

### 存储与启动

目录保存在业务库 `blog_converter` 的 `lattice_metadata` 模式中，服务启动时自动创建，只新增对象。连接串取自 `LATTICE_METADATA_DSN`，未设置时沿用 `LATTICE_QUALITY_DSN` 或默认的本机业务库；PostgreSQL 不可达时自动改用 `.runtime/webui/metadata.sqlite`，页面左下角会显示当前使用的存储。

首次启动后，后台线程会为每个平台数据源和本地 Polaris 登记一个数据库服务，并立即拾取一次库、模式、表、字段、视图定义与血缘，此后默认每天拾取一次。拾取计划可在「元数据拾取 → 服务」中修改；设置 `LATTICE_METADATA_SCHEDULER=0` 可关闭这个线程。在页面上删除的服务不会被自动重新登记，点击「同步平台数据源」可以恢复。

### 血缘来源

- SQL 解析：`INSERT … SELECT`、`CREATE TABLE … AS`、`CREATE VIEW`、`MERGE` 识别写入目标，纯 `SELECT` 需要在页面指定目标；列投影生成字段级映射。
- 视图：拾取时读取 DuckDB、PostgreSQL、MySQL、StarRocks、Doris、ClickHouse 的视图定义并解析。
- Polaris 通用表：通过「数据接入」登记的表会连到其来源表；语义模型的数据集会连到其来源表。
- 手工登记与导入：血缘图上添加上下游，或导入 OpenMetadata 的 `lineage` 数组。

### MCP 服务

端点为 `http://127.0.0.1:8787/api/metadata/mcp`（Streamable HTTP）。Claude Code 接入：

```bash
claude mcp add --transport http lattice-metadata http://127.0.0.1:8787/api/metadata/mcp
```

「AI 上下文 → MCP 服务」页面列出全部工具、资源、提示模板以及 Claude Desktop、Cursor 的配置片段；「工具调试」可以直接调用每个工具。服务只接受本机地址，拒绝跨来源请求；写操作以用户 `mcp-agent` 的身份执行并记录到活动信息流，`query_datasource` 使用与 SQL 工作台相同的只读校验。

## Lattice WebUI 功能

- **智能问数**：面向任一已注册数据源提问，显示月度销售额、类别销售额、商家排名、订单量和平均金额；支持柱状图、折线图、条形图、饼图和数据表，保留查询历史。
  - 未配置模型时使用内置规则。规则以 DuckDB SQL 写一次，按所选引擎的方言转换后执行，因此九个本地引擎都能回答这五个问题，页面标注「本地规则」。
  - 配置模型后由模型读取该数据源的真实表结构并生成 SQL。模型未必严格遵守目标方言，因此引擎拒绝执行时会按目标方言转换后自动重试一次，并在思考过程中说明，转换后的 SQL 同样展示给用户。

### 配置模型

在「智能问数 → 模型设置」中按下拉列表选择服务商与模型，无需手工拼接参数：

| 服务商 | 协议 | 说明 |
| --- | --- | --- |
| Anthropic 官方 API | Anthropic | 需要 API Key |
| OpenAI 官方 / 兼容接口 | OpenAI | 需要 API Key 与接口地址 |
| DeepSeek、阿里云百炼（通义千问）、月之暗面 Kimi、智谱 AI（GLM） | OpenAI 兼容 | 已预置官方接口地址，填 API Key 即可 |
| Ollama 本地模型、vLLM / 本地兼容服务 | OpenAI 兼容 | 本机服务，无需 API Key |
| 不使用模型 | — | 回到内置规则 |

选择服务商会自动填入其接口地址与常用模型；模型也可以选「自定义…」后手工填写。「读取服务商模型列表」会用当前的地址与 API Key 调用服务商自己的模型列表接口（Anthropic 为 `/v1/models`，OpenAI 兼容服务为 `{接口地址}/models`），把账号下真实可用的模型并入下拉列表。读取失败不会影响使用：下拉列表仍显示常用模型，并说明失败原因（未填 API Key、密钥未通过验证、本机网络禁止访问该服务商、该服务未提供列表接口等）。「测试连接」会用当前配置真实调用一次模型并显示延迟与返回内容。API Key 只保存在本机，接口一律返回掩码，任何错误信息都不会回显密钥。

访问外网服务商时会使用环境中的 `HTTPS_PROXY`／`HTTP_PROXY`，与 Anthropic 官方 SDK 的行为一致；`NO_PROXY` 保证 Ollama、vLLM 等本机服务仍走直连。若这两个变量未设置而服务商需要经代理访问，读取模型列表和提问都会失败，并在界面上说明是网络不可达而非密钥问题。
- **数据源**：卡片式列表，新增、编辑、测试和删除九种类型的连接，浏览库表与字段，预览数据。新增时可从「环境预设」下拉列表套用本机已有数据源的连接参数（机密留空由用户填写），保存前即可测试连接；密码等机密只保存在本机，接口一律返回掩码。每张卡片都可以删除：自建数据源会连同配置一起删除，内置数据源只从列表中隐藏（它由本机运行的引擎自动登记，每次启动都会重新生成），用页面右上角的「恢复内置数据源」可以全部找回。

各页面的数据源下拉框以引擎名称作为选项（DuckDB、ClickHouse、Apache Paimon…）；只有多个数据源使用同一引擎时才追加区分信息——优先用所连数据库名，两者连的是同一个库时才回退到数据源名称。PostgreSQL 只登记一个数据源，指向本机业务库 `blog_converter`；项目 PostgreSQL 集群里的 `lattice_demo` 示例库仍由引擎管理脚本创建和写入，只是不再单独列为数据源。为了让语义模型与指标在这个数据源上也能计算，启动时同样把七张示例表写入业务库的 `public` 模式：只新建缺失的表，已有的表（哪怕行数不同）一律不改动，业务库不可达时跳过并提示。

内置规则问数依赖六张业务示例表。所选数据源没有这些表时（例如自建的业务数据源），页面会直接说明缺少哪张表并建议配置模型或改用 SQL 工作台，而不是把引擎的 SQL 报错抛给用户。
- **数据质量**：核查规则、调度任务、质量报告、统计分析与执行日志五个页签，规则在真实数据源上以只读方式执行，详见下一节。
- **数据接入**：把任意数据源中的表注册为 Polaris Generic Table，使其在 Catalog 中可见，并可随时取消注册。
- **SQL 工作台**：对选定数据源执行只读 SELECT / WITH，展示实际结果；限制外部访问、查询时间及返回行数，截断时显示提示。
- **数据与质量页面**：查看表结构、数据量、字段、现有的 Apache Ossie 规范和模型校验结果。
- **语义模型**：使用仓库现有的 Apache Ossie 校验器检查 YAML；既可调用 Polaris 原生语义模型接口发布、加载、更新和删除模型，也可将 YAML 保存为 Lattice 扩展的独立版本存档。
- **Catalog、身份与权限**：展示真实 Polaris 元数据，可进入管理 API 执行变更。
- **API 控制台**：解析固定版本的完整源规范，展示全部 78 个接口的参数、请求体及响应。包含 Catalog、身份、角色、授权、Iceberg 表和视图、事务、通用表、策略及映射、通知、认证和存储凭据。

Apache Polaris 1.7.0 自身实现了其中 73 个接口。其余 5 个原生语义模型接口（`createSemanticModel`、`listSemanticModels`、`loadSemanticModel`、`updateSemanticModel`、`dropSemanticModel`）在上游 `SemanticModelCatalogAdapter` 中仍是返回 HTTP 501 的占位实现，本项目按官方 OpenAPI 定义在同源的 Lattice 网关（`webapi/semantic_models.py`）中实现了它们：

- 请求体与响应体、错误类型和状态码与源规范一致；
- 目标命名空间必须在真实 Polaris 中存在，否则返回 404；
- 文档按仓库内的 Apache Ossie JSON Schema（`core-spec/ossie-schema.json`）校验，未通过返回 400；`document.version` 接受当前 schema 版本 `0.2.0.dev0` 与已发布的 `0.1.1`（Polaris 官方示例所用；0.2.0 只在 0.1.1 之上增加字段，因此按向后兼容规则校验），文档按调用方提交的原样保存；
- 更新使用 `entity-version` 乐观并发，版本不匹配返回 409；
- 模型作为 Generic Table 记录保存在请求指定的 Catalog 与命名空间中，随 Polaris 一同持久化。

外部客户端可以带自己的 Polaris 令牌直接调用本服务上的同名路径 `http://127.0.0.1:8787/polaris/v1/{prefix}/namespaces/{namespace}/semantic-models`。这些路径上的每一次上游调用都使用调用方自己的令牌，因此权限由 Polaris 按该主体的授权判断，网关不会用自己的 root 身份替调用方放行：只读主体可以列出和读取模型，创建、更新和删除会收到 403，未带令牌返回 401。控制台中这些接口标注为“Lattice 网关实现”，其余 73 个仍直接转发给 Polaris。

`scripts/verify-webui.py` 会在每次验证时临时创建一个只读主体来检查这一点，运行后删除。

截图中的 Trino、Genie 和 LLM 不在原仓库中。本地问数明确标注“示例数据 · 本地规则”，实际执行 DuckDB SQL。外部数据源、云对象存储、Catalog federation、OIDC、OPA/Ranger、事件基础设施等需要对应的服务和凭据。完整上游实现、管理工具和配置入口均保留，详见 [Polaris 集成说明](../integrations/polaris/README.md)。

启动服务后可重跑真实接口验证：

```bash
.runtime/envs/core/bin/python scripts/verify-webui.py
```

脚本使用唯一名称创建临时目录、身份、表、视图和策略，执行后清理，报告写入 `.runtime/webui/verification.json`。报告区分成功操作、预期的权限拒绝及上游未实现的 501，不将返回错误的接口记作功能成功。

各 Python 3.12 环境位于 `.runtime/envs/core` 和 `.runtime/envs/<转换器名>`。不需要手动激活环境，也不会要求将项目包安装到系统 Python。最近一次启动日志位于 `.runtime/logs/latest.log`；失败后先查看该文件，修复原因后重新运行 `./start.sh`。

## 使用 Apache Ossie 校验器和转换器

`./lattice` 只是本仓库的启动器，它派发到的全部是 Apache Ossie 自己的校验器和转换器：子命令名、参数和行为都没有变，改的只是启动器的文件名。

```bash
./lattice --help
./lattice validate examples/tpcds_semantic_model.yaml
./lattice validate /path/to/model.yaml --schema /path/to/schema.json
./lattice dbt --help
./lattice nvidia --help
```

`--help` 无需提前安装依赖。实际功能缺少运行环境时会提示先执行 `start.sh`。所有输入、输出相对路径都以调用时所在的目录为准，启动器不会切换工作目录。

| 命令 | 运行对象 |
| --- | --- |
| `./lattice validate` | Apache Ossie Python 校验器，含结构、引用和 SQL 校验 |
| `./lattice dbt` | dbt 转换器 |
| `./lattice databricks` | Databricks 转换器 |
| `./lattice honeydew` | Honeydew 转换器 |
| `./lattice nvidia` | NVIDIA GSF 转换器 |
| `./lattice omni` | Omni 转换器 |
| `./lattice orionbelt` | OrionBelt 转换器 |
| `./lattice sigma` | Sigma 转换器 |
| `./lattice snowflake` | Snowflake 转换器 |
| `./lattice wisdom` | Wisdom 转换器 |
| `./lattice ontology <ZIP 或目录>` | Palantir ontology 转换脚本，YAML 写到标准输出 |
| `./lattice salesforce` | Salesforce Java 转换器 |
| `./lattice polaris` | Polaris Java 转换器，自动包含运行依赖 |
| `./lattice python <core 或转换器名>` | 指定环境中的 Python，支持 GoodData API |

转换器参数各不相同。九个 Python 命令行转换器可以追加 `--help` 查看参数；Java 和 ontology 工具的参数见各自 README，不支持通用的 `--help`。

例如，将 Apache Ossie 模型转换为 dbt semantic manifest（子命令 `ossie-to-msi` 属于转换器本身，保持原名）：

```bash
./lattice dbt ossie-to-msi -i examples/tpcds_semantic_model.yaml -o /tmp/semantic_manifest.json
```

运行本地 Python 脚本或使用只有 API 的 GoodData 转换器（`apache-ossie*` 系列包名保持原样）：

```bash
./lattice python core /path/to/script.py
./lattice python gooddata -c 'import ossie_gooddata; print(ossie_gooddata.__file__)'
```

运行 ontology 转换器：

```bash
./lattice ontology /path/to/palantir_export.zip > /tmp/model.yaml
```

运行 Salesforce 导出；输出写在输入文件所在目录，文件名由模型决定：

```bash
./lattice salesforce toSF /path/to/model.yaml
```

## 当前项目的功能边界

- Apache Ossie Go CLI 的 `convert`、`validate`、`plugin install` 和 `plugin remove` 仍是仓库中的占位实现。根目录的 `lattice` 是本项目的启动器，直接调用已有的 Python 和 Java 工具；上游 Go CLI 的产物 `cli/dist/ossie` 是另一个程序，它的构建和测试通过不代表这些占位命令已实现。
- Polaris Java 转换器保留原有 import/export 格式和协议；现在可配置连接本项目启动的真实 Polaris。其参数见 [转换器 README](../converters/polaris/README.md)，服务说明见 [运行时集成](../integrations/polaris/README.md)。
- Salesforce → Apache Ossie 导入所需 schema 会在首次运行时从 [Salesforce 官方文档](https://developer.salesforce.com/docs/data/semantic-layer/guide/salesforce-semantic-model-schema.html) 下载、提取并校验，保存至 `converters/salesforce/src/main/resources/schemas/salesforce-semantic-model-schema.json`。该下载文件已加入 Git 忽略规则。也可事先手动放入有效 schema，脚本会校验并保留已有文件。Apache Ossie → Salesforce 使用仓库自带的 Apache Ossie schema（`core-spec/ossie-schema.json`）。
- 测试和基本运行检查验证本机环境及仓库覆盖的场景；具体输入是否能转换，仍由对应转换器的格式、支持范围和外部服务状态决定。
