import argparse
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from datetime import timedelta


def parse_requests(df: pd.DataFrame):
    df = df.sort_values('complete_ts')
    df['window'] = np.digitize(df['end_sec'], bins)

    df_read = df[df['rwbs'].str.startswith('R')]
    df_write = df[df['rwbs'].str.startswith('W')]

    # 计算每个时间窗口的数据
    throughput = df.groupby('window')['data_size'].sum()
    throughput_read = df_read.groupby('window')['data_size'].sum()
    throughput_write = df_write.groupby('window')['data_size'].sum()
    throughput_cum = throughput.cumsum()
    throughput_cum_read = throughput_read.cumsum()
    throughput_cum_write = throughput_write.cumsum()

    window_total = bins[throughput.index - 1] - min_time
    window_read = bins[throughput_read.index - 1] - min_time
    window_write = bins[throughput_write.index - 1] - min_time

    throughput_mbps = throughput.values / (window_size * 1e6)  # 转换为 MB/s
    throughput_read_mbps = throughput_read.values / (window_size * 1e6)  # 转换为 MB/s
    throughput_write_mbps = throughput_write.values / (window_size * 1e6)
    throughput_cum_mb = throughput_cum.values / (1e6)  # 转换为 MB
    throughput_cum_read_mb = throughput_cum_read.values / (1e6)  # 转换为 MB
    throughput_cum_write_mb = throughput_cum_write.values / (1e6)

    return window_total, throughput_mbps, throughput_cum_mb, \
            window_read, throughput_read_mbps, throughput_cum_read_mb, \
            window_write, throughput_write_mbps, throughput_cum_write_mb


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Block Ftrace Processor")
    parser.add_argument('--tag', type=str, required=True, help='Tag') # 输入csv文件的后缀
    parser.add_argument('--process', type=str, nargs='+', help='Processes', default=[]) # 图中绘制的进程list
    args = parser.parse_args()
    tag = args.tag
    processes = args.process
    print(processes)

    # 读取数据
    df = pd.read_csv(f'block_requests_{tag}.csv')

    df['start_sec'] = df['issue_ts'] / 1e9
    df['end_sec'] = df['complete_ts'] / 1e9
    min_time = df['start_sec'].min()
    max_time = df['end_sec'].max()
    window_size = 0.1  # 100ms
    bins = np.arange(min_time, max_time + window_size, window_size)

    df_processes = []
    for process in processes:
        df_processes.append(df[df['process_issue'] == process])
    
    df_processes.append(df[~df['process_issue'].isin(processes)])
    processes.append('others')
    
    x_all_total, y_tp_all_total, y_cum_all_total, x_all_read, y_tp_all_read, y_cum_all_read, x_all_write, y_tp_all_write, y_cum_all_write = parse_requests(df)

    res_process = []
    for df_process in df_processes:
        res_process.append(parse_requests(df_process))

    plt.figure(figsize=(10, 6))

    # 吞吐量
    plt.plot(x_all_total, y_tp_all_total, 'ro-', markersize=1, linewidth=1, label='Total Throughput')
    plt.plot(x_all_read, y_tp_all_read, 'go-', markersize=1, linewidth=1, label='Read Throughput', alpha=0.3)
    plt.plot(x_all_write, y_tp_all_write, 'bo-', markersize=1, linewidth=1, label='Write Throughput', alpha=0.3)
    plt.title(f'IO Throughput')
    plt.ylabel('Throughput (MB/s)')
    plt.grid(axis='y', linestyle='--')
    plt.legend(loc='best')
    # 请求数
    # request_count = df.groupby('window').size()
    # ax2 = plt.twinx()
    # ax2.plot(window_total, request_count, 'bo-', markersize=2, linewidth=1, alpha=0.5)
    # ax2.set_ylabel('Total Number of Completed Block Requests', color='b')
    plt.tight_layout()
    plt.savefig(f'io_throughput_{tag}.png', dpi=300)
    plt.show()

    # 吞吐量分类
    if len(processes) > 0:
        plt.figure(figsize=(10, 6))
        plt.plot(x_all_total, y_tp_all_total, markersize=1, linewidth=1, label='Total Throughput of All Apps')
        for i in range(len(processes)):
            x_process_total, y_tp_process_total, _, _, _, _, _, _, _ = res_process[i]
            plt.plot(x_process_total, y_tp_process_total, markersize=1, linewidth=1, label=f'{processes[i]}', alpha=0.5)
        plt.title(f'IO Throughput of Apps')
        plt.ylabel('Throughput (MB/s)')
        plt.grid(axis='y', linestyle='--')
        plt.legend(loc='best')
        plt.tight_layout()
        plt.savefig(f'io_throuput_process_{tag}.png', dpi=300)
        plt.show()

    # 数据量
    plt.figure(figsize=(10, 6))
    plt.plot(x_all_total, y_cum_all_total, 'ro-', markersize=1, linewidth=1, label='Total Bytes')
    plt.plot(x_all_read, y_cum_all_read, 'go-', markersize=1, linewidth=1, label='Read Bytes', alpha=0.3)
    plt.plot(x_all_write, y_cum_all_write, 'bo-', markersize=1, linewidth=1, label='Write Bytes', alpha=0.3)
    plt.title(f'Cumulative IO Bytes')
    plt.ylabel('Total Bytes (MB)')
    plt.grid(axis='y', linestyle='--')
    plt.legend(loc='best')
    plt.tight_layout()
    plt.savefig(f'io_cumbytes_{tag}.png', dpi=300)
    plt.show()

    # 数据量分类
    if len(processes) > 0:
        plt.figure(figsize=(10, 6))
        plt.plot(x_all_total, y_cum_all_total, markersize=1, linewidth=1, label='Total Bytes of All Apps')
        for i in range(len(processes)):
            x_process_total, _, y_cum_process_total, x_process_read, _, y_cum_process_read, x_process_write, _, y_cum_process_write = res_process[i]
            plt.plot(x_process_total, y_cum_process_total, markersize=1, linewidth=1, label=f'{processes[i]}', alpha=0.5)
            if len(processes) == 2 and i == 0:
                plt.plot(x_process_read, y_cum_process_read, markersize=1, linewidth=1, label=f'{processes[i]} Read', alpha=0.5)
                plt.plot(x_process_write, y_cum_process_write, markersize=1, linewidth=1, label=f'{processes[i]} Write', alpha=0.5)
        plt.title(f'Cumulative IO Bytes of Apps')
        plt.ylabel('Total Bytes (MB)')
        plt.grid(axis='y', linestyle='--')
        plt.legend(loc='best')
        plt.tight_layout()
        plt.savefig(f'io_cumbytes_process_{tag}.png', dpi=300)
        plt.show()

    print("Ohter Processes:")
    df_other = df_processes[-1]
    other_bytes_df = (
        df_other
        .groupby('process_issue')['data_size']
        .sum()
        .reset_index()
    )

    other_bytes_df['total_mb'] = other_bytes_df['data_size'] / 1e6

    other_bytes_df = other_bytes_df.sort_values(by='total_mb', ascending=False)

    result_df = other_bytes_df[['process_issue', 'total_mb']]
    print(result_df['total_mb'].sum())
    print(result_df)