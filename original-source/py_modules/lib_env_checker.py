import argparse
import importlib, sys
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

from py_modules.lib_aosp_base import *
from test_tools.ci_base import Ci_Base
from ci_config import AndroidRuntimeTestType, Config
from compile_tools.lib_aosp_image_store import verify_ArtProduct, TestEnv, ArtProduct_to_env
import subprocess

from py_modules.logging_lib import setup_logging
logger = setup_logging(simple_fmt=True)
logger.info(serial)
if serial not in subprocess.getoutput("adb devices"):
    logger.fatal(f"Device {serial} is not connected")
    exit(1)



class TestEnvChecker():
        
    @staticmethod
    def check_kernel() -> TestEnv:
        # check /proc/lxr_read_pmu_cycles exists
        not_oaRAM = 0
        not_vanilla = 0
        not_fleet = 0
        output = As("cat /proc/lxr_is_oaRAM", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT])
        if "No such file or directory" in output:
            not_oaRAM = 1

        output = As("cat /proc/lxr_is_vanilla", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT])
        if "No such file or directory" in output:
            not_vanilla = 1
        
        output = As("cat /proc/lxr_is_fleet", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT])
        if "No such file or directory" in output:
            not_fleet = 1
        if sum([not_oaRAM, not_vanilla, not_fleet]) != 2:
            error_msg = f"Can not determine the kernel environment, not_oaRAM: {not_oaRAM}, not_vanilla: {not_vanilla}, not_fleet: {not_fleet}"
            print(error_msg)
            print(output)
            print("sleep 120 seconds")
            time.sleep(120)
            raise Exception(error_msg)
        if not_oaRAM == 0:
            return TestEnv.OaRam
        if not_vanilla == 0:
            return TestEnv.Vanilla
        if not_fleet == 0:
            return TestEnv.Fleet
        raise Exception("Can not determine the kernel environment")


    @staticmethod
    def check_art() -> TestEnv:
        output = As("dalvikvm64", [AsOption.STDOUT_NO_PRINT, AsOption.STDERR_TO_STDOUT])
        if "Mlock" in output:
            return TestEnv.OaRam
        elif "This is a fleet ART" in output:
            return TestEnv.Fleet
        elif "Class name required":
            return TestEnv.Vanilla
        raise Exception(f"Can not determine the ART environment, output: {output}")
        
    @staticmethod
    def check_env(expected_env: TestEnv) -> bool:

        check_art_value = TestEnvChecker.check_art().value
        check_kernel_value = TestEnvChecker.check_kernel().value
        ret = check_art_value == expected_env.value and check_kernel_value == expected_env.value
        if not ret:
            print(f"----------------->>>>>>>>>>>>>>>>")
            print(f"Error: Environment mismatch. Expected: {expected_env}, but got ART environment: {check_art_value} and Kernel environment: {check_kernel_value}. ")
            print(f"<<<<<<<<<<<<<<<<-----------------")
        else:
            print(f"Test environment is {expected_env}")
        return ret

def check_test_env_config(art_test_type) -> bool:
    if art_test_type == AndroidRuntimeTestType.INSTR_NO_STRESS or art_test_type == AndroidRuntimeTestType.INSTR_STRESS:
        expected_env = TestEnv.OaRam
    else:
        expected_env = TestEnv.Vanilla
    return check_test_env(expected_env)
    
def check_test_env(expected_env: TestEnv) -> bool:
    return TestEnvChecker.check_env(expected_env)

if __name__ == '__main__':
    if len(sys.argv) == 1 or sys.argv[1] == "-h" or sys.argv[1] == "--help" or len(sys.argv) > 2:
        print("Usage: python3 lib_env_checker.py --[cached-]vanilla[-suffix] or --[cached-]oaRAM[-suffix] or --[cached-]fleet[-suffix]")
        exit(1)
    art_product = sys.argv[1]
    if art_product.startswith("--"):
        art_product = art_product[2:]
    if not verify_ArtProduct(art_product):
        raise ValueError("Invalid product name")
    test_env = ArtProduct_to_env(art_product)
    boot = wait_boot_complete()
    if boot:
        assert check_test_env(test_env)
        exit(0)
    else:
        assert ("Boot complete" == None)

        

    
