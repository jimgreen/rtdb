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
    "eng_name": "users",
    "max_elements": 10000
  },
  "columns": [
    {
      "chn_name": "用户编号",
      "eng_name": "user_id",
      "primary_key": true,
      "data_type": "int",
      "data_length": 8,
      "disp_type": "数字",
      "disp_menu": ""
    },
    {
      "chn_name": "用户名",
      "eng_name": "name",
      "primary_key": false,
      "data_type": "str",
      "data_length": 64,
      "disp_type": "文本",
      "disp_menu": ""
    },
    {
      "chn_name": "状态",
      "eng_name": "status",
      "primary_key": false,
      "data_type": "str",
      "data_length": 16,
      "disp_type": "下拉框",
      "disp_menu": "启用|停用"
    }
  ]
}
```

字段约定：

- `head.chn_name`：表中文名。
- `head.eng_name`：表英文名，必须与 JSON 文件名一致。
- `head.max_elements`：非负整数，表示表允许保存的最大行数。
- `columns[].chn_name`：列中文名。
- `columns[].eng_name`：列英文名，必须是 Python/SQL 英文标识符。
- `columns[].primary_key`：是否主键；每张表至少有一个主键列。
- `columns[].data_type`：存储类型，例如 `int`、`float`、`str`、`datetime`。
- `columns[].data_length`：非负整数，表示列长度或精度上限。
- `columns[].disp_type`：显示类型，例如 `数字`、`文本`、`下拉框`。
- `columns[].disp_menu`：原始菜单描述，多项使用 `|` 分隔。

Python 读取接口：

```python
from core import FileMappedDatabase, SchemaRegistry, load_table_definition

definition = load_table_definition(r"D:\\codex\\rtdb\\def\\table", "users")
print(definition.primary_keys)
print(definition.column("status").disp_menu_items)

registry = SchemaRegistry(r"D:\\codex\\rtdb\\def\\table")
print(registry.names())

# 数据目录为 D:\\codex\\rtdb\\dat 时会自动发现同级 def\\table。
db = FileMappedDatabase(r"D:\\codex\\rtdb\\dat")
users = db.table("users")
print(users.schema.max_elements)
```

