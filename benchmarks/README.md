# RTDB 基准测试

`benchmark_core.py` 使用当前公开的 `Table` 接口，对固定数据规模测试：

- 单行条件查询；
- 单行条件修改；
- 批量新增；
- 4 进程并发追加；
- 8 线程混合读写。

从仓库根目录运行：

```powershell
python benchmarks\benchmark_core.py --json
```

结果中的 `seconds` 是一次完整工作负载的耗时。比较优化前后时，应使用
相同 Python 版本、相同磁盘和相同参数；加速比计算为：

```text
优化前耗时 / 优化后耗时
```
