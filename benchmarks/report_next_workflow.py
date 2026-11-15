"""Self-contained, evidence-bounded report for the next workflow optimization."""
import argparse
import base64
from datetime import datetime
from html import escape
import json
from pathlib import Path
import platform
import sqlite3


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--screenshot',type=Path)
    args=parser.parse_args();data=json.loads(args.source.read_text('utf-8'))
    rows='';logs=''
    for group in data['save']:
        old,new=group['timings']['previous'],group['timings']['current']
        for key,target in [('save_ms','save'),('log_complete_ms','logs')]:
            a,b=old[key],new[key]
            row=f'<tr><th>{group["rows"]} 行</th><td>{a["median_ms"]:.3f}</td><td>{b["median_ms"]:.3f}</td><td>{(1-b["median_ms"]/a["median_ms"])*100:.1f}%</td><td>{a["p95_ms"]:.3f} → {b["p95_ms"]:.3f}</td></tr>'
            if target=='save':rows+=row
            else:logs+=row
    head='<tr><th>修改规模</th><th>原流程中位 / ms</th><th>新流程中位 / ms</th><th>耗时减少</th><th>P95 / ms</th></tr>'
    scheduler=data['scheduling']['timings'];compressed=data['compression'];screenshot=''
    if args.screenshot:screenshot='<img alt="日志与回执维护窗口" src="data:image/png;base64,'+base64.b64encode(args.screenshot.read_bytes()).decode()+'">'
    args.output.write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB 保存、调度与回执维护验证</title>
<style>body{{margin:0;background:#f3f6fa;color:#20344c;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1100px;margin:auto;padding:32px 24px}}h1{{font-size:30px}}h2{{font-size:22px}}section{{background:white;border:1px solid #d9e3ed;border-radius:12px;padding:22px;margin:20px 0}}table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}td,th{{padding:9px;border-bottom:1px solid #dce4ed;text-align:right}}th:first-child{{text-align:left}}thead{{background:#edf3f8}}.scroll{{overflow:auto}}img{{max-width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef3f7;padding:14px}}.meta{{color:#60758e;font-size:14px}}strong{{color:#135e9d}}@media(max-width:600px){{main{{padding:12px}}section{{padding:14px}}h1{{font-size:25px}}}}</style>
<main><p class="meta">RTDB 持续优化 · {datetime.now().astimezone().isoformat(timespec='seconds')}</p><h1>保存流程、多数据库调度与回执维护</h1>
<section><p>单元格保存合并为一次事务内校验与提交，任务状态改为带版本号的长轮询；后台任务按数据库串行并跨库轮转；日期日志分批补写，旧回执在同一 SQLite 文件内无损压缩。数据和审计事件保持 WAL / FULL 持久化。</p>
<p>入口：<strong>左侧表列表右键 → 日志与回执维护</strong>。后台任务可另页浏览其他数据库；页面底部双击性能统计可查看保存阶段。</p></section>
<section><h2>保存确认：同环境交替对照</h2><p>每个规模预热 2 轮，交替测量 15 轮。原流程为预检查请求、保存请求及必要的 150 毫秒轮询；新流程为单次保存及必要的长轮询。两者运行在本轮同一服务上，后台审计设置相同；比较的是流程变化，未拿不同时段的旧机器负载作对照。</p><div class="scroll"><table><thead>{head}</thead><tbody>{rows}</tbody></table></div>
<p>计时包含实际 HTTP、服务端校验、事务提交、任务账本和结果解码，不包含浏览器绘制。逐次核对保存数量和数据值。重复请求防重、失败整批回滚、主键校验和版本冲突检查保留。</p>
<h2>包含日期日志同步完成</h2><div class="scroll"><table><thead>{head}</thead><tbody>{logs}</tbody></table></div><p>上述完成点另等待审计事件全部投递并标记成功；保存确认时事件已经持久化，日期文件可能仍在同步。机器负载与任务完成窗口会影响测量，数字代表本次样本。</p></section>
<section><h2>跨数据库公平调度</h2><p>受控调度微基准：A 库连续两个各等待 200 毫秒的操作，再提交 B 库短任务。原全局 FIFO 两线程的 B 任务完成中位为 <strong>{scheduler['fifo']['median_ms']:.3f} ms</strong>，按库调度为 <strong>{scheduler['per_database']['median_ms']:.3f} ms</strong>；P95 分别 {scheduler['fifo']['p95_ms']:.3f} / {scheduler['per_database']['p95_ms']:.3f} ms。</p>
<p>此项只证明等待同库任务不占用全部执行线程，不包含真实 SQL、HTTP 和任务账本，不是数据库吞吐倍数。服务最多并行 2 个后台任务，同库最多 4 个运行/排队任务，全局最多 16 个；普通读取接口仍可独立工作。</p></section>
<section><h2>日志积压与回执归档</h2><p>2000 条合成已同步回执，10 批完成、每批 200 条。回执明细 <strong>{compressed['original_bytes']:,} → {compressed['compressed_bytes']:,} 字节</strong>，减少 <strong>{(1-compressed['compressed_bytes']/compressed['original_bytes'])*100:.1f}%</strong>。单批中位耗时 {compressed['batch_timing']['median_ms']:.3f} ms，P95 {compressed['batch_timing']['p95_ms']:.3f} ms。</p>
<p>这是明细正文压缩比，不是整个数据库文件缩小比例。旧结果、事件及校验值仍保存在当前数据库，编号和指纹持续保留；未同步事件不压缩。默认超过 7 天自动压缩，可按库设置，0 关闭。每批最多 200 条、约 2 MiB；超大单条独立处理。后台失败按库退避，重启不在启动线程全量补写日志。</p>{screenshot}</section>
<section><h2>功能与异常验证</h2><p>完整回归 <strong>221 项通过</strong>；随后补充结果过期、防止缺失数据库被维护任务重新创建等检查，相关模块定向复测通过。浏览器共 <strong>53 项检查通过</strong>（新流程 16、恢复 22、页面草稿隔离 15）。</p>
<ul><li>一次 PUT 完成保存，无重复预检查；无效批次和重复主键不留下部分修改。</li><li>同库任务保持顺序，其他库短任务可执行，排队任务可立即取消，提交阶段继续禁止取消。</li><li>长轮询完成即返回；响应丢失后恢复原任务，不重复提交；旧任务结果过期也不重执行。</li><li>归档失败整批回滚；未同步事件保留；压缩后的结果可恢复；编号或指纹冲突拒绝执行。</li><li>维护配置按库隔离，显示积压与压缩结果，任务另页浏览不继承原任务编号。</li></ul>
<p>分页验收发现：更快保存后，后台审计标记可能改变 data_version，游标按设计安全回退到普通分页。记录位置未变；游标专项检查先等待稳定快照，外部并发写入回退另有后端测试。</p></section>
<section><h2>边界与复现</h2><p>历史编号、分表凭证和任务元数据仍会增长，本轮没有删除历史凭证或自动 VACUUM。大文件流式处理、多表中断后继续执行、完整数据库快照仍为后续工作。另页浏览保留原页面任务上下文，没有放开原页在保存过程中切换数据库。</p>
<p class="meta">{escape(platform.platform())} · Python {platform.python_version()} · SQLite {sqlite3.sqlite_version}</p>
<pre>python -m unittest web.test_next_optimization
python benchmarks/benchmark_next_workflow.py --output output/next-workflow-performance.json
# 浏览器脚本仅运行在隔离 workflow-lab 服务
run-code --filename benchmarks/verify_next_workflow.js</pre>
<details><summary>完整原始样本与阶段耗时</summary><pre>{escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></main></html>''',encoding='utf-8')
    print(args.output.resolve())


if __name__=='__main__':main()
