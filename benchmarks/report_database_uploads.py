"""Build a self-contained report from measured upload results and QA artifacts."""
import base64
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output'


def main():
    data=json.loads((OUT/'database-upload-performance-20261115.json').read_text(encoding='utf-8'))
    log=(OUT/'upload-full-regression-final.log').read_text(encoding='utf-8-sig')
    assert 'Ran 249 tests' in log and log.rstrip().endswith('OK')
    rows=data['results'];small=rows[0]
    reduction=100*(1-small['request_body_bytes']['chunked']/small['request_body_bytes']['base64'])
    memory=small['python_peak_bytes'];mem_reduction=100*(1-memory['chunked']/memory['base64'])
    def mib(value):return f'{value/1024**2:.2f}'
    def timing(row,kind):
        if kind not in row['timings']:return '超过原接口限制'
        value=row['timings'][kind];return f"{value['median_ms']:.1f} ms<br><small>{value['min_ms']:.1f}–{value['max_ms']:.1f} ms</small>"
    table=''.join(f'<tr><td>{mib(row["file_bytes"])} MiB</td><td>{row["repeats"]}</td><td>{timing(row,"base64")}</td><td>{timing(row,"chunked")}</td><td>{mib(row["python_peak_bytes"]["chunked"])} MiB</td></tr>' for row in rows)
    def picture(name):return 'data:image/png;base64,'+base64.b64encode((OUT/'playwright'/name).read_bytes()).decode()
    body=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RTDB 持续优化 · 整库快照续传</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fb;color:#182a43;font:16px/1.7 "Microsoft YaHei",sans-serif}}main{{max-width:1100px;margin:auto;padding:42px 24px}}h1{{font-size:34px;line-height:1.25;margin:8px 0 20px}}h2{{font-size:22px;margin-top:0}}p{{margin:12px 0}}.eyebrow{{color:#335fc3;font-weight:700;letter-spacing:2px}}.lead{{font-size:19px;max-width:860px}}section{{background:white;padding:25px;border:1px solid #dae2ee;border-radius:14px;margin:22px 0}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card{{background:#e9f0ff;border-radius:12px;padding:20px}}.card strong{{display:block;font-size:28px;color:#214dae}}small,.muted{{color:#61718a;font-size:13px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px 10px;text-align:left;border-bottom:1px solid #dfe6f0;vertical-align:top}}th{{background:#edf2f9}}.scroll{{overflow-x:auto}}code{{background:#edf2f9;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}}img{{max-width:100%;border:1px solid #dae2ee;border-radius:10px}}a{{color:#2455af}}li{{margin:7px 0}}.mobile{{max-width:300px;display:block;margin:20px auto}}.notice{{border-left:4px solid #d6a12c;padding:12px 18px;background:#fff8e8}}@media(max-width:620px){{main{{padding:22px 14px}}h1{{font-size:27px}}section{{padding:18px}}.cards{{grid-template-columns:1fr}}}}
</style><main><div class="eyebrow">RTDB · 持续优化 / 2026-11-15</div>
<h1>本地整库快照：分块上传、校验与恢复</h1>
<p class="lead">本轮打通超过16 MiB的本地整库快照上传。每块确认后保存进度，断线或刷新后续传；校验通过后，由用户指定新数据库名称完成恢复。</p>
<div class="cards"><div class="card"><strong>{reduction:.1f}%</strong>请求体传输量减少<br><small>约8 MiB文件，与原Base64路径对比</small></div><div class="card"><strong>{mem_reduction:.1f}%</strong>Python分配峰值降低<br><small>同进程HTTP客户端＋服务端，不是总内存</small></div><div class="card"><strong>1 MiB</strong>浏览器单次读取上限<br><small>真实浏览器20.1 MiB文件验证</small></div></div>
<section><h2>使用入口与工作流程</h2><p><b>左侧表列表右键 → 整库快照 / 恢复 → 上传本地快照</b>。另一入口位于“选择 / 新建数据库”的本地上传区域。</p>
<ol><li>选择通过RTDB“生成整库快照”得到的自包含SQLite文件。新接口上限2 GiB，已有Base64多文件接口继续限制为16 MiB/文件。</li><li>上传期间显示已确认字节、进度和速度，可暂停。刷新后重新选择同一文件，核对已接收块后继续。</li><li>后台校验完整性、定义指纹、记录数与SHA-256；可选输入预期整文件SHA-256。</li><li>确认新库名称后恢复。禁止覆盖同名数据库，恢复后可从数据库选择框进入。</li></ol>
<p>临时会话保留24小时；创建新会话时清理过期临时内容。最多8个未完成会话、声明总量最多8 GiB。放弃临时上传保留已发布快照与已恢复数据库。Web Crypto要求localhost或HTTPS。</p></section>
<section><h2>实测性能</h2><div class="scroll"><table><thead><tr><th>SQLite文件</th><th>重复次数</th><th>原Base64全流程</th><th>分块上传＋校验＋恢复</th><th>新路径Python峰值</th></tr></thead><tbody>{table}</tbody></table></div>
<p>时间为中位数，小字为最小–最大值。每组预热一次，约8 MiB场景交替执行新旧路径；计时含读取、编码/哈希、HTTP、校验和新库落地，结果核查在计时外。每个结果均核对两张表各1000条记录及大字段长度，并执行quick_check。</p>
<p>约8 MiB文件请求体：原路径{mib(small['request_body_bytes']['base64'])} MiB，新路径{mib(small['request_body_bytes']['chunked'])} MiB（含小型控制请求，不含HTTP头）。Python分配峰值：原路径{mib(memory['base64'])} MiB，新路径{mib(memory['chunked'])} MiB。</p>
<p class="notice">分块路径逐块刷盘确认，额外保留已校验快照并执行恢复校验，工作量高于原路径；不能将传输量和内存下降解释为所有文件都更快。32/64 MiB场景原接口不支持，故不提供虚构的旧耗时。2 GiB是配置上限，本轮实际测到约64 MiB，并未完整压测2 GiB。</p>
<p class="muted">{html.escape(data['platform'])} · Python {html.escape(data['python'])} · SQLite {html.escape(data['sqlite'])} · 本机HTTP回环，实际磁盘。内存使用tracemalloc单独测量，计时不含追踪开销；不含SQLite原生缓存或浏览器进程内存。真实网络速度尚未测量。</p></section>
<section><h2>验证结果</h2><ul><li>249项后端回归通过，涵盖数据与结构修改、数据库隔离、导入、分页、日志、任务恢复、快照和上传。</li><li>23项真实浏览器检查通过：暂停、刷新续传、确认响应丢失重试、不重复写入、同名库拒绝、两表恢复、默认值保留、未保存编辑保留、窄屏布局。</li><li>20.1 MiB浏览器测试文件成功恢复两张表，校验节点表250条记录。浏览器每次读取不超过1 MiB；续传不重发已确认的第0块。</li><li>服务进程中断前已发布结果可恢复；发布文件与清单之间中断可完成发布；校验取消保留已上传数据；无效内容和同名文件冲突均拒绝并记录原因。</li><li>浏览器控制台有两条预期请求失败：模拟丢失确认、同名恢复409。未出现JavaScript异常。</li></ul>
<img alt="真实浏览器上传完成并恢复新库" src="{picture('database-upload-complete.png')}"><img class="mobile" alt="430像素窄屏上传窗口" src="{picture('database-upload-mobile.png')}"></section>
<section><h2>后续值得继续做的优化</h2><ol><li><b>CSV/E流式预览与导入：</b>按批读取、限制预览样本数，复用分块上传与后台任务，减少大文本解析内存。</li><li><b>多表导入按已提交步骤继续：</b>持久化每表确认与执行结果，失败时只重试未完成部分，避免重复写入。</li><li><b>快照保留与备份配套：</b>按时间和容量预览清理候选，并打包服务日志、任务账本等数据库外信息。</li></ol><p>上述三项尚未实现；本轮交付范围是整库快照上传与恢复。</p></section>
<section><h2>复现与实现位置</h2><p><code>python benchmarks/benchmark_database_uploads.py</code><br><code>python -m unittest web.test_database_uploads</code></p>
<p>上传接收：web/database_uploads.py、web/sqlite_server.py、web/management.py；任务恢复：web/tasks.py；界面：web/sqlite_static/database_uploads.js、database_snapshots.js、tasks.js、index.html、app.css；说明：web/README.md。</p>
<p>原始数据：output/database-upload-performance-20261115.json。完整回归日志：output/upload-full-regression-final.log。测试和基准仅使用隔离临时数据库。</p>
<p>实现参考：<a href="https://developer.mozilla.org/en-US/docs/Web/API/Blob/slice">Blob.slice</a>、<a href="https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/digest">SubtleCrypto.digest</a>。浏览器摘要API不支持流式输入，因此只对单块求摘要，完整文件摘要由服务端流式计算。</p></section>
<footer class="muted">生成依据：{html.escape(data['created'])}实测结果。没有推送Git远程；本轮修改保留在工作区。</footer></main></html>'''
    target=OUT/'rtdb-resumable-upload-20261115.html';target.write_text(body,encoding='utf-8');print(target)


if __name__=='__main__':main()
