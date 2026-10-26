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
chn_name, eng_name, max_elements,
primary_key, data_type, data_length, disp_type, disp_menu
```

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/schemas` | 列出全部表结构 |
| GET | `/api/schemas/{table}` | 查询一张表结构 |
| POST | `/api/schemas` | 新增表结构，表名取 `head.eng_name` |
| PUT | `/api/schemas/{table}` | 覆盖更新表结构 |
| DELETE | `/api/schemas/{table}` | 删除表结构；保留数据文件 |
| GET | `/api/tables` | 列出表结构和数据行数 |
| GET | `/api/tables/{table}` | 查询表信息、行数和结构 |
| POST | `/api/tables` | 新增表结构的兼容入口 |
| PUT | `/api/tables/{table}` | 更新表结构的兼容入口 |
| DELETE | `/api/tables/{table}` | 删除表结构的兼容入口 |

删除有数据的表结构会返回 `409`。需要连数据一起删除时使用：

```text
DELETE /api/tables/users?delete_data=true
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

`/api/tables/{table}/data` 是同一组数据接口的另一种路径形式。`where` 可以是
JSON 对象，例如 `{"status":"启用"}`，也可以是核心层支持的条件表达式，例如
`age >= 18`。

## 健康检查

```text
GET /api/health
```

成功响应：

```json
{"ok": true, "service": "rtdb"}
```

