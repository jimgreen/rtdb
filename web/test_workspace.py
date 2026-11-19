"""Workspace directory routing uses isolated SQLite files and real HTTP."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core import SQLiteDatabase
from web.server import create_server
from web.sqlite_service import atomic_json
from web.test_sqlite_multi_database import table_definition


class WorkspaceWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtdb-workspace-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / 'workspace'
        for project in ('qinling', 'another'):
            data = self.workspace / project / 'sqldb' / 'model'
            schema = root / ('schema-' + project)
            schema.mkdir()
            atomic_json(schema / 'devices.json', {
                'database': {'eng_name': 'devices', 'chn_name': project},
                'tables': [table_definition('nodes', '节点', 'id')],
            })
            with SQLiteDatabase(data, schema_directory=schema) as db:
                db['nodes'].insert_one({'id': 1, 'name': project})
        self.server = create_server('127.0.0.1', 0, root=root / 'rtdb',
                                    workspace_dir='../workspace')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def call(self, route, *, workspace=None, database=None, method='GET', body=None, status=200, **params):
        if workspace is not None:
            params['workspace'] = workspace
        if database:
            params['database'] = database
        request = Request(self.base + '/api/manage/' + route + '?' + urlencode(params),
                          method=method, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            result = json.load(response)
            expected_statuses = (status,) if isinstance(status, int) else status
            self.assertIn(response.status, expected_statuses, result)
            return result

    def test_catalog_and_scoped_reads_without_copying(self):
        listing = self.call('workspaces')
        self.assertEqual(Path(listing['workspace_root']), self.workspace)
        self.assertEqual({item['path'] for item in listing['directories']},
                         {'.', 'qinling', 'qinling/sqldb', 'qinling/sqldb/model',
                          'another', 'another/sqldb', 'another/sqldb/model'})
        counts = {item['path']: item['database_count'] for item in listing['directories']}
        self.assertEqual(counts['.'], 0)
        self.assertEqual(counts['qinling/sqldb'], 0)
        self.assertEqual(counts['qinling/sqldb/model'], 1)
        self.assertFalse((self.workspace / 'qinling/sqldb/model/.rtdb-schema').exists())
        for project in ('qinling', 'another', 'qinling'):
            workspace = project + '/sqldb/model'
            info = self.call('info', workspace=workspace)
            self.assertEqual(Path(info['data_dir']), self.workspace / workspace)
            self.assertEqual(len(self.call('databases', workspace=workspace)['databases']), 1)
            result = self.call('database-files/select', workspace=workspace, method='POST',
                               body={'filename': 'devices.sqlite3'})
            self.assertTrue(result['selected'])
            rows = self.call('tables/nodes/rows', workspace=workspace, database='devices.sqlite3')
            self.assertEqual(rows['records'][0]['row']['name'], project)
        self.assertEqual(self.call('databases')['databases'], [])
        self.assertFalse((self.server.sqlite_manager.data_dir / 'devices.sqlite3').exists())

    def test_empty_nested_directories_can_be_listed_opened_and_used(self):
        relative = 'qinling/sqldb/agc/空目录'
        target = self.workspace / relative
        target.mkdir(parents=True)
        listing = self.call('workspaces')
        item = next(item for item in listing['directories'] if item['path'] == relative)
        self.assertEqual((item['database_count'], item['files']), (0, []))
        self.assertEqual(list(target.iterdir()), [])  # Listing does not create Web state.
        self.assertEqual(self.call('database-files', workspace=relative)['files'], [])
        result = self.call('database-files/create', workspace=relative, method='POST',
                           body={'name': 'empty_project', 'tables': []})
        self.assertTrue(result['created'])
        self.assertTrue((target / 'empty_project.sqlite3').is_file())
        self.assertEqual(self.call('databases', workspace=relative)['databases'][0]['filename'],
                         'empty_project.sqlite3')
        self.assertEqual(Path(self.call('info', workspace='.')['data_dir']), self.workspace)

    def test_background_tasks_and_writes_are_directory_scoped(self):
        workspace = 'qinling/sqldb/model'
        request_id = 'a' * 32
        task_route = 'tasks/' + request_id
        result = self.call('tables/nodes/sql', workspace=workspace, database='devices.sqlite3',
                           method='POST', body={'sql': "UPDATE nodes SET name='updated' WHERE id=1", 'confirm': 'nodes'},
                           background='1', request_id=request_id, status=(200, 202))
        if 'task' in result:
            task = result['task']
            for _ in range(5):
                if task['status'] not in ('queued', 'running'):
                    break
                task = self.call(task_route, workspace=workspace, database='devices.sqlite3',
                                 wait=1, revision=task['revision'])
            self.assertEqual(task['status'], 'done', task)
            result = task['result']
        self.assertEqual(result['rowcount'], 1)
        task = self.call(task_route, workspace=workspace, database='devices.sqlite3')
        self.assertEqual(task['status'], 'done')
        self.call(task_route, workspace='another/sqldb/model', database='devices.sqlite3', status=404)
        rows = self.call('tables/nodes/rows', workspace='another/sqldb/model', database='devices.sqlite3')
        self.assertEqual(rows['records'][0]['row']['name'], 'another')
        with SQLiteDatabase(self.workspace / workspace, database_file='devices.sqlite3') as db:
            self.assertEqual(db['nodes'].get_by_pk(1)['name'], 'updated')

    def test_rejects_absolute_traversal_and_missing_directories(self):
        for path in ('../rtdb', '/tmp', 'C:/Windows', 'qinling/../another', 'qinling\\sqldb\\model'):
            with self.subTest(path=path):
                self.call('info', workspace=path, status=400)
        self.call('info', workspace='missing', status=404)
        self.assertFalse((self.workspace / 'missing').exists())

    def test_symlink_outside_workspace_is_not_listed_or_opened(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        link = self.workspace / 'escape'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlinks unavailable')
        self.call('info', workspace='escape', status=400)
        self.assertNotIn('escape', {item['path'] for item in self.call('workspaces')['directories']})


if __name__ == '__main__':
    unittest.main()
