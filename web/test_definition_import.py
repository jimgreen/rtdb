"""Isolated HTTP import/migration regression tests; never use the user's database."""
import base64
import copy
import json
import runpy
import unittest
from unittest.mock import patch

from core import SQLiteDatabase, SchemaError
from core.definition_files import canonical_data_type
from web import test_sqlite_server as fixtures
from web.definition_import import DefinitionImporter

record = fixtures.record


DBDEF = '''<table_info>
@ table_id table_name_eng table_name_chn app_type max_record_num
# 1 meters电表 public 100
</table_info>
<meters>
@ column_id column_name_eng column_name_chn data_type data_length is_key display_type menu_name
# 1 id 编号 LONG 8 1 数字 ''
# 2 value 功率 DOUBLE 8 0 数字 ''
</meters>
'''
EFILE = '''<! version=1.0 !>
<meters>
@ id value name
// 编号 功率 名称
# 1 12.5 '北侧 电表'
# 2 18.5 'O''Brien'
# 9223372036854775807 0 ''
</meters>
'''
EFILE_CONFIRM = '''<! version=1.0 !>
<confirm_table>
@ code value label
# A 1.5 first
# B 2.5 second
</confirm_table>
'''


class DefinitionImportTests(unittest.TestCase):
    setUp = fixtures.SQLiteWebTests.setUp
    tearDown = fixtures.SQLiteWebTests.tearDown
    call = fixtures.SQLiteWebTests.call
    insert = fixtures.SQLiteWebTests.insert

    def preview(self, text, format='e', **options):
        payload = dict(text=text, format=format, **options)
        preview = self.call('POST', '/api/manage/definition-preview', payload)
        return payload, preview

    def apply(self, payload, preview, status=200):
        return self.call('POST', '/api/manage/definition-import', {**payload, 'token': preview['token']}, status=status)

    def rows(self, name='meters'):
        return self.call('GET', f'/api/manage/tables/{name}/rows')['records']

    def assert_files(self, name):
        path = self.root / 'def/table' / name
        document = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        module = runpy.run_path(str(path.with_suffix('.py')))
        self.assertEqual(module['TABLE_DEFINITION'], document)
        self.assertEqual(module['Row'].__tablename__, name)
        self.assertEqual([field.metadata['eng_name'] for field in module['Row'].__dataclass_fields__.values()
                          if 'eng_name' in field.metadata], [col['eng_name'] for col in document['columns']])
        return document

    def test_efile_empty_labels_default_to_english_without_changing_data(self):
        source = r'''<label_defaults name=' null '>
@ id blank spaces uppercase marker voltage omitted
// null '' '  ' NULL \N 电压
# 1 null text 2 3 4.5 last
</label_defaults>'''
        payload, preview = self.preview(source)
        definition = preview['tables'][0]['definition']
        self.assertEqual(definition['head']['chn_name'], 'label_defaults')
        self.assertEqual([col['chn_name'] for col in definition['columns']],
                         ['id', 'blank', 'spaces', 'uppercase', 'marker', '电压', 'omitted'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(self.assert_files('label_defaults'), definition)
        self.assertEqual(self.rows('label_defaults')[0]['row']['blank'], 'null')
        self.assertTrue(self.call('GET', '/api/manage/tables/label_defaults/check')['ok'])

    def test_dbdef_null_labels_default_to_english(self):
        source = DBDEF.replace('meters电表', 'meters null').replace('id 编号', 'id NULL').replace('value 功率', "value ''")
        payload, preview = self.preview(source, 'dbdef')
        definition = preview['tables'][0]['definition']
        self.assertEqual(definition['head']['chn_name'], 'meters')
        self.assertEqual([col['chn_name'] for col in definition['columns']], ['id', 'value'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(self.assert_files('meters'), definition)

    def test_dbdef_create_and_efile_extend_merge(self):
        payload, preview = self.preview(DBDEF, 'dbdef')
        self.assertFalse((self.root / 'def/table/meters.json').exists())
        self.assertEqual(preview['tables'][0]['definition']['head']['chn_name'], '电表')
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(self.assert_files('meters')['columns'][0]['data_type'], 'int')
        payload, preview = self.preview(EFILE)
        self.assertEqual(preview['tables'][0]['added'], ['name'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.rows()[0]['row']['name'], '北侧 电表')
        self.assertEqual(self.rows()[2]['row']['id'], '9223372036854775807')
        payload, preview = self.preview('<meters>\n@ id value\n# 1 99\n</meters>')
        self.assertEqual(preview['tables'][0]['updated'], 1)
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(self.rows()[0]['row']['name'], '北侧 电表')
        self.assertEqual(self.rows()[0]['row']['value'], 99)
        self.assert_files('meters')

    def test_efile_preview_defaults_first_key_and_apply_confirmed_schema(self):
        payload, preview = self.preview(EFILE_CONFIRM)
        self.assertEqual(preview['tables'][0]['primary_keys'], ['code'])
        self.assertEqual([column['data_type'] for column in preview['tables'][0]['definition']['columns']],
                         ['str', 'float', 'str'])
        payload['definition_overrides'] = {
            'confirm_table': {'columns': [
                {'eng_name': 'code', 'data_type': 'str', 'primary_key': False},
                {'eng_name': 'value', 'data_type': 'float', 'primary_key': True},
                {'eng_name': 'label', 'data_type': 'str', 'primary_key': False},
            ]}
        }
        self.assertTrue(self.apply(payload, preview)['ok'])
        definition = self.assert_files('confirm_table')
        self.assertEqual([column['primary_key'] for column in definition['columns']], [False, True, False])
        self.assertEqual([row['row']['value'] for row in self.rows('confirm_table')], [1.5, 2.5])

    def test_efile_confirmed_schema_requires_a_key(self):
        payload, preview = self.preview(EFILE_CONFIRM)
        payload['definition_overrides'] = {'confirm_table': {'columns': [
            {'eng_name': 'code', 'data_type': 'str', 'primary_key': False},
            {'eng_name': 'value', 'data_type': 'float', 'primary_key': False},
            {'eng_name': 'label', 'data_type': 'str', 'primary_key': False},
        ]}}
        self.call('POST', '/api/manage/definition-import', {**payload, 'token': preview['token']}, status=422)
        self.assertFalse((self.root / 'def/table/confirm_table.json').exists())

    def test_populated_dbdef_migration_and_forbidden_type_changes(self):
        self.apply(*self.preview(EFILE))
        payload, preview = self.preview(DBDEF, 'dbdef')
        self.assertEqual(preview['tables'][0]['removed'], ['name'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertNotIn('name', self.rows()[0]['row'])
        before = self.assert_files('meters')
        self.call('POST', '/api/manage/definition-preview', {'format': 'dbdef', 'text': DBDEF.replace('DOUBLE', 'LONG')}, status=409)
        self.assertEqual(self.assert_files('meters'), before)
        self.assertEqual(self.rows()[0]['row']['value'], 12.5)
        self.call('POST', '/api/manage/definition-preview', {'format': 'dbdef', 'text': DBDEF.replace('LONG', 'DOUBLE')}, status=409)

    def test_populated_dbdef_add_drop_and_schema_guards(self):
        self.apply(*self.preview(EFILE))
        extended = DBDEF.replace('</meters>', "# 3 extra 备注 VARCHAR 32 0 文本 ''\n</meters>")
        payload, preview = self.preview(extended, 'dbdef')
        self.assertEqual(preview['tables'][0]['added'], ['extra'])
        self.assertEqual(preview['tables'][0]['removed'], ['name'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual([item['row']['extra'] for item in self.rows()], [None] * 3)
        before, rows = self.assert_files('meters'), self.rows()
        for text in [
            extended.replace('LONG 8 1', 'LONG 8 0').replace('DOUBLE 8 0', 'DOUBLE 8 1'),
            extended.replace('VARCHAR 32 0', 'VARCHAR 32 1'),
            extended.replace("# 1 id 编号 LONG 8 1 数字 ''\n", '').replace("# 3 extra", "# 1 id 编号 LONG 8 1 数字 ''\n# 3 extra"),
        ]:
            with self.subTest(text=text):
                self.call('POST', '/api/manage/definition-preview', dict(format='dbdef', text=text), status=409)
        self.assertEqual(self.assert_files('meters'), before)
        self.assertEqual(self.rows(), rows)
        # Adding an E-file field also preserves unspecified existing field values.
        self.assertTrue(self.apply(*self.preview('<meters>\n@ id added\n# 1 7.5\n</meters>'))['ok'])
        self.assertEqual(self.rows()[0]['row']['added'], 7.5)
        self.assertEqual(self.rows()[0]['row']['value'], 12.5)
        self.assert_files('meters')

    def test_encoding_multiline_empty_and_composite_key(self):
        text = "<readings>\n@ site seq value\n// 站点 序号 数值\n# '北侧\n测点' 01 2\n# 南侧 01 3\n</readings>"
        payload = dict(format='e', content=base64.b64encode(text.encode('gb18030')).decode(), keys='site,seq')
        preview = self.call('POST', '/api/manage/definition-preview', payload)
        self.assertEqual(preview['encoding'], 'gb18030')
        self.assertEqual(preview['tables'][0]['primary_keys'], ['site', 'seq'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(self.rows('readings')[0]['row']['seq'], '01')
        self.assert_files('readings')
        payload = dict(format='dbdef', content=base64.b64encode(DBDEF.encode('utf-16')).decode())
        preview = self.call('POST', '/api/manage/definition-preview', payload)
        self.assertEqual(preview['encoding'], 'utf-16')

    def test_stale_preview_and_all_table_prevalidation(self):
        payload, preview = self.preview(EFILE)
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.apply(payload, preview, status=409)
        payload, preview = self.preview(EFILE)
        with SQLiteDatabase(self.root / 'sqlite_dat', schema_directory=self.root / 'def/table') as db:
            db['meters'].update_by_pk(1, {'value': 44.0})
        self.apply(payload, preview, status=409)
        self.assertEqual(self.rows()[0]['row']['value'], 44)
        invalid = EFILE.replace('meters', 'newmeters') + '<broken>\n@ id\n# 1\n# \\N\n</broken>'
        self.call('POST', '/api/manage/definition-preview', dict(format='e', text=invalid), status=422)
        self.assertFalse((self.root / 'def/table/newmeters.json').exists())

    def test_append_replace_and_invalid_format(self):
        self.apply(*self.preview(EFILE))
        before = self.rows()
        payload, preview = self.preview(EFILE, mode='append')
        self.assertEqual(preview['tables'][0]['skipped'], 3)
        result = self.apply(payload, preview)
        self.assertTrue(result['ok'])
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(self.rows(), before)
        payload, preview = self.preview('<meters>\n@ id value\n# 7 22\n</meters>', mode='replace')
        self.assertTrue(preview['tables'][0]['warnings'])
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['row']['id'], 7)
        for text in ('<bad>\n@ id value\n# 1\n</bad>', '<../bad>\n@ id\n# 1\n</../bad>'):
            self.call('POST', '/api/manage/definition-preview', dict(format='e', text=text), status=422 if '../' not in text else 400)

    def test_duplicate_keys_warn_and_continue_for_every_mode_and_table(self):
        for mode in ('append', 'merge', 'replace'):
            for existing in (False, True):
                name = f'conflicts_{mode}_{int(existing)}'
                with self.subTest(mode=mode, existing=existing):
                    if existing:
                        self.apply(*self.preview(f'<{name}>\n@ id value\n# 1744819200 5\n# 9 90\n</{name}>'))
                    text = (f'<{name}>\n@ id value\n# 1744819200 10\n# 1744819200 20\n'
                            f'# 2 30\n# 2 40\n</{name}>\n<{name}_next>\n@ id\n# 1\n</{name}_next>')
                    payload, preview = self.preview(text, mode=mode)
                    summary = preview['tables'][0]
                    skipped = (3 if existing else 2) if mode == 'append' else 0
                    self.assertEqual((summary['duplicates'], summary['skipped']), (2, skipped))
                    self.assertTrue(any('文件内主键重复 2 条' in warning for warning in summary['warnings']))
                    result = self.apply(payload, preview)
                    self.assertTrue(result['ok'])
                    self.assertEqual((result['duplicates'], result['skipped']), (2, skipped))
                    self.assertEqual(result['results'][0]['warnings'], summary['warnings'])
                    rows = {item['row']['id']: item['row']['value'] for item in self.rows(name)}
                    expected = {1744819200: (5 if existing else 10), 2: 30} if mode == 'append' else {1744819200: 20, 2: 40}
                    if existing and mode != 'replace':
                        expected[9] = 90
                    self.assertEqual(rows, expected)
                    self.assertEqual(len(self.rows(name + '_next')), 1)
                    self.assert_files(name)
                    self.assertTrue(self.call('GET', f'/api/manage/tables/{name}/check')['ok'])
                    logs = self.call('GET', '/api/manage/logs?limit=500')['logs']
                    audit = next(item for item in logs if item['operation'] == '加载定义文件并更新结构/数据')
                    self.assertEqual(audit['status'], 'success')
                    self.assertEqual((audit['result']['duplicates'], audit['result']['skipped']), (2, skipped))
                    self.assertTrue(any('文件内主键重复 2 条' in warning for warning in audit['result']['warnings']))

    def test_duplicate_preview_allows_confirming_composite_keys(self):
        text = '<series>\n@ time unit value\n# 1744819200 A 10\n# 1744819200 B 20\n</series>'
        payload, preview = self.preview(text, mode='append')
        self.assertEqual(preview['tables'][0]['duplicates'], 1)
        self.assertFalse((self.root / 'def/table/series.json').exists())
        columns = copy.deepcopy(preview['tables'][0]['definition']['columns'])
        columns[1]['primary_key'] = True
        payload['definition_overrides'] = {'series': {'columns': columns}}
        result = self.apply(payload, preview)
        self.assertTrue(result['ok'])
        self.assertEqual((result['duplicates'], result['skipped']), (0, 0))
        self.assertEqual([row['row']['unit'] for row in self.rows('series')], ['A', 'B'])
        self.assertEqual([col['eng_name'] for col in self.assert_files('series')['columns'] if col['primary_key']], ['time', 'unit'])
        payload, preview = self.preview(text.replace('# 1744819200 B 20', '# 1744819200 A 99\n# 1744819200 B 20'), mode='merge')
        self.assertEqual(preview['tables'][0]['duplicates'], 1)
        self.assertTrue(self.apply(payload, preview)['ok'])
        self.assertEqual([row['row']['value'] for row in self.rows('series')], [99, 20])

    def test_confirmed_type_conversion_duplicates_warn_but_invalid_values_fail(self):
        payload, preview = self.preview('<codes>\n@ code value\n# 01 10\n# 1 20\n</codes>')
        columns = copy.deepcopy(preview['tables'][0]['definition']['columns'])
        columns[0]['data_type'] = 'int'
        payload['definition_overrides'] = {'codes': {'columns': columns}}
        result = self.apply(payload, preview)
        self.assertTrue(result['ok'])
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(self.rows('codes')[0]['row'], {'code': 1, 'value': 20})
        before = self.rows('codes')
        for line in ('# 1 invalid', '# \\N 20', '# 9223372036854775808 20'):
            self.call('POST', '/api/manage/definition-preview', {
                'format': 'e', 'mode': 'append', 'text': f'<codes>\n@ code value\n# 1 10\n{line}\n</codes>'}, status=422)
            self.assertEqual(self.rows('codes'), before)

    def test_file_write_recovery_and_manual_schema_python_sync(self):
        payload, preview = self.preview(DBDEF, 'dbdef')
        with patch('web.definition_import.atomic_text', side_effect=OSError('simulated file failure')):
            result = self.apply(payload, preview)
        self.assertFalse(result['ok'])
        self.assert_files('meters')
        self.assertFalse((self.root / 'def/table/.sqlite-web-pending.json').exists())
        schema = self.call('GET', '/api/manage/tables/meters/schema')
        document = copy.deepcopy(schema['definition'])
        document['head']['chn_name'] = '新名称'
        self.call('PUT', '/api/manage/tables/meters/schema', dict(definition=document, version=schema['version']))
        self.assertEqual(self.assert_files('meters')['head']['chn_name'], '新名称')

    def test_existing_object_fields_survive_merge(self):
        self.insert([record(1)])
        text = '<devices>\n@ site id value\n# A 1 5.5\n</devices>'
        self.assertTrue(self.apply(*self.preview(text))['ok'])
        self.assertEqual(self.rows('devices')[0]['row']['name'], '设备1')
        self.assertTrue(self.rows('devices')[0]['row']['enabled'])

    def test_sql_failure_rolls_back_structure_and_data(self):
        self.apply(*self.preview(EFILE))
        before = self.rows()
        definition = self.assert_files('meters')
        original = DefinitionImporter._apply_table
        def fail_after_drop(importer, plan):
            plan['ddl'].append('INVALID SQL')
            return original(importer, plan)
        payload, preview = self.preview(DBDEF, 'dbdef')
        with patch.object(DefinitionImporter, '_apply_table', fail_after_drop):
            result = self.apply(payload, preview)
        self.assertFalse(result['ok'])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.assert_files('meters'), definition)
        payload, preview = self.preview(DBDEF.replace('meters', 'newmeters'), 'dbdef')
        with patch.object(DefinitionImporter, '_apply_table', fail_after_drop):
            self.assertFalse(self.apply(payload, preview)['ok'])
        self.assertFalse((self.root / 'sqlite_dat/newmeters.sqlite3').exists())
        self.assertFalse((self.root / 'def/table/newmeters.json').exists())
        self.assertTrue(self.apply(payload, preview)['ok'])

    def test_legacy_unsigned_glued_column_and_compact_header(self):
        text = '''<table_info>
@ flags 状态表 public 100 1 0 public
</table_info>
<flags>
@ column_name_eng column_name_chn data_type is_key display_type
# id 编号 INT 1 数字
# receive_own_messages接收自身报文 UCHAR 0 单选菜单
</flags>'''
        payload, preview = self.preview(text, 'dbdef')
        self.assertTrue(self.apply(payload, preview)['ok'])
        definition = self.assert_files('flags')
        self.assertEqual(definition['columns'][1]['eng_name'], 'receive_own_messages')
        self.assertEqual(definition['columns'][1]['data_type'], 'int')
        self.call('POST', '/api/manage/definition-preview', dict(format='dbdef', text=text+'\n# orphan field'), status=422)

    def test_legacy_dbdef_types_collapse_to_sqlite_types(self):
        aliases = {
            'UCHAR': 'int', 'SHORT': 'int', 'INT': 'int', 'LONG': 'int', 'DATETIME': 'int',
            'CHAR': 'str', 'BINARY': 'str', 'FLOAT': 'float', 'DOUBLE': 'float',
            '3': 'int', '4': 'int', '5': 'int', '8': 'int', '15': 'int',
            '1': 'str', '2': 'str', '10': 'str', '6': 'float', '7': 'float',
        }
        for raw, expected in aliases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_data_type(raw), expected)

        fields = list(aliases)
        rows = ['# 1 id 编号 INT 8 1 数字 \'\'']
        for index, raw in enumerate(fields[1:], 2):
            rows.append(f"# {index} field_{index} 字段{index} {raw} 8 0 文本 ''")
        text = '''<table_info>
@ table_id table_name_eng table_name_chn app_type max_record_num
# 1 collapsed 归一化 public 100
</table_info>
<collapsed>
@ column_id column_name_eng column_name_chn data_type data_length is_key display_type menu_name
''' + '\n'.join(rows) + '''
</collapsed>'''
        payload, preview = self.preview(text, 'dbdef')
        self.assertTrue(self.apply(payload, preview)['ok'])
        definition = self.assert_files('collapsed')
        self.assertEqual([column['data_type'] for column in definition['columns']],
                         [aliases[raw] for raw in fields])

    def test_old_external_connections_reject_stale_layout(self):
        self.apply(*self.preview(DBDEF, 'dbdef'))
        with SQLiteDatabase(self.root / 'sqlite_dat', schema_directory=self.root / 'def/table') as db:
            old_table = db['meters']
            self.assertTrue(self.apply(*self.preview(EFILE))['ok'])
            with self.assertRaises(SchemaError):
                old_table.update_by_pk(1, {'value': 999})
            with self.assertRaises(SchemaError):
                old_table.select()
            with self.assertRaises(SchemaError):
                old_table.count({'value': 12.5})
        self.assertEqual(self.rows()[0]['row']['value'], 12.5)


if __name__ == '__main__':
    unittest.main()
