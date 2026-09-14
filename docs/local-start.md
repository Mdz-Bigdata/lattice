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
| PostgreSQL（业务库） | `127.0.0.1:5432/blog_converter` | 本机已有的库，质量元数据在 `lattice_quality` 模式 |
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

WebUI 的数据源页面支持九种类型：DuckDB、MySQL、PostgreSQL、ClickHouse、StarRocks、Apache Doris、Apache Hive、Apache Iceberg 和 Apache Paimon。每种类型都可以在页面中新增连接、测试连通性、浏览库表字段、预览数据并执行只读 SQL。

项目同时提供九个开箱即用的本地实例，写入同样的六张示例表 `t_lattice_orders`、`t_lattice_order_items`、`t_lattice_customers`、`t_lattice_products`、`t_lattice_sellers`、`t_lattice_payments`（`lattice_demo` 库，共 3390 行），因此同一条 SQL 在九个数据源上返回一致结果：

```bash
./start.sh                  # 首次：准备全部组件并启动九个数据源（含镜像下载）
./start-web.sh              # 日常：启动 WebUI 与全部本地示例引擎
./start-web.sh engines      # 只启动本地示例引擎
.runtime/envs/core/bin/python scripts/local-engines.py status   # 查看每个引擎的版本、表数与行数
```

| 引擎 | 运行方式 | 说明 |
| --- | --- | --- |
| DuckDB | 项目文件 | 启动时生成的 `.runtime/webui/sample.duckdb` |
| PostgreSQL | 本机业务库 | `localhost:5432/blog_converter`，数据质量模块的元数据也在其中 |
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

规则、调度、执行日志和核查结果保存在本机业务库 `blog_converter` 的 `lattice_quality` 模式中，表结构对应 DataVines 的 `dv_rule`、`dv_job_schedule`、`dv_job_execution` 和 `dv_job_execution_result`。该模式由服务启动时自动创建，只新增对象，不读取也不修改库中原有的业务表。连接串默认取自 `LATTICE_QUALITY_DSN`，未设置时使用 `postgresql://postgres:postgres@localhost:5432/blog_converter`；SQLAlchemy 风格的 `+asyncpg` 等驱动后缀会被自动去掉，后端使用 psycopg 3 同步连接。

平台的运行时标识符统一使用 `lattice` 前缀：质量元数据模式 `lattice_quality`、示例库 `lattice_demo`、Polaris 元数据库与角色 `lattice_polaris`、示例表 `t_lattice_*`、Polaris 目录 `lattice`（realm `LATTICE`）与存储桶 `lattice-warehouse`、Docker 标签 `lattice.project`、网络 `lattice-engines` 以及 `lattice-` 开头的容器和卷名。这些对象都由启动脚本创建和写入，重建运行时即可重新生成。

同一个库同时注册为内置数据源「业务库 PostgreSQL（数据质量）」，因此规则可以直接核查其中的真实业务表。元数据库不可用时，Lattice 其余功能照常启动，质量页面会显示明确的中文提示。

### 规则执行与调度

手动执行在请求内同步完成，受连接器的查询超时限制。调度由服务内的一个后台线程驱动，cron 支持界面所示的 Quartz 6 位写法（秒在最前，`?` 等同 `*`，如 `0 0 12 * * ?`），也支持 5 位 Unix 写法。错过的触发不会补跑，同一任务不会并发触发；cron 无效的任务会被停止并记录原因，不会影响其他任务。目前可调度的只有内置的 `QualityTask.run`。

所有核查 SQL 都经过与 SQL 工作台相同的只读校验，标识符按方言引用，正则、枚举、数值等字面量单独校验后才拼入 SQL；含分号、控制字符或子查询的输入会被拒绝。错误数据只做只读抽样展示，不写回任何数据源。Iceberg 与 Paimon 通过 DuckDB 读取，单表最多扫描 50 万行，页面会对这两类数据源标注统计可能不完整。

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

各页面的数据源下拉框以引擎名称作为选项（DuckDB、ClickHouse、Apache Paimon…）；只有多个数据源使用同一引擎时才追加区分信息——优先用所连数据库名，两者连的是同一个库时才回退到数据源名称。PostgreSQL 只登记一个数据源，指向本机业务库 `blog_converter`；项目 PostgreSQL 集群里的 `lattice_demo` 示例库仍由引擎管理脚本创建和写入，只是不再单独列为数据源。

内置规则问数依赖六张示例表。所选数据源没有这些表时（例如业务库 PostgreSQL），页面会直接说明缺少哪张表并建议配置模型或改用 SQL 工作台，而不是把引擎的 SQL 报错抛给用户。
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
- 文档按仓库内的 Apache Ossie JSON Schema（`core-spec/ossie-schema.json`）校验，未通过返回 400；
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
