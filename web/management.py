"""Browser workflows built on the SQLite RTDB management service."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import time
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError
from core.sqlite_db import _INT, _FLOAT, _BOOL, _OBJECT, _TEXT
from .audit import status_for_exception
from .errors import APIError
from .tasks import checkpoint, record_timing
from . import write_receipts
from .sqlite_service import fingerprint, browser_value, json_blob


class SQLiteManagement:
    def __init__(self, manager):
        self.manager = manager

    def table_exists(self, name):
        try:
            self.manager.definition(name)
            return True
        except APIError:
            return False

    def rows(self, name, query):
        started = time.perf_counter()
        try:
            limit = int(query.get('limit') or 100)
            offset = int(query.get('offset') or 0)
        except (TypeError, ValueError) as error:
            raise APIError(400, '分页参数必须为整数') from error
        if not 1 <= limit <= 500:
            raise APIError(400, '单页必须为 1–500 行')
        with self.manager.session(name) as table:
            with table._read_transaction() as connection:
                rows, total = self.manager._selection(table, connection, query.get('where', ''),
                    query.get('order', ''), limit, offset)
                table_total = self.manager._row_count(table, connection)
            records = [{'row': row, 'ref': {'key': [row[key] for key in table.primary_keys],
                                           'version': fingerprint(row)}} for row in rows]
        return dict(records=records, total=total, table_total=table_total, limit=limit, offset=offset,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2))

    def sql(self, name, payload, *, exporting=False):
        statement = payload.get('sql', '')
        # Only deterministic full-table views qualify. General SQL (time/random,
        # computed columns, aggregates) always executes, even without data commits.
        eligible = (not exporting and payload.get('compact') and self.manager.database_file
            and isinstance(statement, str) and re.fullmatch(
                r'SELECT\s+\*\s+FROM\s+(?:"'+re.escape(name)+r'"|'+re.escape(name)+r')\s*;?', statement.strip(), re.I))
        if not eligible:
            return self._sql(name, payload, exporting=exporting)
        key = fingerprint([name,statement,payload.get('limit',100),payload.get('offset',0),payload.get('order')])
        started = time.perf_counter()
        with self.manager.read_scope():
            cache = self.manager._runtime.versions
            result, stamp = cache.probe(self.manager.database_file,key,payload.get('if_version'))
            if result is not None:
                result['elapsed_ms'] = round((time.perf_counter()-started)*1000,2)
                result['cache_hit'] = True
                result['timings']={'cache_ms':result['elapsed_ms'],'open_ms':0,'query_ms':0,'decode_ms':0,'version_ms':0}
                return result
            signature=fingerprint([self.manager.database_file,name,statement,payload.get('limit',100),payload.get('order')])
            page={'seek':cache.page_anchor(payload.get('page_cursor'),signature,stamp,payload.get('offset',0))}
            result = self._sql(name,payload,exporting=exporting,page=page)
            _,after=cache.probe(self.manager.database_file,key,None)
            if after!=stamp and page.get('used'):
                stamp=after;page={}
                result=self._sql(name,payload,exporting=exporting,page=page)
                _,after=cache.probe(self.manager.database_file,key,None)
            if after==stamp and page.get('edges'):
                first,last=page['edges'];offset=result['offset'];limit=result['limit']
                if offset>=limit:result['previous_cursor']=cache.page_token(signature,stamp,offset-limit,first,True)
                if offset+limit<result['total']:result['next_cursor']=cache.page_token(signature,stamp,offset+limit,last,False)
            cache.store(self.manager.database_file,key,stamp,result)
            return result

    def resolve_records(self, name, payload):
        keys=payload.get('keys')
        if not isinstance(keys,list) or len(keys)>500:
            raise APIError(400,'一次最多检查 500 条草稿记录')
        with self.manager.session(name) as table, table._read_transaction() as connection:
            if payload.get('schema_version')!=fingerprint(table.definition.to_mapping()):
                raise APIError(409,'表结构已变化，不能恢复旧结构草稿；请导出草稿后核对')
            coerced=[self.manager._key(table,key) for key in keys]
            values=[tuple(item[k] for k in table.primary_keys) for item in coerced]
            lookup=[v[0] if len(v)==1 else v for v in values]
            rows=table.kv._get_many(connection,lookup)
            records=[{'row':rows[key],'ref':{'key':list(values[i]),'version':fingerprint(rows[key])}} if key in rows else None for i,key in enumerate(lookup)]
        return {'records':records}

    def _sql(self, name, payload, *, exporting=False, page=None):
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
        timings={}
        with self.manager.session(name) as table:
            self.manager._view(table, table._connection())
            timings['open_ms']=(time.perf_counter()-started)*1000
            # Only the unmodified table view has unambiguous editable row identities.
            full_table = bool(re.fullmatch(r'SELECT\s+\*\s+FROM\s+(?:"' + re.escape(name) + r'"|' + re.escape(name) + r')', statement, re.I))
            editable = full_table and not getattr(table, 'read_only', False)
            source = f'({statement}\n)'
            query_started=time.perf_counter()
            with table._read_transaction() as connection:
                table_total = self.manager._row_count(table, connection)
                native_pk_notnull = True
                if editable and table.native:
                    pk_notnull = {
                        row[1]: bool(row[3])
                        for row in connection.execute(f'PRAGMA table_info({table._data_sql})')
                    }
                    native_pk_notnull = all(pk_notnull.get(k, False) for k in table.primary_keys)
                with self.manager._reader(table, connection):
                    columns = [item[0] for item in connection.execute(f'SELECT * FROM {source} LIMIT 0').description]
                    order = payload.get('order')
                    if order is not None:
                        if not isinstance(order, dict) or order.get('column') not in columns or order.get('direction') not in ('ASC', 'DESC'):
                            raise APIError(400, '排序列或方向无效')
                        ordering = ' ORDER BY "' + order['column'].replace('"', '""') + '" ' + order['direction']
                    else:
                        ordering = ' ORDER BY ' + ','.join('"' + key.replace('"', '""') + '"' for key in table.primary_keys) if full_table else ''
                    total = table_total if full_table else connection.execute(f'SELECT COUNT(*) FROM {source}').fetchone()[0]
                    if exporting and total > self.manager.max_rows:
                        raise APIError(413, '导出结果超过 100,000 行，请增加 SQL 过滤条件')
                    can_seek = (
                        editable
                        and native_pk_notnull
                        and all(table._types[k] in _INT | _FLOAT | _TEXT | _BOOL for k in table.primary_keys)
                        and (order is None or (len(table.primary_keys) == 1 and order['column'] == table.primary_keys[0]))
                    )
                    raw_seek = page.get('seek') if page is not None and can_seek else None
                    seek = raw_seek if (raw_seek and all(v is not None for v in raw_seek.get('anchor', ()))) else None
                    if seek:
                        descending=bool(order and order['direction']=='DESC')
                        reverse=seek['backwards'];less=descending!=reverse
                        key_names=','.join('"'+k.replace('"','""')+'"' for k in table.primary_keys)
                        params=','.join('?' for _ in table.primary_keys)
                        boundary=f'({key_names}) {"<" if less else ">"} ({params})'
                        direction='DESC' if descending!=reverse else 'ASC'
                        seek_order=' ORDER BY '+','.join('"'+k.replace('"','""')+'" '+direction for k in table.primary_keys)
                        values=connection.execute(f'SELECT * FROM {source} WHERE {boundary}{seek_order} LIMIT ?',(*seek['anchor'],limit)).fetchall()
                        if reverse:values.reverse()
                        page['used']=True
                    else:
                        values = connection.execute(f'SELECT * FROM {source}{ordering} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            timings['query_ms']=(time.perf_counter()-query_started)*1000
            decode_started=time.perf_counter()
            # 对非全表查询中的重复列名分配唯一显示别名，防止字典键静默覆盖。
            output_columns = columns
            if not editable and len(set(columns)) != len(columns):
                counts, deduped = {}, []
                for col in columns:
                    counts[col] = counts.get(col, 0) + 1
                    deduped.append(col if counts[col] == 1 else f'{col}_{counts[col]}')
                output_columns = deduped
            rows = []
            for raw in values:
                if editable:
                    rows.append(table._decode(raw))
                    continue
                row = {}
                for raw_col, out_col, value in zip(columns, output_columns, raw):
                    col_kind = table._types.get(raw_col)
                    if not table.native and col_kind in _BOOL and isinstance(value, int) and value in (0, 1):
                        value = bool(value)
                    elif isinstance(value, bytes):
                        if col_kind in _OBJECT:
                            try:
                                value = table._msgpack.unpackb(value, raw=False, strict_map_key=False)
                            except Exception:
                                value = value.hex()
                        else:
                            value = value.hex()
                    row[out_col] = value
                rows.append(row)
            columns = output_columns
            timings['decode_ms']=(time.perf_counter()-decode_started)*1000
            version_started=time.perf_counter()
            records = [{'row': row, 'ref': {'key': [row[key] for key in table.primary_keys], 'version': fingerprint(row)}}
                       for row in rows] if editable and not exporting else []
            if page is not None and can_seek and rows:
                page['edges']=[tuple(rows[i][k] for k in table.primary_keys) for i in (0,-1)]
        result = dict(rows=rows, records=records, columns=columns, total=total, table_total=table_total,
                    rowcount=len(rows), limit=limit, offset=offset, readonly=True, editable=editable,
                    truncated=False, elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    pagination_mode='cursor' if page and page.get('used') else 'offset')
        if payload.get('compact') and not exporting:
            version = fingerprint({'database': self.manager.database_file, 'table': name,
                'schema': table._schema_fingerprint, 'sql': statement, 'order': order,
                'offset': offset, 'limit': limit, 'columns': columns,
                'records': [record['ref'] for record in records] if editable else rows})
            result['result_version'] = version
            if editable:
                result.pop('rows')
            else:
                result.pop('records')
            if payload.get('if_version') == version:
                result.pop('records', None); result.pop('rows', None)
                result['unchanged'] = True
            elif editable and payload.get('if_version') and 'known_records' in payload:
                known = payload['known_records']
                if not isinstance(known, list) or len(known) > 500 or any(
                        not isinstance(ref, dict) or not isinstance(ref.get('key'), list) or
                        not isinstance(ref.get('version'), str) for ref in known):
                    raise APIError(400, 'known_records 必须是最多 500 条记录版本')
                previous = {fingerprint(browser_value(ref['key'])): ref['version'] for ref in known}
                result['record_order'] = [record['ref']['key'] for record in records]
                result['records'] = [record for record in records
                    if previous.get(fingerprint(browser_value(record['ref']['key']))) != record['ref']['version']]
                result['delta'] = True
        timings['version_ms']=(time.perf_counter()-version_started)*1000
        result['timings']={k:round(v,3) for k,v in timings.items()}
        return result

    def sql_export(self, name, payload):
        checkpoint('查询导出数据')
        result = self.sql(name, {**payload, 'readonly_only': True}, exporting=True)
        checkpoint('生成导出文件')
        format = payload.get('format', 'json')
        if format == 'json':
            document = {'table': name, 'sql': payload['sql'], 'columns': result['columns'], 'rows': result['rows']}
            if result['editable']:
                document['definition'] = self.manager.definition(name).to_mapping()
            return {'text': json.dumps(document, ensure_ascii=False, indent=2, default=json_blob),
                    'mime': 'application/json', 'filename': name + '.json'}
        if format != 'csv':
            raise APIError(400, '仅支持 JSON / CSV')
        fields = payload.get('columns', result['columns'])
        if not isinstance(fields, list) or not fields or any(field not in result['columns'] for field in fields):
            raise APIError(400, '请选择有效的导出字段')
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        for index, row in enumerate(result['rows']):
            if index % 500 == 0: checkpoint('生成 CSV 文件', index, len(result['rows']))
            cells = []
            for field in fields:
                value = row[field]
                if value is None:
                    cells.append(r'\N')
                elif isinstance(value, (dict, list, bool)):
                    cells.append(json.dumps(value, ensure_ascii=False))
                elif isinstance(value, bytes):
                    cells.append(value.hex())
                else:
                    value = str(value)
                    cells.append('\\' + value if value.startswith('\\') else value)
            writer.writerow(cells)
        return {'text': '\ufeff' + stream.getvalue(), 'mime': 'text/csv;charset=utf-8', 'filename': name + '.csv'}

    @staticmethod
    def revision(table, connection):
        """流式增量计算全表版本哈希，避免大表一次性加载为巨型 JSON 字符串。"""
        hasher = hashlib.sha256()
        hasher.update(fingerprint(table.definition.to_mapping()).encode('ascii'))
        fields = ','.join(table._fields.values())
        cursor = connection.execute(f'SELECT {fields} FROM {table._data_sql} ORDER BY {table._row_order}')
        while True:
            batch = cursor.fetchmany(1000)
            if not batch:
                break
            for raw in batch:
                hasher.update(b'\n')
                hasher.update(fingerprint(table._decode(raw)).encode('ascii'))
        return hasher.hexdigest()

    def snapshot(self, name):
        with self.manager.session(name) as table:
            with table._read_transaction() as connection:
                return {'version': self.revision(table, connection),
                        'count': self.manager._row_count(table, connection)}

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
        new_rows, new_keys, warnings, issues = [], {}, [], []
        lookup = []
        key_value = lambda key: key[0] if len(table.primary_keys) == 1 else tuple(key)
        for index, row in enumerate(inserts, 1):
            if not isinstance(row, dict):
                raise APIError(400, f'新增第 {index} 行必须是记录对象')
            row = table.definition.apply_defaults(row)
            missing = [key for key in table.primary_keys if row.get(key) is None
                       or isinstance(row[key], str) and not row[key].strip()]
            if missing:
                warnings.append(f'新增第 {index} 行：主键字段 {"、".join(missing)} 不能为空')
                issues.append({'draft_index':index-1,'fields':missing,'message':warnings[-1]})
                continue
            try:
                value = self.manager._coerce(table, row, complete=True)
            except (APIError, SchemaError) as error:
                warnings.append(f'新增第 {index} 行：{error}')
                invalid=[]
                for field,value in row.items():
                    try: self.manager._coerce(table,{field:value})
                    except (APIError,SchemaError,TypeError,ValueError): invalid.append(field)
                issues.append({'draft_index':index-1,'fields':invalid or list(row),'message':warnings[-1]})
                continue
            identity = tuple(value[key] for key in table.primary_keys)
            key_text = '，'.join(f'{key}={value[key]!r}' for key in table.primary_keys)
            if identity in new_keys:
                warnings.append(f'新增第 {index} 行与新增第 {new_keys[identity]} 行主键重复（{key_text}）')
                issues.append({'draft_index':index-1,'fields':list(table.primary_keys),'message':warnings[-1]})
            else:
                lookup.append((index, identity, key_text))
            new_keys.setdefault(identity, index)
            new_rows.append(value)
        existing = table.kv._get_many(connection, [key_value(identity) for _, identity, _ in lookup])
        for index, identity, key_text in lookup:
            if key_value(identity) in existing:
                warnings.append(f'新增第 {index} 行与数据库已有记录主键重复（{key_text}）')
                issues.append({'draft_index':index-1,'fields':list(table.primary_keys),'message':warnings[-1]})
        if warnings:
            issues.sort(key=lambda issue: issue['draft_index'])
            warnings = [issue['message'] for issue in issues]
            raise APIError(422, '\n'.join(warnings), details={'warnings': warnings,'issues':issues})

        prepared, seen, conflicts = [], set(), []
        references = []
        for change in changes:
            if not isinstance(change, dict):
                raise APIError(400, '单元格修改必须为对象')
            reference = change.get('ref')
            if not isinstance(reference, dict):
                raise APIError(400, '记录引用必须是对象')
            try:
                values = self.manager._coerce(table, change.get('values'))
            except (APIError,SchemaError,TypeError,ValueError) as error:
                supplied=change.get('values') if isinstance(change.get('values'),dict) else {}
                invalid=[]
                for field,value in supplied.items():
                    try: self.manager._coerce(table,{field:value})
                    except (APIError,SchemaError,TypeError,ValueError): invalid.append(field)
                raise APIError(422,str(error),details={'issues':[{'key':reference.get('key'),
                    'fields':invalid or list(supplied),'message':str(error)}]}) from error
            if not values or set(values) & set(table.primary_keys):
                raise APIError(422, '请选择非主键字段修改；复制记录可创建新主键')
            where = self.manager._key(table, reference.get('key'))
            pk_tuple = tuple(
                float(where[k]) if table._types.get(k) in _FLOAT and isinstance(where[k], (int, float)) else where[k]
                for k in table.primary_keys
            )
            if pk_tuple in seen:
                raise APIError(400, '重复选择了同一记录')
            seen.add(pk_tuple)
            references.append((reference, key_value([where[k] for k in table.primary_keys])))
            prepared.append((where, values))
        current_rows = table.kv._get_many(connection, [key for _, key in references])
        for (reference, key), (_, values) in zip(references, prepared):
            current = current_rows.get(key)
            if current is None or fingerprint(current) != reference.get('version'):
                conflicts.append({'key':reference.get('key'),'fields':list(values),'current':current,
                    'current_ref':{'key':reference.get('key'),'version':fingerprint(current)} if current else None})
        if conflicts:
            raise APIError(409,'记录已变化，本批修改未保存。请比较服务器值与本地值后重新保存。',details={'conflicts':conflicts})
        return prepared, new_rows

    def validate_edits(self, name, payload):
        changes, inserts = self._cell_edit_payload(payload)
        with self.manager.session(name) as table, table._read_transaction() as connection:
            prepared, new_rows = self._prepare_cell_edits(table, connection, changes, inserts)
        return dict(ok=True, updated=len(prepared), inserted=len(new_rows))

    def cell_edits(self, name, payload):
        changes, inserts = self._cell_edit_payload(payload)
        opened=time.perf_counter()
        with self.manager.session(name) as table:
            record_timing('open_ms',opened)
            def update(connection):
                started=time.perf_counter()
                prepared, new_rows = self._prepare_cell_edits(table, connection, changes, inserts)
                record_timing('validate_ms',started)
                started=time.perf_counter()
                updated = sum(table._change(connection, 'update', where, values, strict=True) for where, values in prepared)
                inserted = table._insert(connection, new_rows)
                record_timing('write_ms',started)
                return dict(updated=updated, inserted=inserted) if 'inserts' in payload else dict(updated=updated)

            return write_receipts.write(table, update)

    def clear(self, name, payload):
        if payload.get('confirm') != name:
            raise APIError(400, '请输入表名确认清空')
        with self.manager.session(name) as table:
            def apply(connection):
                if payload.get('version') != self.revision(table, connection):
                    raise APIError(409, '表数据已变化，请重新确认；没有删除数据')
                return connection.execute(f'DELETE FROM {table._data_sql}').rowcount
            return write_receipts.write(table, lambda c: {'deleted': apply(c)})

    def export(self, name, query):
        content, mime, filename = self.manager.export(name, query)
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
        if payload.get('upload_id'):
            from .csv_imports import apply
            return apply(self.manager,name,payload)
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
                    connection.execute(f'DELETE FROM {table._data_sql}')
                if mode != 'merge':
                    return table._insert(connection, rows)
                for row in rows:
                    where = {key: row[key] for key in table.primary_keys}
                    values = {key: value for key, value in row.items() if key not in table.primary_keys}
                    table._change(connection, 'update', where, values, strict=True, upsert=row)
                return len(rows)

            def import_with_step(connection):
                result={'imported':apply(connection),'mode':mode}
                write_receipts.record_step(connection,name,{'name':name,'ok':True,**result})
                return result
            return write_receipts.write(table,import_with_step)

    def backup(self, payload):
        names = payload.get('tables')
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            raise APIError(400, '请选择要备份的表')
        if len(names) != len(set(names)):
            raise APIError(400, '备份表名重复')
        documents = []
        for index, name in enumerate(names):
            checkpoint(f'备份表 {name}', index, len(names))
            documents.append(self.manager.export_document(name))
        from .sqlite_service import json_blob
        return {'filename': 'rtdb-backup.json', 'mime': 'application/json',
                'text': json.dumps({'format': 'rtdb-web-backup-v1', 'tables': documents}, ensure_ascii=False, indent=2, default=json_blob)}

    def restore_documents(self, payload):
        if self.manager.native:
            raise APIError(403, '整批恢复不适用于原生 SQLite 库，请逐表导入记录')
        raw_text = payload.get('text')
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise APIError(400, '备份文件内容不能为空')
        document = json.loads(raw_text.lstrip('\ufeff'))
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
            existing = self.table_exists(name)
            if existing:
                current = self.manager.definition(name)
                if current.native_table is not None:
                    raise APIError(403, f'{name} 是原生 SQLite 表，整批恢复不能覆盖')
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
                exists = self.table_exists(name)
                snapshot = self.snapshot(name) if exists else {'version': None, 'count': 0}
                tables.append({'name': name, 'incoming': len(document['rows']), 'exists': exists, **snapshot})
            return {'tables': tables}
        if payload.get('confirm') != '恢复备份' or not isinstance(payload.get('versions'), dict):
            raise APIError(400, '请先预览备份并输入“恢复备份”确认')
        versions = payload['versions']
        with self.manager.exclusive():
            for document in documents:
                name = document['table']
                exists = self.table_exists(name)
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
        if method == 'POST' and parts == ['database-files', 'rebuild-embedded-definition']:
            return 'structure', '重新生成 SQLite 内置定义表', None
        if method == 'POST' and parts == ['database-files', 'delete-orphan']:
            return 'structure', '删除未定义数据表', None
        if method == 'POST' and parts == ['database-files', 'rebuild-from-embedded']:
            return 'structure', '重建数据表', None
        if method == 'GET' and parts == ['logs']:
            return 'system', '加载操作日志', None
        if parts == ['receipt-maintenance']:
            return 'system', {'GET':'查看回执维护','PUT':'修改回执压缩配置','POST':'申请压缩历史回执'}.get(method,'回执维护'), None
        if parts == ['storage-maintenance']:
            return 'system', {'GET':'预览存储清理','PUT':'修改存储保留配置','POST':'执行存储清理'}.get(method,'存储维护'), None
        if parts and parts[0] == 'database-snapshots':
            return 'structure', ('恢复整库快照为新数据库' if len(parts) == 3 else
                                 '生成整库快照' if method == 'POST' else '浏览整库快照'), None
        if parts and parts[0] == 'database-uploads':
            return 'structure', ('放弃临时上传' if method=='DELETE' else '校验并接收上传文件' if len(parts)==3 else
                                 '创建分块上传会话' if method=='POST' else '读取上传进度'), None
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
        with write_receipts.operation(self.manager, method, parts, query, payload, self.operation_descriptor(method,parts,payload)) as context:
            return self._audited_dispatch(method,parts,query,payload,context)

    @staticmethod
    def _is_readonly_audit_exempt(method, parts, result, context):
        """高频只读接口（含缓存命中）成功时跳过同步 fsync 审计；写操作、导入/导出/快照/维护与全部失败事件均完整留痕。"""
        if context.get('committed') or context.get('steps_written'):
            return False
        if isinstance(result, dict) and result.get('ok') is False:
            return False
        if method == 'GET':
            # 导出、数据检查仍审计；其余只读元数据/数据拉取跳过成功落盘
            if len(parts) == 3 and parts[0] == 'tables' and parts[2] in ('export', 'check'):
                return False
            return True
        if method == 'POST':
            is_table_request = len(parts) == 3 and parts[0] == 'tables'
            if parts in (['database-files', 'select'],):
                return True
            if is_table_request and parts[2] in ('sql', 'query-plan', 'validate-edits', 'resolve-records', 'schema-preview'):
                # POST /tables/{t}/sql 若执行了写 SQL，result['mode'] == 'write' 或 committed=True，必须审计
                if parts[2] == 'sql' and isinstance(result, dict) and result.get('mode') == 'write':
                    return False
                return True
        return False

    def _audited_dispatch(self, method, parts, query, payload, context):
        category, operation, table = self.operation_descriptor(method, parts, payload)
        if method == 'GET' and (parts in (['logs'], ['log-config'], ['audit-delivery'], ['receipt-maintenance'], ['database-snapshots'], ['storage-maintenance']) or parts and parts[0]=='database-uploads'):
            return self._dispatch(method, parts, query, payload)
        started = time.perf_counter()
        try:
            result = self._dispatch(method, parts, query, payload)
        except Exception as error:
            if context.get('steps_written'):
                delivery=getattr(self.manager,'audit_delivery',None)
                if delivery:delivery.enqueue(self.manager.database_file)
                else:write_receipts.publish(self.manager)
            self.manager.record_operation(category=category, operation=operation, status='failure',
                                          http_status=status_for_exception(error), method=method,
                                          path='/api/manage/' + '/'.join(parts), table=table,
                                          duration_ms=(time.perf_counter() - started) * 1000, error=error)
            raise
        if self._is_readonly_audit_exempt(method, parts, result, context):
            if isinstance(result, dict) and 'timings' in result:
                result['timings']['audit_ms'] = 0.0
            return result
        audit_started=time.perf_counter()
        if context.get('steps_written'):
            delivery=getattr(self.manager,'audit_delivery',None)
            if delivery:delivery.enqueue(self.manager.database_file)
            else:write_receipts.publish(self.manager)
        if context['committed']:
            delivery=getattr(self.manager,'audit_delivery',None)
            if delivery:
                delivery.enqueue(self.manager.database_file)
                if isinstance(result, dict):
                    result['audit_pending']=True
                logged=True
            else:logged = write_receipts.publish(self.manager,context['id'])
        else:
            logged = self.manager.record_operation(category=category, operation=operation, status='failure' if isinstance(result,dict) and result.get('ok') is False else 'success',
                                      http_status=200, method=method,
                                      path='/api/manage/' + '/'.join(parts), table=table,
                                      duration_ms=(time.perf_counter() - started) * 1000, result=result)
        if not logged and isinstance(result,dict):
            result['audit_warning'] = ('数据已提交，审计事件已保存在数据库，日志文件暂未写入；重启服务会补写。' if context['committed'] else '操作已完成，但日志文件写入失败，请检查日志目录。')
        if isinstance(result,dict) and 'timings' in result:result['timings']['audit_ms']=round((time.perf_counter()-audit_started)*1000,3)
        return result

    def _dispatch(self, method, parts, query, payload):
        if method != 'GET':
            is_table_request = len(parts) == 3 and parts[0] == 'tables'
            read_post = method == 'POST' and (
                parts in (['backup'], ['database-files', 'select'], ['restore-preview'], ['definition-preview']) or
                (is_table_request and parts[2] in ('sql', 'sql-export', 'query-plan', 'validate-edits', 'resolve-records', 'schema-preview'))
            )
            if not read_post:
                self.manager.require_writable(name=parts[1] if is_table_request else None)
        if parts == ['storage-maintenance']:
            maintenance = self.manager.storage_maintenance
            if method == 'GET': return maintenance.preview()
            if method == 'PUT': return maintenance.configure(payload)
            if method == 'POST': return maintenance.clean(payload)
            raise APIError(400, '不支持的存储维护操作')
        if parts and parts[0]=='database-uploads':
            from . import database_uploads
            if method!='GET' and not isinstance(payload,dict):raise APIError(400,'请求必须是 JSON 对象')
            if len(parts)==1 and method=='POST':return database_uploads.create(self.manager,payload)
            if len(parts)==2 and method=='GET':return database_uploads.status(self.manager,parts[1])
            if len(parts)==2 and method=='DELETE':return database_uploads.discard(self.manager,parts[1])
            if len(parts)==3 and parts[2]=='finalize' and method=='POST':return database_uploads.finalize(self.manager,parts[1])
            raise APIError(405,'不支持的分块上传操作')
        if parts and parts[0] == 'database-snapshots':
            from . import database_snapshots
            if method != 'GET' and not isinstance(payload, dict): raise APIError(400, '请求必须是 JSON 对象')
            if method == 'GET' and len(parts) == 1: return database_snapshots.listing(self.manager)
            if method == 'POST' and len(parts) == 1: return database_snapshots.create(self.manager)
            if method == 'POST' and len(parts) == 3 and parts[2] == 'restore':
                return database_snapshots.restore(self.manager, parts[1], payload)
            raise APIError(405, '不支持的整库快照操作')
        if parts==['receipt-maintenance']:
            from . import receipt_maintenance
            if not self.manager.database_file:raise APIError(400,'请先选择数据库')
            delivery=getattr(self.manager,'audit_delivery',None)
            if method=='GET':return {**receipt_maintenance.statistics(self.manager),'delivery':delivery.status() if delivery else {}}
            if not isinstance(payload,dict):raise APIError(400,'请求必须是 JSON 对象')
            if method=='PUT':
                result=receipt_maintenance.configure(self.manager,payload)
                if delivery:delivery.request_archive(self.manager.database_file)
                return result
            if method=='POST':
                if delivery:
                    delivery.request_archive(self.manager.database_file)
                    return {'queued':True}
                return receipt_maintenance.archive_batch(self.manager)
            raise APIError(405,'不支持的回执维护操作')
        if method=='GET' and parts==['audit-delivery']:
            delivery=getattr(self.manager,'audit_delivery',None)
            return delivery.status() if delivery else {'pending_databases':[],'error':None}
        if method != 'GET' and not isinstance(payload, dict):
            raise APIError(400, '请求必须是 JSON 对象')
        if method == 'GET' and parts == ['info']:
            return self.manager.info()
        if method == 'GET' and parts == ['tables']:
            return self.manager.tables()
        if method == 'GET' and parts == ['databases']:
            return self.manager.databases()
        if method == 'GET' and parts == ['database-files']:
            return self.manager.database_files()
        if method == 'POST' and parts == ['database-files', 'select']:
            return self.manager.select_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'create']:
            return self.manager.create_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'upload']:
            return self.manager.upload_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'rebuild-embedded-definition']:
            return self.manager.rebuild_embedded_definition(payload)
        if method == 'POST' and parts == ['database-files', 'delete-orphan']:
            return self.manager.delete_orphan_database_file(payload)
        if method == 'POST' and parts == ['database-files', 'rebuild-from-embedded']:
            return self.manager.rebuild_from_embedded_definition(payload)
        if method == 'GET' and parts == ['logs']:
            delivery=getattr(self.manager,'audit_delivery',None)
            if delivery:delivery.drain(2)
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
            if self.manager.native:
                raise APIError(403, '定义批量导入不适用于原生 SQLite 库，请在表结构页逐表编辑')
            if payload.get('upload_id'):
                from . import efile_imports
                return (efile_imports.validate if parts==['definition-preview'] else efile_imports.apply)(self.manager,payload)
            from .definition_import import DefinitionImporter
            return DefinitionImporter(self.manager).run(payload, preview=parts == ['definition-preview'])
        if len(parts) != 3 or parts[0] != 'tables':
            raise APIError(404, '接口不存在')
        _prefix, name, action = parts
        if method=='POST' and action=='schema-preview':
            from .schema_operations import preview
            return preview(self.manager,name,payload)
        if method=='POST' and action=='query-plan':
            from .schema_operations import query_plan
            return query_plan(self.manager,name,payload)
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
                    count = self.manager._row_count(table, connection)
                return {'definition': definition, 'version': fingerprint(definition), 'row_count': count}
        operations = {('POST', 'rows'): self.manager.insert, ('PUT', 'rows'): self.batch_edit,
                      ('PUT', 'cell-edits'): self.cell_edits,
                      ('POST', 'validate-edits'): self.validate_edits,
                      ('POST', 'resolve-records'): self.resolve_records,
                      ('DELETE', 'rows'): self.manager.delete_rows, ('POST', 'clear'): self.clear,
                      ('POST', 'import'): self.import_data, ('POST', 'sql'): self.sql, ('POST', 'sql-export'): self.sql_export,
                      ('DELETE', 'schema'): self.manager.drop}
        if (method, action) in operations:
            return operations[method, action](name, payload)
        if method == 'PUT' and action == 'schema':
            return self.manager.save_schema(name, payload, creating=bool(payload.get('creating')))
        raise APIError(404, '接口不存在')
