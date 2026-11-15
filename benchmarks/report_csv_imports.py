"""Self-contained measured report for the CSV import round."""
import base64
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output'


def main():
    data=json.loads((OUT/'csv-import-performance-20261115.json').read_text(encoding='utf-8'))
    log=(OUT/'csv-full-regression-final.log').read_text(encoding='utf-8-sig')
    assert 'Ran 260 tests' in log and log.rstrip().endswith('OK')
    rows=data['results'];largest=rows[-1]['results'];old=largest['legacy'];new=largest['streamed']
    speed=old['apply_ms']/new['apply_ms'];memory=100*(1-new['peak_bytes']/old['peak_bytes'])
    def mib(value):return f'{value/1024**2:.2f}'
    table=''.join(f'''<tr><td>{r['rows']:,}</td><td>{mib(r['file_bytes'])} MiB</td><td>{r['results']['legacy']['total_ms']:.1f} ms</td><td>{r['results']['streamed']['total_ms']:.1f} ms</td><td>{mib(r['results']['legacy']['peak_bytes'])} → {mib(r['results']['streamed']['peak_bytes'])} MiB</td></tr>''' for r in rows)
    def image(name):return 'data:image/png;base64,'+base64.b64encode((OUT/'playwright'/name).read_bytes()).decode()
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB 持续优化 · CSV 流式导入</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fa;color:#1c3049;font:16px/1.7 "Microsoft YaHei",sans-serif}}main{{max-width:1100px;margin:auto;padding:36px 24px}}h1{{font-size:32px;line-height:1.3}}h2{{font-size:21px;margin-top:0}}.tag{{color:#3160bc;letter-spacing:2px;font-weight:bold}}section{{background:white;border:1px solid #dbe4ef;border-radius:14px;padding:24px;margin:22px 0}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card{{background:#e9f0ff;border-radius:12px;padding:20px}}strong.metric{{display:block;font-size:30px;color:#2653ad}}small,.muted{{color:#64748b;font-size:13px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:12px 10px;border-bottom:1px solid #dbe4ef}}th{{background:#eef3fa}}.scroll{{overflow:auto}}img{{max-width:100%;border:1px solid #dbe4ef;border-radius:10px}}.mobile{{max-width:280px;display:block;margin:20px auto}}li{{margin:8px 0}}code{{background:#edf2f9;padding:2px 5px;overflow-wrap:anywhere}}a{{color:#245bb1}}.note{{border-left:4px solid #cf9b2c;background:#fff7e6;padding:14px 18px}}@media(max-width:600px){{main{{padding:22px 14px}}h1{{font-size:26px}}.cards{{grid-template-columns:1fr}}section{{padding:18px}}}}
</style><main><p class="tag">RTDB · 持续优化 / 2026-11-15</p><h1>CSV：分块上传、样本预览、原子导入</h1><p>本轮完成CSV大文件加载流程，减少整文件对象堆积和逐行合并查询。E/dbdef多表结构导入继续使用原流程，后续单独迁移。</p>
<div class="cards"><div class="card"><strong class="metric">{speed:.2f}×</strong>5万条确认后处理旧/新耗时比<br><small>新路径已完成预览，不代表完整流程提速</small></div><div class="card"><strong class="metric">{memory:.1f}%</strong>Python分配峰值下降<br><small>5万条场景，不代表进程总内存</small></div><div class="card"><strong class="metric">20条</strong>页面预览样本上限<br><small>完整文件在服务端逐行校验</small></div></div>
<section><h2>使用入口</h2><p><b>选择数据库和目标表 → 左侧表列表右键 → CSV 流式导入。</b></p><ol><li>选择CSV和编码，上传并预览。上传会话固定绑定数据库、表和编码；可暂停、刷新后重新选择原文件续传。</li><li>核对目标、前20条样本、字段类型和KEY。长文本只截取显示，完整数据保留。</li><li>选择追加、按主键合并或替换整表并确认。合并和替换需输入表名；替换前再次核对预览版本。</li></ol><p>支持UTF-8（含BOM）与GB18030/GBK，128 MiB、最多10万条记录，单字段最多128 Ki字符；须包含全部英文字段。临时数据保留24小时，创建新会话时清理过期内容。浏览器每次读取最多1 MiB，使用localhost或HTTPS。</p></section>
<section><h2>性能对比</h2><div class="scroll"><table><thead><tr><th>导入记录</th><th>文件大小</th><th>旧文本合并</th><th>新流式流程</th><th>Python峰值（旧 → 新）</th></tr></thead><tbody>{table}</tbody></table></div>
<p>每组预热一次、交替重复3次，表中为中位数。旧路径包含读取CSV、解析与原逐行合并；新路径包含按块读取、校验及上传落盘、临时SQLite预览、最终批量合并。新路径执行了额外校验与暂存操作，所有阶段都计入结果。</p>
<p><b>5万条记录：确认后的处理时间 {old['apply_ms']/1000:.2f} → {new['apply_ms']/1000:.2f} 秒；完整流程 {old['total_ms']/1000:.2f} → {new['total_ms']/1000:.2f} 秒。</b>旧路径确认后还需解析，新路径已经预览，因此前者的等待时间下降不等于总流程更快。采样有波动，原始JSON保留了各次耗时。</p>
<p>每份数据50%主键已存在、50%为新增，逐组核对最终行数、新旧数据及原记录位置。设置数据库、填充原始数据和结果核查不计时；Python内存另测，不混入耗时。全部文件位于独立临时目录。</p>
<p class="note">这是本机Python服务调用基准，不含HTTP传输、浏览器解析和人工确认时间，也不代表远程网络下的端到端耗时。内存由tracemalloc测量，不包括SQLite原生缓存、操作系统文件缓存或浏览器进程。</p>
<p class="muted">{html.escape(data['platform'])} · Python {html.escape(data['python'])} · SQLite {html.escape(data['sqlite'])} · {html.escape(data['created'])}</p></section>
<section><h2>功能与一致性</h2><ul><li>输入逐行解码、转换和校验后写入临时SQLite，唯一索引检查跨批次重复KEY；错误预览不修改目标库。</li><li>页面只返回20条样本；前端和上传后端按1 MiB读取，最终写入每批最多512条。</li><li>所有批次在同一个事务内提交，追加冲突、类型错误或中途失败整体回滚。分批内存处理不会分批提交。</li><li>合并使用UPSERT保留原记录位置；替换在取得写锁后核对预览版本，拒绝覆盖期间发生的外部修改。</li><li>业务数据、去重标记与写入回执同事务提交，同一上传重复确认不重复写入；日志与后台任务结果可恢复。</li><li>校验可取消；进入提交阶段后禁止取消，失败自动回滚。清除暂存不删除已导入的数据。</li></ul></section>
<section><h2>验证证据</h2><p>260项完整后端回归通过；另复核CSV与上传专项。20项真实浏览器检查通过，18.1 MiB文件完成18,000条记录导入，验证暂停、刷新续传、丢确认重试、样本限制、窄屏、原记录保留、同库其他表不变以及重复确认。</p><p>浏览器刷新曾发现静态资源连接被拒绝，服务端等待队列由5调为64后，72次并发静态请求均返回200。第一轮完整回归的既有100毫秒写进程用例在并行浏览器负载下超时，停止并行操作后重跑完整回归；没有放宽该测试的超时。</p><img alt="CSV 样本预览" src="{image('csv-preview.png')}"><img class="mobile" alt="CSV 窄屏预览" src="{image('csv-mobile.png')}"><img alt="CSV 导入成功且重复提交受到保护" src="{image('csv-import-complete.png')}"></section>
<section><h2>后续优先项</h2><ol><li>E文件多表流式暂存，保留逐表人工确认KEY和类型，再统一验证结构和数据。</li><li>多表导入失败后从未完成步骤继续，已提交表有回执可核对，避免重复执行。</li><li>快照、临时文件、日志与任务账本的容量统计和保留策略，提供清理候选预览。</li></ol><p>这些是后续事项，本轮没有将E文件改为流式路径。</p></section>
<section><h2>文件与复现</h2><p>新增：web/csv_imports.py、web/sqlite_static/csv_imports.js、web/test_csv_imports.py；修改：web/database_uploads.py、web/management.py、web/tasks.py、web/sqlite_server.py、web/test_database_uploads.py、web/sqlite_static/index.html、app.js、app.css、tasks.js、web/README.md。</p><p><code>python benchmarks/benchmark_csv_imports.py</code><br><code>python -m unittest web.test_csv_imports web.test_database_uploads</code></p><p>原始数据：output/csv-import-performance-20261115.json；完整回归：output/csv-full-regression-final.log；浏览器记录：output/csv-browser.log。未提交或推送Git。</p><p>参考：<a href="https://docs.python.org/3/library/csv.html">Python CSV</a>、<a href="https://www.sqlite.org/lang_upsert.html">SQLite UPSERT</a>。</p></section></main></html>'''
    path=OUT/'rtdb-csv-streaming-20261115.html';path.write_text(text,encoding='utf-8');print(path)


if __name__=='__main__':main()
