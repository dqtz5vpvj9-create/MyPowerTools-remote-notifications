import json
import traceback
from typing import OrderedDict
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import numpy as np
import os
import socket
import os
import threading
import signal
from datetime import datetime as datetime_class
import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path

def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError: # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from py_modules.lib_aosp_base import *

def gen_frecords_perfetto_config(app_package: str | list[str] | None, duration_ms: int) -> str:

    new_config = """

data_sources {
  config {
    name: "linux.ftrace"
    ftrace_config {
      ftrace_events: "sched/sched_switch"
      ftrace_events: "sched/sched_wakeup_new"
      ftrace_events: "sched/sched_waking"
      ftrace_events: "sched/sched_process_exit"
      ftrace_events: "sched/sched_process_free"
      ftrace_events: "sched/sched_blocked_reason"
      ftrace_events: "task/task_newtask"
      ftrace_events: "task/task_rename"
      ftrace_events: "power/cpu_frequency"
      ftrace_events: "power/cpu_idle"
      ftrace_events: "power/suspend_resume"
      ftrace_events: "kmem/rss_stat"
      ftrace_events: "block/*"
      ftrace_events: "mm_event/mm_event_record"
      symbolize_ksyms: true
    }
  }
}
data_sources {
  config {
    name: "linux.process_stats"
    process_stats_config {
      scan_all_processes_on_start: true
      proc_stats_poll_ms: 250
    }
  }
}
data_sources {
  config {
    name: "linux.sys_stats"
    sys_stats_config {
      meminfo_period_ms: 250
      meminfo_counters: MEMINFO_MEM_TOTAL
      meminfo_counters: MEMINFO_MEM_FREE
      meminfo_counters: MEMINFO_MEM_AVAILABLE
      meminfo_counters: MEMINFO_BUFFERS
      meminfo_counters: MEMINFO_CACHED
      meminfo_counters: MEMINFO_SWAP_CACHED
      meminfo_counters: MEMINFO_ACTIVE
      meminfo_counters: MEMINFO_INACTIVE
      meminfo_counters: MEMINFO_ACTIVE_ANON
      meminfo_counters: MEMINFO_INACTIVE_ANON
      meminfo_counters: MEMINFO_ACTIVE_FILE
      meminfo_counters: MEMINFO_INACTIVE_FILE
      meminfo_counters: MEMINFO_UNEVICTABLE
      meminfo_counters: MEMINFO_MLOCKED
      meminfo_counters: MEMINFO_SWAP_TOTAL
      meminfo_counters: MEMINFO_SWAP_FREE
      meminfo_counters: MEMINFO_DIRTY
      meminfo_counters: MEMINFO_WRITEBACK
      meminfo_counters: MEMINFO_ANON_PAGES
      meminfo_counters: MEMINFO_MAPPED
      meminfo_counters: MEMINFO_SHMEM
      meminfo_counters: MEMINFO_SLAB
      meminfo_counters: MEMINFO_SLAB_RECLAIMABLE
      meminfo_counters: MEMINFO_SLAB_UNRECLAIMABLE
      meminfo_counters: MEMINFO_KERNEL_STACK
      meminfo_counters: MEMINFO_PAGE_TABLES
      meminfo_counters: MEMINFO_COMMIT_LIMIT
      meminfo_counters: MEMINFO_COMMITED_AS
      meminfo_counters: MEMINFO_VMALLOC_TOTAL
      meminfo_counters: MEMINFO_VMALLOC_USED
      meminfo_counters: MEMINFO_VMALLOC_CHUNK
      meminfo_counters: MEMINFO_CMA_TOTAL
      meminfo_counters: MEMINFO_CMA_FREE
      vmstat_period_ms: 250
      vmstat_counters: VMSTAT_NR_FREE_PAGES
      vmstat_counters: VMSTAT_NR_ALLOC_BATCH
      vmstat_counters: VMSTAT_NR_INACTIVE_ANON
      vmstat_counters: VMSTAT_NR_ACTIVE_ANON
      vmstat_counters: VMSTAT_NR_INACTIVE_FILE
      vmstat_counters: VMSTAT_NR_ACTIVE_FILE
      vmstat_counters: VMSTAT_NR_UNEVICTABLE
      vmstat_counters: VMSTAT_NR_MLOCK
      vmstat_counters: VMSTAT_NR_ANON_PAGES
      vmstat_counters: VMSTAT_NR_MAPPED
      vmstat_counters: VMSTAT_NR_FILE_PAGES
      vmstat_counters: VMSTAT_NR_DIRTY
      vmstat_counters: VMSTAT_NR_WRITEBACK
      vmstat_counters: VMSTAT_NR_SLAB_RECLAIMABLE
      vmstat_counters: VMSTAT_NR_SLAB_UNRECLAIMABLE
      vmstat_counters: VMSTAT_NR_PAGE_TABLE_PAGES
      vmstat_counters: VMSTAT_NR_KERNEL_STACK
      vmstat_counters: VMSTAT_NR_OVERHEAD
      vmstat_counters: VMSTAT_NR_UNSTABLE
      vmstat_counters: VMSTAT_NR_BOUNCE
      vmstat_counters: VMSTAT_NR_VMSCAN_WRITE
      vmstat_counters: VMSTAT_NR_VMSCAN_IMMEDIATE_RECLAIM
      vmstat_counters: VMSTAT_NR_WRITEBACK_TEMP
      vmstat_counters: VMSTAT_NR_ISOLATED_ANON
      vmstat_counters: VMSTAT_NR_ISOLATED_FILE
      vmstat_counters: VMSTAT_NR_SHMEM
      vmstat_counters: VMSTAT_NR_DIRTIED
      vmstat_counters: VMSTAT_NR_WRITTEN
      vmstat_counters: VMSTAT_NR_PAGES_SCANNED
      vmstat_counters: VMSTAT_WORKINGSET_REFAULT
      vmstat_counters: VMSTAT_WORKINGSET_ACTIVATE
      vmstat_counters: VMSTAT_WORKINGSET_NODERECLAIM
      vmstat_counters: VMSTAT_NR_ANON_TRANSPARENT_HUGEPAGES
      vmstat_counters: VMSTAT_NR_FREE_CMA
      vmstat_counters: VMSTAT_NR_SWAPCACHE
      vmstat_counters: VMSTAT_NR_DIRTY_THRESHOLD
      vmstat_counters: VMSTAT_NR_DIRTY_BACKGROUND_THRESHOLD
      vmstat_counters: VMSTAT_PGPGIN
      vmstat_counters: VMSTAT_PGPGOUT
      vmstat_counters: VMSTAT_PGPGOUTCLEAN
      vmstat_counters: VMSTAT_PSWPIN
      vmstat_counters: VMSTAT_PSWPOUT
      vmstat_counters: VMSTAT_PGALLOC_DMA
      vmstat_counters: VMSTAT_PGALLOC_NORMAL
      vmstat_counters: VMSTAT_PGALLOC_MOVABLE
      vmstat_counters: VMSTAT_PGFREE
      vmstat_counters: VMSTAT_PGACTIVATE
      vmstat_counters: VMSTAT_PGDEACTIVATE
      vmstat_counters: VMSTAT_PGFAULT
      vmstat_counters: VMSTAT_PGMAJFAULT
      vmstat_counters: VMSTAT_PGREFILL_DMA
      vmstat_counters: VMSTAT_PGREFILL_NORMAL
      vmstat_counters: VMSTAT_PGREFILL_MOVABLE
      vmstat_counters: VMSTAT_PGSTEAL_KSWAPD_DMA
      vmstat_counters: VMSTAT_PGSTEAL_KSWAPD_NORMAL
      vmstat_counters: VMSTAT_PGSTEAL_KSWAPD_MOVABLE
      vmstat_counters: VMSTAT_PGSTEAL_DIRECT_DMA
      vmstat_counters: VMSTAT_PGSTEAL_DIRECT_NORMAL
      vmstat_counters: VMSTAT_PGSTEAL_DIRECT_MOVABLE
      vmstat_counters: VMSTAT_PGSCAN_KSWAPD_DMA
      vmstat_counters: VMSTAT_PGSCAN_KSWAPD_NORMAL
      vmstat_counters: VMSTAT_PGSCAN_KSWAPD_MOVABLE
      vmstat_counters: VMSTAT_PGSCAN_DIRECT_DMA
      vmstat_counters: VMSTAT_PGSCAN_DIRECT_NORMAL
      vmstat_counters: VMSTAT_PGSCAN_DIRECT_MOVABLE
      vmstat_counters: VMSTAT_PGSCAN_DIRECT_THROTTLE
      vmstat_counters: VMSTAT_PGINODESTEAL
      vmstat_counters: VMSTAT_SLABS_SCANNED
      vmstat_counters: VMSTAT_KSWAPD_INODESTEAL
      vmstat_counters: VMSTAT_KSWAPD_LOW_WMARK_HIT_QUICKLY
      vmstat_counters: VMSTAT_KSWAPD_HIGH_WMARK_HIT_QUICKLY
      vmstat_counters: VMSTAT_PAGEOUTRUN
      vmstat_counters: VMSTAT_ALLOCSTALL
      vmstat_counters: VMSTAT_PGROTATED
      vmstat_counters: VMSTAT_DROP_PAGECACHE
      vmstat_counters: VMSTAT_DROP_SLAB
      vmstat_counters: VMSTAT_PGMIGRATE_SUCCESS
      vmstat_counters: VMSTAT_PGMIGRATE_FAIL
      vmstat_counters: VMSTAT_COMPACT_MIGRATE_SCANNED
      vmstat_counters: VMSTAT_COMPACT_FREE_SCANNED
      vmstat_counters: VMSTAT_COMPACT_ISOLATED
      vmstat_counters: VMSTAT_COMPACT_STALL
      vmstat_counters: VMSTAT_COMPACT_FAIL
      vmstat_counters: VMSTAT_COMPACT_SUCCESS
      vmstat_counters: VMSTAT_COMPACT_DAEMON_WAKE
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_CULLED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_SCANNED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_RESCUED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_MLOCKED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_MUNLOCKED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_CLEARED
      vmstat_counters: VMSTAT_UNEVICTABLE_PGS_STRANDED
      vmstat_counters: VMSTAT_NR_ZSPAGES
      vmstat_counters: VMSTAT_NR_ION_HEAP
      vmstat_counters: VMSTAT_NR_GPU_HEAP
      vmstat_counters: VMSTAT_ALLOCSTALL_DMA
      vmstat_counters: VMSTAT_ALLOCSTALL_MOVABLE
      vmstat_counters: VMSTAT_ALLOCSTALL_NORMAL
      vmstat_counters: VMSTAT_COMPACT_DAEMON_FREE_SCANNED
      vmstat_counters: VMSTAT_COMPACT_DAEMON_MIGRATE_SCANNED
      vmstat_counters: VMSTAT_NR_FASTRPC
      vmstat_counters: VMSTAT_NR_INDIRECTLY_RECLAIMABLE
      vmstat_counters: VMSTAT_NR_ION_HEAP_POOL
      vmstat_counters: VMSTAT_NR_KERNEL_MISC_RECLAIMABLE
      vmstat_counters: VMSTAT_NR_SHADOW_CALL_STACK_BYTES
      vmstat_counters: VMSTAT_NR_SHMEM_HUGEPAGES
      vmstat_counters: VMSTAT_NR_SHMEM_PMDMAPPED
      vmstat_counters: VMSTAT_NR_UNRECLAIMABLE_PAGES
      vmstat_counters: VMSTAT_NR_ZONE_ACTIVE_ANON
      vmstat_counters: VMSTAT_NR_ZONE_ACTIVE_FILE
      vmstat_counters: VMSTAT_NR_ZONE_INACTIVE_ANON
      vmstat_counters: VMSTAT_NR_ZONE_INACTIVE_FILE
      vmstat_counters: VMSTAT_NR_ZONE_UNEVICTABLE
      vmstat_counters: VMSTAT_NR_ZONE_WRITE_PENDING
      vmstat_counters: VMSTAT_OOM_KILL
      vmstat_counters: VMSTAT_PGLAZYFREE
      vmstat_counters: VMSTAT_PGLAZYFREED
      vmstat_counters: VMSTAT_PGREFILL
      vmstat_counters: VMSTAT_PGSCAN_DIRECT
      vmstat_counters: VMSTAT_PGSCAN_KSWAPD
      vmstat_counters: VMSTAT_PGSKIP_DMA
      vmstat_counters: VMSTAT_PGSKIP_MOVABLE
      vmstat_counters: VMSTAT_PGSKIP_NORMAL
      vmstat_counters: VMSTAT_PGSTEAL_DIRECT
      vmstat_counters: VMSTAT_PGSTEAL_KSWAPD
      vmstat_counters: VMSTAT_SWAP_RA
      vmstat_counters: VMSTAT_SWAP_RA_HIT
      vmstat_counters: VMSTAT_WORKINGSET_RESTORE
      vmstat_counters: VMSTAT_ALLOCSTALL_DEVICE
      vmstat_counters: VMSTAT_ALLOCSTALL_DMA32
      vmstat_counters: VMSTAT_BALLOON_DEFLATE
      vmstat_counters: VMSTAT_BALLOON_INFLATE
      vmstat_counters: VMSTAT_BALLOON_MIGRATE
      vmstat_counters: VMSTAT_CMA_ALLOC_FAIL
      vmstat_counters: VMSTAT_CMA_ALLOC_SUCCESS
      vmstat_counters: VMSTAT_NR_FILE_HUGEPAGES
      vmstat_counters: VMSTAT_NR_FILE_PMDMAPPED
      vmstat_counters: VMSTAT_NR_FOLL_PIN_ACQUIRED
      vmstat_counters: VMSTAT_NR_FOLL_PIN_RELEASED
      vmstat_counters: VMSTAT_NR_SEC_PAGE_TABLE_PAGES
      vmstat_counters: VMSTAT_NR_SHADOW_CALL_STACK
      vmstat_counters: VMSTAT_NR_SWAPCACHED
      vmstat_counters: VMSTAT_NR_THROTTLED_WRITTEN
      vmstat_counters: VMSTAT_PGALLOC_DEVICE
      vmstat_counters: VMSTAT_PGALLOC_DMA32
      vmstat_counters: VMSTAT_PGDEMOTE_DIRECT
      vmstat_counters: VMSTAT_PGDEMOTE_KSWAPD
      vmstat_counters: VMSTAT_PGREUSE
      vmstat_counters: VMSTAT_PGSCAN_ANON
      vmstat_counters: VMSTAT_PGSCAN_FILE
      vmstat_counters: VMSTAT_PGSKIP_DEVICE
      vmstat_counters: VMSTAT_PGSKIP_DMA32
      vmstat_counters: VMSTAT_PGSTEAL_ANON
      vmstat_counters: VMSTAT_PGSTEAL_FILE
      vmstat_counters: VMSTAT_THP_COLLAPSE_ALLOC
      vmstat_counters: VMSTAT_THP_COLLAPSE_ALLOC_FAILED
      vmstat_counters: VMSTAT_THP_DEFERRED_SPLIT_PAGE
      vmstat_counters: VMSTAT_THP_FAULT_ALLOC
      vmstat_counters: VMSTAT_THP_FAULT_FALLBACK
      vmstat_counters: VMSTAT_THP_FAULT_FALLBACK_CHARGE
      vmstat_counters: VMSTAT_THP_FILE_ALLOC
      vmstat_counters: VMSTAT_THP_FILE_FALLBACK
      vmstat_counters: VMSTAT_THP_FILE_FALLBACK_CHARGE
      vmstat_counters: VMSTAT_THP_FILE_MAPPED
      vmstat_counters: VMSTAT_THP_MIGRATION_FAIL
      vmstat_counters: VMSTAT_THP_MIGRATION_SPLIT
      vmstat_counters: VMSTAT_THP_MIGRATION_SUCCESS
      vmstat_counters: VMSTAT_THP_SCAN_EXCEED_NONE_PTE
      vmstat_counters: VMSTAT_THP_SCAN_EXCEED_SHARE_PTE
      vmstat_counters: VMSTAT_THP_SCAN_EXCEED_SWAP_PTE
      vmstat_counters: VMSTAT_THP_SPLIT_PAGE
      vmstat_counters: VMSTAT_THP_SPLIT_PAGE_FAILED
      vmstat_counters: VMSTAT_THP_SPLIT_PMD
      vmstat_counters: VMSTAT_THP_SWPOUT
      vmstat_counters: VMSTAT_THP_SWPOUT_FALLBACK
      vmstat_counters: VMSTAT_THP_ZERO_PAGE_ALLOC
      vmstat_counters: VMSTAT_THP_ZERO_PAGE_ALLOC_FAILED
      vmstat_counters: VMSTAT_VMA_LOCK_ABORT
      vmstat_counters: VMSTAT_VMA_LOCK_MISS
      vmstat_counters: VMSTAT_VMA_LOCK_RETRY
      vmstat_counters: VMSTAT_VMA_LOCK_SUCCESS
      vmstat_counters: VMSTAT_WORKINGSET_ACTIVATE_ANON
      vmstat_counters: VMSTAT_WORKINGSET_ACTIVATE_FILE
      vmstat_counters: VMSTAT_WORKINGSET_NODES
      vmstat_counters: VMSTAT_WORKINGSET_REFAULT_ANON
      vmstat_counters: VMSTAT_WORKINGSET_REFAULT_FILE
      vmstat_counters: VMSTAT_WORKINGSET_RESTORE_ANON
      vmstat_counters: VMSTAT_WORKINGSET_RESTORE_FILE
      stat_period_ms: 50
      stat_counters: STAT_CPU_TIMES
      stat_counters: STAT_FORK_COUNT
      cpufreq_period_ms: 50
    }
  }
}

data_sources {
  config {
    name: "android.power"
    android_power_config {
      battery_poll_ms: 250
      battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT
      battery_counters: BATTERY_COUNTER_CHARGE
      battery_counters: BATTERY_COUNTER_CURRENT
      collect_power_rails: true
    }
  }
}

data_sources {
  config {
    name: "android.log"
    android_log_config {
    }
  }
}

"""

    timeline_config = """

data_sources {
  config {
    name: "android.surfaceflinger.frametimeline"
  }
}

"""

    old_config = f"""

buffers {{
    size_kb: 262144
}}
write_into_file: true
file_write_period_ms: 2500
max_file_size_bytes: 5000000000
flush_period_ms: 10000
data_sources {{
    config {{
        name: "linux.ftrace"
        ftrace_config {{
            atrace_categories: "sched"
            atrace_categories: "gfx"
            atrace_categories: "view"
            atrace_apps_place_holder
        }}
    }}
}}
data_sources {{
    config {{
        name: "linux.process_stats"
        target_buffer: 0
    }}
}}
data_sources {{
    config {{
        name: "android.surfaceflinger.frame"
    }}
}}
duration_ms: {duration_ms}

"""
    atrace_app_builder = ""
    if app_package is None or len(app_package) == 0:
        atrace_app_builder = ""
    elif isinstance(app_package, str):
        atrace_app_builder = f'atrace_apps: "{app_package}"'
    elif isinstance(app_package, list):
        atrace_app_builder = '\n'.join([f'atrace_apps: "{app}"' for app in app_package])
    old_config = old_config.replace("atrace_apps_place_holder", atrace_app_builder)
    if serial == "px2:25555" or serial == "px3:35555":
        return new_config + old_config + timeline_config
    else:
        return new_config + old_config
        # return  old_config
