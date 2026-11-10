"""Preview and apply dbdef/E imports, with per-table transactional migrations."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from core import SQLiteDatabase, TableDefinition, SchemaError
from core.definition_files import decode_upload, parse_modules, dbdef_definitions, infer_type, python_definition, normalize_definition_labels
from core.file_mapped_db import _locked
from core.sqlite_db import _INT, _FLOAT, _TEXT, _BOOL, _OBJECT
from .errors import APIError
from .sqlite_service import atomic_json, atomic_text, fingerprint, validate_populated_schema


def convert(value, column, *, from_file=False):
    kind, name = column.data_type.lower(), column.eng_name
    if value is None or (from_file and value == r'\N') or (value == '' and kind not in _TEXT):
        return None
    try:
        if kind in _INT:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError('小数不能转换为整数')
            if isinstance(value, str) and not re.fullmatch(r'[+-]?\d+', value):
                raise ValueError('必须为整数')
            return int(value)
        if kind in _FLOAT:
            result = float(value)
            if not math.isfinite(result):
                raise ValueError('必须为有限数值')
            if isinstance(value, int) and int(result) != value:
                raise ValueError('整数转为浮点数会丢失精度')
            return result
        if kind in _BOOL:
            if str(value).lower() not in ('true', 'false', '0', '1'):
                raise ValueError('布尔值应为 true/false 或 0/1')
            return str(value).lower() in ('true', '1')
        if kind in _TEXT:
            return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if kind in _OBJECT:
            return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError) as error:
        raise SchemaError(f'{name}: {error}') from error
    raise SchemaError(f'{name}: 不支持的类型 {kind}')


class DefinitionImporter:
    def __init__(self, manager):
        self.manager = manager

    def _existing(self, name):
        try:
            definition = self.manager.definition(name)
        except APIError:
            if (self.manager.data_dir / (name + '.sqlite3')).exists():
                raise APIError(409, f'{name} 存在无 JSON 定义的数据库，请先恢复对应定义')
            return None, [], None
        with SQLiteDatabase(self.manager.data_dir, schema_directory=self.manager.schema_dir) as db:
            table = db[name]
            with table._read_transaction() as connection:
                if len(table) > self.manager.max_rows:
                    raise APIError(413, f'{name} 超过 100,000 行，暂不支持通过网页迁移')
                rows = table._select(connection)
        return definition, rows, fingerprint({'definition': definition.to_mapping(), 'rows': rows})

    def _efile(self, module, old, keys):
        fields = module['columns']
        rows = []
        for line, cells in module['rows']:
            if len(cells) != len(fields):
                raise SchemaError(f"{module['name']} 第 {line} 行：需要 {len(fields)} 个值，实际 {len(cells)} 个；空字符串请写 ''")
            rows.append(dict(zip(fields, cells)))
        definition = copy.deepcopy(old.to_mapping()) if old else dict(
            head=dict(eng_name=module['name'], chn_name=module['props'].get('name', module['name'])), columns=[])
        known = {column['eng_name'] for column in definition['columns']}
        primary = list(old.primary_keys) if old else keys or [next((field for field in ('key', 'id') if field in fields), fields[0])]
        if not all(key in fields for key in primary):
            raise SchemaError(f"{module['name']}：E 文件必须包含主键 {', '.join(primary)}")
        for index, field in enumerate(fields):
            if field in known:
                continue
            kind = infer_type([row[field] for row in rows])
            descriptions = module['descriptions']
            definition['columns'].append(dict(eng_name=field,
                chn_name=(descriptions[index] if index < len(descriptions) else '') or field,
                data_type=kind, primary_key=field in primary, disp_type='数字' if kind in ('int', 'float') else '文本', disp_menu=''))
        return definition, rows

    @staticmethod
    def _apply_efile_overrides(definition, table_name, overrides):
        """Apply the user's confirmed E-file field choices to one definition.

        The preview remains an inference; the second request may carry an
        explicit, complete column list after the user has checked each field.
        Only schema metadata is accepted here, while values are still parsed
        and validated by the normal import path below.
        """
        if overrides is None:
            return definition
        if not isinstance(overrides, dict):
            raise SchemaError('E 文件字段确认结果必须是对象')
        choice = overrides.get(table_name)
        if choice is None:
            return definition
        if not isinstance(choice, dict) or not isinstance(choice.get('columns'), list):
            raise SchemaError(f'{table_name}：字段确认结果无效')
        aliases = {
            'integer': 'int', 'long': 'int', 'double': 'float', 'number': 'float', 'decimal': 'float',
            'string': 'str', 'text': 'str', 'varchar': 'str', 'char': 'str', 'date': 'str',
            'datetime': 'str', 'time': 'str', 'boolean': 'bool', 'array': 'list', 'object': 'dict',
        }
        allowed = _INT | _FLOAT | _TEXT | _BOOL | _OBJECT
        current = {column['eng_name']: column for column in definition['columns']}
        if len(choice['columns']) != len(current):
            raise SchemaError(f'{table_name}：必须确认全部 {len(current)} 个字段')
        columns, seen = [], set()
        for item in choice['columns']:
            if not isinstance(item, dict):
                raise SchemaError(f'{table_name}：字段确认项必须是对象')
            field = item.get('eng_name')
            if field not in current or field in seen:
                raise SchemaError(f'{table_name}：字段确认结果包含未知或重复字段 {field!r}')
            seen.add(field)
            raw_kind = str(item.get('data_type', '')).strip().lower()
            current_kind = str(current[field].get('data_type', '')).strip().lower()
            kind = current_kind if raw_kind == current_kind else aliases.get(raw_kind, raw_kind)
            if kind not in allowed:
                raise SchemaError(f'{table_name}.{field}：不支持的数据类型 {raw_kind}')
            primary = item.get('primary_key')
            if not isinstance(primary, bool):
                raise SchemaError(f'{table_name}.{field}：主键标记必须是布尔值')
            column = dict(current[field])
            column['data_type'] = kind
            column['primary_key'] = primary
            column['disp_type'] = '数字' if kind in _INT | _FLOAT else column.get('disp_type') or '文本'
            columns.append(column)
        if not any(column['primary_key'] for column in columns):
            raise SchemaError(f'{table_name}：至少选择一个 KEY 字段')
        definition['columns'] = columns
        return definition

    def _prepare(self, payload, staging):
        text, encoding = decode_upload(payload)
        format = payload.get('format')
        if format not in ('dbdef', 'e'):
            raise APIError(400, '请选择 dbdef 或 E 文件')
        modules = parse_modules(text, dbdef=format == 'dbdef')
        mode = payload.get('mode', 'merge')
        if mode not in ('merge', 'append', 'replace'):
            raise APIError(400, '无效的导入方式')
        keys_text = payload.get('keys', '')
        if not isinstance(keys_text, str):
            raise APIError(400, '主键必须为逗号分隔的字段名')
        keys = [key.strip() for key in keys_text.split(',') if key.strip()]
        if len(set(keys)) != len(keys):
            raise SchemaError('主键字段重复')
        documents = dbdef_definitions(modules) if format == 'dbdef' else modules
        if not 1 <= len(documents) <= 200:
            raise SchemaError('一次导入应包含 1–200 张表')
        plans, seen_names = [], set()
        for document in documents:
            name = self.manager.name(document['head']['eng_name'] if format == 'dbdef' else document['name'])
            if name.casefold() in seen_names:
                raise SchemaError(f'重复表名（忽略大小写）：{name}')
            seen_names.add(name.casefold())
            old, existing, version = self._existing(name)
            desired, incoming = (document, []) if format == 'dbdef' else self._efile(document, old, keys)
            desired = normalize_definition_labels(desired)
            inferred_definition = TableDefinition.from_mapping(desired, self.manager.schema_dir, name)
            token_definition = inferred_definition.to_mapping()
            if format == 'e':
                desired = self._apply_efile_overrides(copy.deepcopy(token_definition), name, payload.get('definition_overrides'))
            definition = TableDefinition.from_mapping(desired, self.manager.schema_dir, name)
            if old and existing:
                validate_populated_schema(old, definition)
            desired = definition.to_mapping()
            source = python_definition(desired)
            if len(incoming) > self.manager.max_rows:
                raise APIError(413, f'{name} 超过 100,000 条导入记录')
            # Validate both converted old data and incoming data before touching production.
            retained = [] if format == 'e' and mode == 'replace' else [
                {col.eng_name: convert(row.get(col.eng_name), col) for col in definition.columns} for row in existing]
            definition.validate_rows(retained)
            identity = lambda row: tuple(row.get(key) for key in definition.primary_keys)
            indexed = {identity(row): row for row in retained}
            seen, inserted, updated = set(), 0, 0
            duplicates, skipped = 0, 0
            duplicate_examples, skipped_examples = [], []
            for number, row in enumerate(incoming, 1):
                converted = {col.eng_name: convert(row[col.eng_name], col, from_file=True) for col in definition.columns if col.eng_name in row}
                # A duplicate is recoverable; malformed values and missing keys are not.
                definition.validate_rows([converted])
                key = identity(converted)
                if key in seen:
                    duplicates += 1
                    if len(duplicate_examples) < 5:
                        duplicate_examples.append(f'第 {number} 条 {str(key)[:160]}')
                seen.add(key)
                if key in indexed:
                    if mode == 'append':
                        skipped += 1
                        if len(skipped_examples) < 5:
                            skipped_examples.append(f'第 {number} 条 {str(key)[:160]}')
                        continue
                    indexed[key].update(converted)
                    updated += 1
                else:
                    item = {col.eng_name: converted.get(col.eng_name) for col in definition.columns}
                    indexed[key] = item
                    retained.append(item)
                    inserted += 1
            if len(retained) > self.manager.max_rows:
                raise APIError(413, f'{name} 合并后超过 100,000 行')
            definition.to_json(staging / 'def')
            with SQLiteDatabase(staging / 'data', schema_directory=staging / 'def') as db:
                table = db[name]
                if retained:
                    table.insert_many(retained)
                connection = table._connection()
                ddl = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT IN ('metadata', 'sqlite_sequence') ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END")]
                encoded = connection.execute('SELECT ' + ','.join(table._fields.values()) + ' FROM data ORDER BY _pos').fetchall()
            old_columns = {col.eng_name: col for col in old.columns} if old else {}
            new_columns = {col.eng_name: col for col in definition.columns}
            removed = [key for key in old_columns if key not in new_columns]
            added = [key for key in new_columns if key not in old_columns]
            changed = [key for key in old_columns if key in new_columns and old_columns[key] != new_columns[key]]
            warnings = []
            if duplicates:
                policy = '追加模式保留已有或首次出现的记录，跳过后续重复记录' if mode == 'append' else '同一主键采用文件中最后一条记录'
                warnings.append(f'文件内主键重复 {duplicates} 条：{policy}，继续加载。示例：' + '；'.join(duplicate_examples))
            if skipped:
                warnings.append(f'追加时跳过主键冲突 {skipped} 条（含文件内重复及与数据库已有记录冲突），其余记录继续加载。示例：' + '；'.join(skipped_examples))
            if format == 'dbdef':
                warnings.append('max_record_num / data_length 等旧版容量信息不写入新定义。')
            if removed:
                warnings.append('将删除字段及其数据：' + ', '.join(removed))
            if format == 'e' and mode == 'replace' and existing:
                warnings.append(f'将用文件内数据替换原有 {len(existing)} 条记录。')
            if format == 'e' and not old:
                warnings.append('新表字段类型由文件推断，主键：' + ', '.join(definition.primary_keys))
            schema_path = self.manager.schema_dir / (name + '.json')
            summary = dict(name=name, exists=old is not None, before=len(existing), after=len(retained),
                incoming=len(incoming), inserted=inserted, updated=updated, duplicates=duplicates, skipped=skipped,
                added=added, removed=removed, changed=changed,
                primary_keys=list(definition.primary_keys), definition=desired, warnings=warnings,
                json_path=str(schema_path), python_path=str(schema_path.with_suffix('.py')))
            plans.append(dict(summary=summary, version=version, old=old.to_mapping() if old else None,
                              definition=desired, token_definition=token_definition, ddl=ddl, encoded=encoded,
                              retained=retained,
                              source=source))
        # Bind the token to the source and database revision, allowing the user
        # to confirm different keys/types after preview (including duplicate keys).
        token = fingerprint({'input': payload.get('content', text), 'encoding': encoding, 'format': format, 'mode': mode, 'keys': keys,
                             'plans': [(plan['token_definition'], plan['version']) for plan in plans]})
        return plans, encoding, token

    def _group_database_name(self, payload, plans):
        value = payload.get('database_name') or payload.get('source_name') or ''
        value = Path(str(value)).stem if value else ''
        try:
            return self.manager.name(value)
        except APIError:
            return plans[0]['summary']['name']

    def _apply_group(self, plans, payload, staging):
        """Create one SQLite/JSON/Python bundle for a multi-table import."""
        if any(plan['old'] is not None for plan in plans):
            raise APIError(409, '多表导入暂不覆盖已有分散数据库，请先将目标表整理到同一数据库文件')
        database_name = self._group_database_name(payload, plans)
        target_db = self.manager.data_dir / (database_name + '.sqlite3')
        target_json = self.manager.schema_dir / (database_name + '.json')
        target_python = self.manager.schema_dir / (database_name + '.py')
        if any(path.exists() for path in (target_db, target_json, target_python)):
            if payload.get('confirm') != database_name:
                raise APIError(409, f'多表数据库 {database_name} 已存在，请在确认字段输入 {database_name}')
            raise APIError(409, '多表导入覆盖已有数据库前，请先备份并删除原文件')
        document = {
            'database': {'eng_name': database_name, 'chn_name': payload.get('database_chn_name') or database_name},
            'tables': [plan['definition'] for plan in plans],
        }
        group_root = Path(staging) / 'group'
        group_def = group_root / 'def'
        group_data = group_root / 'data'
        group_def.mkdir(parents=True)
        atomic_json(group_def / (database_name + '.json'), document)
        with SQLiteDatabase(group_data, schema_directory=group_def) as database:
            for plan in plans:
                table = database[plan['summary']['name']]
                if plan['retained']:
                    table.insert_many(plan['retained'])
            for name in (plan['summary']['name'] for plan in plans):
                connection = database[name]._connection()
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        source_db = group_data / (database_name + '.sqlite3')
        if not source_db.is_file():
            raise APIError(500, '多表 SQLite 文件未生成')
        target_db.parent.mkdir(parents=True, exist_ok=True)
        target_json.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_db, target_db)
        atomic_json(target_json, document)
        atomic_text(target_python, python_definition(document))
        return database_name

    def _apply_grouped_existing(self, plan):
        """Apply one-table E/dbdef changes while preserving grouped siblings."""
        name = plan['summary']['name']
        old = plan['old']
        with SQLiteDatabase(self.manager.data_dir, schema_directory=self.manager.schema_dir) as database:
            table = database[name]
            with table._read_transaction() as connection:
                current_rows = table._select(connection)
            if fingerprint({'definition': old, 'rows': current_rows}) != plan['version']:
                raise APIError(409, f'{name} 数据已变化，请重新预览')
        self.manager.save_schema(name, {'definition': plan['definition'],
                                        'version': fingerprint(old)}, _already_locked=True)
        with SQLiteDatabase(self.manager.data_dir, schema_directory=self.manager.schema_dir) as database:
            table = database[name]
            def replace(connection):
                connection.execute(f'DELETE FROM {table._data_sql}')
                if plan['retained']:
                    table._insert(connection, plan['retained'])
                return len(plan['retained'])
            replace_count = table._write(replace)
        return replace_count

    def _apply(self, plan):
        path = self.manager.data_dir / (plan['summary']['name'] + '.sqlite3')
        created = plan['old'] is None
        if created:
            try:
                with path.open('xb'):
                    pass
            except FileExistsError as error:
                raise APIError(409, '数据库已创建，请重新预览') from error
        try:
            return self._apply_table(plan)
        except BaseException:
            # Remove only our exclusively-created, uncommitted database shell.
            if created:
                with closing(sqlite3.connect(path)) as connection:
                    committed = connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone()
                if not committed:
                    path.unlink(missing_ok=True)
            raise

    def _apply_table(self, plan):
        manager = self.manager
        name, definition = plan['summary']['name'], plan['definition']
        path = manager.data_dir / (name + '.sqlite3')
        schema_path = manager.schema_dir / (name + '.json')
        # BEGIN IMMEDIATE checks the preview revision after acquiring the writer lock.
        with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            try:
                if plan['old']:
                    actual = connection.execute('SELECT fingerprint FROM metadata').fetchone()[0]
                    if actual != fingerprint(plan['old']):
                        raise APIError(409, f'{name} 结构已变化，请重新预览')
                    # Decode the locked snapshot using the old physical column order.
                    columns = plan['old']['columns']
                    rows = []
                    for raw in connection.execute('SELECT ' + ','.join(f'c{i}' for i in range(len(columns))) + ' FROM data ORDER BY _pos'):
                        row = {}
                        for column, value in zip(columns, raw):
                            kind = column['data_type'].lower()
                            if value is not None:
                                if kind in _OBJECT:
                                    import msgpack
                                    value = msgpack.unpackb(value, raw=False, strict_map_key=False)
                                elif kind in _BOOL:
                                    value = bool(value)
                                elif kind in _FLOAT:
                                    value = float(value)
                            row[column['eng_name']] = value
                        rows.append(row)
                    if fingerprint({'definition': plan['old'], 'rows': rows}) != plan['version']:
                        raise APIError(409, f'{name} 数据已变化，请重新预览')
                elif connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                    raise APIError(409, f'{name} 已由其他进程创建，请重新预览')
                atomic_json(manager._journal, dict(name=name, old=plan['old'], new=definition, python=True, created=plan['old'] is None))
                if plan['old']:
                    connection.execute('DROP TABLE data')
                    connection.execute('UPDATE metadata SET fingerprint=?, row_count=0 WHERE version=1', (fingerprint(definition),))
                else:
                    connection.execute('CREATE TABLE metadata (version INTEGER PRIMARY KEY CHECK(version=1), fingerprint TEXT NOT NULL, row_count INTEGER NOT NULL)')
                    connection.execute('INSERT INTO metadata VALUES (1,?,0)', (fingerprint(definition),))
                for statement in plan['ddl']:
                    connection.execute(statement)
                fields = ','.join(f'c{i}' for i in range(len(definition['columns'])))
                placeholders = ','.join('?' for _ in definition['columns'])
                connection.executemany(f'INSERT INTO data ({fields}) VALUES ({placeholders})', plan['encoded'])
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        atomic_json(schema_path, definition)
        atomic_text(schema_path.with_suffix('.py'), plan['source'])
        manager._journal.unlink()

    def run(self, payload, *, preview):
        manager = self.manager
        with manager._gate, _locked(manager._schema_lock), tempfile.TemporaryDirectory(prefix='rtdb-definition-') as folder:
            manager._recover()
            plans, encoding, token = self._prepare(payload, Path(folder))
            # Multi-table input always uses the grouped SQLite/JSON/Python
            # representation; when no source/database name is supplied,
            # _group_database_name() falls back to the first table name.
            grouped = len(plans) > 1
            if grouped:
                database_name = self._group_database_name(payload, plans)
                for plan in plans:
                    plan['summary']['database_file'] = database_name + '.sqlite3'
                    plan['summary']['json_path'] = str(manager.schema_dir / (database_name + '.json'))
                    plan['summary']['python_path'] = str(manager.schema_dir / (database_name + '.py'))
            warnings = [f'{plan["summary"]["name"]}：{warning}' for plan in plans for warning in plan['summary']['warnings']]
            if preview:
                return dict(tables=[plan['summary'] for plan in plans], encoding=encoding, token=token, warnings=warnings)
            if payload.get('token') != token:
                raise APIError(409, '文件、导入选项或数据库已变化，请重新预览；本次尚未写入')
            results = []
            def outcome(ok, pending):
                return dict(ok=ok, results=results, pending=pending, warnings=warnings,
                            duplicates=sum(item.get('duplicates', 0) for item in results if item['ok']),
                            skipped=sum(item.get('skipped', 0) for item in results if item['ok']))
            if grouped:
                grouped_sources = set()
                grouped_existing = True
                for plan in plans:
                    if plan['old'] is None:
                        grouped_existing = False
                        break
                    try:
                        source = manager.definition_source(plan['summary']['name'])
                        raw_document = json.loads(source.read_text(encoding='utf-8-sig'))
                    except Exception:
                        grouped_existing = False
                        break
                    if not (isinstance(raw_document, dict) and isinstance(raw_document.get('tables'), list)):
                        grouped_existing = False
                        break
                    grouped_sources.add(source)
                if grouped_existing and len(grouped_sources) == 1:
                    for plan in plans:
                        try:
                            self._apply_grouped_existing(plan)
                            summary = plan['summary']
                            results.append(dict(name=summary['name'], ok=True, rows=summary['after'],
                                                **{key: summary[key] for key in ('incoming', 'inserted', 'updated', 'duplicates', 'skipped', 'warnings')}))
                        except Exception as error:
                            manager._recover()
                            results.append(dict(name=plan['summary']['name'], ok=False, error=str(error)))
                            return outcome(False, [p['summary']['name'] for p in plans[len(results):]])
                    return outcome(True, [])
                try:
                    database_name = self._apply_group(plans, payload, Path(folder))
                    for plan in plans:
                        summary = plan['summary']
                        results.append(dict(name=summary['name'], ok=True, rows=summary['after'],
                                            database_name=database_name,
                                            **{key: summary[key] for key in ('incoming', 'inserted', 'updated', 'duplicates', 'skipped', 'warnings')}))
                    return outcome(True, [])
                except Exception as error:
                    manager._recover()
                    results.append(dict(name=plans[0]['summary']['name'], ok=False, error=str(error)))
                    return outcome(False, [p['summary']['name'] for p in plans])
            if plans:
                plan = plans[0]
                try:
                    source = manager.definition_source(plan['summary']['name'])
                    raw_document = json.loads(source.read_text(encoding='utf-8-sig'))
                except Exception:
                    raw_document = None
                if isinstance(raw_document, dict) and isinstance(raw_document.get('tables'), list):
                    try:
                        self._apply_grouped_existing(plan)
                        summary = plan['summary']
                        results.append(dict(name=summary['name'], ok=True, rows=summary['after'],
                                            **{key: summary[key] for key in ('incoming', 'inserted', 'updated', 'duplicates', 'skipped', 'warnings')}))
                        return outcome(True, [])
                    except Exception as error:
                        manager._recover()
                        results.append(dict(name=plan['summary']['name'], ok=False, error=str(error)))
                        return outcome(False, [])
            for plan in plans:
                try:
                    self._apply(plan)
                    summary = plan['summary']
                    results.append(dict(name=summary['name'], ok=True, rows=summary['after'],
                                        **{key: summary[key] for key in ('incoming', 'inserted', 'updated', 'duplicates', 'skipped', 'warnings')}))
                except Exception as error:
                    manager._recover()
                    results.append(dict(name=plan['summary']['name'], ok=False, error=str(error)))
                    return outcome(False, [p['summary']['name'] for p in plans[len(results):]])
            return outcome(True, [])
