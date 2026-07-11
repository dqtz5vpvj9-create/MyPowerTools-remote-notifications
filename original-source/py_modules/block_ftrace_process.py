import argparse
import os
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import json
import matplotlib.pyplot as plt
import re

query_block = """
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

# tp = TraceProcessor(addr='127.0.0.1:9001') # 从自己启动的服务器读取trace数据，启动时设置了分析的文件
from py_modules.lib_aosp_base import aosp_host_working_dir
trace_path = f"{aosp_host_working_dir}/aiagent_test/multiapp/agent_20250714_163824/ctrip.android.view/xiechengsequential_1_direc_2025-07-14-16-38-25/perfetto-ctrip.android.view-8777-1752482305.765387.trace"
tp = TraceProcessor(
    trace=trace_path,
    config=TraceProcessorConfig(bin_path=os.path.expanduser(os.environ.get('TRACE_PROCESSOR_SHELL', '~/repo/perfetto/out/linux_v50.1_release/trace_processor_shell')), ingest_ftrace_in_raw=True)
)
def query_result(query: str):
    try:
        result = tp.query(query)
    finally:
        tp.close()
    return result.as_pandas_dataframe()

def parse_block_args(args_str):
    """解析 block 事件的 args 字段"""
    pattern = r'.*: dev=(\d+) sector=(\d+) nr_sector=(\d+) (?:bytes=(\d+) )?rwbs=(\S+)'
    match = re.match(pattern, args_str)
    if match:
        dev, sector, nr_sector, bytes, rwbs= match.groups()
        return dev, sector, nr_sector, rwbs
    else:
        print(f"无法解析 args 字段: {args_str}")
        return None, None, None, None

def pair_requests(df, process=None):
    """配对 block_rq_issue 和 block_rq_complete 事件并计算延迟"""
    df = df[df['name'].isin(['block_rq_issue', 'block_rq_complete'])].copy()
    df[['dev', 'sector', 'nr_sector', 'rwbs']] = df['args'].apply(
        lambda x: pd.Series(parse_block_args(x))
    )
    df['ts'] = pd.to_numeric(df['ts'])
    df['key'] = df['dev'].astype(str) + '_' + df['sector'].astype(str) + '_' + df['nr_sector'].astype(str) + '_' + df['rwbs'].astype(str)
    requests = []

    counter_single_issue = 0
    counter_single_complete = 0
    requests_single_issue = []
    requests_single_complete = []
    # 严格配对
    for key, group in df.groupby('key'):
        issues = group[group['name'] == 'block_rq_issue'].sort_values('ts')
        completes = group[group['name'] == 'block_rq_complete'].sort_values('ts')
        if issues.empty:
            counter_single_complete += completes.shape[0]
            requests_single_complete.extend(completes.to_dict('records'))
            continue
        if completes.empty:
            counter_single_issue += issues.shape[0]
            requests_single_issue.extend(issues.to_dict('records'))
            continue
        if not issues.empty and not completes.empty:
            for i in range(len(issues)):
                issue = issues.iloc[i]
                if process is not None and issue['process'] != process:
                    continue
                complete = completes[completes['ts'] > issue['ts']].iloc[0] if any(completes['ts'] > issue['ts']) else None
                if complete is not None:
                    latency = float(complete['ts'] - issue['ts'])  # 纳秒
                    requests.append({
                        'issue_id': issue['id'],
                        'key': key,
                        'issue_ts': issue['ts'],
                        'complete_ts': complete['ts'],
                        'latency_ns': latency,
                        'latency_ms': latency / 1000000,
                        'cpu': issue['cpu'],
                        'thread_issue': issue['thread'],
                        'thread_complete': complete['thread'],
                        'process_issue': issue['process'],
                        'process_complete': complete['process'],
                        'upid_issue': issue['upid'],
                        'upid_complete': complete['upid'],
                        'dev': issue['dev'],
                        'sector': issue['sector'],
                        'nr_sector': issue['nr_sector'],
                        'rwbs': issue['rwbs'],
                        'data_size': (int(issue['nr_sector']) * 512) if issue['nr_sector'] else None  # 512字节/扇区
                    })

    print(f"单独的 block_rq_issue 事件数量: {counter_single_issue}")
    print(f"单独的 block_rq_complete 事件数量: {counter_single_complete}")

    if requests_single_issue:
        tmp = pd.DataFrame(requests_single_issue)
        tmp.to_csv('block_rq_issue_only.csv', index=False)
    if requests_single_complete:
        tmp = pd.DataFrame(requests_single_complete)
        tmp.to_csv('block_rq_complete_only.csv', index=False)

    return pd.DataFrame(requests)

def analyze_block_requests(df):
    """分析 block 请求的延迟"""
    if df.empty:
        print("未找到任何配对的 block 请求事件")
        return
    
    # 基本统计信息
    print("\n===== Block 请求延迟统计 =====")
    print(f"总请求数: {len(df)}")
    print(f"平均延迟: {df['latency_ms'].mean():.3f} ms")
    print(f"最小延迟: {df['latency_ms'].min():.3f} ms")
    print(f"最大延迟: {df['latency_ms'].max():.3f} ms")
    print(f"中位数延迟: {df['latency_ms'].median():.3f} ms")
    print(f"95% 延迟: {df['latency_ms'].quantile(0.95):.3f} ms")
    
    # 按设备统计
    if 'dev' in df and not df['dev'].isnull().all():
        print("\n===== 按设备统计延迟 =====")
        dev_stats = df.groupby('dev')['latency_ms'].agg(['count', 'min', 'max', 'mean', 'median'])
        print(dev_stats.sort_values('count', ascending=False))
    
    # 按读写类型统计
    if 'rwbs' in df and not df['rwbs'].isnull().all():
        print("\n===== 按 I/O 类型统计延迟 =====")
        rwbs_stats = df.groupby('rwbs')['latency_ms'].agg(['count', 'min', 'max', 'mean', 'median'])
        print(rwbs_stats.sort_values('count', ascending=False))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Block Ftrace Processor")
    parser.add_argument('--tag', type=str, required=True, help='Tag') # 输出文件的后缀
    parser.add_argument('--process', type=str, required=False, help='Process name', default=None) # 是否仅分析单个进程（建议不要设置，设为None即可）
    args = parser.parse_args()
    tag = args.tag
    process = args.process

    df = query_result(query_block)
    if df.empty:
        print("未找到符合条件的 block 事件")
        exit(0)
    print(f"事件类型分布: \n{df['name'].value_counts()}")
    
    print("\n正在配对 block 请求...")
    requests_df = pair_requests(df, process)
    
    if requests_df.empty:
        print("未找到任何配对的 block 请求事件")
        exit(0)
        
    print(f"成功配对 {len(requests_df)} 个 block 请求")
    
    # 步骤3: 分析结果
    analyze_block_requests(requests_df)
    
    # (可选) 保存结果到CSV文件
    requests_df.to_csv(f'block_requests_{tag}.csv', index=False)
    print(f"\n分析结果已保存到 block_requests_{tag}.csv")