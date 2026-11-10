# SQLite WEB 管理终端

`web/sqlite_server.py` 提供 SQLite HTTP 服务，`web/sqlite_static/` 提供中文 HTML 界面。
SQLite 管理终端的交互参考 Qt `tools/rtdbmng`，包括表索引、字段树、SQL 增删改查、排序、分页和日志区域。
管理对象为本项目 `core.sqlite_db.SQLiteDatabase` 的表：一张逻辑表一个 `.sqlite3` 文件，
搭配同名 JSON 定义。当前不直接管理任意第三方 SQLite 文件或跨表 JOIN。

从仓库根目录启动：

```powershell
Set-Location D:\codex\rtdb
python -m web.sqlite_server --host 127.0.0.1 --port 8878 `
  --data-dir D:\codex\rtdb\sqlite_dat `
  --schema-dir D:\codex\rtdb\def\table
```

浏览器访问 <http://127.0.0.1:8878>。统一入口同样支持：

```powershell
python -m web.server --engine sqlite --port 8878
```

普通标量字段只用 Python 标准库；`list/array/dict/object/json` 字段需要 `msgpack>=1`。
服务默认仅监听本机。跨站请求被拒绝；本版本没有用户登录或权限角色，
如需局域网访问，应在有身份验证的反向代理后部署。

## 界面操作

- 拖拽左侧竖分割条调整表索引宽度，向左拖到边缘收起；从边缘分割条向右拖拽展开。
- 拖拽查询区下方横分割条调整高度，向上拖到边缘收起；从保留的分割条向下拖拽展开。
- 分区大小和收起状态自动记忆。分割条聚焦后也可用方向键微调，Home 收起、End 展开至上限。
- 查询、记录操作和分页保持单行；窄窗口可横向滚动工具栏。表格占满剩余高度并独立滚动。
- 数据路径和刷新状态固定在页面底部单行显示；结果、表结构、曲线和日志区域自动填满剩余高度，曲线随可用空间重新绘制。
- 左侧按英文/中文表名搜索，可只显示非空表，查看字段和联合主键。
- 表结构页通过表单新建、读取和修改定义，支持编辑英文名、中文名、类型、KEY、显示类型和选项；有数据时按数据安全规则锁定已有字段的英文名、类型、KEY 和相对顺序，仍可增删字段。支持完整性检查、导出定义和删除空表。
- 结果页顶部以单行输入框统一输入 SELECT / WITH / INSERT / UPDATE / DELETE，点击“执行”或按 Enter / Ctrl+Enter；历史语句通过输入框内的下拉列表选择，不再设独立模板、历史、重置控件或 SQL 页面。
- SELECT 结果直接显示在主表格，支持投影、别名、聚合、50/100/200/500 行分页和点击列名排序；SQL 自带的 LIMIT/OFFSET 会先应用，再对其结果分页。
- “重置”恢复当前表的 `SELECT *` 原始记录视图，可勾选、编辑、复制和批量操作。自定义查询结果通过 SQL 修改，避免将计算列或聚合行误作原始记录。
- 写入确认后显示影响行数并切回当前表查询；刷新、自动刷新、翻页和导出仅使用上一次成功查询，不执行输入框中尚未提交的语句。
- 新增、编辑和复制记录使用字段表单；布尔值和菜单字段使用选择框，也可切换 JSON 模式。
- 编辑/批量删除使用完整主键和记录摘要；其他连接修改后的旧版本请求返回 409，整批删除回滚。
- 导入 CSV/JSON 采用一次事务，失败不留下部分记录；导出包含上一次成功 SQL 查询的全部结果及排序，最多 100,000 行，CSV 遵循显示字段选择。
- 清空表、删除表和 SQL 写入需要输入表名确认；SQL 写入使用执行时最新内容，不带行版本检查。
- 日志页读取服务端持久化审计日志，记录结构、数据和系统操作的成功/失败、HTTP 状态、耗时、结果摘要和错误信息；新日志按本地日期写入数据目录的 `rtdb-operation-logs/YYYY-MM-DD.jsonl`，旧版单文件日志继续兼容读取。页面支持指定日期、关键词、类别、状态过滤和分页；导出当前查询页。日志保留天数可在页面设置，`0` 表示永久保留；设置大于 0 后，服务启动及每天首次写入时自动清理过期日期文件，也可立即执行清理。
- 双击顶部“RTDB 管理终端”标题打开数据库文件管理窗口。服务端文件页按文件名、表名、记录数、定义文件和校验状态浏览 `.sqlite3` 文件，可打开有效文件或新建默认表文件；上传本地文件页支持选择同名 `.sqlite3` 与可选 `.json` 定义，服务端校验 `metadata`、结构指纹、记录数和 `quick_check` 后原子更新数据库，并生成同名 Python 定义。已有文件必须确认覆盖，失败不会替换原文件，相关成功/失败均写入操作日志。

表定义统一为：

```json
{
  "head": {"chn_name": "设备", "eng_name": "devices"},
  "columns": [
    {"chn_name": "站点", "eng_name": "site", "primary_key": true, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
    {"chn_name": "编号", "eng_name": "id", "primary_key": true, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
    {"chn_name": "状态", "eng_name": "state", "primary_key": false, "data_type": "str", "disp_type": "下拉框", "disp_menu": "启用|停用"}
  ]
}
```

文件映射、Redis、SQLite 三个版本都不再接受 `max_elements`、`data_length`。
SQLite/Redis 没有定义级容量或长度限制。文件映射仍由引擎参数控制物理布局，详见
[公共定义说明](../def/table/README.md)。旧 SQLite/Redis 结构指纹需要离线迁移，
不会自动改写已有数据库；新建库直接使用新格式。

## SQLite API

网页统一 SQL 入口使用 `POST /api/manage/tables/{name}/sql`：传入 `sql`、`limit`、`offset`，
可带 `order: {column, direction}`。查询返回 `columns`、`rows`、`total`；原始记录视图另带用于编辑的 `records` 引用。
刷新/翻页传 `readonly_only: true`，服务器拒绝写入语句。写入传 `confirm` 表名并返回 `rowcount`。
`POST /api/manage/tables/{name}/sql-export` 使用相同查询和排序，另传 `format`（JSON/CSV）及 CSV `columns`。
以下 `/api/sqlite/` 接口仍保留，其中原生 SQL 接口的单次返回上限为 500 行。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health`、`/api/sqlite/info` | 服务、路径、SQLite 版本、持久化配置 |
| GET | `/api/manage/logs?page=1&page_size=50&date=YYYY-MM-DD&keyword=...&category=...&status=...` | 按日期、关键词、类别和状态分页查询持久化操作日志 |
| GET | `/api/manage/database-files` | 浏览服务端 RTDB `.sqlite3` 文件及定义、记录数、大小和校验状态 |
| POST | `/api/manage/database-files/select` | 选择并校验服务端文件，参数为 `{ "filename": "devices.sqlite3" }` |
| POST | `/api/manage/database-files/create` | 新建服务端数据库文件，参数为 `{ "name": "measurements", "chn_name": "测量表" }` |
| POST | `/api/manage/database-files/upload` | 上传并原子更新文件，参数为 Base64 `files: [{ "name": "x.sqlite3", "content": "..." }, { "name": "x.json", "content": "..." }]`；覆盖时增加 `confirm` |
| GET / PUT | `/api/manage/log-config` | 读取/设置日志保留天数；`0` 表示永久保留 |
| POST | `/api/manage/logs/cleanup` | 按当前保留天数立即清理旧日志文件 |
| GET | `/api/sqlite/tables` | 所有定义、结构版本和记录数 |
| POST | `/api/sqlite/schemas` | 用 `{ "definition": {...} }` 新建表 |
| GET / PUT | `/api/sqlite/tables/{name}/schema` | 读取/修改定义；PUT 携带 `definition` 和 `version` |
| GET | `/api/sqlite/tables/{name}/rows` | `where`、`order`、`limit`、`offset` 查询 |
| POST | `/api/sqlite/tables/{name}/rows` | `{ "rows": [...] }` 原子批量追加 |
| PUT | `/api/sqlite/tables/{name}/row` | `{ "key": [...], "values": {...}, "version": "..." }` |
| DELETE | `/api/sqlite/tables/{name}/rows` | `{ "records": [{"key": [...], "version": "..."}] }` |
| POST | `/api/sqlite/tables/{name}/clear` | `{ "confirm": "表名", "expected_count": 数量 }` |
| DELETE | `/api/sqlite/tables/{name}/table` | `{ "confirm": "表名", "version": "结构版本" }`，仅空表 |
| POST | `/api/sqlite/sql` | `{ "table": "表名", "sql": "...", "confirm": "写入时的表名" }` |
| POST | `/api/sqlite/tables/{name}/import` | `{ "format": "csv或json", "text": "内容" }` |
| GET | `/api/sqlite/tables/{name}/export` | `format=csv/json`，可带 `where`、`order` |
| GET | `/api/sqlite/tables/{name}/check` | quick_check、实际数量与元数据数量核对 |

SELECT/WITH 查询和单条 INSERT/UPDATE/DELETE 使用 SQLite 原生执行。
支持聚合、表达式、LIKE、IN 等；DML 通过受限可写视图触发器做类型和主键校验。
PRAGMA、ATTACH、DDL、扩展加载、修改内部表和多语句执行被拒绝；写入 RETURNING、INSERT/UPDATE OR 冲突策略暂不支持。
表名 data、metadata、sqlite_ 前缀由内核保留。
结构化对象写入请用记录/导入 API。SQL 查询最多返回 500 行，执行时限 5 秒。
分页和导入导出的接口限制用于控制单次请求资源：单页 500、导入导出 100000 行、请求体 32 MiB，
这些不是数据库总行数限制。

CSV 第一行是英文字段名，UTF-8 可带 BOM；NULL 使用 `\N`，以反斜杠开头的文本需转义。
JSON 导入可用记录数组，或包含 `rows` 数组的导出对象。
浏览器返回的大整数转为十进制字符串，以保留 64 位主键精度；整数列接受该字符串作为输入。

SQLite 保持 WAL + synchronous=FULL。每次请求创建并关闭自己的连接，提交后其他已打开连接
下一次读取可见新数据；网页通过查询/刷新获取变化，没有 WebSocket 自动推送。
当前服务的管理操作使用进程内及跨进程锁串行协调结构操作，批量导入使用单次事务，
尚未接入 SQLiteWriteProcess 的跨 HTTP 请求分组提交。

有数据时允许修改中文名/显示信息，以及增删非主键字段；新增字段的原有记录值为 NULL，删除字段会删除对应数据。
已有字段类型、主键（包括增删主键字段）和保留字段的相对顺序不可改；新字段可插入任意位置。
表单锁定已有字段英文名以保留字段身份，新增字段可选择类型。空表可以调整类型、主键和顺序。
结构保存通过单个 SQLite 事务迁移，保留记录位置、计数和自增序列，重新建立索引；同步更新 JSON/Python 定义。
结构操作前须关闭其他程序持有的表对象，完成后重新打开；这是结构变更的要求，日常数据写入无需如此。
结构发布使用事务、临时文件 fsync/replace 及恢复日志；失败后按 SQLite 指纹恢复定义。
删除空表会把数据库和定义移入 `.web-archive`；一个 schema 目录应只绑定同一数据目录。

回归命令：

```powershell
python -m unittest web.test_server web.test_sqlite_server web.test_management `
  web.test_definition_import web.test_schema_migration core.test_table_schema `
  core.test_sqlite_db core.test_sqlite_writer core.test_file_mapped_db `
  core.test_file_mapped_writer core.test_redis_db -q
```

---

# RTDB WEB 服务

`web/server.py` 提供一个只使用 Python 标准库的线程化 HTTP 服务，默认使用 SQLite 内核，绑定：

```text
http://127.0.0.1:8877
```

从仓库根目录启动：

```powershell
python -m web.server --engine sqlite
```

也可以指定数据目录和 JSON 表结构目录：

```powershell
python -m web.server --engine sqlite --host 127.0.0.1 --port 8877 `
  --data-dir D:\codex\rtdb\sqlite_dat `
  --schema-dir D:\codex\rtdb\def\table
```

打开根地址即可使用浏览器管理页面。页面参考 `tools/rtdbmng` 提供表索引过滤、字段显示选择、SQL 增删改查、排序分页、记录增删改复制、批量修改、CSV/JSON 导入导出、多表备份恢复、数据检查、曲线和操作日志。

SQLite 管理台使用 `WAL + synchronous=FULL`。网页 API 使用逻辑主键和行版本校验，排序、分页后编辑不会依赖行号；批量修改、删除和清空遇到并发变化会整体撤销。恢复备份先校验所有表结构、主键和数据，再逐表提交并报告已完成项目。

默认服务入口等价于 `python -m web.sqlite_server --port 8877`。如需继续使用旧的文件映射页面，可明确指定 `--engine file_mapped`；该模式仍使用 `/api/schemas`、`/api/data/...` 兼容接口，默认页面保留在 `web/static/`。

## 表结构接口

表结构 JSON 使用 `head` 和 `columns` 两部分，字段固定为：

```text
chn_name, eng_name,
primary_key, data_type, disp_type, disp_menu
```

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/schemas` | 列出全部表结构 |
| GET | `/api/schemas/{table}` | 查询一张表结构 |
| POST | `/api/schemas` | 新增表结构，表名取 `head.eng_name` |
| PUT | `/api/schemas/{table}` | 覆盖更新表结构 |
| DELETE | `/api/schemas/{table}` | 删除表结构；保留数据文件 |

删除有数据的表结构会返回 `409`。需要连数据一起删除时使用：

```text
DELETE /api/schemas/users
```

## 表数据接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/data/{table}` | 查询行，支持 `where`、`order_by`、`columns`、`limit`、`offset` |
| POST | `/api/data/{table}` | 新增一行；也接受 `{ "rows": [...] }` 批量新增 |
| PUT | `/api/data/{table}` | 按 `where` 批量更新，格式 `{ "values": {...}, "where": {...}, "limit": 1 }` |
| DELETE | `/api/data/{table}` | 按 `where` 删除，格式 `{ "where": {...}, "limit": 1 }` |
| GET | `/api/data/{table}/{index}` | 按列表下标读取一行 |
| PUT | `/api/data/{table}/{index}` | 按列表下标替换一行 |
| DELETE | `/api/data/{table}/{index}` | 按列表下标删除一行 |

`where` 可以是
JSON 对象，例如 `{"status":"启用"}`，也可以是核心层支持的条件表达式，例如
`age >= 18`。

如果表结构将多个列标记为 `primary_key: true`，这些列组成联合主键，列的
定义顺序就是主键值顺序。WEB 查询、更新和删除可以在 `where` 中同时给出
这些列，例如：

```text
GET /api/data/links?where={"site":"a","device":2}
```

对应的更新请求为：

```json
{
  "where": {"site": "a", "device": 2},
  "values": {"state": "down"}
}
```

## SQLite：加载 dbdef / E 文件

左侧 **加载 dbdef / E** 可在尚无数据表时使用。选择文件与编码，点击“预览”，每张表以表名为标签独立展示字段增删、类型、主键、记录数及警告；切换标签保留各表的类型与 KEY 设置，确认后统一导入全部表。支持 UTF-8（含 BOM）、GBK/GB18030 和带 BOM 的 UTF-16，默认自动识别。表或字段的中文名为空、`null`（不区分大小写）或 `\N` 时，自动使用英文名；导入和保存结构时同步写入 JSON、Python 定义。已有表的页面显示也使用该规则，业务数据中的 `null` 文本不受影响。

- `.dbdef`：读取旧版 `<table_info>` 和各表字段模块；生成/更新 SQLite 表以及 `--schema-dir/<表名>.json`、`--schema-dir/<表名>.py`。Python 文件提供 `TABLE_DEFINITION`、同名 dataclass 和 `Row` 别名，字段 metadata 与 JSON 一致。Python 保留字字段使用别名，原名保留在 metadata 的 `eng_name` 中。
- `.e`：读取 `<表名>`、`@ 英文字段名`、可选 `// 中文字段名` 和 `# 数据`。新表根据数据推断类型；主键可填写逗号分隔的字段名，留空优先使用 `key`、`id`，否则使用首列。导入前检查唯一性。已有表沿用原有类型/主键，保留未提供的字段，增加文件中的新字段。
- E 数据支持“按主键合并”“追加”“替换整表数据”。合并时，文件未提供的原有字段值保留。重复主键仅警告，不阻断预览和加载：追加时保留已有/首次记录，跳过与数据库或文件内的冲突记录；合并和替换时采用文件内同一主键的最后一条记录。预览仍可人工调整 KEY（如联合主键）再确认导入。预览、执行结果和持久化日志记录重复/跳过数量及警告。空主键、无效类型转换、超范围整数仍会拒绝导入。字符串含空格/换行须用单引号或双引号包围；同种引号用连续两个转义；空字符串写 `''`，空值写 `\N`。
- dbdef 可以迁移有数据的表：保留同名字段，新增非主键字段为 NULL；删除非主键字段会删除对应数据，预览会列明。有数据时禁止变更已有字段类型、主键和保留字段的相对顺序，与表结构编辑页一致。
- 旧版 `max_record_num` / `data_length` 是导入源的描述信息，不写入新的 JSON。当前 JSON 严格结构契约保持不变。
- 预览不写业务表或定义文件。应用时重新检查数据/结构版本；先验证文件内全部表，再逐表提交。单张表的结构和数据在同一 SQLite 事务中更新，JSON/Python 使用原子替换和恢复日志同步。**多张表并非一个整体事务**：中途失败会列出已完成、失败及未处理表。结构变更后，外部长期持有数据库对象的程序应重新打开连接。
- 单文件最大 20 MiB、最多 200 张表、单表最多 100,000 行。一般表结构表单的保存也同步更新 Python 定义。

E 文件例子：

```text
<meters>
@ id name value
// 编号 名称 功率
# 1 '北侧 电表' 12.5
# 2 南侧电表 18.5
</meters>
```

接口：`POST /api/manage/definition-preview` 和 `POST /api/manage/definition-import`。请求包含 `format: "dbdef" | "e"`、`text`（Unicode 文本）或 `content`（原始文件 Base64）、`encoding`、`mode`、`keys`。E 文件预览按全部数据行推断类型，新表默认首字段为 KEY；人工确认后的应用请求可附带 `definition_overrides: {表名: {columns: [{eng_name, data_type, primary_key}, ...]}}` 覆盖类型和主键。应用导入时必须回传预览结果的 `token`。自动化调用应检查响应的 `ok` 和逐表 `results`。

结果表格使用行标题、列标题和鼠标拖拽框选；Ctrl 支持增选，Shift 支持连续选择；编辑/复制/删除记录位于右键菜单。顶部只保留“结果展示 / 表结构”；表格底部通过“曲线 / 日志”标签切换，导出和清除按钮随标签切换。曲线与单元格双向同步，改动以黄色高亮；“保存改单元格”后写入数据库。底部共用分割条，向下拖到底部收起，向上拖动恢复；切换标签保留表格显示、选区和未保存修改。

左侧“显示字段”列表同时显示字段中文名、英文名和归一化数据类型（`INT`、`FLOAT`、`STR`、`BOOL`、`JSON`、`LIST`、`DICT`）；KEY 字段继续显示 `PK` 标识。

结果区的新增记录、导入、导出查询（JSON / CSV）、批量修改选中、删除选中记录及清空表统一放入右键菜单。表格单元格、标题和空白区域都可打开菜单，空表也能新增或导入。批量修改/删除须先点击行标题选择行；SQL 聚合或投影结果不提供原记录的编辑/删除。删除和清空仍须确认。待保存的单元格修改显示保存/撤销按钮；处理这些修改后再执行其他写操作。菜单支持 Shift+F10 打开、方向键选择及 Escape 关闭。

结果单元格单击仅选中，双击才进入编辑；Enter 或点击其他位置结束编辑，Escape 取消本次编辑并恢复进入编辑前的值。修改仍需点击“保存改单元格”才写入数据库。KEY 列只允许选择；对象/数组字段双击打开记录编辑窗口。

SQL 无需以分号结尾，例如 `SELECT * FROM "devices"`。默认语句、执行后的输入和历史回填均省略末尾分号；输入带分号的旧语句仍可执行，字符串内容中的分号保持不变。

SQL 工具栏使用单行输入框，已移除独立的模板、历史和重置控件。点击输入框内的下拉箭头查看当前表的历史语句，输入内容可筛选历史；鼠标选择，或用上下键选中后按 Enter，仅填入待执行语句。点击“执行”、直接按 Enter 或 Ctrl+Enter 才提交当前 SQL，写入语句仍需确认。成功执行的最近 30 条语句去重置顶，按数据库路径和表名保存在当前浏览器，刷新后保留；取消或执行失败的语句不加入历史。查询区域保持单行高度，分割条仍可拖拽折叠和恢复。

选中单元格区域、多行或多列后，右键选择“左轴曲线 / 右轴曲线”可批量加入曲线。按选区中不同的行、列数量判断：行数大于列数时，每列一条曲线，以列名命名；行数小于或等于列数时，每行一条曲线，以 KEY（复合主键用 ` / ` 连接）命名。行列数量包含选中的非数值列，但只绘制数值字段；不连续选区中的空缺保持断点。

每条曲线分别按字段定义的数据类型筛选：同时选中 INT 和 FLOAT 数据时，忽略该曲线中的 INT 点及其横轴标签；仅有 INT 时正常绘制。FLOAT 字段即使当前值为整数（如 `1.0`）也保留；不同曲线之间不互相影响。此规则适用于右键追加的左右轴曲线，过滤后的点继续绑定原单元格，支持双向编辑。

曲线只在右键执行“左轴曲线 / 右轴曲线”后生成或追加，表格选区变化不会改变已有曲线。首次打开、切换表、SQL、页码或排序时清空曲线及图例，保持空白，不自动生成默认曲线。重复加入同一组选点只调整左右轴；图例的 × 可移除单条，“清空曲线”清除全部曲线。混合方向或不同横轴标签的曲线使用点序号对齐，游标显示各点实际标签。单元格编辑、曲线拖拽和保存后刷新保持绑定。只读 SQL 结果也可绘图，但不可拖拽修改；无 KEY 的结果按行序号命名。

图例位于曲线板右上角。点击曲线名称或色点可隐藏，再次点击恢复；隐藏项置灰并加删除线。隐藏曲线不参与坐标轴自动缩放、游标读数或拖拽编辑，原数据和曲线配置保留。图例中的左右轴下拉框和 × 按钮分别用于换轴、移除，与显示/隐藏相互独立。

结果中的 KEY（主键）列可选择、不可编辑，以浅绿色和 KEY 标记区分，始终排在数据列最左侧并冻结；联合主键各列依次冻结，横向滚动、窗口缩放不会使其位置错乱。显示字段的取消/反选保留主键列。此规则不向 SQL 投影结果补入原本未查询的字段。

表格选中单元格区域、行或列后，按 Ctrl+C，或在右键菜单选择“复制选中区域”，会按制表符和换行符复制为可直接粘贴到 Excel 的二维文本。将焦点放在选区后按 Ctrl+V，或选择“粘贴到选区”，数据从选区左上角按行列展开；只读 KEY 单元格会跳过，其他字段先作为待保存修改并高亮，确认无误后点击“保存改单元格”。粘贴范围必须落在当前页和当前显示字段内，字段值仍按表结构类型校验。

SQL UPDATE 同样不允许改变现有记录的 KEY；新增或复制记录仍可填写新 KEY。曲线默认按行绘制并使用 KEY 命名；也可手动切换为按列绘制，此时使用字段名。单元格编辑保留 64 位整数精度；超过 JavaScript 精确整数范围的点仅供查看，不允许通过曲线拖动改值。坐标轴目前自动缩放，未提供手动上下限设置。

与旧版 rtdbmng 的差异：目前没有 E 格式导出、独立菜单文件导入导出、导入时自动重编关键字、批量清空所选表、跨应用/主机/上下文切换以及商用库双向同步。备份恢复使用本项目 JSON 格式，不能直接读取旧版原生备份。这些旧版功能尚未移植，不能按“全量功能等价”验收。

## 健康检查

```text
GET /api/health
```

成功响应：

```json
{"ok": true, "service": "rtdb"}
```

