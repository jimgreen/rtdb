import unittest
from unittest.mock import patch

from web import test_database_scope
from web.management import SQLiteManagement
from web.errors import APIError
from web.sqlite_service import fingerprint


class PagingRecoveryTests(unittest.TestCase):
    setUp=test_database_scope.DatabaseScopeTests.setUp
    tearDown=test_database_scope.DatabaseScopeTests.tearDown

    def prepare(self):
        self.scoped=self.manager.for_database('plant_a.sqlite3');self.api=SQLiteManagement(self.scoped)
        self.scoped.insert('nodes',{'rows':[{'id':i,'name':str(i%3)} for i in range(1,301)]})
        return {'sql':'SELECT * FROM nodes','compact':True,'limit':50,'offset':100}

    def test_next_and_previous_equal_offset_pages(self):
        query=self.prepare();first=self.api.sql('nodes',query)
        following=self.api.sql('nodes',{**query,'offset':150,'page_cursor':first['next_cursor']})
        expected=self.api.sql('nodes',{**query,'offset':150})
        self.assertEqual(following['records'],expected['records']);self.assertEqual(following['pagination_mode'],'cursor')
        previous=self.api.sql('nodes',{**query,'page_cursor':following['previous_cursor']})
        self.assertEqual(previous['records'],first['records']);self.assertEqual(previous['pagination_mode'],'cursor')

    def test_descending_primary_key(self):
        query={**self.prepare(),'order':{'column':'id','direction':'DESC'}}
        first=self.api.sql('nodes',query)
        following=self.api.sql('nodes',{**query,'offset':150,'page_cursor':first['next_cursor']})
        self.assertEqual(following['records'],self.api.sql('nodes',{**query,'offset':150})['records'])
        self.assertEqual(following['pagination_mode'],'cursor')

    def test_non_key_sort_and_general_sql_fall_back(self):
        query=self.prepare()
        for extra in ({'order':{'column':'name','direction':'ASC'}},{'sql':'SELECT id,name FROM nodes WHERE id>3'}):
            result=self.api.sql('nodes',{**query,**extra})
            self.assertNotIn('next_cursor',result);self.assertEqual(result['pagination_mode'],'offset')

    def test_external_write_invalidates_old_anchor(self):
        query=self.prepare();first=self.api.sql('nodes',query)
        with self.scoped._database() as db:db['nodes'].kv.delete(1)
        result=self.api.sql('nodes',{**query,'offset':150,'page_cursor':first['next_cursor']})
        self.assertEqual(result['pagination_mode'],'offset')
        self.assertEqual(result['records'][0]['row']['id'],152)

    def test_commit_racing_cursor_query_reexecutes_offset(self):
        query=self.prepare();first=self.api.sql('nodes',query)
        original=self.api._sql;calls=[]
        def race(*args,**kwargs):
            result=original(*args,**kwargs);calls.append(result['pagination_mode'])
            if len(calls)==1:
                with self.scoped._database() as db:db['nodes'].kv.delete(1)
            return result
        with patch.object(self.api,'_sql',side_effect=race):
            result=self.api.sql('nodes',{**query,'offset':150,'page_cursor':first['next_cursor']})
        self.assertEqual(calls,['cursor','offset']);self.assertEqual(result['records'][0]['row']['id'],152)

    def test_invalid_token_or_mismatched_offset_cannot_change_results(self):
        query=self.prepare();first=self.api.sql('nodes',query)
        for cursor in ('invalid',first['next_cursor']):
            result=self.api.sql('nodes',{**query,'offset':200,'page_cursor':cursor})
            self.assertEqual(result['pagination_mode'],'offset');self.assertEqual(result['records'][0]['row']['id'],201)

    def test_compound_primary_key_and_unicode(self):
        query=self.prepare()
        definition={'head':{'eng_name':'compound','chn_name':'联合'},'columns':[
            dict(eng_name='area',chn_name='地区',data_type='str',primary_key=True,disp_type='文本',disp_menu=''),
            dict(eng_name='id',chn_name='编号',data_type='int',primary_key=True,disp_type='整数',disp_menu='')]}
        self.scoped.save_schema('compound',{'definition':definition},creating=True)
        self.scoped.insert('compound',{'rows':[{'area':area,'id':i} for area in ['甲','乙'] for i in range(90)]})
        query={'sql':'SELECT * FROM compound','compact':True,'offset':0,'limit':50}
        first=self.api.sql('compound',query)
        result=self.api.sql('compound',{**query,'offset':50,'page_cursor':first['next_cursor']})
        self.assertEqual(result['pagination_mode'],'cursor')
        self.assertEqual(result['records'],self.api.sql('compound',{**query,'offset':50})['records'])

    def test_phase_timings_are_current_and_cache_does_not_reuse_old_costs(self):
        query=self.prepare();first=self.api.sql('nodes',query)
        for field in ('open_ms','query_ms','decode_ms','version_ms'):self.assertGreaterEqual(first['timings'][field],0)
        cached=self.api.sql('nodes',{**query,'if_version':first['result_version']})
        self.assertTrue(cached['cache_hit']);self.assertEqual(cached['timings']['query_ms'],0)

    def test_resolve_records_returns_current_versions_and_missing_slots(self):
        self.prepare();version=fingerprint(self.scoped.definition('nodes').to_mapping())
        result=self.api.resolve_records('nodes',{'schema_version':version,'keys':[[5],[999],[1]]})
        self.assertEqual(result['records'][0]['row']['id'],5);self.assertIsNone(result['records'][1])
        self.assertEqual(result['records'][2]['row']['id'],1)
        self.assertEqual(result['records'][0]['ref']['version'],fingerprint(result['records'][0]['row']))

    def test_resolve_rejects_stale_schema_wrong_keys_and_excessive_size(self):
        self.prepare();version=fingerprint(self.scoped.definition('nodes').to_mapping())
        for payload in ({'schema_version':'stale','keys':[[1]]},{'schema_version':version,'keys':[[1,2]]},
                        {'schema_version':version,'keys':[[1]]*501}):
            with self.assertRaises(APIError):self.api.resolve_records('nodes',payload)
