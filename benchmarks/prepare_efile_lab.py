"""Create only isolated E import browser fixtures."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager

root=Path('output/playwright/workflow-lab');name='e_ui_'+str(int(time.time()))
manager=SQLiteManager(root/'sqlite_dat',root/'def/table')
try:manager.create_database_file({'name':name,'tables':[]})
finally:manager.close()
path=Path('output/playwright/multi-large.e')
with path.open('w',encoding='utf-8',newline='') as stream:
    for table in ('meters','branches'):
        stream.write(f'<{table}>\n@ code id value name\n// 编码 编号 功率 名称\n')
        for i in range(6000):
            value=str(i) if i<5999 else '1.5'
            stream.write(f"# K{i} {i} {value} '测点{'x'*320}'\n")
        stream.write(f'</{table}>\n')
info=dict(database=name+'.db',file=str(path.resolve()),rows_per_table=6000,bytes=path.stat().st_size)
Path('output/playwright/efile-lab.json').write_text(json.dumps(info),encoding='utf-8');print(json.dumps(info))
