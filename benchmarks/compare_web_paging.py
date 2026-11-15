"""Render one isolated paging benchmark as a standalone HTML report."""
import argparse
import html
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verification', default='本报告只包含基准样本，功能回归结果请另行核验。')
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding='utf-8'))
    rows = []
    for item in data['results']:
        before, after = item['offset_read'], item['cursor_read']
        ratio = before['median_ms'] / after['median_ms']
        rows.append(f"<tr><th>{item['offset']:,}</th><td>{before['median_ms']:.3f}</td>"
                    f"<td>{after['median_ms']:.3f}</td><td>{before['p95_ms']:.3f}</td>"
                    f"<td>{after['p95_ms']:.3f}</td><td>{ratio:.2f}×</td></tr>")
    source = html.escape(json.dumps(data, ensure_ascii=False, indent=2))
    last = data['results'][-1]
    before, after = last['offset_read']['median_ms'], last['cursor_read']['median_ms']
    highlight = (f"{data['rows']:,} 行、每页 {data['page_size']} 行：偏移 {last['offset']:,} 时，"
                 f"管理层耗时中位数从 <b>{before:.3f} ms 降至 {after:.3f} ms</b>，"
                 f"中位数比值 <b>{before/after:.2f} 倍</b>。")
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RTDB 深分页性能对比</title><style>
body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;color:#18324a;background:#f3f6fa;margin:0}
main{max-width:1080px;margin:32px auto;padding:32px;background:white;border-radius:12px}
h1{font-size:27px;margin:0 0 12px}h2{font-size:20px;margin-top:28px}p{margin:12px 0}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:9px 12px;border-bottom:1px solid #dce4ee;text-align:right}
th:first-child{text-align:left}thead{background:#eaf0fa}code{background:#edf2f7;padding:2px 5px}
.scroll{overflow:auto}.scope{border-left:4px solid #4d6ed8;padding:8px 16px;background:#f5f7fd}
pre{overflow:auto;font-size:12px;max-height:600px}small{color:#53697c}
@media(max-width:700px){main{margin:0;padding:18px}th,td{white-space:nowrap}}
@media print{body{background:white}main{margin:0;padding:0}details{display:none}}
</style><main><h1>RTDB 深分页性能对比</h1>
<p class="scope">HIGHLIGHT</p>
<p>本轮实现任务状态持久化和重复提交保护、浏览器本地草稿恢复、查询阶段计时与主键游标分页。业务数据及表定义仍由各 SQLite 数据库管理。</p>
<h2>实测结果</h2><p>同一机器、同一临时数据库；PARAMETERS。每次检查返回主键与预期页面完全一致。时间单位：毫秒。</p>
<div class="scroll"><table><thead><tr><th>起始偏移</th><th>OFFSET 中位数</th><th>游标中位数</th><th>OFFSET P95</th><th>游标 P95</th><th>中位数比值</th></tr></thead><tbody>ROWS</tbody></table></div>
<p>计时包括 <code>SQLiteManagement.sql</code> 中打开数据库、SQL/取数、解码、版本与游标处理；不含 HTTP 传输、审计调度、响应编码和浏览器绘制。此结果是后端分页路径的配对比较，不能解释为整体 Web 页面同等提速，也不是 mmap、Redis、SQLite 三种引擎的对比。</p>
<h2>适用边界</h2><p>完整表查询按主键顺序连续前后翻页时使用游标。一般 SQL、任意跳页、非主键排序或数据版本变化时回退 OFFSET。游标有数量和有效期限制；浏览器页脚的性能统计可观察实际执行方式和各阶段耗时。</p>
<p>后台任务回执独立持久化，结果正文保留 7 天；服务中断时不自动重做可能已经提交的写入。恢复本地草稿不写数据库，仍需版本比较与人工保存；结构不匹配时允许导出但禁止直接恢复。同一表的本地草稿不支持多个浏览器标签页协同编辑。</p>
<h2>验证与复现</h2><p>VERIFICATION</p>
<p>基准脚本：<code>benchmarks/benchmark_web_paging.py</code>；报告生成：<code>python benchmarks/compare_web_paging.py output/paging-performance-20261115.json --output output/rtdb-paging-performance-20261115.html</code>。浏览器测试使用独立测试库，没有修改正式业务记录。</p>
<details><summary>查看全部原始测量数据</summary><pre>SOURCE</pre></details>
<p><small>样本日期：2026-11-15。测试环境、缓存状态、磁盘与并发负载变化会影响数值；比值只适用于本次测量。</small></p></main></html>'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    parameters = (f"{data['rows']:,} 行、{data['columns']} 列，每页 {data['page_size']} 行；"
                  f"每种模式预热 {data['warmups']} 次，再交替采样 {data['repeats']} 次")
    rendered = document.replace('HIGHLIGHT', highlight).replace('PARAMETERS', parameters)
    rendered = rendered.replace('VERIFICATION', html.escape(args.verification))
    args.output.write_text(rendered.replace('ROWS', ''.join(rows)).replace('SOURCE', source), encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
