#!/usr/bin/env python3
"""GPU Monitor 基础测试 —— 纯标准库, 无需 GPU / NVML / pytest。

运行方式:
    python tests/test_core.py
"""
import os
import sys
import tempfile
import time

# 将项目根目录加入导入路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import gpu_monitor as gm

FAILS = []


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 1. Meter 累计逻辑 (跨重启核心)
# ---------------------------------------------------------------------------
def test_meter():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        m = gm.Meter(path, rate=0.60)
        m.reset_energy()  # 清掉可能残留
        m.add_gpu(10.0)
        m.add_cpu(5.0)
        snap = m.snapshot()
        check("Meter GPU 累计", abs(snap["gpu_energy_wh"] - 10.0) < 1e-6)
        check("Meter CPU 累计", abs(snap["cpu_energy_wh"] - 5.0) < 1e-6)
        check("Meter 总计", abs(snap["total_energy_wh"] - 15.0) < 1e-6)
        # 电费 = 总度数(kWh) * 电价 = 15/1000 * 0.60 = 0.009
        check("Meter 电费计算", abs(snap["elec_cost_yuan"] - 0.009) < 1e-9)
        # 每日明细
        today = time.strftime("%Y-%m-%d")
        check("Meter 每日明细", abs(snap["daily"].get(today, 0) - 15.0) < 1e-6)
        # 负增量忽略
        m.add_gpu(-100.0)
        check("Meter 忽略负增量", abs(m.snapshot()["gpu_energy_wh"] - 10.0) < 1e-6)
        # 改电价后电费重算
        m.set_rate(1.0)
        check("Meter 改电价", abs(m.snapshot()["elec_cost_yuan"] - 0.015) < 1e-9)
        # reset 保留电价
        m.reset_energy()
        s2 = m.snapshot()
        check("Meter reset 清零能量", s2["total_energy_wh"] == 0.0)
        check("Meter reset 保留电价", s2["elec_rate"] == 1.0)
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# 2. Meter 跨重启 (落盘后重新加载)
# ---------------------------------------------------------------------------
def test_meter_persist():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        m1 = gm.Meter(path, rate=0.6)
        m1.reset_energy()
        m1.add_gpu(100.0)
        m1.save(force=True)  # 模拟优雅退出时的强制落盘
        m2 = gm.Meter(path, rate=0.6)  # 重新加载
        check("Meter 跨重启读取", abs(m2.snapshot()["gpu_energy_wh"] - 100.0) < 1e-6)
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# 3. History (SQLite 落盘 + 范围查询 + 裁剪)
# ---------------------------------------------------------------------------
def test_history():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    h = None
    try:
        h = gm.History(path, interval=5.0, retention_days=30)
        now = time.time()
        # 插入一条当前样本
        h.add(gpu_pw=120.0, gpu_util=50.0, gpu_temp=70.0, gpu_e_wh=1.0,
               cpu_util=10.0, cpu_pw=30.0, cpu_e_wh=0.5,
               mem_pct=40.0, total_pw=150.0, total_e_wh=1.5, total_cost=0.001)
        q = h.query("24h")
        check("History 24h 查询返回", len(q) == 1)
        if q:
            check("History 字段映射", q[0]["gpu_pw"] == 120.0 and q[0]["total_cost"] == 0.001)
        # 插入一条很久以前的样本, 应被 query 过滤 (samples 表现在 16 列, 需补满)
        old = now - 100 * 86400.0
        h.conn.execute(
            "INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (old, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1))
        h.conn.commit()
        check("History 范围过滤旧样本", len(h.query("24h")) == 1)
        # 裁剪: 超过 retention_days 的应被删除
        h.prune()
        check("History 裁剪生效", len(h.query("30d")) == 1)

        # ---- 降采样归档 (新) ----
        # 注意: History.add() 固定用 time.time() 打时间戳, 必须用裸 SQL 显式写入历史 ts
        # 才能构造"跨越多天的旧数据"。下面插入最近 30 天、step=300s 的样本。
        h2 = gm.History(path + ".ds", interval=5.0, retention_days=30)
        now = time.time()
        step = 300
        span_days = 30
        n_recent = 0  # 最近 7 天内的样本数(应保留原始)
        n_old = 0     # 7~30 天前的样本数(应被聚合)
        total_span = span_days * 86400  # 累计值随时间递增: 越新(ago 越小)累计越大
        for ago in range(0, span_days * 86400, step):
            ts = now - ago
            if ago >= 7 * 86400:
                n_old += 1
            else:
                n_recent += 1
            h2.conn.execute(
                "INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, 100.0 + (ago % 50) * 0.1, 50.0, 70.0,
                 float(total_span - ago),                          # gpu_e_wh 随时间长增
                 10.0, 30.0, float((total_span - ago) * 0.5),      # cpu_e_wh
                 40.0, 130.0,
                 float((total_span - ago) * 1.5),                  # total_e_wh
                 float((total_span - ago) * 0.001),               # total_cost
                 1.0, 2.0, 3.0, 4.0))
        h2.conn.commit()
        raw_before = h2.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        h2.downsample()
        raw_after = h2.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        agg_cnt = h2.conn.execute("SELECT COUNT(*) FROM samples_agg").fetchone()[0]
        check("降采样后原始表只留最近 full_res_days 天", raw_after <= n_recent + 5)
        check("降采样后归档表有粗桶(旧数据)", agg_cnt > 100)  # 23 天 * 24 ≈ 552 桶
        check("降采样确实删除了旧原始行", raw_after < raw_before)
        # 归档桶数 ≈ (30-7)天 * 24 小时
        check("归档桶数≈(30-7)天*24", abs(agg_cnt - (23 * 24)) <= 3)
        # 合并查询: 30d 视图应覆盖到约 retention 起点, 且 ts 单调递增、无重复
        series = h2.query("30d")
        ts_list = [r["ts"] for r in series]
        check("合并查询 ts 单调递增", all(ts_list[i] < ts_list[i + 1] for i in range(len(ts_list) - 1)))
        check("合并查询覆盖到约 30 天前", ts_list and ts_list[0] <= now - 29 * 86400)
        # 累计能耗在合并序列中单调不减(桶起点值使相邻桶边界严丝合缝)
        ewh = [r["total_e_wh"] for r in series]
        mono = all(ewh[i] <= ewh[i + 1] + 1e-6 for i in range(len(ewh) - 1))
        check("累计能耗在合并序列中单调不减", mono)
        # 幂等: 再 downsample 一次不应改变归档桶数 / 原始行数
        h2.downsample()
        check("降采样幂等(归档桶数不变)", h2.conn.execute("SELECT COUNT(*) FROM samples_agg").fetchone()[0] == agg_cnt)
        check("降采样幂等(原始行数不变)", h2.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == raw_after)
        # 导出全量也应合并两张表
        all_rows = h2.rows("all")
        check("rows('all') 合并两张表(>原始仅全分辨率)", len(all_rows) > raw_after)
        h2.close()
        try:
            os.remove(path + ".ds")
        except Exception:
            pass
    finally:
        if h is not None:
            try:
                h.close()
            except Exception:
                pass
        os.remove(path)


# ---------------------------------------------------------------------------
# 4. 工具函数
# ---------------------------------------------------------------------------
def test_helpers():
    check("decode_throttle 空", gm.decode_throttle(0) == [])
    # 单原因
    one = gm.decode_throttle(0x1)
    check("decode_throttle 单原因", isinstance(one, list) and len(one) == 1)
    # 多原因 (0x8 | 0x20) 应解出 2 条
    multi = gm.decode_throttle(0x8 | 0x20)
    check("decode_throttle 多原因数量", len(multi) == 2)


if __name__ == "__main__":
    print("== GPU Monitor 基础测试 ==")
    test_meter()
    test_meter_persist()
    test_history()
    test_helpers()
    print("=======================")
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("全部通过 ✓")
