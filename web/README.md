# SQLite WEB 管理终端

`web/sqlite_server.py` 提供 SQLite HTTP 服务，`web/sqlite_static/` 提供中文 HTML 界面。
SQLite 管理终端的交互参考 Qt `tools/rtdbmng`，包括表索引、字段树、条件查询、排序、分页、SQL 和日志区域。
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

- 左侧按英文/中文表名搜索，可只显示非空表，查看字段和联合主键。
- 表结构页新建、读取、修改 JSON 定义，支持完整性检查和删除空表。
- 结果页按条件过滤、排序，支持 50/100/200/500 行分页，选择当前页。
- 新增、编辑和复制记录使用字段表单；布尔值和菜单字段使用选择框，也可切换 JSON 模式。
- 编辑/批量删除使用完整主键和记录摘要；其他连接修改后的旧版本请求返回 409，整批删除回滚。
- 导入 CSV/JSON 采用一次事务，失败不留下部分记录；导出遵循当前过滤和排序。
- 清空表、删除表和 SQL 写入需要输入表名确认；SQL 写入使用执行时最新内容，不带行版本检查。
- 日志记录当前浏览器会话的操作，最多保留 500 条；它不是持久化审计日志。

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

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health`、`/api/sqlite/info` | 服务、路径、SQLite 版本、持久化配置 |
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

有数据时允许修改中文名/显示信息；字段增删、重排、类型和主键改变需先导出并清空。
结构操作前须关闭其他程序持有的表对象，完成后重新打开；这是结构变更的要求，日常数据写入无需如此。
结构发布使用事务、临时文件 fsync/replace 及恢复日志；失败后按 SQLite 指纹恢复定义。
删除空表会把数据库和定义移入 `.web-archive`；一个 schema 目录应只绑定同一数据目录。

回归命令：

```powershell
python -m unittest web.test_server web.test_sqlite_server core.test_table_schema `
  core.test_sqlite_db core.test_sqlite_writer core.test_file_mapped_db core.test_file_mapped_writer -q
```

---

# RTDB WEB 服务

`web/server.py` 提供一个只使用 Python 标准库的线程化 HTTP 服务，默认绑定：

```text
http://127.0.0.1:8877
```

从仓库根目录启动：

```powershell
python -m web.server
```

也可以指定数据目录和 JSON 表结构目录：

```powershell
python -m web.server --host 127.0.0.1 --port 8877 `
  --data-dir D:\codex\rtdb\dat `
  --schema-dir D:\codex\rtdb\def\table
```

打开根地址即可使用浏览器管理页面。页面支持表结构 JSON 编辑、表数据查询、添加、编辑和删除。

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

## 健康检查

```text
GET /api/health
```

成功响应：

```json
{"ok": true, "service": "rtdb"}
```

