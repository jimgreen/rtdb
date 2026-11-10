"""Browser workflows built on the SQLite RTDB management service."""
from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import time
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError
from core.sqlite_db import _INT, _FLOAT, _BOOL, _OBJECT
from .audit import status_for_exception
from .errors import APIError
from .sqlite_service import fingerprint


class SQLiteManagement:
    def __init__(self, manager):
        self.manager = manager

    def rows(self, name, query):
        started = time.perf_counter()
        limit, offset = int(query.get('limit', 100)), int(query.get('offset', 0))
        if not 1 <= limit <= 500:
            raise APIError(400, '单页必须为 1–500 行')
        with self.manager.session(name) as table:
            with table._read_transaction() as connection:
                rows, total = self.manager._selection(table, connection, query.get('where', ''),
                    query.get('order', ''), limit, offset)
                table_total = connection.execute('SELECT row_count FROM metadata').fetchone()[0]
            records = [{'row': row, 'ref': {'key': [row[key] for key in table.primary_keys],
                                           'version': fingerprint(row)}} for row in rows]
        return dict(records=records, total=total, table_total=table_total, limit=limit, offset=offset,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2))

    def sql(self, name, payload, *, exporting=False):
        statement = payload.get('sql', '')
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 20000:
            raise APIError(400, '请输入 SQL（最多 20,000 字符）')
        # Preserve quoted strings/identifiers while removing SQL comments.
        tokens = r"""'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/"""
        statement = re.sub(tokens, lambda match: ' ' if match[0].startswith(('--', '/*')) else match[0], statement)
        statement = statement.strip().removesuffix(';').strip()
        kind = statement.split(None, 1)[0].upper() if statement else ''
        if kind not in ('SELECT', 'WITH'):
            if payload.get('readonly_only') or exporting:
                raise APIError(400, '刷新、分页和导出只允许 SELECT / WITH 查询')
            return self.manager.sql(name, {**payload, 'sql': statement})
        limit = self.manager.max_rows if exporting else payload.get('limit', 100)
        offset = 0 if exporting else payload.get('offset', 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= (self.manager.max_rows if exporting else 500):
            raise APIError(400, '单页必须为 1–500 行')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise APIError(400, 'offset 必须为非负整数')
        started = time.perf_counter()
        with self.manager.session(name) as table:
            self.manager._view(table, table._connection())
            # Only the unmodified table view has unambiguous editable row identities.
            editable = bool(re.fullmatch(r'SELECT\s+\*\s+FROM\s+(?:"' + re.escape(name) + r'"|' + re.escape(name) + r')', statement, re.I))
            source = f'({statement}\n)'
            with table._read_transaction() as connection:
                table_total = connection.execute('SELECT row_count FROM metadata').fetchone()[0]
                with self.manager._reader(table, connection):
                    columns = [item[0] for item in connection.execute(f'SELECT * FROM {source} LIMIT 0').description]
                    order = payload.get('order')
                    if order is not None:
                        if not isinstance(order, dict) or order.get('column') not in columns or order.get('direction') not in ('ASC', 'DESC'):
                            raise APIError(400, '排序列或方向无效')
                        ordering = ' ORDER BY "' + order['column'].replace('"', '""') + '" ' + order['direction']
                    else:
                        ordering = ' ORDER BY ' + ','.join('"' + key + '"' for key in table.primary_keys) if editable else ''
                    total = connection.execute(f'SELECT COUNT(*) FROM {source}').fetchone()[0]
                    if exporting and total > self.manager.max_rows:
                        raise APIError(413, '导出结果超过 100,000 行，请增加 SQL 过滤条件')
                    values = connection.execute(f'SELECT * FROM {source}{ordering} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            rows = []
            for raw in values:
                if editable:
                    rows.append(table._decode(raw))
                    continue
                row = {}
                for column, value in zip(columns, raw):
                    if isinstance(value, bytes):
                        value = table._msgpack.unpackb(value, raw=False, strict_map_key=False) if table._types.get(column) in _OBJECT else value.hex()
                    row[column] = value
                rows.append(row)
            records = [{'row': row, 'ref': {'key': [row[key] for key in table.primary_keys], 'version': fingerprint(row)}}
                       for row in rows] if editable and not exporting else []
        return dict(rows=rows, records=records, columns=columns, total=total, table_total=table_total,
                    rowcount=len(rows), limit=limit, offset=offset, readonly=True, editable=editable,
                    truncated=False, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))

    def sql_export(self, name, payload):
        result = self.sql(name, {**payload, 'readonly_only': True}, exporting=True)
        format = payload.get('format', 'json')
        if format == 'json':
            document = {'table': name, 'sql': payload['sql'], 'columns': result['columns'], 'rows': result['rows']}
            if result['editable']:
                document['definition'] = self.manager.definition(name).to_mapping()
            return {'text': json.dumps(document, ensure_ascii=False, indent=2), 'mime': 'application/json', 'filename': name + '.json'}
        if format != 'csv':
            raise APIError(400, '仅支持 JSON / CSV')
        fields = payload.get('columns', result['columns'])
        if not isinstance(fields, list) or not fields or any(field not in result['columns'] for field in fields):
            raise APIError(400, '请选择有效的导出字段')
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        for row in result['rows']:
            cells = []
            for field in fields:
                value = row[field]
                if value is None:
                    cells.append(r'\N')
                elif isinstance(value, (dict, list, bool)):
                    cells.append(json.dumps(value, ensure_ascii=False))
                else:
                    value = str(value)
                    cells.append('\\' + value if value.startswith('\\') else value)
            writer.writerow(cells)
        return {'text': '\ufeff' + stream.getvalue(), 'mime': 'text/csv;charset=utf-8', 'filename': name + '.csv'}

    @staticmethod
    def revision(table, connection):
        return fingerprint({'definition': table.definition.to_mapping(), 'rows': table._select(connection)})

    def snapshot(self, name):
        with self.manager.session(name) as table:
            with table._read_transaction() as connection:
                return {'version': self.revision(table, connection),
                        'count': connection.execute('SELECT row_count FROM metadata').fetchone()[0]}

    def batch_edit(self, name, payload):
        records = payload.get('records')
        if not isinstance(records, list) or not 1 <= len(records) <= 500:
            raise APIError(400, '请选择 1–500 条记录')
        return self.cell_edits(name, {'changes': [{'ref': record, 'values': payload.get('values')} for record in records]})

    @staticmethod
    def _cell_edit_payload(payload):
        changes, inserts = payload.get('changes', []), payload.get('inserts', [])
        if (not isinstance(changes, list) or not isinstance(inserts, list)
                or len(changes) > 500 or len(inserts) > 500 or not (changes or inserts)):
            raise APIError(400, '请提供待保存记录，新增和修改分别最多 500 条')
        return changes, inserts

    def _prepare_cell_edits(self, table, connection, changes, inserts):
        new_rows, new_keys, warnings = [], {}, []
        for index, row in enumerate(inserts, 1):
            if not isinstance(row, dict):
                raise APIError(400, f'新增第 {index} 行必须是记录对象')
            row = table.definition.apply_defaults(row)
            missing = [key for key in table.primary_keys if row.get(key) is None
                       or isinstance(row[key], str) and not row[key].strip()]
            if missing:
                warnings.append(f'新增第 {index} 行：主键字段 {"、".join(missing)} 不能为空')
                continue
            try:
                value = self.manager._coerce(table, row, complete=True)
            except (APIError, SchemaError) as error:
                warnings.append(f'新增第 {index} 行：{error}')
                continue
            identity = tuple(value[key] for key in table.primary_keys)
            key_text = '，'.join(f'{key}={value[key]!r}' for key in table.primary_keys)
            if identity in new_keys:
                warnings.append(f'新增第 {index} 行与新增第 {new_keys[identity]} 行主键重复（{key_text}）')
            elif table._select(connection, dict(zip(table.primary_keys, identity)), limit=1):
                warnings.append(f'新增第 {index} 行与数据库已有记录主键重复（{key_text}）')
            new_keys.setdefault(identity, index)
            new_rows.append(value)
        if warnings:
            raise APIError(422, '\n'.join(warnings), details={'warnings': warnings})

        prepared, seen = [], set()
        for change in changes:
            if not isinstance(change, dict):
                raise APIError(400, '单元格修改必须为对象')
            reference = change.get('ref')
            if not isinstance(reference, dict):
                raise APIError(400, '记录引用必须是对象')
            values = self.manager._coerce(table, change.get('values'))
            if not values or set(values) & set(table.primary_keys):
                raise APIError(422, '请选择非主键字段修改；复制记录可创建新主键')
            where = self.manager._key(table, reference.get('key'))
            identity = fingerprint(where)
            if identity in seen:
                raise APIError(400, '重复选择了同一记录')
            seen.add(identity)
            rows = table._select(connection, where)
            if not rows or fingerprint(rows[0]) != reference.get('version'):
                raise APIError(409, '记录已变化，本批修改未保存，请刷新后重试')
            prepared.append((where, values))
        return prepared, new_rows

    def validate_edits(self, name, payload):
        changes, inserts = self._cell_edit_payload(payload)
        with self.manager.session(name) as table, table._read_transaction() as connection:
            prepared, new_rows = self._prepare_cell_edits(table, connection, changes, inserts)
        return dict(ok=True, updated=len(prepared), inserted=len(new_rows))

    def cell_edits(self, name, payload):
        changes, inserts = self._cell_edit_payload(payload)
        with self.manager.session(name) as table:
            def update(connection):
                prepared, new_rows = self._prepare_cell_edits(table, connection, changes, inserts)
                updated = sum(table._change(connection, 'update', where, values, strict=True) for where, values in prepared)
                inserted = table._insert(connection, new_rows)
                return dict(updated=updated, inserted=inserted) if 'inserts' in payload else dict(updated=updated)

            return table._write(update)

    def clear(self, name, payload):
        if payload.get('confirm') != name:
            raise APIError(400, '请输入表名确认清空')
        with self.manager.session(name) as table:
            def apply(connection):
                if payload.get('version') != self.revision(table, connection):
                    raise APIError(409, '表数据已变化，请重新确认；没有删除数据')
                return connection.execute('DELETE FROM data').rowcount
            return {'deleted': table._write(apply)}

    def export(self, name, query):
        content, mime, filename = self.manager.export(name, query)
        if query.get('format') == 'csv' and query.get('columns'):
            fields = query['columns'].split(',')
            reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
            if set(fields) - set(reader.fieldnames or []):
                raise APIError(400, '导出字段不存在')
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(reader)
            content = stream.getvalue().encode('utf-8-sig')
        return {'text': content.decode('utf-8'), 'mime': mime, 'filename': filename}

    def parse_import(self, table, payload):
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            raise APIError(400, '导入文件不能为空')
        if payload.get('format') == 'json':
            document = json.loads(text.lstrip('\ufeff'))
            rows = document.get('rows') if isinstance(document, dict) else document
            if isinstance(document, dict) and document.get('table', table.name) != table.name:
                raise APIError(422, '文件表名与当前表不同，请使用备份恢复或选择正确的表')
        elif payload.get('format') == 'csv':
            reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)) or set(fields) != set(table.columns):
                raise APIError(422, 'CSV 须包含全部英文字段名，不能重复或包含未知字段')
            rows = []
            for source in reader:
                if None in source or None in source.values():
                    raise APIError(422, 'CSV 列数与表头不一致')
                row = {}
                for name, value in source.items():
                    kind = table._types[name]
                    if value == r'\N':
                        value = None
                    elif kind in _INT:
                        value = int(value)
                    elif kind in _FLOAT:
                        value = float(value)
                    elif kind in _BOOL | _OBJECT:
                        value = json.loads(value)
                    elif value.startswith('\\\\'):
                        value = value[1:]
                    row[name] = value
                rows.append(row)
                if len(rows) > self.manager.max_rows:
                    raise APIError(413, '导入超过 100,000 行')
        else:
            raise APIError(400, '仅支持 JSON / CSV')
        if not isinstance(rows, list) or len(rows) > self.manager.max_rows:
            raise APIError(422, '导入应为记录数组，最多 100,000 行')
        prepared = [self.manager._coerce(table, row, complete=True) for row in rows]
        table.definition.validate_rows(prepared)
        return prepared

    def import_data(self, name, payload):
        mode = payload.get('mode', 'append')
        if mode not in {'append', 'merge', 'replace'}:
            raise APIError(400, '未知导入方式')
        if mode != 'append' and payload.get('confirm') != name:
            raise APIError(400, '合并或替换导入需要输入表名确认')
        with self.manager.session(name) as table:
            rows = self.parse_import(table, payload)

            def apply(connection):
                if mode == 'replace':
                    if payload.get('version') != self.revision(table, connection):
                        raise APIError(409, '表数据已变化，请重新确认覆盖')
                    connection.execute('DELETE FROM data')
                if mode != 'merge':
                    return table._insert(connection, rows)
                for row in rows:
                    where = {key: row[key] for key in table.primary_keys}
                    values = {key: value for key, value in row.items() if key not in table.primary_keys}
                    table._change(connection, 'update', where, values, strict=True, upsert=row)
                return len(rows)

            return {'imported': table._write(apply), 'mode': mode}

    def backup(self, payload):
        names = payload.get('tables')
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            raise APIError(400, '请选择要备份的表')
        if len(names) != len(set(names)):
            raise APIError(400, '备份表名重复')
        documents = [json.loads(self.export(name, {'format': 'json'})['text']) for name in names]
        return {'filename': 'rtdb-backup.json', 'mime': 'application/json',
                'text': json.dumps({'format': 'rtdb-web-backup-v1', 'tables': documents}, ensure_ascii=False, indent=2)}

    def restore_documents(self, payload):
        document = json.loads(payload.get('text', '').lstrip('\ufeff'))
        if not isinstance(document, dict) or document.get('format') != 'rtdb-web-backup-v1':
            raise APIError(422, '请选择“备份所选表”生成的备份文件')
        tables = document.get('tables')
        if not isinstance(tables, list) or not tables or len(tables) > 200:
            raise APIError(422, '备份应包含 1–200 张表')
        seen = set()
        for item in tables:
            if not isinstance(item, dict):
                raise APIError(422, '无效的备份表')
            name = self.manager.name(item.get('table'))
            if name in seen:
                raise APIError(422, '备份存在重复表名')
            seen.add(name)
            definition = TableDefinition.from_mapping(item.get('definition'), self.manager.schema_dir, name)
            with tempfile.TemporaryDirectory(prefix='rtdb-restore-validate-') as folder:
                root = Path(folder)
                definition.to_json(root / 'def')
                with SQLiteDatabase(root / 'data', schema_directory=root / 'def') as database:
                    self.parse_import(database[name], {'format': 'json', 'text': json.dumps(item)})
            existing = (self.manager.schema_dir / (name + '.json')).exists()
            if existing:
                current = self.manager.definition(name)
                layout = lambda schema: [(col.eng_name, col.data_type, col.primary_key) for col in schema.columns]
                if layout(current) != layout(definition):
                    raise APIError(409, f'{name} 的字段布局与备份不同；本次尚未写入')
        return tables

    def restore(self, payload, *, preview=False):
        documents = self.restore_documents(payload)
        if preview:
            tables = []
            for document in documents:
                name = document['table']
                exists = (self.manager.schema_dir / (name + '.json')).exists()
                snapshot = self.snapshot(name) if exists else {'version': None, 'count': 0}
                tables.append({'name': name, 'incoming': len(document['rows']), 'exists': exists, **snapshot})
            return {'tables': tables}
        if payload.get('confirm') != '恢复备份' or not isinstance(payload.get('versions'), dict):
            raise APIError(400, '请先预览备份并输入“恢复备份”确认')
        versions = payload['versions']
        for document in documents:
            name = document['table']
            exists = (self.manager.schema_dir / (name + '.json')).exists()
            current = self.snapshot(name)['version'] if exists else None
            if name not in versions or versions[name] != current:
                raise APIError(409, f'{name} 已变化，请重新预览；本次尚未写入')
        results = []
        for document in documents:
            name = document['table']
            try:
                version = versions[name]
                if version is None:
                    self.manager.save_schema(name, {'definition': document['definition']}, creating=True)
                    version = self.snapshot(name)['version']
                imported = self.import_data(name, {'format': 'json', 'text': json.dumps(document),
                    'mode': 'replace', 'confirm': name, 'version': version})
                results.append({'name': name, 'ok': True, **imported})
            except Exception as error:
                results.append({'name': name, 'ok': False, 'error': str(error)})
                return {'ok': False, 'results': results, 'pending': [item['table'] for item in documents[len(results):]]}
        return {'ok': True, 'results': results, 'pending': []}

    @staticmethod
    def operation_descriptor(method, parts, payload=None):
        """Return (category, label, table) for the browser API operation."""
        payload = payload if isinstance(payload, dict) else {}
        table = parts[1] if len(parts) == 3 and parts[0] == 'tables' else None
        if method == 'GET' and parts == ['info']:
            return 'system', '加载服务信息', None
        if method == 'GET' and parts == ['tables']:
            return 'structure', '加载表结构列表', None
        if method == 'GET' and parts == ['database-files']:
            return 'system', '浏览服务端数据库文件', None
        if method == 'POST' and parts == ['database-files', 'select']:
            return 'system', '选择服务端数据库文件', None
        if method == 'POST' and parts == ['database-files', 'create']:
            return 'structure', '新建数据库文件', None
        if method == 'POST' and parts == ['database-files', 'upload']:
            return 'structure', '上传并更新数据库文件', None
        if method == 'GET' and parts == ['logs']:
            return 'system', '加载操作日志', None
        if method == 'GET' and parts == ['log-config']:
            return 'system', '加载日志配置', None
        if method == 'PUT' and parts == ['log-config']:
            return 'system', '修改日志保留配置', None
        if method == 'POST' and parts == ['logs', 'cleanup']:
            return 'system', '清理历史日志', None
        if method == 'POST' and parts == ['backup']:
            return 'structure', '备份表结构和数据', None
        if method == 'POST' and parts == ['restore-preview']:
            return 'structure', '预览恢复备份', None
        if method == 'POST' and parts == ['restore']:
            return 'structure', '恢复表结构和数据', None
        if method == 'POST' and parts == ['definition-preview']:
            return 'structure', '预览加载定义文件', None
        if method == 'POST' and parts == ['definition-import']:
            return 'structure', '加载定义文件并更新结构/数据', None
        if len(parts) == 3 and parts[0] == 'tables':
            action = parts[2]
            if method == 'GET':
                return {
                    'rows': ('data', '加载表数据'),
                    'snapshot': ('data', '读取数据版本'),
                    'export': ('data', '导出表数据'),
                    'check': ('structure', '检查表结构和数据'),
                    'schema': ('structure', '加载表结构'),
                }.get(action, ('system', f'访问表操作：{action}')) + (table,)
            if method == 'PUT' and action == 'schema':
                return 'structure', '添加表结构' if payload.get('creating') else '修改表结构', table
            if method == 'PUT' and action == 'cell-edits' and payload.get('inserts'):
                return 'data', '添加并修改数据' if payload.get('changes') else '添加数据', table
            return {
                ('POST', 'rows'): ('data', '添加数据'),
                ('PUT', 'rows'): ('data', '批量修改数据'),
                ('PUT', 'cell-edits'): ('data', '修改单元格数据'),
                ('POST', 'validate-edits'): ('data', '保存前检查'),
                ('DELETE', 'rows'): ('data', '删除数据'),
                ('POST', 'clear'): ('data', '清空表数据'),
                ('POST', 'import'): ('data', '加载数据文件'),
                ('POST', 'sql'): ('data', '执行 SQL'),
                ('POST', 'sql-export'): ('data', '导出 SQL 查询结果'),
                ('DELETE', 'schema'): ('structure', '删除表结构'),
            }.get((method, action), ('system', f'访问表操作：{action}')) + (table,)
        return 'system', f'{method} 操作', None

    def dispatch(self, method, parts, query, payload):
        category, operation, table = self.operation_descriptor(method, parts, payload)
        if method == 'GET' and parts in (['logs'], ['log-config']):
            return self._dispatch(method, parts, query, payload)
        started = time.perf_counter()
        try:
            result = self._dispatch(method, parts, query, payload)
        except Exception as error:
            self.manager.record_operation(category=category, operation=operation, status='failure',
                                          http_status=status_for_exception(error), method=method,
                                          path='/api/manage/' + '/'.join(parts), table=table,
                                          duration_ms=(time.perf_counter() - started) * 1000, error=error)
            raise
        self.manager.record_operation(category=category, operation=operation, status='success',
                                      http_status=200, method=method,
                                      path='/api/manage/' + '/'.join(parts), table=table,
                                      duration_ms=(time.perf_counter() - started) * 1000, result=result)
        return result

    def _dispatch(self, method, parts, query, payload):
        if method != 'GET' and not isinstance(payload, dict):
            raise APIError(400, '请求必须是 JSON 对象')
        if method == 'GET' and parts == ['info']:
            return self.manager.info()
        if method == 'GET' and parts == ['tables']:
            return self.manager.tables()
        if method == 'GET' and parts == ['database-files']:
            return self.manager.database_files()
        if method == 'POST' and parts == ['database-files', 'select']:
            return self.manager.select_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'create']:
            return self.manager.create_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'upload']:
            return self.manager.upload_database_file(payload)
        if method == 'GET' and parts == ['logs']:
            try:
                page = int(query.get('page', 1))
                page_size = int(query.get('page_size', query.get('limit', 50)))
            except (TypeError, ValueError) as error:
                raise APIError(400, '日志页码和分页大小必须是整数') from error
            try:
                result = self.manager.audit_log.query(
                    date_value=query.get('date', ''), page=page, page_size=page_size,
                    category=query.get('category', ''), status=query.get('status', ''),
                    keyword=query.get('keyword', ''), table=query.get('table', ''),
                    operation=query.get('operation', ''),
                )
            except ValueError as error:
                raise APIError(400, str(error)) from error
            return {**result, 'path': str(self.manager.audit_log.log_dir),
                    'legacy_path': str(self.manager.audit_log.legacy_path),
                    'config': self.manager.audit_log.config()}
        if method == 'GET' and parts == ['log-config']:
            config = self.manager.audit_log.config()
            return {**config, 'path': str(self.manager.audit_log.log_dir),
                    'dates': self.manager.audit_log.available_dates()}
        if method == 'PUT' and parts == ['log-config']:
            if not isinstance(payload, dict) or 'retention_days' not in payload:
                raise APIError(400, '请提供日志保留天数')
            try:
                return self.manager.audit_log.set_retention(payload['retention_days'])
            except ValueError as error:
                raise APIError(400, str(error)) from error
        if method == 'POST' and parts == ['logs', 'cleanup']:
            try:
                return self.manager.audit_log.cleanup()
            except ValueError as error:
                raise APIError(400, str(error)) from error
        if method == 'POST' and parts == ['backup']:
            return self.backup(payload)
        if method == 'POST' and parts in (['restore-preview'], ['restore']):
            return self.restore(payload, preview=parts == ['restore-preview'])
        if method == 'POST' and parts in (['definition-preview'], ['definition-import']):
            from .definition_import import DefinitionImporter
            return DefinitionImporter(self.manager).run(payload, preview=parts == ['definition-preview'])
        if len(parts) != 3 or parts[0] != 'tables':
            raise APIError(404, '接口不存在')
        _prefix, name, action = parts
        if method == 'GET':
            if action == 'rows':
                return self.rows(name, query)
            if action == 'snapshot':
                return self.snapshot(name)
            if action == 'export':
                return self.export(name, query)
            if action == 'check':
                return self.manager.check(name)
            if action == 'schema':
                with self.manager.session(name) as table, table._read_transaction() as connection:
                    definition = table.definition.to_mapping()
                    count = connection.execute('SELECT row_count FROM metadata').fetchone()[0]
                return {'definition': definition, 'version': fingerprint(definition), 'row_count': count}
        operations = {('POST', 'rows'): self.manager.insert, ('PUT', 'rows'): self.batch_edit,
                      ('PUT', 'cell-edits'): self.cell_edits,
                      ('POST', 'validate-edits'): self.validate_edits,
                      ('DELETE', 'rows'): self.manager.delete_rows, ('POST', 'clear'): self.clear,
                      ('POST', 'import'): self.import_data, ('POST', 'sql'): self.sql, ('POST', 'sql-export'): self.sql_export,
                      ('DELETE', 'schema'): self.manager.drop}
        if (method, action) in operations:
            return operations[method, action](name, payload)
        if method == 'PUT' and action == 'schema':
            return self.manager.save_schema(name, payload, creating=bool(payload.get('creating')))
        raise APIError(404, '接口不存在')
