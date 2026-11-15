"""Bulk edit validation uses bounded indexed reads in the same transaction."""
import copy
import unittest
from unittest.mock import patch

from core.sqlite_db import SQLiteTable
from web import test_database_scope
from web.errors import APIError
from web.management import SQLiteManagement
from web.test_sqlite_server import definition, record


class BatchEditQueryTests(unittest.TestCase):
    setUp = test_database_scope.DatabaseScopeTests.setUp
    tearDown = test_database_scope.DatabaseScopeTests.tearDown

    def prepare(self):
        scoped = self.manager.for_database('plant_a.sqlite3')
        scoped.save_schema('devices', {'definition': definition()}, creating=True)
        scoped.insert('devices', {'rows': [record(i) for i in range(500)]})
        return scoped, SQLiteManagement(scoped)

    def test_composite_500_updates_use_bounded_reads(self):
        scoped, api = self.prepare()
        rows = api.rows('devices', {'limit': 500})['records']
        changes = [{'ref': r['ref'], 'values': {'value': 9.5}} for r in rows]
        selects = []
        original = SQLiteTable._connection
        def traced(table):
            connection = original(table)
            connection.set_trace_callback(lambda sql: selects.append(sql) if
                sql.startswith('SELECT ') and 'FROM "rtdb_data_devices"' in sql else None)
            return connection
        with patch.object(SQLiteTable, '_connection', traced):
            self.assertEqual(api.cell_edits('devices', {'changes': changes}), {'updated': 500})
        self.assertLessEqual(len(selects), 2)  # <=900 parameters per indexed read, composite key width=2.
        self.assertTrue(all(r['row']['value'] == 9.5 for r in api.rows('devices', {'limit': 500})['records']))

    def test_stale_member_rolls_back_all_and_returns_conflicts(self):
        scoped, api = self.prepare()
        rows = api.rows('devices', {'limit': 500})['records']
        changes = [{'ref': r['ref'], 'values': {'value': 9.5}} for r in rows]
        with scoped.session('devices') as table: table.update_by_pk(('A', 499), {'value': 99})
        with self.assertRaises(APIError) as caught: api.cell_edits('devices', {'changes': changes})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.details['conflicts'][0]['key'], ['A', 499])
        self.assertEqual(api.rows('devices', {'limit': 1})['records'][0]['row']['value'], 0)

    def test_duplicate_new_keys_and_mixed_save_remain_atomic(self):
        scoped, api = self.prepare()
        first = api.rows('devices', {'limit': 1})['records'][0]
        payload = {'changes': [{'ref': first['ref'], 'values': {'value': 9.5}}],
                   'inserts': [record(500), record(500), record(499)]}
        with self.assertRaises(APIError) as caught: api.cell_edits('devices', payload)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual([issue['draft_index'] for issue in caught.exception.details['issues']], [1, 2])
        self.assertEqual(api.rows('devices', {})['total'], 500)
        self.assertEqual(api.rows('devices', {'limit': 1})['records'][0]['row']['value'], 0)


if __name__ == '__main__': unittest.main()
