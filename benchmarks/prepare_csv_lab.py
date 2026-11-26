"""Create isolated browser CSV fixtures; no production database access."""
import csv
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager

root=Path('output/playwright/workflow-lab');name='csv_ui_'+str(int(time.time()))
manager=SQLiteManager(root/'sqlite_dat',root/'def/table')
try:
    definition=manager.for_database('plant_a.db').definition('devices').to_mapping()
    sibling={**definition,'head':{'eng_name':'samples','chn_name':'隔离对照'}}
    manager.create_database_file({'name':name,'tables':[definition,sibling]})
    row={'id':1,'name':'原始记录','value':1.5,'other':2.5,'enabled':True}
    scoped=manager.for_database(name+'.db');scoped.insert('devices',{'rows':[row]});scoped.insert('samples',{'rows':[row]})
finally:manager.close()
path=Path('output/playwright/csv-large.csv');count=18000
with path.open('w',encoding='utf-8-sig',newline='') as stream:
    writer=csv.writer(stream);writer.writerow(['id','name','value','other','enabled'])
    for i in range(count):writer.writerow([1000+i,('带逗号,换行\n名称' if i==0 else '测点')+'x'*1024,i/10,i/20,'true'])
info={'database':name+'.db','path':str(path.resolve()),'rows':count,'bytes':path.stat().st_size}
Path('output/playwright/csv-lab.json').write_text(json.dumps(info),encoding='utf-8')
print(json.dumps(info))
