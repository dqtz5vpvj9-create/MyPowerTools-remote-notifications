"""ADB 序列号 ↔ FNB USB 功率计 HID 序列号的映射 + 传输层元数据。

4 台真机现在拓扑统一：每台都挂 USB 以太网（10.33.2.155-158），关 Wi-Fi
也不会断 ADB；同物理机原来的 Wi-Fi-direct 链路（10.33.0.155-158）退到
alt 仅供 recovery。Wi-Fi-toggle 任务可以派发到全部 4 台主链路。

主通路：
  - 10.33.2.155-158:5555 — USB 以太网（关 Wi-Fi 不会断；wifi-toggle 任务
                           的派发白名单覆盖全部 4 台，见 WIFI_OFF_DISPATCH_ALLOWED）

备用通路（仅 recovery 用，不再参与 dispatch swap）：
  - 10.33.0.155-158:5555 — Wi-Fi-direct，分别对应同一物理机的以太网主

Wi-Fi-toggle 任务的派发策略：白名单式（WIFI_OFF_DISPATCH_ALLOWED），
全部 4 台以太网主链路均可接，Wi-Fi-direct alt 不接（关掉 Wi-Fi 自己就断）。
"""
from typing import Dict, FrozenSet, List, Optional, Tuple

# ---- 主映射：默认 transport（全员 USB 以太网）------------------------------
ADB_TO_FNB_SERIAL: Dict[str, str] = {
    "10.33.2.155:5555":  "FNB-58-88872",  # USB 以太网
    "10.33.2.156:5555":  "FNB-58-69343",  # USB 以太网
    "10.33.2.157:5555":  "FNB-58-86360",  # USB 以太网
    "10.33.2.158:5555":  "FNB-58-69717",  # USB 以太网
}

# ---- 备用映射（仅 recovery 用；不再参与 dispatch swap）---------------------
# 登记这些 serial 让 get_fnb_serial() 在 recovery 场景下仍能查到对的功率计。
_ALT_ADB_TO_FNB_SERIAL: Dict[str, str] = {
    "10.33.0.155:5555":  "FNB-58-88872",  # .155 物理机 Wi-Fi 备份
    "10.33.0.156:5555":  "FNB-58-69343",  # .156 物理机 Wi-Fi 备份
    "10.33.0.157:5555":  "FNB-58-86360",  # .157 物理机 Wi-Fi 备份
    "10.33.0.158:5555":  "FNB-58-69717",  # .158 物理机 Wi-Fi 备份
}

# ---- 物理设备 family pair：(primary, alt) ---------------------------------
# 同一物理机的两条 ADB 通路。DevicePool 用它防止同一物理机被双重 acquire；
# Phase B `recover_primary_wifi_via_alt` 用它做 pre-flight 失败时的兜底救援。
# 4 台都是 (eth primary, wifi alt)。
FAMILY_PAIRS: List[Tuple[str, str]] = [
    ("10.33.2.155:5555", "10.33.0.155:5555"),
    ("10.33.2.156:5555", "10.33.0.156:5555"),
    ("10.33.2.157:5555", "10.33.0.157:5555"),
    ("10.33.2.158:5555", "10.33.0.158:5555"),
]

# ---- 传输层元数据：哪些 ADB serial 依赖设备自己的 Wi-Fi --------------------
# 描述式（非派发决策入口）：transport 依赖 Wi-Fi，关 Wi-Fi 就断。
# 4 台 wifi-direct alt 全列入；以太网主不依赖设备 Wi-Fi。
WIFI_UNSAFE_DEVICES: FrozenSet[str] = frozenset({
    "10.33.0.155:5555",
    "10.33.0.156:5555",
    "10.33.0.157:5555",
    "10.33.0.158:5555",
})

# ---- Wi-Fi-toggle 任务的派发白名单 ----------------------------------------
# WifiAwareDispatchPolicy 用它判定：哪些 device 允许接受会关 Wi-Fi 的任务。
# 4 台原生以太网 primary 全部入选 —— 关 Wi-Fi 也不会断它们的 ADB 链路。
WIFI_OFF_DISPATCH_ALLOWED: FrozenSet[str] = frozenset({
    "10.33.2.155:5555",
    "10.33.2.156:5555",
    "10.33.2.157:5555",
    "10.33.2.158:5555",
})


def _full_mapping() -> Dict[str, str]:
    return {**ADB_TO_FNB_SERIAL, **_ALT_ADB_TO_FNB_SERIAL}


def get_fnb_serial(adb_serial: Optional[str]) -> Optional[str]:
    """Return FNB-58-xxxxx for a given ADB serial (primary or alt), else None."""
    if not adb_serial:
        return None
    return _full_mapping().get(adb_serial)


def get_adb_serial(fnb_serial: Optional[str]) -> Optional[str]:
    """Return the PREFERRED ADB serial for a given FNB meter, else None."""
    if not fnb_serial:
        return None
    for adb, fnb in ADB_TO_FNB_SERIAL.items():
        if fnb == fnb_serial:
            return adb
    return None


def is_wifi_toggle_safe(adb_serial: str) -> bool:
    """True if the device's ADB transport survives Wi-Fi being turned off."""
    return adb_serial not in WIFI_UNSAFE_DEVICES


def all_fnb_serials() -> List[str]:
    return list(ADB_TO_FNB_SERIAL.values())


def preferred_adb_serials() -> List[str]:
    """ADB serials intended for benchmark --devices, in a stable order."""
    return list(ADB_TO_FNB_SERIAL.keys())


def primary_to_alt() -> Dict[str, str]:
    """Map primary serial → alt serial (consumed by recovery / quarantine reprobe)."""
    return {p: a for p, a in FAMILY_PAIRS}


# Back-compat alias (older callers expected this name).
all_adb_serials = preferred_adb_serials
