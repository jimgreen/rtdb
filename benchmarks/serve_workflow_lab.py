"""Isolated two-database fixture for workflow and recovery browser checks."""
import argparse
import json
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from web.sqlite_server import create_sqlite_server
from web.sqlite_service import SQLiteManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    root = repo / 'output/playwright/workflow-lab'
    manager = SQLiteManager(root / 'sqlite_dat', root / 'def/table')
    try:
        for name, initial in [('plant_a', 10), ('plant_b', 100)]:
            if (root / 'sqlite_dat' / (name + '.sqlite3')).exists():
                continue
            definitions = []
            for table in ['devices', 'samples']:
                definitions.append({'head': {'eng_name': table, 'chn_name': '设备' if table == 'devices' else '采样'}, 'columns': [
                    {'eng_name': 'id', 'chn_name': '编号', 'data_type': 'int', 'primary_key': True},
                    {'eng_name': 'name', 'chn_name': '名称', 'data_type': 'str', 'primary_key': False},
                    {'eng_name': 'value', 'chn_name': '测量值', 'data_type': 'float', 'primary_key': False},
                    {'eng_name': 'other', 'chn_name': '参考值', 'data_type': 'float', 'primary_key': False},
                    {'eng_name': 'enabled', 'chn_name': '启用', 'data_type': 'bool', 'primary_key': False}]})
            for definition in definitions:
                for column in definition['columns']:
                    column.update(disp_type='文本', disp_menu='')
            manager.create_database_file({'name': name, 'chn_name': name, 'tables': definitions})
            scoped = manager.for_database(name + '.sqlite3')
            scoped.insert('devices', {'rows': [{'id': i, 'name': name, 'value': initial+i/10,
                'other': initial+i/5, 'enabled': True} for i in range(1, 201)]})
    finally:
        manager.close()
    server = create_sqlite_server('127.0.0.1', args.port, root=root)
    info = {'pid': os.getpid(), 'url': f'http://127.0.0.1:{server.server_port}'}
    (repo / 'output/playwright/workflow-server.json').write_text(json.dumps(info), encoding='utf-8')
    print(json.dumps(info), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
