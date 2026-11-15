"""Generate a standalone, evidence-linked HTML from two history benchmark runs."""
import argparse
import html
import json
from pathlib import Path
import re


def compare(before_path, after_path, output, verification):
    before, after = [json.loads(p.read_text(encoding='utf-8')) for p in (before_path, after_path)]
    for key in ('log_records', 'log_files', 'source_bytes', 'repeats', 'python', 'platform', 'scope', 'results'):
        if before[key] != after[key]:
            raise ValueError(f'不可直接比较：{key} 不一致')
    log = verification.read_text(encoding='utf-8-sig')
    counts = re.findall(r'Ran (\d+) tests', log)
    if not counts or not log.rstrip().endswith('OK'):
        raise ValueError('回归日志没有通过标记')
    labels = {'latest_50': '最近 50 条日志', 'deep_page_1000': '日志第 1000 页',
              'date_failures': '指定日期 + 失败状态', 'keyword_failures': '类别 + 状态 + Unicode 关键词',
              'reopen_latest_50': '重新打开日志管理器后查询', 'save_500_rows': '保存 500 行修改'}
    rows = []
    for key, label in labels.items():
        a, b = before['timings'][key], after['timings'][key]
        reduction = (1 - b['median_ms']/a['median_ms'])*100
        rows.append(f'<tr><td>{label}</td><td>{a["median_ms"]:.2f}</td><td>{b["median_ms"]:.2f}</td>'
                    f'<td>{reduction:.1f}%</td><td>{a["p95_ms"]:.2f} → {b["p95_ms"]:.2f}</td></tr>')
    cold_a, cold_b = before['cold_first_query']['median_ms'], after['cold_first_query']['median_ms']
    links = ' · '.join(f'<a href="{html.escape(p.name)}">{name}</a>' for p, name in
                      [(before_path, '优化前原始数据'), (after_path, '优化后原始数据'), (verification, '回归测试日志')])
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RTDB 第二轮性能对比</title>
<style>body{{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#f4f7fb;color:#24334a;margin:0}}
main{{max-width:1080px;margin:30px auto;padding:32px;background:white;border-radius:12px}}h1{{margin:0 0 10px}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px;border-bottom:1px solid #dce3ed;text-align:right}}th:first-child,td:first-child{{text-align:left}}
th{{background:#edf3fa}}.ok{{padding:14px;background:#eaf6ef;border-left:4px solid #29905c}}.note{{padding:14px;background:#fff5e1;border-left:4px solid #dc9e25}}code{{font-size:14px}}a{{color:#225cab}}@media(max-width:700px){{main{{padding:14px}}table{{font-size:12px}}th,td{{padding:6px}}}}@media print{{body{{background:white}}main{{margin:0;padding:0}}}}</style>
<main><h1>RTDB 第二轮优化：日志查询与批量保存</h1>
<p>对比对象为本轮修改前的工作区版本与修改后的版本。使用同一台计算机、相同生成数据和同一基准程序；不与其他数据库产品的理论性能作比较。</p>
<p class="ok">{counts[-1]} 项回归通过；4 类日志查询的完整响应摘要完全一致，包括记录内容、顺序、总数、页数及日期列表。</p>
<h2>测量结果</h2><p>单位：毫秒。每项重复 {after['repeats']} 次；P95 使用排序样本的第 floor(0.95×(n−1)) 项。耗时降低比例按中位数计算。</p>
<table><thead><tr><th>场景</th><th>优化前中位</th><th>优化后中位</th><th>耗时降低</th><th>P95 前 → 后</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class="note">首次查询（包含首次建索引，仅测一次）：{cold_a:.2f} → {cold_b:.2f} ms。索引建立有一次性时间和额外磁盘成本；上表日志查询均为索引已建立后的耗时，不能将其视为首次打开速度。</p>
<h2>功能保护与改动</h2><ul><li>日期 JSONL 文件保留为原始日志，原有 flush/fsync 写入方式不变；SQLite 索引可重建，损坏或不可用时回退原文件读取。</li>
<li>支持追加内容增量索引、文件替换和截断后的重建、跨进程追加、历史日期及 Unicode 字面关键词过滤、原有分页和保留期清理。建索引不占用日志追加锁。</li>
<li>500 条复合主键的版本校验从 500 次查询降至 2 次分批查询；主键、字段类型、并发冲突检查和整批回滚继续保留。</li></ul>
<h2>环境与边界</h2><p>{after['log_files']} 个日期文件，合计 {after['log_records']:,} 条日志，原始文件 {after['source_bytes']/1024/1024:.2f} MiB；分页每页 50 条。保存基准使用 1 万行表，每次修改 500 行。</p>
<p>Python {html.escape(after['python'])}；{html.escape(after['platform'])}。测试使用临时目录，未修改业务数据。计时包括服务层调用、日志文件读取和 SQLite 提交，不含 HTTP、浏览器渲染及 API 审计日志追加。单机测试受系统负载与文件缓存影响，不代表所有部署环境。</p>
<p>{links}</p><h2>复测</h2><pre><code>python benchmarks/benchmark_web_history.py --output output/history-timing.json --rows 100000 --repeats 15</code></pre>
</main></html>'''
    output.write_text(body, encoding='utf-8')
    print(json.dumps({'output': str(output.resolve()), 'result_digests_match': True,
                      'regression_tests': int(counts[-1])}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('before', 'after', 'output', 'verification'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    compare(args.before, args.after, args.output, args.verification)
