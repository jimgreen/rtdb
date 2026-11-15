"""Measure the cost of durable receipts relative to the existing audited write path."""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web.sqlite_service import SQLiteManager
from web.management import SQLiteManagement
from web import write_receipts


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rtdb-receipts-') as directory:
        root=Path(directory);manager=SQLiteManager(root/'data',root/'defs')
        try:
            columns=[dict(eng_name=n,chn_name=n,data_type=t,primary_key=n=='id',disp_type='文本',disp_menu='') for n,t in [('id','int'),('name','str')]]
            manager.create_database_file({'name':'lab','tables':[{'head':{'eng_name':'samples','chn_name':'样本'},'columns':columns}]})
            api=SQLiteManagement(manager.for_database('lab.sqlite3'));api.manager.insert('samples',{'rows':[{'id':i,'name':'base'} for i in range(1,501)]})
            results=[];durable_write=write_receipts.write
            for size in (1,100,500):
                samples={'previous':[],'receipts':[]}
                for iteration in range(-2,15):
                    for mode in (('previous','receipts') if iteration%2 else ('receipts','previous')):
                        records=api.rows('samples',{'limit':500})['records'][:size]
                        value=f'{size}-{iteration}-{mode}'
                        payload={'changes':[{'ref':row['ref'],'values':{'name':value}} for row in records]}
                        with patch('web.write_receipts.write',durable_write if mode=='receipts' else lambda table,callback:table._write(callback)):
                            started=time.perf_counter();result=api.dispatch('PUT',['tables','samples','cell-edits'],{},payload);elapsed=(time.perf_counter()-started)*1000
                        assert result['updated']==size and 'audit_warning' not in result
                        assert all(r['row']['name']==value for r in api.rows('samples',{'limit':500})['records'][:size])
                        if iteration>=0:samples[mode].append(elapsed)
                def summarize(values):return {'median_ms':round(statistics.median(values),3),'p95_ms':round(sorted(values)[math.ceil(len(values)*.95)-1],3)}
                results.append({'rows':size,'timings':{k:summarize(v) for k,v in samples.items()},'samples_ms':samples})
            args.output.write_text(json.dumps({'repeats':15,'warmups':2,'scope':'management dispatch including validation, data commit and audit; excludes HTTP and task ledger; previous mode bypasses receipt wrapper only','results':results},indent=2),encoding='utf-8')
            print(json.dumps([{k:v for k,v in row.items() if k!='samples_ms'} for row in results]))
        finally:manager.close()


if __name__=='__main__':main()
