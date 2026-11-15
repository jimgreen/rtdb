"""Build a self-contained report from the paired UI and write-receipt measurements."""
import argparse
import base64
from datetime import datetime
from html import escape
import json
from pathlib import Path
import platform
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('render', type=Path)
    parser.add_argument('writes', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    render = json.loads(args.render.read_text(encoding='utf-8-sig'))
    writes = json.loads(args.writes.read_text(encoding='utf-8-sig'))
    before, after = render['timings']['full'], render['timings']['virtual']
    def row(label, old, new):
        delta = (new['median_ms'] / old['median_ms'] - 1) * 100
        return f'<tr><th>{escape(label)}</th><td>{old["median_ms"]:.3f}</td><td>{new["median_ms"]:.3f}</td><td>{delta:+.1f}%</td><td>{old["p95_ms"]:.3f} → {new["p95_ms"]:.3f}</td></tr>'
    render_rows = row('同步表格绘制', before['render'], after['render']) + row('绘制至第二次动画帧', before['frame'], after['frame'])
    write_rows = ''.join(row(f'{item["rows"]} 行批量修改', item['timings']['previous'], item['timings']['receipts']) for item in writes['results'])
    table_head = '<thead><tr><th>场景</th><th>对照 / ms</th><th>本轮 / ms</th><th>中位变化</th><th>P95 / ms</th></tr></thead>'
    raw = escape(json.dumps({'render': render, 'writes': writes}, ensure_ascii=False, indent=2))
    screenshot = ''
    if args.screenshot:
        data = base64.b64encode(args.screenshot.read_bytes()).decode()
        screenshot = f'<figure><img alt="隔离测试库中的虚拟表格，KEY 列冻结，曲线与日志保留" src="data:image/png;base64,{data}"><figcaption>隔离测试库中的 500 行宽表。业务数据未用于写入测试。</figcaption></figure>'
    html = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RTDB 持续优化验证 · 2026-11-15</title>
<style>body{{margin:0;background:#f4f7fa;color:#1b293c;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1120px;margin:auto;padding:40px 24px}}h1{{font-size:32px;line-height:1.35}}h2{{margin-top:36px;font-size:22px}}.lead,section{{background:white;padding:22px 26px;border:1px solid #dfe6ed;border-radius:12px;margin-top:20px}}.meta,figcaption{{color:#607184;font-size:14px}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px 12px;border-bottom:1px solid #e1e6ec;text-align:right}}th:first-child{{text-align:left}}thead{{background:#edf3f9}}.scroll{{overflow:auto}}strong{{color:#174e88}}code,pre{{font-family:Consolas,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff3f7;padding:16px}}img{{max-width:100%;height:auto}}figure{{margin:22px 0}}.cost{{border-left:4px solid #c48428;padding-left:16px}}@media(max-width:600px){{main{{padding:16px}}section,.lead{{padding:16px}}h1{{font-size:26px}}}}</style>
<main><p class="meta">RTDB / 本机隔离环境 / {datetime.now().astimezone().isoformat(timespec='seconds')}</p>
<h1>宽表绘制提速，补齐草稿与提交恢复</h1>
<div class="lead">本轮完成行列虚拟渲染、按页面隔离的 IndexedDB 数据与结构草稿，以及单数据库数据写入的事务内回执和审计事件。性能对照采用同一工作区代码开关，测试库与正式业务库分离；不是不同 Git 版本的整站压测。</div>
<section><h2>宽表绘制</h2><p>同一页面 500 行 × 40 列，视口 {render['viewport']['width']} × {render['viewport']['height']}；完整绘制与虚拟绘制交替运行，每组预热 2 次、记录 15 次。实际挂载单元格从 <strong>{before['cells']:,}</strong> 减为 <strong>{after['cells']:,}</strong>。</p>
<div class="scroll"><table>{table_head}<tbody>{render_rows}</tbody></table></div>
<p>计时从 renderGrid 开始。同步绘制不含 HTTP、SQL、JSON 解析；第二次动画帧只是浏览器帧估计，不等于屏幕实际显示延迟。完整布局采用原来的自动列宽，虚拟布局使用 160 像素列宽、29 像素行高。当前页数据仍完整保留，渲染提速不能解释为数据库查询同倍数提速。</p></section>
<section><h2>写入可靠性的代价</h2><p>同一临时 SQLite 数据库，配对比较绕过回执包装的原有审计写入路径与启用回执后的路径；每组预热 2 次、记录 15 次，并核对修改条数与内容。计时包含管理层校验、数据提交和日志追加，不含 HTTP、浏览器或后台任务账本。</p>
<div class="scroll"><table>{table_head}<tbody>{write_rows}</tbody></table></div>
<p class="cost">此项增加了持久化工作，写入更慢。收益是数据、提交结果和成功审计事件在同一业务事务中提交；进程在提交后异常退出，也可恢复成功结果，避免把已完成操作误认为失败。业务写入继续使用 WAL / FULL；没有通过降低持久性换取速度。</p></section>
<section><h2>正确性验证</h2><p><strong>162 项后端测试、93 项浏览器检查通过。</strong>JavaScript 语法和 Git 空白检查通过。</p><ul>
<li>真实子进程在提交后退出；重启恢复成功，不重复插入。批量错误及审计事件生成失败时数据整体回滚。</li>
<li>日志文件失败时显示“数据已提交”，待补写事件留在业务库；重启补写按事件编号去重。重复 DELETE 请求返回原结果。</li>
<li>双页面同时编辑、保存冲突、结构草稿和无效默认值恢复；模拟浏览器配额不足后，原 IndexedDB 草稿仍保留。</li>
<li>鼠标拖拽自动滚动、跨窗口复制粘贴、屏幕外记录刷新、KEY 冻结、双击编辑、修改高亮、撤销重做、曲线同步；选择过程不新增曲线。</li></ul>
<p>浏览器检查分别为恢复 22、草稿隔离 15、虚拟表格 23、原有局部更新 19、冲突工作流 14。真实业务数据库仅作健康、列表和读取检查。</p>{screenshot}</section>
<section><h2>适用范围与后续重点</h2><p>事务内回执覆盖选定数据库的管理接口 rows、cell-edits、clear、import 和写入 SQL。结构、多表导入/恢复、文件操作与旧 /api/sqlite 接口尚未统一纳入；无确定回执的中断任务仍需核查，不自动重做。回执目前持续保留，会占用磁盘。下一轮重点是降低日志投递和回执写入的固定开销，并为结构类操作补充可核查的阶段恢复。</p>
<p>草稿采用 IndexedDB 配额，浏览器关闭前尚未完成的大草稿写入不能保证恢复。离开页面额外尝试不超过 2 Mi 字符的应急副本；存储失败明确提示用户。多数据库和内置定义仍维持原有权威关系。</p></section>
<section><h2>复现与原始结果</h2><p>运行环境：{escape(platform.platform())}；Python {platform.python_version()}；SQLite {sqlite3.sqlite_version}。性能测试在其他回归结束后顺序执行，未同时运行两个性能负载。结果属于本机、当前测试规模和页面布局，不是生产容量承诺。</p>
<pre>python benchmarks/serve_web_render_lab.py
# 在该测试地址通过 Playwright CLI 执行：
run-code --filename benchmarks/benchmark_web_virtual.js
python benchmarks/benchmark_write_receipts.py --output output/write-receipts-performance-20261115.json
python -m unittest web.test_write_receipts</pre>
<details><summary>全部原始样本与测量范围</summary><pre>{raw}</pre></details></section></main></html>'''
    args.output.write_text(html, encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
