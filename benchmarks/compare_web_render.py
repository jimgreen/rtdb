"""Build a standalone browser-render comparison from two measured JSON artifacts."""
import argparse
import html
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('before',type=Path)
    parser.add_argument('after',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    before=json.loads(args.before.read_text(encoding='utf-8-sig'))
    after=json.loads(args.after.read_text(encoding='utf-8-sig'))
    assert (before['rows'],before['columns'],before['repeats'],before['browser']) == (after['rows'],after['columns'],after['repeats'],after['browser'])
    assert before['forceFull'] and not after['forceFull']
    rows=[]
    labels={'ready_ms':'界面处理完成','frame_ms':'下一帧估计','grid_ms':'同步表格更新','added_cells':'新建单元格数量'}
    for scenario, caption in [('single_cell','一个值变化'),('unchanged','数据未变化')]:
        for metric,label in labels.items():
            a,b=(item['timings'][scenario][metric] for item in (before,after))
            speed=f"{a['median']/b['median']:.2f}×" if a['median'] and b['median'] else '—'
            unit='' if metric=='added_cells' else ' ms'
            rows.append(f"<tr><td>{caption}</td><td>{label}</td><td>{a['median']:g}{unit}</td><td>{b['median']:g}{unit}</td><td>{a['p95']:g} → {b['p95']:g}{unit}</td><td>{speed}</td></tr>")
    data=html.escape(json.dumps({'before':before,'after':after},ensure_ascii=False,indent=2))
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RTDB 浏览器刷新性能对照</title><style>body{font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif;background:#f3f6fa;color:#24364c;margin:0}main{max-width:1120px;margin:28px auto;padding:28px;background:white;border-radius:12px}h1{font-size:27px;margin-top:0}table{border-collapse:collapse;width:100%;margin:18px 0}td,th{text-align:left;padding:8px 10px;border-bottom:1px solid #dce4ee}th{background:#eaf0f8}small{color:#63758a}.wrap{overflow-x:auto}pre{white-space:pre-wrap;max-height:450px;overflow:auto;background:#f3f6fa;padding:12px}li{margin:5px 0}</style><main>
<h1>RTDB 浏览器刷新性能对照</h1><p>2026-11-15 · 500 行 × 40 列 · 本机独立测试数据库</p>
<p>同一版本后端、同一浏览器、同一份数据，依次比较完整重建与局部更新。完整重建对照通过测试注入禁用局部更新，其他逻辑保持一致；不把后端、网络或绘图之外的差异算作渲染优化成果。</p>
<div class="wrap"><table><thead><tr><th>场景</th><th>指标</th><th>完整重建中位数</th><th>局部更新中位数</th><th>P95 前 → 后</th><th>中位数比值</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></div>
<p>每个场景预热 2 次，采样 15 次；P95 取排序样本的 floor(0.95×(n−1)) 位置。修改第 101 行 v08 后，通过正式 SQL/分页接口刷新；结束时恢复测试值。计时含 HTTP 和 JSON，曲线为空，不能代表复杂曲线或所有数据规模。</p>
<ul><li>“界面处理完成”为查询开始至 DOM 同步更新结束；“下一帧”为两次 requestAnimationFrame 回调估计，不是物理屏幕显示延迟。</li><li>局部更新保持原单元格 DOM；完整重建每次创建 20,000 个数据单元格。翻页、排序、字段和记录顺序变化仍完整重建。</li><li>主键冻结、只读限制、选区与滚动位置、修改高亮、曲线绑定、保存版本冲突经过独立浏览器回归验证。</li><li>长任务显示阶段与进度，安全边界接受取消；已经提交的表保留，刷新页面只恢复查询，不重发写入。</li><li>机器负载、磁盘同步和浏览器帧调度影响结果。无变化场景用于观察额外开销，不把小幅波动解释成稳定收益。</li></ul>
<p><small>浏览器：'''+html.escape(after['browser'])+'''</small></p><details><summary>原始测量数据（含全部样本）</summary><pre>'''+data+'''</pre></details></main></html>'''
    args.output.write_text(document,encoding='utf-8')
    print(args.output)


if __name__=='__main__': main()
