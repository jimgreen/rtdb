"""本地 E 文件、Excel (.xlsx / .xls) 与 CSV 工作会话全链路集成测试。"""
from __future__ import annotations

import base64
import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import openpyxl

from web.efile_workspace import parse_efile_document
from web.sqlite_server import create_sqlite_server
from web.xls_codec import OLE2_SIGNATURE, read_xls_sheets, write_xls_sheets


def _http_json(base_url: str, path: str, method: str = 'GET', body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = Request(
        base_url + path,
        data=data,
        method=method,
        headers={'Content-Type': 'application/json'} if data is not None else {},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode('utf-8')) if raw else {}
    except HTTPError as err:
        raw = err.read()
        return err.code, json.loads(raw.decode('utf-8')) if raw else {}


class LocalFileWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='rtdb-local-file-test-')
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / 'data'
        self.schema_dir = self.root / 'def'
        self.data_dir.mkdir(parents=True)
        self.schema_dir.mkdir(parents=True)
        self.server = create_sqlite_server(
            '127.0.0.1', 0,
            root=self.root,
            data_dir=self.data_dir,
            schema_dir=self.schema_dir,
        )
        self.port = self.server.server_address[1]
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def _q(self, session_id: str, extra: str = '') -> str:
        base = f'efile_session={quote(session_id)}&database=efile_session.db'
        return f'?{base}&{extra}' if extra else f'?{base}'

    def test_efile_gb18030_crud_and_lossless_export(self):
        """验证 GB18030 E 文件加载、无主键合成 _e_rowid、SQL 查询、增删改与无损回写。"""
        efile_text = (
            "<! System=EMS Version=1.0 Time='2026-11-27 10:00:00' !>\r\n"
            "<Substation::Base area=north>\r\n"
            "@ id code name voltage\r\n"
            "// 编号 设备编码 变电站名称 电压等级\r\n"
            "# 1 0012 \"一号 主变\" 220.5\r\n"
            "# 2 0015 二号站 \\N\r\n"
            "</Substation::Base>\r\n"
            "<Measure>\r\n"
            "@ dev value note\r\n"
            "// 设备 数值 备注\r\n"
            "# A 10.5 正常\r\n"
            "# A 12.0 \"\"\r\n"
            "</Measure>\r\n"
        )
        raw_bytes = efile_text.encode('gb18030')

        status, session = _http_json(self.base_url, '/api/manage/efile-sessions', 'POST', {
            'filename': 'grid_model.e',
            'content': base64.b64encode(raw_bytes).decode('ascii'),
            'encoding': 'auto',
        })
        self.assertEqual(status, 201)
        self.assertEqual(session['kind'], 'e')
        self.assertEqual(session['encoding'], 'gb18030')
        self.assertEqual(session['table_count'], 2)
        sid = session['id']

        # 1. SQL 查询与筛选
        status, sql_res = _http_json(
            self.base_url,
            f'/api/manage/tables/Substation/sql{self._q(sid)}',
            'POST',
            {'sql': "SELECT id, code, name, voltage FROM Substation WHERE code = '0012'"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(sql_res['total'], 1)
        self.assertEqual(sql_res['rows'][0]['code'], '0012')
        self.assertEqual(sql_res['rows'][0]['name'], '一号 主变')

        # 2. 新增记录（Substation）
        status, ins_res = _http_json(
            self.base_url,
            f'/api/manage/tables/Substation/rows{self._q(sid)}',
            'POST',
            {'rows': [{'id': 3, 'code': '0099', 'name': '三号站', 'voltage': 500.0}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(ins_res['inserted'], 1)

        # 3. 无主键表 Measure 自动注入 _e_rowid，修改单元格 + 删除行
        status, rows_res = _http_json(self.base_url, f'/api/manage/tables/Measure/rows{self._q(sid)}')
        self.assertEqual(status, 200)
        self.assertEqual(rows_res['total'], 2)
        first_rec = rows_res['records'][0]
        second_rec = rows_res['records'][1]
        self.assertIn('_e_rowid', first_rec['row'])

        status, edit_res = _http_json(
            self.base_url,
            f'/api/manage/tables/Measure/cell-edits{self._q(sid)}',
            'PUT',
            {'changes': [{'ref': first_rec['ref'], 'values': {'value': 88.5, 'note': '已校准'}}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(edit_res['updated'], 1)

        status, del_res = _http_json(
            self.base_url,
            f'/api/manage/tables/Measure/rows{self._q(sid)}',
            'DELETE',
            {'records': [second_rec['ref']]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(del_res['deleted'], 1)

        # 4. 导出 E 文件并二次解析验证无损还原
        status, exp = _http_json(
            self.base_url,
            f'/api/manage/efile-sessions/{sid}/export?format=json',
        )
        self.assertEqual(status, 200)
        self.assertEqual(exp['encoding'], 'gb18030')
        out_bytes = base64.b64decode(exp['content_base64'])
        out_text = out_bytes.decode('gb18030')
        self.assertIn("<! System=EMS Version=1.0 Time='2026-11-27 10:00:00' !>", out_text)
        self.assertIn('<Substation::Base area=north>', out_text)
        self.assertNotIn('_e_rowid', out_text)

        reparsed = parse_efile_document(out_text)
        self.assertEqual(len(reparsed['modules']), 2)
        sub_mod, meas_mod = reparsed['modules']
        self.assertEqual(len(sub_mod['rows']), 3)
        self.assertEqual(sub_mod['rows'][0][1], '0012')
        self.assertEqual(sub_mod['rows'][2][2], '三号站')
        self.assertEqual(meas_mod['columns'], ['dev', 'value', 'note'])
        self.assertEqual(len(meas_mod['rows']), 1)
        self.assertEqual(meas_mod['rows'][0], ['A', '88.5', '已校准'])

        # 5. 关闭会话并验证临时目录已清理
        sess_dir = self.data_dir / '.web-efiles' / sid
        self.assertTrue(sess_dir.is_dir())
        status, closed = _http_json(self.base_url, f'/api/manage/efile-sessions/{sid}', 'DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(closed['closed'])
        self.assertFalse(sess_dir.exists())

    def test_xlsx_multi_sheet_add_rename_delete_and_row_crud(self):
        """验证 .xlsx 多 Sheet 读取、新增 Sheet、重命名 Sheet、删除非空 Sheet 与行 CRUD。"""
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = '主变台账'
        ws1.append(['设备编号', '设备名称', '额定容量'])
        ws1.append(['T001', '1号主变', 180])
        ws1.append(['T002', '2号主变', 240])

        ws2 = wb.create_sheet('临时草稿')
        ws2.append(['说明', '分值'])
        ws2.append(['待删行', 1])
        ws2.append(['待删行', 2])

        ws3 = wb.create_sheet('空白页')
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        status, session = _http_json(self.base_url, '/api/manage/efile-sessions', 'POST', {
            'filename': '台账.xlsx',
            'content': base64.b64encode(buf.getvalue()).decode('ascii'),
        })
        self.assertEqual(status, 201)
        self.assertEqual(session['kind'], 'xlsx')
        self.assertEqual(session['table_count'], 3)
        sid = session['id']
        table_names = [t['name'] for t in session['tables']]
        chn_names = [t['chn_name'] for t in session['tables']]
        self.assertEqual(chn_names, ['主变台账', '临时草稿', '空白页'])
        t_main, t_draft = table_names[0], table_names[1]

        # 1. SQL 聚合查询
        status, sql_res = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_main}/sql{self._q(sid)}',
            'POST',
            {'sql': f'SELECT COUNT(*) AS cnt FROM {t_main}'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(sql_res['rows'][0]['cnt'], 2)

        # 2. 修改主变台账 Sheet 名（表结构 chn_name 改名）
        status, sch = _http_json(self.base_url, f'/api/manage/tables/{t_main}/schema{self._q(sid)}')
        self.assertEqual(status, 200)
        new_def = sch['definition']
        new_def['head']['chn_name'] = '主变核心台账'
        status, saved_sch = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_main}/schema{self._q(sid)}',
            'PUT',
            {'definition': new_def, 'version': sch['version']},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved_sch['definition']['head']['chn_name'], '主变核心台账')

        # 3. 删除非空 Sheet（临时草稿）
        status, draft_sch = _http_json(self.base_url, f'/api/manage/tables/{t_draft}/schema{self._q(sid)}')
        self.assertEqual(status, 200)
        status, drop_res = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_draft}/schema{self._q(sid)}',
            'DELETE',
            {'confirm': t_draft, 'version': draft_sch['version'], 'force': True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(drop_res['deleted'])

        # 4. 新增 Sheet（线路台账）并写入记录
        status, create_res = _http_json(
            self.base_url,
            f'/api/manage/tables/lines/schema{self._q(sid)}',
            'PUT',
            {
                'creating': True,
                'definition': {
                    'head': {'eng_name': 'lines', 'chn_name': '线路台账'},
                    'columns': [
                        {'eng_name': 'line_id', 'chn_name': '线路号', 'data_type': 'str', 'primary_key': True, 'disp_type': '文本'},
                        {'eng_name': 'length_km', 'chn_name': '长度km', 'data_type': 'float', 'primary_key': False, 'disp_type': '浮点数'},
                    ],
                },
            },
        )
        self.assertEqual(status, 200)
        status, ins_line = _http_json(
            self.base_url,
            f'/api/manage/tables/lines/rows{self._q(sid)}',
            'POST',
            {'rows': [{'line_id': 'L220-1', 'length_km': 42.5}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(ins_line['inserted'], 1)

        # 5. 导出回 .xlsx 并用 openpyxl 二次校验全部 Sheet 增删改结果
        status, exp = _http_json(self.base_url, f'/api/manage/efile-sessions/{sid}/export?format=json')
        self.assertEqual(status, 200)
        self.assertEqual(exp['kind'], 'xlsx')
        out_wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(exp['content_base64'])), data_only=True)
        try:
            self.assertEqual(out_wb.sheetnames, ['主变核心台账', '空白页', '线路台账'])
            main_rows = list(out_wb['主变核心台账'].iter_rows(values_only=True))
            self.assertEqual(main_rows[0], ('设备编号', '设备名称', '额定容量'))
            self.assertEqual(len(main_rows), 3)
            line_rows = list(out_wb['线路台账'].iter_rows(values_only=True))
            self.assertEqual(line_rows, [('线路号', '长度km'), ('L220-1', 42.5)])
        finally:
            out_wb.close()

    def test_xls_biff8_multi_sheet_roundtrip_and_crud(self):
        """验证纯 Python OLE2 + BIFF8 .xls 多 Sheet 读写、新增 Sheet 与删改回写。"""
        xls_bytes = write_xls_sheets([
            {'name': '厂站表', 'rows': [['编号', '名称', '启用'], [1, '北京站', True], [2, '上海站', False]]},
            {'name': '量测点', 'rows': [['测点', '数值'], ['P1', 12.5], ['P1', 15.25]]},
        ])
        self.assertTrue(xls_bytes.startswith(OLE2_SIGNATURE))

        status, session = _http_json(self.base_url, '/api/manage/efile-sessions', 'POST', {
            'filename': 'legacy.xls',
            'content': base64.b64encode(xls_bytes).decode('ascii'),
        })
        self.assertEqual(status, 201)
        self.assertEqual(session['kind'], 'xls')
        self.assertEqual(session['table_count'], 2)
        sid = session['id']
        t_station, t_meas = [t['name'] for t in session['tables']]

        # 新增记录到 厂站表
        status, sch = _http_json(self.base_url, f'/api/manage/tables/{t_station}/schema{self._q(sid)}')
        col_names = [c['eng_name'] for c in sch['definition']['columns']]
        status, ins = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_station}/rows{self._q(sid)}',
            'POST',
            {'rows': [{col_names[0]: 3, col_names[1]: '广州站', col_names[2]: True}]},
        )
        self.assertEqual(status, 200)

        # 删除非空 Sheet 量测点
        status, m_sch = _http_json(self.base_url, f'/api/manage/tables/{t_meas}/schema{self._q(sid)}')
        status, drop = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_meas}/schema{self._q(sid)}',
            'DELETE',
            {'confirm': t_meas, 'version': m_sch['version'], 'force': True},
        )
        self.assertEqual(status, 200)

        # 导出回 .xls 并用 read_xls_sheets 验证
        status, exp = _http_json(self.base_url, f'/api/manage/efile-sessions/{sid}/export?format=json')
        self.assertEqual(status, 200)
        out_bytes = base64.b64decode(exp['content_base64'])
        self.assertTrue(out_bytes.startswith(OLE2_SIGNATURE))
        out_sheets = read_xls_sheets(out_bytes)
        self.assertEqual(len(out_sheets), 1)
        self.assertEqual(out_sheets[0]['name'], '厂站表')
        self.assertEqual(len(out_sheets[0]['rows']), 4)
        self.assertEqual(out_sheets[0]['rows'][3], [3, '广州站', True])

    def test_csv_gb18030_crud_and_export(self):
        """验证本地 GB18030 CSV 加载、前导零保护、无主键编辑与原编码无损回写。"""
        csv_text = '编号,名称,备注\r\n0001,一号开关,"含,逗号"\r\n0001,二号开关,正常\r\n'
        raw_bytes = csv_text.encode('gb18030')

        status, session = _http_json(self.base_url, '/api/manage/efile-sessions', 'POST', {
            'filename': '开关清单.csv',
            'content': base64.b64encode(raw_bytes).decode('ascii'),
        })
        self.assertEqual(status, 201)
        self.assertEqual(session['kind'], 'csv')
        self.assertEqual(session['encoding'], 'gb18030')
        sid = session['id']
        t_name = session['tables'][0]['name']

        # 读取行（应含合成主键 _e_rowid 且前导零保留）
        status, rows_res = _http_json(self.base_url, f'/api/manage/tables/{t_name}/rows{self._q(sid)}')
        self.assertEqual(status, 200)
        self.assertEqual(rows_res['total'], 2)
        rec0 = rows_res['records'][0]
        self.assertIn('_e_rowid', rec0['row'])
        cols = [k for k in rec0['row'] if k != '_e_rowid']
        self.assertEqual(rec0['row'][cols[0]], '0001')

        # 修改首行名称 + 新增第 3 行
        status, edit_res = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_name}/cell-edits{self._q(sid)}',
            'PUT',
            {'changes': [{'ref': rec0['ref'], 'values': {cols[1]: '一号主开关'}}]},
        )
        self.assertEqual(status, 200)
        status, ins_res = _http_json(
            self.base_url,
            f'/api/manage/tables/{t_name}/rows{self._q(sid)}',
            'POST',
            {'rows': [{'_e_rowid': 3, cols[0]: '0003', cols[1]: '三号开关', cols[2]: '新增'}]},
        )
        self.assertEqual(status, 200)

        # 导出回 CSV 验证 GB18030 编码、无 _e_rowid、前导零与逗号转义完整
        status, exp = _http_json(self.base_url, f'/api/manage/efile-sessions/{sid}/export?format=json&table={t_name}')
        self.assertEqual(status, 200)
        self.assertEqual(exp['encoding'], 'gb18030')
        out_text = base64.b64decode(exp['content_base64']).decode('gb18030')
        self.assertNotIn('_e_rowid', out_text)
        parsed_rows = list(csv.reader(io.StringIO(out_text)))
        self.assertEqual(parsed_rows[0], ['编号', '名称', '备注'])
        self.assertEqual(parsed_rows[1], ['0001', '一号主开关', '含,逗号'])
        self.assertEqual(parsed_rows[3], ['0003', '三号开关', '新增'])


if __name__ == '__main__':
    unittest.main()
