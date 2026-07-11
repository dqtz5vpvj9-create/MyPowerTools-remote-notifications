import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import matplotlib.pyplot as plt
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig


QUERY_BLOCK = """
SELECT 
    ftrace_event.id as id, 
    ftrace_event.ts as ts, 
    ftrace_event.name as name, 
    ftrace_event.cpu as cpu, 
    thread.name as thread, 
    process.name as process, 
    process.upid as upid,
    to_ftrace(ftrace_event.id) as args 
FROM ftrace_event 
JOIN thread USING (utid) 
LEFT JOIN process ON thread.upid = process.upid 
WHERE 
    ftrace_event.name LIKE 'block%'
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate combined IO throughput and per-second sequential/random ratio plot from Perfetto trace with optional wall-clock anchoring"
    )
    parser.add_argument("--trace", required=True, help="Path to perfetto trace file")
    parser.add_argument("--anchor-wall-time", required=True, help="Wall clock time string present in android_logs (HH:MM:SS.mmm)")
    parser.add_argument("--time-limit", type=int, default=None, help="Seconds to include from anchor (default: full trace)")
    parser.add_argument("--threshold", type=int, default=0, help="Sector continuity threshold for sequential classification (default 0 strict)")
    parser.add_argument("--bin-path", default=os.path.expanduser(os.environ.get('TRACE_PROCESSOR_SHELL', '~/repo/perfetto/out/linux_v50.1_release/trace_processor_shell')), help="trace_processor_shell binary path")
    parser.add_argument("--output", default="trace_io_seq_random_plot.pdf", help="Output PDF filename")
    parser.add_argument("--max-log-rows", type=int, default=500000, help="Safety cap for android_logs rows scanned")
    parser.add_argument("--app-launch-times", type=str, default=None, help="Comma-separated list of app launch completion times in seconds (e.g. '7.93,10.24,10.25,15.67')")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser.parse_args()


def open_trace(trace_path: str, bin_path: str) -> TraceProcessor:
    if not os.path.exists(trace_path):
        print(f"Error: trace file not found: {trace_path}")
        sys.exit(1)
    if not os.path.exists(bin_path):
        print(f"Error: trace processor binary not found: {bin_path}")
        sys.exit(1)
    return TraceProcessor(trace=trace_path, config=TraceProcessorConfig(bin_path=bin_path, ingest_ftrace_in_raw=True))


def get_cache_path(trace_path: str, anchor_wall_time: str, time_limit: Optional[int], threshold: int) -> str:
    """
    根据输入参数生成唯一的缓存文件路径。
    缓存文件存放在 trace 文件同目录下。
    """
    # 使用 trace 文件的绝对路径 + 关键参数生成 hash
    trace_abs = os.path.abspath(trace_path)
    cache_key = f"{trace_abs}|{anchor_wall_time}|{time_limit}|{threshold}"
    cache_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    
    # 缓存文件名：trace文件名_参数hash.cache.json
    trace_dir = os.path.dirname(trace_abs)
    trace_name = os.path.basename(trace_abs)
    cache_name = f"{trace_name}_{cache_hash}.io_cache.json"
    return os.path.join(trace_dir, cache_name)


def save_cache(cache_path: str, ts_df: pd.DataFrame, cpu_df: pd.DataFrame, verbose: bool = False):
    """保存数据到缓存文件"""
    cache_data = {
        'ts_df': ts_df.to_dict(orient='records'),
        'cpu_df': cpu_df.to_dict(orient='records')
    }
    with open(cache_path, 'w') as f:
        json.dump(cache_data, f)
    if verbose:
        print(f"Cache saved to {cache_path}")


def load_cache(cache_path: str, verbose: bool = False) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """从缓存文件加载数据"""
    with open(cache_path, 'r') as f:
        cache_data = json.load(f)
    ts_df = pd.DataFrame(cache_data['ts_df'])
    cpu_df = pd.DataFrame(cache_data['cpu_df'])
    if verbose:
        print(f"Loaded cache from {cache_path}")
        print(f"  ts_df: {len(ts_df)} rows, cpu_df: {len(cpu_df)} rows")
    return ts_df, cpu_df


def query_block_events(tp: TraceProcessor) -> pd.DataFrame:
    result = tp.query(QUERY_BLOCK)
    return result.as_pandas_dataframe()


def get_clock_snapshot(tp: TraceProcessor) -> Tuple[int, int, int]:
    df = tp.query('SELECT clock_name, clock_value FROM clock_snapshot').as_pandas_dataframe()
    if df.empty:
        raise RuntimeError('clock_snapshot table empty')
    # fallback to first occurrence of each
    def val(name):
        rows = df[df['clock_name'] == name]
        if rows.empty:
            raise RuntimeError(f'Clock {name} not found in snapshot')
        return int(rows['clock_value'].iloc[0])
    monotonic = val('MONOTONIC')
    realtime = val('REALTIME')
    boottime = val('BOOTTIME')
    return monotonic, realtime, boottime


def query_android_logs(tp: TraceProcessor, limit: int) -> pd.DataFrame:
    # Fetch android_logs for anchor matching
    q = f"SELECT id, ts, utid, prio, tag, msg FROM android_logs ORDER BY ts LIMIT {int(limit)}"
    return tp.query(q).as_pandas_dataframe()


def convert_ts_to_realtime(raw_ts: int, boottime_ts: int, realtime_ts: int) -> int:
    # Same logic as export_android_logs: shift BOOTTIME domain to REALTIME
    return raw_ts - boottime_ts + realtime_ts


def find_anchor_realtime_ns(log_df: pd.DataFrame, anchor_wall_time: str, boottime_ts: int, realtime_ts: int, verbose=False) -> Optional[int]:
    # anchor_wall_time format HH:MM:SS.mmm
    anchor_pattern = anchor_wall_time.strip()
    # Convert logs to formatted times
    candidates: List[int] = []
    for row in log_df.itertuples():
        rt_ns = convert_ts_to_realtime(int(row.ts), boottime_ts, realtime_ts)
        dt = datetime.fromtimestamp(rt_ns / 1e9)
        wall = dt.strftime('%H:%M:%S.%f')[:-3]  # milliseconds precision
        if wall == anchor_pattern:
            candidates.append(rt_ns)
    if not candidates:
        if verbose:
            print(f"Anchor time {anchor_pattern} not found in first {len(log_df)} log rows")
        return None
    anchor_ns = min(candidates)  # choose earliest if multiple
    if verbose:
        print(f"Anchor matched at realtime ns={anchor_ns}")
    return anchor_ns


ARGS_PATTERN = r'.*: dev=(\d+) sector=(\d+) nr_sector=(\d+) (?:bytes=(\d+) )?rwbs=(\S+)'


def parse_args_field(args_str: str):
    m = re.match(ARGS_PATTERN, args_str)
    if not m:
        return None, None, None, None
    dev, sector, nr_sector, bytes_val, rwbs = m.groups()
    return dev, sector, nr_sector, rwbs


def pair_requests(df: pd.DataFrame, verbose=False) -> pd.DataFrame:
    df = df[df['name'].isin(['block_rq_issue', 'block_rq_complete'])].copy()
    if df.empty:
        return pd.DataFrame()
    df[['dev', 'sector', 'nr_sector', 'rwbs']] = df['args'].apply(lambda x: pd.Series(parse_args_field(x)))
    df['ts'] = pd.to_numeric(df['ts'])
    df['key'] = df['dev'].astype(str) + '_' + df['sector'].astype(str) + '_' + df['nr_sector'].astype(str) + '_' + df['rwbs'].astype(str)

    requests: List[Dict] = []
    for key, group in df.groupby('key'):
        issues = group[group['name'] == 'block_rq_issue'].sort_values('ts')
        completes = group[group['name'] == 'block_rq_complete'].sort_values('ts')
        if issues.empty or completes.empty:
            continue
        for issue in issues.itertuples():
            later = completes[completes['ts'] > issue.ts]
            if later.empty:
                continue
            complete = later.iloc[0]
            latency_ns = float(complete.ts - issue.ts)
            nr_sectors = int(issue.nr_sector) if issue.nr_sector else 0
            data_size = nr_sectors * 512
            requests.append({
                'issue_ts': issue.ts,
                'complete_ts': complete.ts,
                'latency_ns': latency_ns,
                'latency_ms': latency_ns / 1e6,
                'dev': issue.dev,
                'sector': int(issue.sector) if issue.sector else -1,
                'nr_sector': nr_sectors,
                'rwbs': issue.rwbs,
                'bytes': data_size
            })
    req_df = pd.DataFrame(requests)
    if verbose:
        print(f"Paired {len(req_df)} requests")
    return req_df


def classify_sequential(req_df: pd.DataFrame, threshold: int) -> pd.DataFrame:
    if req_df.empty:
        return req_df
    req_df = req_df.sort_values(['dev', 'issue_ts']).copy()
    last_end_sector: Dict[str, int] = {}
    seq_flags: List[bool] = []
    for row in req_df.itertuples():
        dev = str(row.dev)
        start_sector = int(row.sector)
        length = int(row.nr_sector)
        prev_end = last_end_sector.get(dev)
        is_seq = False
        if prev_end is not None:
            # strictly contiguous or within threshold distance
            if abs(start_sector - prev_end) <= threshold:
                is_seq = True
        last_end_sector[dev] = start_sector + length
        seq_flags.append(is_seq)
    req_df['is_sequential'] = seq_flags
    return req_df


def build_time_series(req_df: pd.DataFrame, anchor_realtime_ns: int, boottime_ts: int, realtime_ts: int, limit_seconds: int = None, verbose=False) -> pd.DataFrame:
    if req_df.empty or anchor_realtime_ns is None:
        return pd.DataFrame(columns=['second', 'read_mb_s', 'write_mb_s', 'random_ratio'])
    # Convert issue timestamps to realtime then compute relative seconds from anchor
    issue_rt_ns = req_df['issue_ts'].apply(lambda x: convert_ts_to_realtime(int(x), boottime_ts, realtime_ts))
    req_df = req_df.assign(issue_rt_ns=issue_rt_ns)
    req_df['relative_sec'] = (req_df['issue_rt_ns'] - anchor_realtime_ns) / 1e9
    req_df = req_df[req_df['relative_sec'] >= 0]
    if limit_seconds is not None:
        req_df = req_df[req_df['relative_sec'] <= limit_seconds + 1]
    if req_df.empty:
        return pd.DataFrame(columns=['second', 'read_mb_s', 'write_mb_s', 'random_ratio'])
    req_df['second'] = req_df['relative_sec'].astype(int)
    agg_rows = []
    for sec, group in req_df.groupby('second'):
        total_bytes_read = group[group['rwbs'].str.contains('R')]['bytes'].sum()
        total_bytes_write = group[group['rwbs'].str.contains('W')]['bytes'].sum()
        total_requests = len(group)
        seq_requests = group[group['is_sequential']].shape[0]
        rand_requests = total_requests - seq_requests
        read_mb_s = total_bytes_read / (1024 * 1024)
        write_mb_s = total_bytes_write / (1024 * 1024)
        rand_ratio = rand_requests / total_requests if total_requests else 0
        agg_rows.append({
            'second': sec,
            'read_mb_s': read_mb_s,
            'write_mb_s': write_mb_s,
            'random_ratio': rand_ratio
        })
    out_df = pd.DataFrame(agg_rows).sort_values('second')
    if verbose:
        print(out_df.head())
    return out_df


def compute_cpu_usage(tp: TraceProcessor, anchor_realtime_ns: int, boottime_ts: int, realtime_ts: int, time_limit: int = None, cpu_list: List[int] = None, verbose=False) -> pd.DataFrame:
    # Per-second CPU idle rate on specified CPUs (default 2-5)
    # CPU idle rate = idle_time / num_cpus (as percentage of total capacity)
    if cpu_list is None:
        cpu_list = [2, 3, 4, 5]  # Default to CPUs 2-5
    num_cpus = len(cpu_list)
    
    cpu_filter = f"AND sched_slice.cpu IN ({','.join(str(c) for c in cpu_list)})"
    
    query = f"""
    SELECT
        sched_slice.ts AS ts,
        sched_slice.dur AS dur,
        sched_slice.cpu AS cpu,
        COALESCE(process.name, 'idle') AS process_name
    FROM sched_slice
    LEFT JOIN thread USING(utid)
    LEFT JOIN process USING(upid)
    WHERE sched_slice.dur != -1
    {cpu_filter}
    ORDER BY sched_slice.ts
    """
    df = tp.query(query).as_pandas_dataframe()
    if df.empty:
        return pd.DataFrame(columns=['second', 'cpu_idle_rate'])
    
    df['ts'] = pd.to_numeric(df['ts'])
    df['dur'] = pd.to_numeric(df['dur'])
    
    # Convert to realtime
    df['ts_rt'] = df['ts'].apply(lambda x: convert_ts_to_realtime(int(x), boottime_ts, realtime_ts))
    df['relative_sec'] = (df['ts_rt'] - anchor_realtime_ns) / 1e9
    
    # Filter to time window
    df = df[df['relative_sec'] >= 0]
    if time_limit is not None:
        df = df[df['relative_sec'] <= time_limit + 1]
    if df.empty:
        return pd.DataFrame(columns=['second', 'cpu_idle_rate'])
    
    df['second'] = df['relative_sec'].astype(int)
    
    # Get idle time per second (only idle process)
    idle_df = df[df['process_name'] == 'idle']
    idle_per_sec = idle_df.groupby('second')['dur'].sum().reset_index()
    idle_per_sec.columns = ['second', 'idle_ns']
    
    # Convert to seconds and calculate idle rate
    # idle_rate = idle_time_seconds / num_cpus (max 1.0 per CPU per second)
    idle_per_sec['idle_sec'] = idle_per_sec['idle_ns'] / 1e9
    idle_per_sec['cpu_idle_rate'] = idle_per_sec['idle_sec'] / num_cpus
    # Clamp to [0, 1]
    idle_per_sec['cpu_idle_rate'] = idle_per_sec['cpu_idle_rate'].clip(0, 1)
    
    if verbose:
        print(f"CPU idle rate on CPUs {cpu_list} per second (first 5):")
        print(idle_per_sec[['second', 'cpu_idle_rate']].head())
    
    return idle_per_sec[['second', 'cpu_idle_rate']]


def _draw_io_cpu_plot(ax1, ts_df: pd.DataFrame, cpu_df: pd.DataFrame, 
                      xlim_start: float, xlim_end: float,
                      app_launch_times: List[float] = None,
                      show_labels: bool = True,
                      fontsize: int = 12,
                      tick_fontsize: int = 10):
    """
    通用绘图函数：在给定的axes上绘制IO吞吐和CPU Usage曲线。
    
    Parameters:
    - ax1: matplotlib axes (主轴，用于IO吞吐)
    - ts_df: IO时间序列数据
    - cpu_df: CPU idle rate数据 (会转换为 usage = 1 - idle)
    - xlim_start, xlim_end: x轴范围
    - app_launch_times: 应用启动时间列表（绿色竖线）
    - show_labels: 是否显示轴标签
    - fontsize: 标签字体大小
    - tick_fontsize: 刻度字体大小
    
    Returns:
    - lines: 绑定的线条对象列表
    - labels: 对应的标签列表
    - ax2: 次轴对象
    """
    # Filter data to xlim range
    ts_plot = ts_df[(ts_df['second'] >= xlim_start) & (ts_df['second'] <= xlim_end)]
    cpu_plot = cpu_df[(cpu_df['second'] >= xlim_start) & (cpu_df['second'] <= xlim_end)].copy()
    
    # Convert idle rate to usage rate (1 - idle)
    cpu_plot['cpu_usage'] = 1 - cpu_plot['cpu_idle_rate']
    
    # Primary axis - IO throughput
    if show_labels:
        ax1.set_xlabel('Time (s)', fontsize=fontsize)
        ax1.set_ylabel('IO Throughput (MB/s)', fontsize=fontsize)
    l1, = ax1.plot(ts_plot['second'], ts_plot['read_mb_s'], label='IO Read Throughput (MB/s)', color='tab:blue')
    l2, = ax1.plot(ts_plot['second'], ts_plot['write_mb_s'], label='IO Write Throughput (MB/s)', color='tab:orange', linestyle='--')
    ax1.tick_params(axis='y', labelsize=tick_fontsize)
    ax1.tick_params(axis='x', labelsize=tick_fontsize)
    ax1.set_xlim(xlim_start, xlim_end)
    ax1.grid(True, axis='x', linestyle=':')
    ax1.set_ylim(bottom=0)
    
    # Add vertical lines for app launch times
    v_line = None
    if app_launch_times:
        for t in app_launch_times:
            if xlim_start <= t <= xlim_end:
                v_line = ax1.axvline(x=t, color='green', linestyle=':', linewidth=1.5)
    
    # Secondary y-axis for CPU Usage (1 - idle)
    ax2 = ax1.twinx()
    if show_labels:
        ax2.set_ylabel('CPU Usage (%)', color='tab:red', fontsize=fontsize)
    l3, = ax2.plot(cpu_plot['second'], cpu_plot['cpu_usage'] * 100, label='CPU Usage', color='tab:red', linestyle='-.')
    ax2.tick_params(axis='y', labelcolor='tab:red', labelsize=tick_fontsize)
    ax2.set_ylim(0, 100)
    
    # Collect lines and labels for legend (caller decides where to place)
    lines = [l1, l2, l3]
    labels = [x.get_label() for x in lines]
    if v_line:
        lines.append(v_line)
        labels.append('APP Launch Completed')
    
    return lines, labels, ax2


def plot_combined(ts_df: pd.DataFrame, cpu_df: pd.DataFrame, output_file: str, app_launch_times: List[float] = None):
    if ts_df.empty and cpu_df.empty:
        print("No data to plot.")
        return
    
    # Determine data range
    max_time = max(ts_df['second'].max() if not ts_df.empty else 0,
                   cpu_df['second'].max() if not cpu_df.empty else 0)
    
    # Determine zoom range: up to last app launch time + some margin, or first 50s
    if app_launch_times and len(app_launch_times) > 0:
        zoom_end = max(app_launch_times) + 5  # 5s margin after last app launch
    else:
        zoom_end = min(50, max_time)
    
    # Create figure
    fig, ax1 = plt.subplots(figsize=(8, 4))
    
    # Main plot - full time range
    lines, labels, ax2 = _draw_io_cpu_plot(
        ax1, ts_df, cpu_df,
        xlim_start=0, xlim_end=max_time,
        app_launch_times=app_launch_times,
        show_labels=True,
        fontsize=12,
        tick_fontsize=10
    )
    
    # Place legend outside the plot (above), with more space
    fig.legend(lines, labels, loc='upper center', bbox_to_anchor=(0.5, 1.08), 
               ncol=len(labels) // 2, frameon=True, fontsize=10)
    
    plt.title('IO Throughput Over Time', y=1.02)
    
    # Create inset (zoomed view) - positioned in middle-right area
    # [left, bottom, width, height] in figure coordinates
    ax_inset = fig.add_axes([0.45, 0.48, 0.36, 0.36])
    
    # Inset plot - same content, zoomed range, smaller fonts
    _draw_io_cpu_plot(
        ax_inset, ts_df, cpu_df,
        xlim_start=0, xlim_end=zoom_end,
        app_launch_times=app_launch_times,
        show_labels=True,
        fontsize=9,
        tick_fontsize=8
    )
    
    # Mark the zoomed region on main plot with a rectangle
    from matplotlib.patches import Rectangle, ConnectionPatch
    
    # Get y limits from main plot for drawing zoom box
    y_min, y_max = ax1.get_ylim()
    rect = Rectangle((0, y_min), zoom_end, y_max - y_min, 
                      linewidth=1.5, edgecolor='gray', facecolor='none', linestyle='-', alpha=0.6)
    ax1.add_patch(rect)
    
    # Draw connecting lines from main plot zoom region corners to inset corners
    # Top-right of zoom box -> Top-left of inset
    # Bottom-right of zoom box -> Bottom-left of inset
    inset_ymin, inset_ymax = ax_inset.get_ylim()
    inset_xmin, inset_xmax = ax_inset.get_xlim()
    con1 = ConnectionPatch(
        xyA=(zoom_end, y_max), coordsA=ax1.transData,
        xyB=(inset_xmin, inset_ymax), coordsB=ax_inset.transData,
        color='gray', linewidth=1, linestyle='--', alpha=0.6
    )
    con2 = ConnectionPatch(
        xyA=(zoom_end, y_min), coordsA=ax1.transData,
        xyB=(inset_xmin, inset_ymin), coordsB=ax_inset.transData,
        color='gray', linewidth=1, linestyle='--', alpha=0.6
    )
    fig.add_artist(con1)
    fig.add_artist(con2)
    
    plt.savefig(output_file, format='pdf', bbox_inches='tight')
    print(f"Plot saved to {output_file}")


def main():
    args = parse_args()
    
    # 检查缓存是否存在
    cache_path = get_cache_path(args.trace, args.anchor_wall_time, args.time_limit, args.threshold)
    
    if os.path.exists(cache_path):
        # 使用缓存数据
        if args.verbose:
            print(f"Found cache file: {cache_path}")
        ts_df, cpu_df = load_cache(cache_path, verbose=args.verbose)
    else:
        # 从 trace 读取数据
        if args.verbose:
            print("Opening trace...")
        tp = open_trace(args.trace, args.bin_path)
        # Clock snapshot mapping
        try:
            monotonic_ts, realtime_ts, boottime_ts = get_clock_snapshot(tp)
        except Exception as e:
            print(f"Failed to read clock snapshot: {e}")
            tp.close()
            sys.exit(1)
        if args.verbose:
            print(f"Clock snapshot MONOTONIC={monotonic_ts} REALTIME={realtime_ts} BOOTTIME={boottime_ts}")

        # Query logs to find anchor, then push back 1s
        log_df = query_android_logs(tp, args.max_log_rows)
        anchor_ns = find_anchor_realtime_ns(log_df, args.anchor_wall_time, boottime_ts, realtime_ts, verbose=args.verbose)
        if anchor_ns is None:
            print("Anchor wall time not found; aborting.")
            tp.close()
            sys.exit(1)
        # Push anchor 1s earlier to capture IO before the log line
        anchor_ns -= int(1e9)
        if args.verbose:
            print(f"Adjusted anchor (1s earlier): {anchor_ns}")

        # Block events
        df_events = query_block_events(tp)
        if df_events.empty:
            print("No block events found.")
            tp.close()
            sys.exit(0)
        if args.verbose:
            print(f"Total block events: {len(df_events)}")

        req_df = pair_requests(df_events, verbose=args.verbose)
        if req_df.empty:
            print("No paired requests; cannot compute throughput.")
            tp.close()
            sys.exit(0)
        req_df = classify_sequential(req_df, args.threshold)

        ts_df = build_time_series(req_df, anchor_ns, boottime_ts, realtime_ts, args.time_limit, verbose=args.verbose)
        cpu_df = compute_cpu_usage(tp, anchor_ns, boottime_ts, realtime_ts, args.time_limit, verbose=args.verbose)
        tp.close()
        
        # 保存缓存
        save_cache(cache_path, ts_df, cpu_df, verbose=args.verbose)
    
    # Parse app launch times if provided
    app_launch_times = None
    if args.app_launch_times:
        try:
            app_launch_times = [float(t.strip()) for t in args.app_launch_times.split(',')]
            if args.verbose:
                print(f"App launch times: {app_launch_times}")
        except ValueError as e:
            print(f"Warning: Could not parse app_launch_times '{args.app_launch_times}': {e}")

    plot_combined(ts_df, cpu_df, args.output, app_launch_times)


if __name__ == "__main__":
    main()
