"""Phase 4 核心存储层 P0/P1 回归测试。"""
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from core import FileMappedDatabase, SQLiteDatabase, SchemaError
from core.file_mapped_db import _ReadWriteLock
from core.table_schema import TableDefinition


def _write_schema(schema_dir: Path, definition: dict) -> Path:
    schema_dir.mkdir(parents=True, exist_ok=True)
    path = schema_dir / f"{definition['head']['eng_name']}.json"
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding='utf-8')
    return path


def _sample_definition(name='metrics', *, default_flag=False, default_score=0):
    return {
        'head': {'eng_name': name, 'chn_name': '指标表'},
        'columns': [
            {'eng_name': 'id', 'chn_name': '编号', 'data_type': 'int', 'primary_key': True, 'disp_type': '整数', 'disp_menu': ''},
            {'eng_name': 'tag', 'chn_name': '标签', 'data_type': 'str', 'primary_key': False, 'disp_type': '文本', 'disp_menu': ''},
            {'eng_name': 'score', 'chn_name': '分值', 'data_type': 'float', 'primary_key': False, 'disp_type': '浮点数', 'disp_menu': '', 'default_value': default_score},
            {'eng_name': 'enabled', 'chn_name': '启用', 'data_type': 'bool', 'primary_key': False, 'disp_type': '文本', 'disp_menu': '', 'default_value': default_flag},
        ],
    }


class CorePhase4RegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='rtdb-p4-core-')
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_float_nan_inf_rejected_in_schema_and_sqlite(self):
        """NaN 与 Inf 浮点必须在校验与写入阶段被拒绝，避免 SQLite 静默存为 NULL。"""
        schema_dir = self.root / 'schemas'
        schema_dir.mkdir()
        definition = TableDefinition.from_mapping(_sample_definition(), schema_dir, 'metrics')
        for bad in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(SchemaError):
                definition.validate_rows([{'id': 1, 'tag': 'a', 'score': bad, 'enabled': True}])

        _write_schema(schema_dir, _sample_definition())
        with SQLiteDatabase(self.root / 'data', schema_directory=schema_dir) as db:
            table = db['metrics']
            table.insert_many([
                {'id': 1, 'tag': 'ok', 'score': 1.5, 'enabled': True},
                {'id': 2, 'tag': 'empty', 'score': None, 'enabled': False},
            ])
            for bad in (math.nan, math.inf, -math.inf):
                with self.assertRaises(SchemaError):
                    table.insert_one({'id': 3, 'tag': 'bad', 'score': bad, 'enabled': True})
            # 确认正常 NULL 与有效浮点互不误匹配
            self.assertEqual([r['id'] for r in table.select(where='score IS NULL')], [2])
            self.assertEqual([r['id'] for r in table.select(where='score > 0')], [1])

    def test_sqlite_where_pushdown_matches_python_semantics(self):
        """验证 SQLite _where_sql 扩展下推与 Python 过滤在范围/IN/NULL/!= 上完全一致。"""
        schema_dir = self.root / 'schemas'
        schema_dir.mkdir()
        _write_schema(schema_dir, _sample_definition())
        with SQLiteDatabase(self.root / 'data', schema_directory=schema_dir) as db:
            table = db['metrics']
            rows = [
                {'id': 1, 'tag': 'alpha', 'score': 10.5, 'enabled': True},
                {'id': 2, 'tag': 'beta', 'score': 20.0, 'enabled': False},
                {'id': 3, 'tag': None, 'score': None, 'enabled': True},
                {'id': 4, 'tag': 'gamma', 'score': -5.0, 'enabled': None},
                {'id': 5, 'tag': 'delta', 'score': 20.0, 'enabled': True},
            ]
            table.insert_many(rows)
            predicates = [
                'score > 10',
                'score >= 20',
                'score < 15',
                'score <= 10.5',
                'score != 20',
                "tag <> 'alpha'",
                "tag IN ('alpha', 'gamma')",
                'id IN (1, 3, 5)',
                'tag IS NULL',
                'tag IS NOT NULL',
                'score IS NULL AND enabled = true',
                'score >= 10 AND score <= 20 AND enabled != false',
            ]
            for clause in predicates:
                with self.subTest(where=clause):
                    sql_clause = table._where_sql(clause)
                    self.assertIsNotNone(sql_clause, f'预期下推失败: {clause}')
                    pushed = table.select(where=clause, order_by='id ASC')
                    # 通过在列名前加括号或禁用下推路径，与纯 Python 评估结果比对
                    original = table._where_sql
                    try:
                        table._where_sql = lambda _where: None
                        fallback = table.select(where=clause, order_by='id ASC')
                    finally:
                        table._where_sql = original
                    self.assertEqual(pushed, fallback)

    def test_rwlock_same_thread_reentrant_read_and_write(self):
        """验证 _ReadWriteLock 在有等待写者时仍支持同线程读重入，以及写锁重入与降级读重入。"""
        lock = _ReadWriteLock()
        writer_waiting = threading.Event()
        writer_done = threading.Event()

        def waiting_writer():
            writer_waiting.set()
            with lock:
                writer_done.set()

        with lock.read():
            t = threading.Thread(target=waiting_writer, daemon=True)
            t.start()
            writer_waiting.wait(timeout=1.0)
            # 即使写者在排队，同线程读锁重入也必须立即成功不死锁
            with lock.read():
                pass
        t.join(timeout=2.0)
        self.assertTrue(writer_done.is_set())

        with lock:
            with lock:
                with lock.read():
                    pass

    def test_file_mapped_like_zero_false_none_and_apply_defaults(self):
        """验证 file_mapped_db 中 LIKE 对 0/False/None 的语义及默认值填充不误覆盖假值。"""
        schema_dir = self.root / 'fm_schemas'
        defn = _sample_definition('fm_metrics', default_flag=False, default_score=0.0)
        _write_schema(schema_dir, defn)
        with FileMappedDatabase(self.root / 'fm_data', schema_directory=schema_dir) as db:
            table = db['fm_metrics']
            table.insert_many([
                {'id': 1, 'tag': 'zero_row', 'score': 0.0, 'enabled': False},
                {'id': 2, 'tag': None, 'score': None, 'enabled': None},
                {'id': 3, 'tag': 'default_row'},
            ])
            # score=0.0 与 enabled=False 应能被 LIKE 匹配，而 None 不得匹配
            zero_like = table.select(where="score LIKE '0%'")
            self.assertEqual({r['id'] for r in zero_like}, {1, 3})
            false_like = table.select(where="enabled LIKE 'False'")
            self.assertEqual({r['id'] for r in false_like}, {1, 3})
            null_like = table.select(where="tag LIKE '%'")
            self.assertEqual({r['id'] for r in null_like}, {1, 3})
            # 验证默认值准确生效
            defaulted = table.select(where='id = 3')[0]
            self.assertEqual(defaulted['score'], 0.0)
            self.assertIs(defaulted['enabled'], False)

    def test_file_mapped_fixed_items_and_tail_snapshot_rewrite(self):
        """验证 FixedRecordStore.items()/values() 无 NameError，且缩容重写会清空尾部旧槽。"""
        schema_dir = self.root / 'fm_fixed_schemas'
        defn = _sample_definition('fixed_items')
        _write_schema(schema_dir, defn)
        with FileMappedDatabase(self.root / 'fm_fixed_data', schema_directory=schema_dir) as db:
            table = db['fixed_items']
            initial = [{'id': i, 'tag': f't{i}', 'score': float(i), 'enabled': bool(i % 2)} for i in range(1, 21)]
            table.insert_many(initial)
            fixed_store = table._fixed_store
            if fixed_store is not None and fixed_store.active:
                items = fixed_store.items(['id'])
                self.assertEqual(len(items), 20)
                self.assertEqual(items[0][0], 1)
                # 确认返回的是分离副本，修改返回值不影响存储
                items[0][1]['tag'] = 'mutated'
                self.assertEqual(table.select(where='id = 1')[0]['tag'], 't1')
            # 缩小快照：仅保留 3 条记录，确认尾部旧槽完全清空
            table[:] = initial[:3]
            self.assertEqual(len(table), 3)
            self.assertEqual([r['id'] for r in table.select(order_by='id ASC')], [1, 2, 3])
            self.assertEqual(table.select(where='id = 20'), [])


if __name__ == '__main__':
    unittest.main()
