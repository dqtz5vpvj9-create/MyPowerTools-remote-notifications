import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
#    try:
#        sys.path.remove(str(parent))
#    except ValueError: # already removed
#        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()
sys.path.append(dirname(__file__) + os.sep + pardir)
from py_modules.logging_lib import setup_logging, LogLevel, MyLogger
logger = setup_logging()
from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
from py_modules.lib_sh import get_code_page

import enum
import platform
import subprocess
import requests
import re
from typing import Tuple, Optional, Any
from pprint import pprint

# pywifi has no macOS backend (raises NotImplementedError on import). Treat it
# as optional so this module loads everywhere; callers see a None interface.
try:
    import pywifi  # type: ignore
    _PYWIFI_IFACE_CONNECTED = pywifi.const.IFACE_CONNECTED
except Exception:
    pywifi = None  # type: ignore
    _PYWIFI_IFACE_CONNECTED = 4  # value from pywifi.const, kept for fallback

class WifiStatus(enum.Enum):
    NOT_CONNECTED = "Not connected"
    CONNECTED_TO_HOTSPOT = "Connected to one hotspot"
    CONNECTED_WITH_INTERNET = "Connected and has internet connection"

def get_wifi_interface() -> Optional[Any]:
    if pywifi is None:
        return None
    try:
        wifi = pywifi.PyWiFi()

        # Try to get the first WiFi interface
        if wifi.interfaces():
            return wifi.interfaces()[0]
    except PermissionError:
        pass
    return None


from py_modules.lib_sh import shell_run
def get_ssid() -> str:
    ssid = ''

    if platform.system() == 'Windows':
        code_page = get_code_page()
        try:
            stdout, stderr = shell_run('C:/Windows/System32/netsh.exe wlan show interface', callback=lambda line: True, silent=True, check_error=False)
        except subprocess.CalledProcessError as e:
            logger.error(f"Error: {e.stderr}")
            raise(e)

            
        ssid = next((line.split(':')[1].strip() for line in stdout.splitlines() if 'SSID' in line and 'BSSID' not in line), '')

    elif platform.system() == 'Linux':
        result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True)
        ssid = result.stdout.strip()

    elif platform.system() == 'Darwin':  # This is macOS
        result = subprocess.run(['/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport', '-I'], capture_output=True, text=True)
        ssid = next((line.split(':')[1].strip() for line in result.stdout.splitlines() if ' SSID' in line), '')

    return ssid

import concurrent.futures
import requests

def fetch(url: str, expected_text: str) -> bool:
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()  # Will raise an exception if the status code is 4xx or 5xx
        return response.text.strip() == expected_text
    except requests.RequestException:
        return False

def has_internet_access() -> bool:
    url_to_expected_result = {
        'http://detectportal.firefox.com/success.txt': 'success',
        'http://network-test.debian.org/nm': 'NetworkManager is online',
        'http://www.msftconnecttest.com/connecttest.txt': 'Microsoft Connect Test',
        'http://captive.apple.com/': 'Success',
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(url_to_expected_result)) as executor:
        future_to_url = {executor.submit(fetch, url, expected_text): url for url, expected_text in url_to_expected_result.items()}
        for future in concurrent.futures.as_completed(future_to_url):
            if future.result():
                return True
    return False

def check_wifi_status(interface: Optional[Any] = None) -> Tuple[WifiStatus, str]:
    if not interface:
        iface = get_wifi_interface()
    else:
        iface = interface
    if not iface:
        return WifiStatus.NOT_CONNECTED, ''
    # pprint(vars(iface))
    # print(iface.status())
    if iface.status() == _PYWIFI_IFACE_CONNECTED:
        ssid = get_ssid()
        
        if has_internet_access():
            return WifiStatus.CONNECTED_WITH_INTERNET, ssid
        else:
            return WifiStatus.CONNECTED_TO_HOTSPOT, ssid
    else:
        if has_internet_access():
            return WifiStatus.CONNECTED_WITH_INTERNET, ''
        else:
            return WifiStatus.NOT_CONNECTED, ''
