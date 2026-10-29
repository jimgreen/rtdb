# 表结构 JSON 定义文件

每张表使用一个 JSON 文件，文件名为表的英文名：

```text
<英文表名>.json
```

例如 `users` 表：

```text
users.json
```

文件内容由 `head` 和 `columns` 两部分组成：

```json
{
  "head": {
    "chn_name": "用户表",
    "eng_name": "users"
  },
  "columns": [
    {
      "chn_name": "用户编号",
      "eng_name": "user_id",
      "primary_key": true,
      "data_type": "int",
      "disp_type": "数字",
      "disp_menu": ""
    },
    {
      "chn_name": "用户名",
      "eng_name": "name",
      "primary_key": false,
      "data_type": "str",
      "disp_type": "文本",
      "disp_menu": ""
    },
    {
      "chn_name": "状态",
      "eng_name": "status",
      "primary_key": false,
      "data_type": "str",
      "disp_type": "下拉框",
      "disp_menu": "启用|停用"
    }
  ]
}
```

字段约定：

- `head.chn_name`：表中文名。
- `head.eng_name`：表英文名，必须与 JSON 文件名一致。
- `columns[].chn_name`：列中文名。
- `columns[].eng_name`：列英文名，必须是 Python/SQL 英文标识符。
- `columns[].primary_key`：是否主键；每张表至少有一个主键列。可以将多个列同时设为 `true`，这些列按定义文件中的列顺序组成联合主键。
- `columns[].data_type`：存储类型，例如 `int`、`float`、`str`、`datetime`。
- `columns[].disp_type`：显示类型，例如 `数字`、`文本`、`下拉框`。
- `columns[].disp_menu`：原始菜单描述，多项使用 `|` 分隔。

Python 读取接口：

```python
from core import FileMappedDatabase, SchemaRegistry, load_table_definition

definition = load_table_definition(r"D:\\codex\\rtdb\\def\\table", "users")
print(definition.primary_keys)
print(definition.column("status").disp_menu_items)

# 联合主键的顺序与 columns 中 primary_key=true 的列顺序一致，例如：
# definition.primary_keys == ("site", "device")
# table.kv[("site-a", 7)]

registry = SchemaRegistry(r"D:\\codex\\rtdb\\def\\table")
print(registry.names())

# 数据目录为 D:\\codex\\rtdb\\dat 时会自动发现同级 def\\table。
db = FileMappedDatabase(r"D:\\codex\\rtdb\\dat")
users = db.table("users")
```


公共表定义不再包含 `max_elements`、`data_length`，提交这两个旧字段会报结构错误。
SQLite/Redis 不设置定义级行数和文本长度上限。文件映射仍需固定物理布局，
由 `FileMappedDatabase` 的 `fixed_max_records`、`fixed_text_bytes`、`fixed_object_bytes`
建表参数控制（默认 100000、512、16384 字节；文本和对象宽度按 4 字节对齐）。
这些设置保存在二进制文件中；重开已有表时以文件布局为准，不需要再次传入。
超出固定文件容量或槽宽时会报错并撤销本次操作，不会截断数据；需要更大容量时
应离线导出并重建。移除公共字段不表示 mmap 已支持在线扩容或任意长字段。

已有 JSON 定义需要先删除这两个字段。现有文件映射二进制布局继续读取；
SQLite/Redis 的旧结构指纹不会被静默重写，应使用旧版导出数据，再用新定义导入新库。
