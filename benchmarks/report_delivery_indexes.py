"""Generate the self-contained report for audit delivery and index configuration."""
import argparse
import base64
from datetime import datetime
from html import escape
import json
from pathlib import Path
import platform
import sqlite3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--backend-tests',type=int,required=True);parser.add_argument('--browser-checks',type=int,required=True)
    parser.add_argument('--screenshot',type=Path);args=parser.parse_args()
    result=json.loads(args.source.read_text('utf-8'))
    def row(label,a,b):
        return f'<tr><th>{escape(label)}</th><td>{a["median_ms"]:.3f}</td><td>{b["median_ms"]:.3f}</td><td>{(b["median_ms"]/a["median_ms"]-1)*100:+.1f}%</td><td>{a["p95_ms"]:.3f} → {b["p95_ms"]:.3f}</td></tr>'
    rows=''
    for item in result['http']['results']:
        a,b=item['timings']['sync'],item['timings']['background']
        rows+=row(f'{item["rows"]} 行：保存确认',a['save_ms'],b['save_ms'])
        rows+=row(f'{item["rows"]} 行：含日志同步完成',a['including_log_delivery_ms'],b['including_log_delivery_ms'])
    indexes=result['indexes'];a,b=indexes['timings']['all_columns'],indexes['timings']['selected']
    index_rows=row('500 行 × 18 字段修改',a['batch_update_ms'],b['batch_update_ms'])+row('过滤与排序查询',a['indexed_query_ms'],b['indexed_query_ms'])
    storage=indexes['storage'];old,new=storage['all_columns'],storage['selected']
    head='<thead><tr><th>场景</th><th>对照中位 / ms</th><th>本轮中位 / ms</th><th>变化</th><th>P95 / ms</th></tr></thead>'
    screenshot=''
    if args.screenshot:screenshot='<img alt="结构变更预览与索引设置验证" src="data:image/png;base64,'+base64.b64encode(args.screenshot.read_bytes()).decode()+'">'
    raw=escape(json.dumps(result,ensure_ascii=False,indent=2))
    args.output.write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB 保存与索引优化验证</title>
<style>body{{margin:0;background:#f4f7fa;color:#1c3048;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1120px;margin:auto;padding:36px 24px}}h1{{font-size:32px}}h2{{font-size:22px}}section{{background:white;border:1px solid #dbe3ed;border-radius:12px;padding:20px 26px;margin:20px 0}}table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}th,td{{padding:9px;border-bottom:1px solid #dce3ed;text-align:right}}th:first-child{{text-align:left}}thead{{background:#edf3f9}}.scroll{{overflow:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f4f7;padding:16px}}img{{max-width:100%;height:auto}}.meta{{font-size:14px;color:#5b7089}}strong{{color:#175692}}@media(max-width:600px){{main{{padding:12px}}section{{padding:14px}}h1{{font-size:25px}}}}</style>
<main><p class="meta">RTDB 持续优化 / {datetime.now().astimezone().isoformat(timespec='seconds')}</p><h1>保存响应、索引配置与结构恢复</h1>
<section><p>本轮加入后台批量投递审计日志、单列/联合索引配置、结构变更预览及查询计划，并扩展结构操作的事务回执。默认索引策略保留，业务数据与审计事件仍以 WAL / FULL 提交。性能使用本机隔离数据库，在回归结束后顺序测量；对照为同一代码的同步投递或默认逐列索引。</p></section>
<section><h2>保存响应与日志完成分开衡量</h2><p>每场景预热 2 次、交替测量 15 次。计时包含真实 HTTP 数据校验、保存、后台任务账本、JSON 解码和必要的 150 毫秒任务轮询，不包含浏览器布局与绘制。数据已提交且审计事件已持久化才返回成功；第二行额外等待日期日志和投递标记完成。后台合并窗口约 50 毫秒，单次孤立写入可能更晚完成日志同步。</p>
<div class="scroll"><table>{head}<tbody>{rows}</tbody></table></div><p>保存确认提速与日志同步提速是两个指标，不能将后台未完成工作当成已完成；P95 反映本次采样的波动，也受本机其他负载影响。</p></section>
<section><h2>按需索引的读写权衡</h2><p>两万行、21 列。对照为主键加逐列辅助索引，本轮为主键加 v01/v02 联合索引。500 行事务修改其他 18 个字段；查询使用 v01 等值、v02 范围并排序。结果逐轮核对一致。计时为 SQLite 内核调用，不包含 HTTP、审计或界面。</p>
<div class="scroll"><table>{head}<tbody>{index_rows}</tbody></table></div>
<p>索引总数 <strong>{old['indexes']} → {new['indexes']}</strong>；数据库页占用 <strong>{old['bytes']/1024/1024:.2f} → {new['bytes']/1024/1024:.2f} MiB</strong>。空间是测量时 page_count × page_size，包含数据库页面，不是净业务值大小。减少索引可能让其他筛选条件变慢，不能直接套用到所有业务表。</p>
<pre>逐列索引查询计划：{escape('; '.join(old['query_plan']))}\n按需索引查询计划：{escape('; '.join(new['query_plan']))}</pre></section>
<section><h2>功能与异常验证</h2><p><strong>{args.backend_tests} 项后端/内核测试完成验证、{args.browser_checks} 项浏览器检查通过。</strong>后端首轮 210 项通过，1 项原有写入进程测试在准备阶段的 100 毫秒门限下超时；该模块 11 项独立复跑全部通过。保留此次时序波动记录，未修改超时门限。</p><ul>
<li>字段区右键新增索引设置、结构预览、当前查询计划；配置纳入本地结构草稿，Ctrl+S 后写入 SQLite 内置定义并导出多表 JSON/Python。</li>
<li>主键唯一索引始终保留；仅调整辅助索引时不扫描校验全部业务记录；普通中文名修改保持已配置索引。</li>
<li>实际子进程在建表、改名、删表提交后退出，重启按事务回执恢复成功；回执生成失败时结构与索引回滚。</li>
<li>多表导入在第一张表提交后、任务进度更新前退出，重启仍能核实已完成表；剩余步骤不自动重复执行。</li>
<li>日志阻塞不阻止已持久化数据返回成功；日志按批投递、失败重试、重复回放去重。定义文件导出失败明确提示已提交结果，并保留恢复依据。</li>
<li>原有草稿恢复、双页面隔离、批量编辑、并发冲突、响应丢失恢复与页面交互回归通过。</li></ul>{screenshot}</section>
<section><h2>边界与复现</h2><p>事务回执覆盖限定数据库的管理接口。多表操作按表提交，整体不是单一事务；部分完成任务只提供可核查凭证，尚未自动继续执行。数据库文件上传/替换与旧 /api/sqlite 路由继续沿用原有恢复机制。回执归档、多数据库公平调度和大文件分块导入仍属后续工作。</p>
<p class="meta">{escape(platform.platform())} · Python {platform.python_version()} · SQLite {sqlite3.sqlite_version}</p>
<pre>python -m unittest web.test_schema_receipts_indexes
python benchmarks/benchmark_delivery_indexes.py --output output/delivery-index-performance-20261115.json
# 浏览器检查仅指向隔离工作流服务：
run-code --filename benchmarks/verify_web_indexes.js</pre>
<details><summary>完整原始样本</summary><pre>{raw}</pre></details></section></main></html>''',encoding='utf-8')
    print(args.output.resolve())


if __name__=='__main__':main()
