"""Render the measured snapshot workflow as a self-contained Chinese report."""
import base64
import html
import json
from pathlib import Path

root=Path(__file__).resolve().parents[1]
data=json.loads((root/'output/database-snapshot-performance-20261115.json').read_text(encoding='utf-8'))
rows=[]
for item in data['results']:
    old=item['json']['median_ms'];new=item['sqlite']['median_ms'];memory=item['python_peak_bytes']
    rows.append(f'<tr><th>{item["total_rows"]:,}</th><td>{old:.1f}</td><td>{new:.1f}</td><td>{(1-new/old)*100:.1f}%</td><td>{memory["json"]/1048576:.1f} → {memory["sqlite"]/1048576:.1f}</td></tr>')
sizes=''.join(f'<tr><th>{r["total_rows"]:,}</th><td>{r["json_bytes"]/1048576:.2f}</td><td>{r["sqlite_bytes"]/1048576:.2f}</td><td>{r["restore_ms"]:.1f}</td></tr>' for r in data['results'])
picture=base64.b64encode((root/'output/playwright/database-snapshots.png').read_bytes()).decode()
body=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB 整库快照优化报告</title>
<style>body{{margin:0;background:#f2f5fa;color:#173554;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1080px;margin:auto;padding:28px 18px}}h1{{font-size:30px;line-height:1.4}}h2{{font-size:22px}}section{{background:white;border:1px solid #d6e0ee;border-radius:12px;padding:22px;margin:20px 0}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px;border-bottom:1px solid #d6e0ee;text-align:right}}th:first-child{{text-align:left}}thead{{background:#eef4fb}}.scroll{{overflow:auto}}img{{width:100%;height:auto}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef4fb;padding:14px;font-size:12px}}small{{color:#60778f}}a{{color:#1456a5}}@media(max-width:600px){{section{{padding:13px}}h1{{font-size:25px}}th,td{{padding:7px;font-size:13px}}}}</style>
<main><small>RTDB 持续优化 · {html.escape(data['created'])}</small><h1>整库在线快照、流式下载与恢复新数据库</h1>
<section><p>本轮补齐完整 SQLite 数据库的备份与恢复：覆盖多表数据、索引、中文名、显示类型、默认值、内置定义和库内回执。生成、下载、恢复均有操作日志和失败提示。</p><p><strong>入口：左侧表列表右键 → 整库快照 / 恢复。</strong>未选择数据库时仍能浏览和恢复已有快照；原文件丢失也不影响恢复。恢复只创建新库，不覆盖已有数据库。原页面和未保存草稿保持原样。</p></section>
<section><h2>同环境交替对照</h2><p>每档两张表，分别为1千、1万、5万行/表。每行含整数主键及约160字符文字字段；每种方式预热1次、交替测量{data['repeats']}次。原流程使用现有 JSON 表备份并写盘 fsync；新流程生成 SQLite 快照，执行完整性、元数据和 SHA-256 校验后发布。计时范围为服务端生成落盘，不含 HTTP、浏览器下载或绘制。</p>
<div class="scroll"><table><thead><tr><th>合计记录数</th><th>JSON / ms</th><th>快照 / ms</th><th>耗时减少</th><th>Python 峰值 / MiB</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p>耗时为中位数，5次样本不足以作稳定 P95 推断。Python 分配峰值使用单独的 tracemalloc 测量，不混入上述计时；不包含 SQLite 原生缓存，也不是进程总内存。所有样本位于隔离临时目录，未读写业务记录。</p>
<h2>文件大小与恢复</h2><div class="scroll"><table><thead><tr><th>合计记录数</th><th>JSON / MiB</th><th>快照 / MiB</th><th>新库恢复 / ms</th></tr></thead><tbody>{sizes}</tbody></table></div><p>整库文件包含物理页、索引和库内元数据，本次比 JSON 文件更大；这项优化减少了序列化及内存占用，不是文件压缩。恢复耗时仅为每档一次样本，包含复制、校验、发布与 JSON/Python 定义导出。</p></section>
<section><h2>操作窗口</h2><img src="data:image/png;base64,{picture}" alt="隔离测试库中，快照恢复为新数据库的成功窗口"><p>下载按1 MiB分块传输，支持单段 HTTP Range 与 If-Range；页面直接交给浏览器下载，不构建整库 Blob/Base64。快照保存在服务数据目录 .web-snapshots，可下载到其他位置保存。</p></section>
<section><h2>正确性与异常验证</h2><p><strong>235项完整回归通过；16项浏览器流程检查通过。</strong>新后端专项12项已包含在完整回归中。桌面和窄屏窗口均进行了检查。</p><ul><li>WAL已提交数据、多表与索引完整；外部写入可继续，快照内的多表保持一致数据时点。</li><li>跨库下载隔离；下载范围越界拒绝；校验失败、同名目标、取消均不发布部分结果。</li><li>原库丢失后仍可恢复；JSON/Python导出失败有提示和恢复日志；进程中断后的已发布任务结果可找回。</li><li>浏览器能生成、下载、恢复快照并显示新库；不丢失当前页草稿，快照不包含未提交修改。</li></ul></section>
<section><h2>边界与下一轮重点</h2><p>快照仅备份一个 SQLite 文件。日期日志目录和服务任务账本在文件外，需要另行备份。当前列表显示最新50份，不自动清理旧快照。大快照的读取事务可能暂缓 WAL 截断，生成期间需有足够磁盘空间。原有本地上传接口仍有16 MiB上限，本轮未改动；大快照可在服务器列表中直接恢复。</p><ol><li>大文件分块上传、校验和流式解析，降低导入时的内存与等待。</li><li>多表导入按已提交步骤继续，明确已完成、失败与未执行的表。</li><li>快照保留策略、配套日志/任务账本备份和恢复演练。</li></ol><p>实现依据：<a href="https://www.sqlite.org/backup.html">SQLite Online Backup API</a>、<a href="https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup">Python sqlite3 backup</a>。本项目的隔离、日志、恢复策略由本轮代码和测试验证。</p><small>{html.escape(data['platform'])} · Python {data['python']} · SQLite {data['sqlite']}</small>
<pre>python -m unittest web.test_database_snapshots
python benchmarks/benchmark_database_snapshots.py
# 浏览器流程仅运行在隔离的 workflow-lab
run-code --filename benchmarks/verify_database_snapshots.js</pre><details><summary>完整原始样本</summary><pre>{html.escape(json.dumps(data,ensure_ascii=False,indent=2))}</pre></details></section></main></html>'''
target=root/'output/rtdb-database-snapshots-20261115.html';target.write_text(body,encoding='utf-8');print(target)
