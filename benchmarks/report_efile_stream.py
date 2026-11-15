"""Self-contained E import report from actual paired measurements."""
import base64
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output'


def main():
    report=json.loads((OUT/'efile-stream-performance-20261115.json').read_text(encoding='utf-8'))
    regression=(OUT/'efile-full-regression.log').read_text(encoding='utf-8-sig')
    assert 'Ran 273 tests' in regression and regression.rstrip().endswith('OK')
    focused=(OUT/'efile-final-focused.log').read_text(encoding='utf-8-sig')
    assert 'Ran 14 tests' in focused and focused.rstrip().endswith('OK')
    last=report['results'][-1];old=last['results']['legacy'];new=last['results']['streamed']
    mib=lambda value:f'{value/1024**2:.2f}'
    def timing(r,kind):
        values=[s['total_ms']/1000 for s in r['samples'][kind]]
        return f"{r['results'][kind]['total_ms']/1000:.2f} s<br><small>范围 {min(values):.2f}–{max(values):.2f} s</small>"
    rows=''.join(f"<tr><td>{r['total_incoming']:,}</td><td>{timing(r,'legacy')}</td><td>{timing(r,'streamed')}</td><td>{mib(r['results']['legacy']['peak_bytes'])} → {mib(r['results']['streamed']['peak_bytes'])} MiB</td></tr>" for r in report['results'])
    def image(name):return 'data:image/png;base64,'+base64.b64encode((OUT/'playwright'/name).read_bytes()).decode()
    result=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB E文件多表导入优化</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f1f5f9;color:#20364e;font:16px/1.75 "Microsoft YaHei",sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 24px}}h1{{font-size:32px;line-height:1.35}}h2{{font-size:21px;margin-top:0}}.tag{{color:#2762b8;font-weight:bold}}section{{padding:24px;background:white;border:1px solid #d9e3ed;border-radius:12px;margin:22px 0}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card{{background:#e8f0ff;padding:20px;border-radius:12px}}.metric{{font-size:29px;font-weight:bold;display:block;color:#2457a6}}small{{font-size:13px;color:#677b93}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:12px;border-bottom:1px solid #d8e1ed;text-align:left}}th{{background:#edf3fa}}.scroll{{overflow:auto}}img{{width:100%;border:1px solid #dbe3ed;border-radius:9px}}img.mobile{{display:block;width:min(280px,100%);margin:18px auto}}.note{{padding:14px;border-left:4px solid #bf9227;background:#fff8e7}}li{{margin:8px 0}}code{{overflow-wrap:anywhere;background:#edf3fa;padding:2px 5px}}@media(max-width:600px){{main{{padding:20px 14px}}section{{padding:16px}}h1{{font-size:26px}}.cards{{grid-template-columns:1fr}}}}
</style><main><p class="tag">RTDB · 持续优化 / 2026-11-15</p><h1>E文件多表导入：磁盘暂存、人工确认、失败续作</h1><p>保留原“加载 dbdef / E”入口，E文件改用分块上传与磁盘暂存。确认前完整校验，提交后逐表保存完成回执；dbdef及旧文本E接口保持原路径。</p>
<div class="cards"><div class="card"><span class="metric">{old['total_ms']/1000:.2f} → {new['total_ms']/1000:.2f}s</span>5万条完整流程<br><small>双表各2.5万条，本机服务调用中位数</small></div><div class="card"><span class="metric">{mib(old['peak_bytes'])} → {mib(new['peak_bytes'])}</span>Python分配峰值 / MiB<br><small>不包括SQLite原生缓存和进程总内存</small></div><div class="card"><span class="metric">273 项</span>完整后端回归通过<br><small>另有58项专项、18项浏览器流程检查</small></div></div>
<section><h2>使用流程</h2><p><b>选择或新建数据库 → 左侧表列表右键 → 加载 dbdef / E。</b></p><ol><li>选择E文件并“上传并解析”，每块最多1 MiB。暂停或刷新后重新选择相同文件，可继续已确认进度。</li><li>一张表一个标签页。新表默认第一字段为KEY，类型由全部行推断；已有表保留定义。人工修改各表KEY和类型后，点击“校验字段选择”。</li><li>核对校验后的新增、更新、跳过、重复警告，输入目标数据库文件名确认，逐表提交。修改类型或导入方式会使旧校验失效。</li><li>出现失败时，“继续 / 核对导入”会跳过数据库回执确认已提交的表，只处理失败及剩余步骤。已完成表的后续编辑会保留。</li></ol><p>支持UTF-8/BOM、GB18030/GBK、UTF-16/BOM；文件上限128 MiB，每表10万条、最多200张表、每表512字段，单字段128 Ki字符、单逻辑行1 Mi字符。临时上传保留24小时，过期后需重新上传。配置上限不等于本轮实测容量。</p></section>
<section><h2>同机性能比较</h2><div class="scroll"><table><thead><tr><th>双表导入总记录</th><th>原文本流程</th><th>新流式流程</th><th>Python峰值（旧 → 新）</th></tr></thead><tbody>{rows}</tbody></table></div>
<p>每张表已有N条，再导入N条，50%主键重叠、50%新增。每个路径先预热一次，再交替重复3次，表中为中位数；峰值另开一轮测量。原路径包括读取文件、Base64、预览和确认时再次解析及提交；新路径包括分块校验落盘、解析暂存、人工选择的完整校验和最终提交。</p>
<p>最大场景：每表25,000条，共50,000条；文件 {mib(last['file_bytes'])} MiB。确认后的处理时间 {old['apply_ms']/1000:.2f} → {new['apply_ms']/1000:.2f} 秒。新路径已提前完成解析与校验，确认后时间不能单独代表端到端提速。</p><p class="note">全部使用独立临时数据库。本基准不含HTTP、浏览器操作和人工确认，也不计数据库初始化、结果核对。tracemalloc仅统计Python分配；完整工作流时间包含新机制额外的落盘和校验成本。采样存在明显波动，因此同时列出范围，不将中位数之比视为固定提速倍数。原始JSON保留所有样本。</p><small>{html.escape(report['platform'])} · Python {report['python']} · SQLite {report['sqlite']} · {report['created']}</small></section>
<section><h2>一致性与恢复</h2><ul><li>原始行、主键去重和合并结果放在临时SQLite中，内存只保留当前行、最多512条旧数据批次和有限的样本/结构信息。</li><li>重复KEY延续原策略：追加保留已有/首条并警告；合并和替换采用末条。空KEY、无效类型、超过行数上限均拒绝提交。</li><li>每张表的DDL、数据与完成回执原子提交；单表失败全部回滚。多表不是一个全局事务，已完成表保留。</li><li>提交前在写锁内核对表结构、数据及行号版本；发现外部变化则停止。已完成表按回执跳过，不回写旧预览覆盖后续编辑。</li><li>结构与数据写入SQLite内置定义；同库JSON/Python导出失败会提示已提交、待恢复，恢复日志可重新导出。</li><li>计划开始提交后不允许改变字段/模式；需重新上传产生新计划。单表提交不可取消，表间可取消。</li></ul></section>
<section><h2>验证记录</h2><p>273项完整回归、58项专项通过；随后新增真实进程崩溃测试，最终14项E导入测试全部通过。覆盖多表/同名表隔离、末行类型推断、人工KEY/类型、旧版本冲突、重复策略、行号保留、提交中断回滚、回执恢复、导出失败恢复、编码/引号/换行、数量限制、损坏暂存与取消重试。进程崩溃测试在第一张表提交后强制退出隔离子进程，随后从磁盘回执恢复，只提交第二张表，第一张表不重复写入。</p><p>真实浏览器完成4.0 MiB、两表各6,000条导入。18项检查覆盖暂停、刷新续传、服务端接收后响应丢失的重试、每次读取1 MiB、标签页、字段选择校验、窄屏、准确行数和重复确认。浏览器中记录的网络失败为主动注入；成功流程无JavaScript异常。</p><img src="{image('efile-preview.png')}" alt="E文件字段确认与预览"><img class="mobile" src="{image('efile-mobile.png')}" alt="窄屏布局"><img src="{image('efile-complete.png')}" alt="双表导入完成并防止重复提交"></section>
<section><h2>下一轮优先项</h2><ol><li>统一统计快照、源文件、暂存计划、日志和任务账本的磁盘占用，提供保留策略和清理候选预览。</li><li>按大小分层优化校验流程，减少小文件固定开销及不必要的重复扫描，同时保留完整校验和版本保护。</li><li>为数据库维护提供可核对的执行计划与恢复记录，便于长期运行和排查异常。</li></ol></section>
<section><h2>文件与复现</h2><p>核心新增：<code>core/stream_efile.py</code>、<code>web/efile_imports.py</code>、<code>web/sqlite_static/efile_imports.js</code>、<code>web/test_efile_stream.py</code>。修改：上传分派、管理路由、app.js、index.html、app.css、web/README.md。</p><p><code>python -m unittest web.test_efile_stream</code><br><code>python benchmarks/benchmark_efile_stream.py</code></p><p>原始测量：output/efile-stream-performance-20261115.json；完整回归：output/efile-full-regression.log；专项：output/efile-extended.log；浏览器：output/efile-browser.log。此次没有提交或推送Git。</p></section></main></html>'''
    path=OUT/'rtdb-efile-streaming-20261115.html';path.write_text(result,encoding='utf-8');print(path)


if __name__=='__main__':main()
