
from cmath import log
import sys, os
from time import sleep
from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from pathlib import Path
import argparse
from typing import Dict, Any

# For W3C actions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException
from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException

import time
from socket import timeout
from io import StringIO
import threading
import logging
import os
class PCMarkAppiumTest:
    def __init__(self, args, stop_event, logger):
        self.args = args
        self.logger : logging.Logger = logger
        os.environ["http_proxy"] = ""
        os.environ["https_proxy"] = ""
        self.caps: Dict[str, Any] = {}
        self.caps["platformName"] = "Android"
        self.caps["appium:platformVersion"] = "11"
        self.caps["appium:udid"] = args.device
        self.caps["appium:ensureWebviewsHavePages"] = True
        self.caps["appium:nativeWebScreenshot"] = True
        self.caps["appium:newCommandTimeout"] = 3600
        self.caps["appium:connectHardwareKeyboard"] = True
        self.caps["appium:autoLaunch"] = False
        self.caps["appium:adbExecTimeout"] = 50000

        self.caps["appium:appPackage"] = "com.futuremark.pcmark.android.benchmark"
        self.caps["appium:appActivity"] = "com.futuremark.gypsum.activity.SplashPageActivity"
        self.caps["appium:noReset"] = True
        self.caps["appium:automationName"] = "UiAutomator2"
        self.screen_saving_dir = Path(self.args.result_dir)
        self.screen_saving_dir.mkdir(exist_ok=True, parents=True)
        self.stop_event = stop_event

        timeout_thr = threading.Timer(60, self.timeout_handler)
        timeout_thr.start()
        self.driver = webdriver.Remote("http://127.0.0.1:4725", self.caps)
        self.driver.unlock()
        timeout_thr.cancel()

    def timeout_handler(self):
        raise TimeoutError("Timeout starting appium webdriver or launching app")
    
    def scroll_down(self):
        actions = ActionChains(self.driver)
        actions.w3c_actions = ActionBuilder(self.driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
        actions.w3c_actions.pointer_action.move_to_location(519, 1959)
        actions.w3c_actions.pointer_action.pointer_down()
        actions.w3c_actions.pointer_action.move_to_location(498, 758)
        actions.w3c_actions.pointer_action.release()
        actions.perform()
        

    def run_test(self):
        self.logger.info("Appium Ready to Launch PCMark Homepage")
        if not self.args.debug:
            self.driver.launch_app()
            wait = WebDriverWait(self.driver, 10, poll_frequency=5)
            run_retry = 0
            while True:
                try:
                    if(self.stop_event.is_set()):
                        self.logger.info("Stop event set, exiting appium thread")
                        self.driver.quit()
                        return
                    run_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//android.view.View[@content-desc='运行']")))
                    run_button = wait.until(EC.presence_of_element_located((By.XPATH, "//android.view.View[@content-desc='运行']")))
                    found = True
                    break
                except SeleniumTimeoutException:
                    run_retry += 1
                    # logger.error("Appium Output in exception")
            self.logger.info("Appium Ready to Launch Benchmarks")
            run_button.click()
        else:
            self.logger.info("Debug mode, skip launching app")
        ended = False
        start_time = time.monotonic()
        try:
            wait = WebDriverWait(self.driver, 20, poll_frequency=10)
            while time.monotonic() - start_time < 1200:
                try:
                    if(self.stop_event.is_set()):
                        self.logger.warning("Stop event set, exiting appium thread")
                        self.driver.quit()
                        return
                    finiehed_flag = wait.until(EC.element_to_be_clickable((By.XPATH, "//android.view.View[@content-desc='共享']/android.widget.TextView")))
                    finiehed_flag = wait.until(EC.presence_of_element_located((By.XPATH, "//android.view.View[@content-desc='共享']/android.widget.TextView")))
                    finiehed_flag.text
                    ended = True
                    break
                except SeleniumTimeoutException:
                    pass
                    # logger.error("Appium Output in exception")
        except Exception as e:
            self.logger.error(f"Appium Output in exception, exception: {e}")
        if not ended:
            self.logger.error("Appium Timeout waiting for benchmark to finish")
            self.driver.quit()
            return
        sleep(5)

        self.logger.info("Appium Ready to gather results")
        try:
            self.scroll_down()
        except  WebDriverException:
            self.driver = webdriver.Remote("http://127.0.0.1:4725", self.caps)
            self.driver.unlock()
            self.scroll_down()
        sleep(1)
        self.driver.save_screenshot(str(self.screen_saving_dir / "benchmark_score.png"))

        ss = StringIO()
        for idx in range(2, 14):
            xp = f"/hierarchy/android.widget.FrameLayout/android.view.ViewGroup/android.widget.FrameLayout[2]/android.widget.LinearLayout/android.widget.LinearLayout/android.widget.RelativeLayout/android.webkit.WebView/android.webkit.WebView/android.view.View/android.view.View/android.view.View/android.view.View[2]/android.view.View/android.widget.ListView/android.view.View[{idx}]"
            retry = 0
            while True:
                try:
                    if(self.stop_event.is_set()):
                        self.logger.info("Stop event set, exiting appium thread")
                        self.driver.quit()
                        return
                    el = self.driver.find_element('xpath', xp)
                    break
                except Exception:
                    print("Failed to find element, retrying...")
                    retry += 1
                    sleep(5)
                    continue
            if idx == 2:
                assert(el.text == "工作 3.0 效能 分数")
            elif idx == 4:
                assert(el.text == "网络浏览 3.0 分数")
            elif idx == 6:
                assert(el.text == "视频编辑分数 3.0 分数")
            elif idx == 8:
                assert(el.text == "文档编写 3.0 分数")
            elif idx == 10:
                assert(el.text == "图片编辑 3.0 分数")
            elif idx == 12:
                assert(el.text == "数据操作分数 3.0 分数")
            else:
                ss.write(el.text + "\n")

        with open(str(self.screen_saving_dir / "benchmark_score.txt"), "w") as f:
            ss.seek(0)
            print(ss.getvalue(), file=f)

        self.logger.info("Appium Exited Normally")
        self.driver.quit()
