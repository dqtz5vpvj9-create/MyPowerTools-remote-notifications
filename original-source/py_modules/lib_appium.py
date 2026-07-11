import tempfile
from appium.webdriver import Remote, webdriver
from appium.webdriver.common.appiumby import AppiumBy

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException
from urllib3.exceptions import ReadTimeoutError
from selenium.webdriver.common.by import By
from appium.webdriver.common.appiumby import AppiumBy
import xml.etree.ElementTree as ET
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
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_aosp_base import Aa, As, AsOption
import time, threading
import os
import base64
import openai
from datetime import datetime as datetime_class
from test_tools.am_pm_utils import get_foreground_activities

from PIL import Image
import io

from pydantic import BaseModel
class AppStateAnalysis(BaseModel):
    analysis: str
    is_correct_state: bool
    possible_fix: str

class PreExecuteResult(BaseModel):
    analysis: str
    is_correct_state: bool
    possible_fix: str
    screenshot_path: str

class AppiumWrapper:

    def __init__(self, device_id: str, logger: MyLogger, test_dir: str, catch_all: bool=False, dummy: bool=False):
        if test_dir:
            self.output_dir = os.path.join(test_dir, 'appium_screenshots')
            os.makedirs(self.output_dir, exist_ok=True)
        else:
            self.output_dir = None
        self.logger = logger
        self.device_id = device_id
        self.dummy = dummy
        if not self.dummy:
            self.driver = self.create_driver(device_id)
            self.wait = WebDriverWait(self.driver, 15, poll_frequency=2)
            self.short_wait = WebDriverWait(self.driver, 7)
        self.catch_all = catch_all
        self.should_stop = False
        if self.catch_all:
            self.logger.notice("Fleet: Catch all exceptions")

    def create_driver(self, device_id: str) -> webdriver.WebDriver:
        caps = dict(
            platformName='Android',
            automationName='uiautomator2',
            deviceName='Android',
            udid=device_id,
            ensureWebviewsHavePages=True,
            newCommandTimeout=3600,
            autoLaunch=False,
            adbExecTimeout=50000,
            noReset=True,
            disableSuppressAccessibilityService=True,
        )
        try:
            Aa("uninstall", "io.appium.uiautomator2.server", device_serial=device_id)
        except:
            pass
        try:
            Aa("uninstall", "io.appium.uiautomator2.server.test", device_serial=device_id)
        except:
            pass
        connection = Remote('http://localhost:4723', caps)
        return connection
        while True:
            try:
                connection = Remote('http://localhost:4723', caps)
                return connection
            except Exception as e:
                self.logger.info(e)
                try:
                    Aa("uninstall", "io.appium.uiautomator2.server", device_serial=device_id)
                    Aa("uninstall", "io.appium.uiautomator2.server.test", device_serial=device_id)
                except Exception as e:
                    pass
                pass

    def take_screenshot(self, nr_display = 0):
        tag = datetime_class.now().strftime("%Y-%m-%d_%H%M%S")
        if self.output_dir is None:
            local_image_dir = tempfile.mkdtemp(prefix="screenshot_")
        else:
            local_image_dir = os.path.join(self.output_dir, "temp")
        if not os.path.exists(local_image_dir):
            os.makedirs(local_image_dir)
        
        remote_jpg_path = "/data/local/tmp/screen.jpg"
        remote_png_path = "/data/local/tmp/screen.png"
        local_jpg_path = os.path.join(local_image_dir, f"screen_{tag}.jpg")
        local_png_path = os.path.join(local_image_dir, f"screen_{tag}.png")

        # Take screenshot using minicap
        self.logger.debug("Executing minicap")
        max_retries = 0
        retry_count = 0
        while retry_count < max_retries:
            try:
                if nr_display == 0:
                    As("LD_LIBRARY_PATH=/data/local/tmp/minicap-devel /data/local/tmp/minicap-devel/minicap -P 1080x2340@1080x2340/0 -Q 100 -s > " + remote_jpg_path, [AsOption.STDOUT_NO_PRINT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                    self.logger.debug("Minicap executed successfully")
                else:
                    As(f"screencap -d {nr_display} -p {remote_png_path}", [AsOption.STDOUT_NO_PRINT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                    self.logger.debug("screencap executed successfully")
                break  # Exit the loop if successful
            except Exception as e:
                retry_count += 1
                self.logger.error(f"Minicap execution failed (Attempt {retry_count}/{max_retries}): {e}")
                if retry_count >= max_retries:
                    self.logger.error("Max retries reached. Unable to execute minicap.")
                    raise e
                time.sleep(1)  # Wait before retrying

        # Pull the screenshot
        if nr_display == 0:
            self.logger.info(f"Pulling screenshot to {local_jpg_path}")
            Aa("pull", remote_jpg_path, local_jpg_path, device_serial=self.device_id)
            return local_jpg_path
        else:
            self.logger.info(f"Pulling screenshot to {local_png_path}")
            Aa("pull", remote_png_path, local_png_path, device_serial=self.device_id)
            return local_png_path

    def encode_image(self, image_path):
        # Open the PNG image file
        with Image.open(image_path) as img:
            # Get original image dimensions
            width, height = img.size
            # Define maximum size
            max_size = 512
            # Calculate scaling factor
            scaling_factor = min(max_size / width, max_size / height, 1)
            # If image is larger than max size, scale it down
            if scaling_factor < 1:
                new_width = int(width * scaling_factor)
                new_height = int(height * scaling_factor)
                img = img.resize((new_width, new_height), Image.LANCZOS)
            # Save image to byte stream
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=100)
            # Save resized image for human inspection
            resized_path = f"{image_path}_resized.jpg"
            img.save(resized_path, format='JPEG', quality=100)
            self.logger.debug(f"Resized image saved to {resized_path}")
            # Encode image to base64
            img_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
        return img_str

    def check_app_state_with_gpt(self, image_path, criteria):
        self.logger.debug("Encoding image...")
        base64_image = self.encode_image(image_path)

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": criteria
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64_image}",
                            # "detail": "low"
                        }
                    }
                ]
            }
        ]
        self.logger.debug("Sending image to GPT-4...")
        openai_success = False
        retries = 0
        from openai import OpenAI
        client = OpenAI(
            api_key="sk-pAcTY6FaDHEnqjJh0nnF6x7t1LpMIWHvsD4Eg3n0SsfmU8OR", 
            base_url="https://api.aiproxy.io/v1"
        )

        client = OpenAI(
            api_key="sk-lR0vWAkGTXtkPqXo7b91Ba5bA8E24136993d8a0bDf52B1F9",
            base_url="http://ipads.chat.gpt:3006/v1"
        )
        while retries < 3:
            try:
                completion = client.beta.chat.completions.parse(
                    model="gpt-4o",  # 使用适当的 GPT-4 视觉模型
                    messages=messages,
                    response_format=AppStateAnalysis
                )
                openai_success = True
                break
            except Exception as e:
                self.logger.error(f"Failed to send image to GPT-4: {e}")
                retries += 1
                continue
        
        if not openai_success:
            self.logger.error("Failed to send image to GPT-4.")
            return None
        # return response.choices[0].message.content

        assistant_message = completion.choices[0].message

        if assistant_message.refusal:
            self.logger.error("GPT-4 refused to answer: " + assistant_message.refusal)
            return None

        parsed_response = assistant_message.parsed
        if parsed_response:
            return parsed_response
        else:
            self.logger.error("Failed to parse GPT-4 response.")
            return None

    def analyze_app_state(self, criteria) -> PreExecuteResult:
        self.logger.debug("Analyzing app state...")
        ret =  PreExecuteResult(analysis="No analysis", is_correct_state=True, possible_fix="", screenshot_path="")
        # 截图
        self.logger.debug("Taking screenshot")
        screenshot_path = self.take_screenshot()
        ret.screenshot_path = screenshot_path
        # 使用 GPT-4 视觉功能检查应用状态
        self.logger.debug("Talking with GPT")
        result = self.check_app_state_with_gpt(screenshot_path, criteria)
        if result:
            analysis = result.analysis
            is_correct_state = result.is_correct_state
            possible_fix = result.possible_fix
            self.logger.debug(f"Analysis: {analysis}")
            self.logger.debug(f"Is Correct State: {is_correct_state}")
            if not is_correct_state:
                self.logger.debug(f"Possible Fix: {possible_fix}")
            ret.analysis = analysis
            ret.is_correct_state = is_correct_state
            ret.possible_fix = possible_fix
        return ret
        

    def operate(self):
        self.driver.activate_app('com.zhiliaoapp.musically')

    def recreate_driver(self):
        self.driver.close()
        self.driver = self.create_driver(self.device_id)
        self.driver.notice('recreated appium driver')

    def easybike_handler(self):

        if self.driver.current_package == 'com.jingyao.easybike':
            try:
                self.logger.debug('Trying to click on the dialog element')
                dialog_el = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable((By.ID, 'com.jingyao.easybike:id/actionDialogClose')))
                # dialog_el = self.driver.find_element(AppiumBy.ID, 'com.jingyao.easybike:id/actionDialogClose')
                dialog_el.click()
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException, ReadTimeoutError) as e:
                pass

            try:
                # taxi_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '(//androidx.viewpager.widget.ViewPager[@resource-id="com.jingyao.easybike:id/gridVP"])[1]/androidx.recyclerview.widget.RecyclerView/android.widget.RelativeLayout[1]')))
                self.logger.debug('Trying to click on the taxi element')
                taxi_el = self.short_wait.until(EC.element_to_be_clickable((By.XPATH, '(//android.widget.ImageView[@resource-id="com.jingyao.easybike:id/topIv"])[1]')))
                taxi_el.click()
            except (TimeoutException, StaleElementReferenceException, WebDriverException) as e:
                self.logger.debug('Appium Exception:' + str(e))

            try:
                self.logger.debug('Trying to click on the second AD close button')
                dialog_el = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable((By.ID, 'com.jingyao.easybike:id/actionDialogClose')))
                # dialog_el = self.driver.find_element(AppiumBy.ID, 'com.jingyao.easybike:id/actionDialogClose')
                dialog_el.click()
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException, ReadTimeoutError) as e:
                pass



        # def checker():
        #     while self.driver.current_package == 'com.jingyao.easybike':
        #         try:
        #             dialog_el = self.driver.find_element(AppiumBy.ID, 'com.jingyao.easybike:id/actionDialogClose')
        #             dialog_el.click()
        #         except (NoSuchElementException, StaleElementReferenceException, WebDriverException, ReadTimeoutError) as e:
        #             pass
        #         time.sleep(5)

        # t = threading.Thread(target=checker)
        # t.start()
        # return t
        return

    def temu_handler(self):
        time.sleep(4)
        return
        while self.driver.current_package == 'com.einnovation.temu':
            try:
                self.wait.until(EC.element_to_be_clickable((By.XPATH, '(//android.widget.HorizontalScrollView[@resource-id="com.einnovation.temu:id/temu"])[1]/android.widget.LinearLayout/f.a[3]')))
                break
            except (TimeoutException, WebDriverException) as e:
                continue

    def qqdownloader_handler(self):
        # loop = 0
        while self.driver.current_package == 'com.tencent.android.qqdownloader' and not self.should_stop:
            # logger.info('qqdownloader_handler')
            try:
                # if loop >= 2 and self.catch_all:
                #     break
                # loop += 1
                # el1 = self.driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value="new UiSelector().text(\"小程序\")")
                # el1.click()
                el1 = self.driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value="new UiSelector().text(\"软件\")")
                el1.click()
                time.sleep(2)
                break
            except (TimeoutException, WebDriverException) as e:
                try:
                    tiny_app_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '//android.widget.FrameLayout[@resource-id="com.tencent.android.qqdownloader:id/ah_"]/androidx.recyclerview.widget.RecyclerView/android.widget.RelativeLayout[2]/android.widget.LinearLayout/android.widget.LinearLayout[5]')))
                    tiny_app_el.click()
                    self.logger.debug('Clicked on the tiny app')
                    break
                except (TimeoutException, WebDriverException) as e:
                    pass
                continue

    def map_handler(self):
        while self.driver.current_package == 'com.google.android.apps.maps':
            try:
                dismiss_el = self.driver.find_element(AppiumBy.XPATH, '//android.widget.TextView[@text="Dismiss"]')
                dismiss_el.click()
                break
            except (NoSuchElementException, WebDriverException) as e:
                break

    def iqiyi_handler(self):
        while self.driver.current_package == 'com.iqiyi.i18n':
            try:
                self.logger.debug('Trying to click on the dialog element')
                dialog_el = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable((By.ID, 'com.iqiyi.i18n:id/cancel_btn')))
                # dialog_el = self.driver.find_element(AppiumBy.ID, 'com.jingyao.easybike:id/actionDialogClose')
                dialog_el.click()
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException, ReadTimeoutError) as e:
                break
            # try:
            #     discover_el = self.wait.until(EC.element_to_be_clickable((By.ID, 'com.iqiyi.i18n:id/tab_iv_discover')))
            #     discover_el.click()
            #     break
            # except (TimeoutException, WebDriverException) as e:
            #     self.logger.error(e)
            #     if 'ECONNREFUSED' in e.msg:
            #         self.recreate_driver()
            #     continue

    def iqiyi_handler_tap(self):
        fail_count = 0
        while self.driver.current_package == 'com.iqiyi.i18n':
            if fail_count > 5 and self.catch_all:
                break
            try:
                self.wait.until(EC.element_to_be_clickable((By.ID, 'com.iqiyi.i18n:id/tab_iv_download')))
                # time.sleep(5)
                self.driver.tap([(397, 2120)], 200)
                break
            except (TimeoutException, WebDriverException) as e:
                self.logger.error(e)
                if 'ECONNREFUSED' in e.msg:
                    self.recreate_driver()
                fail_count += 1
                continue

    def qqmusic_handler(self):
        fail_count = 0
        while self.driver.current_package == 'com.tencent.qqmusic':
            if fail_count > 5 and self.catch_all:
                break
            try:
                self.driver.find_element(AppiumBy.ID, 'com.tencent.qqmusic:id/gf4')
                time.sleep(2)
                # daily_el.click()
                break
            except (TimeoutException, WebDriverException, ReadTimeoutError) as e:
                if e is ReadTimeoutError:
                    time.sleep(2)
                    break
                fail_count += 1
                time.sleep(1)
                continue

    def spotify_handler(self):
        while self.driver.current_package == 'com.spotify.music':
            try:
                blog_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '//android.widget.Button[@content-desc="选择播客"]')))
                blog_el.click()
                break
            except (TimeoutException, WebDriverException) as e:
                # try:
                #     cancel_blog_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '//android.widget.Button[@content-desc="取消选择播客"]')))
                #     # Return if the cancel button is found
                #     return
                # except (TimeoutException, WebDriverException) as e:
                #     continue
                continue

    def moji_handler(self):
        self.logger.debug('moji_handler')
        fail_count = 0
        while self.driver.current_package == 'com.moji.mjweather':
            if fail_count > 2 and self.catch_all:
                break
            try:
                self.logger.debug("try to click on 时景")
                self.wait.until(EC.element_to_be_clickable((By.ID, 'com.moji.mjweather:id/xv')))
                # Click the position 3 times using adb input
                As('input tap 325 2100', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                self.logger.debug("clicked on 时景 successfully, break")
                break
            except (NoSuchElementException, TimeoutException, WebDriverException) as e:
                try:
                    self.logger.debug("try to click the second tab")
                    As('input tap 325 2100', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                    view_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '//android.widget.TabWidget[@resource-id="android:id/tabs"]/android.widget.RelativeLayout[2]')))
                    view_el.click()
                    self.logger.debug("clicked on the second tab successfully, break")
                    break
                except (NoSuchElementException, TimeoutException, WebDriverException) as e:
                    # We try adb tap if the above methods fail
                    As('input tap 325 2100', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                    self.logger.debug('Appium Exception:' + str(e) + ', but continue with direct adb tap')
                    break
                    try:
                        self.logger.debug("try to click with the third method")
                        gt_el = self.wait.until(EC.element_to_be_clickable((By.XPATH, '(//android.view.ViewGroup[@resource-id="com.moji.mjweather:id/eui"])[3]')))
                        gt_el.click()
                        break
                    except (NoSuchElementException, TimeoutException, WebDriverException) as e:
                        fail_count += 1
                        self.logger.debug('Appium Exception:' + str(e))
                        if self.driver.current_package != 'com.moji.mjweather':
                            break
                        continue
        def checker():
            while self.driver.current_package == 'com.moji.mjweather':
                try:
                    dialog_el = self.driver.find_element(AppiumBy.ID, 'com.moji.mjweather:id/aui')
                    dialog_el.click()
                except (NoSuchElementException, WebDriverException) as e:
                    pass
                time.sleep(5)

        # t = threading.Thread(target=checker)
        # t.start()
        # return t

    def tiktok_handler(self):
        if self.catch_all:
            time.sleep(3)
            return
        # //android.widget.RelativeLayout/android.widget.ImageView[2]
        if self.driver.current_package == 'com.zhiliaoapp.musically':
            try:
                while True:
                    rs_el = self.driver.find_element(AppiumBy.XPATH, '//android.widget.RelativeLayout/android.widget.ImageView[2]')
                    el_x  = rs_el.location['x']
                    el_y  = rs_el.location['y']
                    if el_x > 900 and el_x < 1000 and el_y > 700 and el_y < 800:
                        self.logger.info("clicking on the tiktok dialog")
                        self.driver.tap([(el_x, el_y)], 200)
                    else:
                        break

            except (NoSuchElementException, WebDriverException) as e:
                pass

    def slack_handler(self):
        if self.driver.current_package == 'com.Slack':
            try:
                el1 = self.driver.find_element(AppiumBy.XPATH, '(//android.widget.ImageView[@content-desc="已折叠"])[1]')
                el1.click()
            except (NoSuchElementException, WebDriverException) as e:
                try:
                    el2 = self.driver.find_element(by=AppiumBy.ACCESSIBILITY_ID, value="oaram开发")
                    el2.click()
                except (NoSuchElementException, WebDriverException) as e:
                    pass

    def wechat_handler(self):
        while self.driver.current_package == 'com.tencent.mm':
            try:
                self.logger.debug('Trying to click on the "发现" element')
                el3 = self.driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value="new UiSelector().text(\"发现\").instance(0)")
                el3.click()
            except (NoSuchElementException, WebDriverException) as e:
                self.logger.error(f"Error while clicking on '发现': {e}")
                continue

            try:
                self.logger.debug('Trying to click on the "朋友圈" element')
                el4 = self.driver.find_element(by=AppiumBy.ANDROID_UIAUTOMATOR, value="new UiSelector().text(\"朋友圈\")")
                el4.click()
                time.sleep(2)
                break
            except (NoSuchElementException, WebDriverException) as e:
                self.logger.error(f"Error while clicking on '朋友圈': {e}")
                continue

    def skydrive_handler(self):
        while self.driver.current_package == 'com.microsoft.skydrive':
            # Perform three taps at the fixed coordinates
            self.logger.info("Tapping on coordinates (311, 2112) three times.")
            for _ in range(3):
                As('input tap 311 2112', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                time.sleep(1)  # Short delay between taps
            # After tapping, you can proceed to perform further actions or AI checks if needed
            self.logger.info("Taps completed, proceeding with further actions.")
            time.sleep(5)
            break

    def close(self):
        if not self.dummy:
            self.driver.quit()
    def cleanup(self):
        try:
            Aa("uninstall", "io.appium.uiautomator2.server", device_serial=self.device_id)
        except:
            pass
        try:
            Aa("uninstall", "io.appium.uiautomator2.server.test", device_serial=self.device_id)
        except:
            pass
    
    def get_criteria(self, activity: str) -> str:
        if 'com.tencent.mm' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于微信朋友圈界面

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'com.microsoft.skydrive' in activity:
            self.skydrive_handler()
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于OneDrive文件列表界面
            - 文件列表区域可正常滑动。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'easybike' in activity:
            criteria = """
            我在进行HelloBike应用的Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于哈喽打车页面, 而非HelloBike主页
              - 错误状态：HelloBike主页上方有三排二级页面图标，上方背景是浅白浅蓝色。
              - 正确状态：哈喽打车已经是二级页面上方没有别的二级页面图标，上方背景是青绿色。
            - 我需要滑动地图（操作界面上半部分中央），因此需要确保没有任何浮窗或弹出窗口阻挡界面上半部分中央，但请勿过度判断：
              - 地图右上部分有一个浮动的小广告图标是不影响操作的。
              - 没有覆盖在地图上的弹窗也是不影响操作的（如界面下方的目的地对话框、优惠券信息）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'iqiyi' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于iQIYI应用首页。
            - 没有任何浮窗或弹出窗口阻挡界面。
            - 没有点击到非预期的页面（如视频播放页面）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'qqdownloader' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于软件界面。
            - 没有任何浮窗或弹出窗口影响我滑动页面主体
            （注意：toast信息不是弹窗，不会影响我的滑动操作，请勿误判）。
            （注意：不在软件滑动区域主体的浮标不是弹窗，不会影响我的滑动操作，请勿误判）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'moji' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断墨迹天气应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于“发现”页面（标题栏显示“发现”），而非天气预报首页
            - 可以滑动“发现”页面的内容，因此没有大型浮窗或弹出窗口阻挡界面主体区域滑动

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'musically' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于抖音（TikTok）主界面。
            - 没有任何浮窗或弹出窗口遮挡页面（注意：toast信息不是弹窗，不会影响我的滑动操作，请勿误判）。
            - 视频内容应可见，界面元素正常显示。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        elif 'facebook.katana' in activity:
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。
            
            正确状态的标准：
            - 当前处于Facebook主界面。
            - 没有任何浮窗或弹出窗口遮挡页面（注意：toast信息不是弹窗，不会影响我的滑动操作，请勿误判）。
            - 页面内容正常显示，可以滑动页面。
            
            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
        return criteria

    def pre_execute_app(self, activity: str, skip_check = False) -> PreExecuteResult:
        ret =  PreExecuteResult(analysis="No analysis", is_correct_state=True, possible_fix="", screenshot_path="")
        if 'com.tencent.mm' in activity:
            self.wechat_handler()
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于微信朋友圈界面

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
        elif 'com.microsoft.skydrive' in activity:
            self.skydrive_handler()
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于OneDrive文件列表界面
            - 文件列表区域可正常滑动。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
        elif 'androidqqmail' in activity:
            # qqmail
            if self.device_id == 'px4:45555' or self.device_id == 'px1:15555':
                As('input tap 750 600', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
            elif self.device_id == 'px2:25555':
                As('input tap 560 510', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
            else:
                self.logger.error(f"The qqmail click position of device {self.device_id} is not defined")
        elif 'easybike' in activity:
            self.easybike_handler()
            criteria = """
            我在进行HelloBike应用的Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于哈喽打车页面, 而非HelloBike主页
              - 错误状态：HelloBike主页上方有三排二级页面图标，上方背景是浅白浅蓝色。
              - 正确状态：哈喽打车已经是二级页面上方没有别的二级页面图标，上方背景是青绿色。
            - 我需要滑动地图（操作界面上半部分中央），因此需要确保没有任何浮窗或弹出窗口阻挡界面上半部分中央，但请勿过度判断：
              - 地图右上部分有一个浮动的小广告图标是不影响操作的。
              - 没有覆盖在地图上的弹窗也是不影响操作的（如界面下方的目的地对话框、优惠券信息）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
        elif 'iqiyi' in activity:
            self.iqiyi_handler()
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于iQIYI应用首页。
            - 没有任何浮窗或弹出窗口阻挡界面。
            - 没有点击到非预期的页面（如视频播放页面）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
        elif 'temu' in activity:
            # self.temu_handler()
            pass
        elif 'qqdownloader' in activity:
            self.qqdownloader_handler()
            self.logger.info("Wait for 5 seconds after clicking on the mini program tab")
            time.sleep(5) # wait for the page to load
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于软件界面。
            - 没有任何浮窗或弹出窗口影响我滑动页面主体
            （注意：toast信息不是弹窗，不会影响我的滑动操作，请勿误判）。
            （注意：不在软件滑动区域主体的浮标不是弹窗，不会影响我的滑动操作，请勿误判）。

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
        elif 'maps' in activity:
            self.map_handler()
        elif 'qqmusic' in activity:
            self.qqmusic_handler()
        elif 'spotify' in activity:
            self.spotify_handler()
        elif 'moji' in activity:
            self.moji_handler()
            self.logger.info("Wait 5 seconds after click on 时景")
            time.sleep(5) # wait for the page to load
            criteria = """
            我在进行Android应用自动化测试，请分析以下截图，并判断墨迹天气应用程序是否处于正确的状态。

            正确状态的标准：
            - 当前处于“发现”页面（标题栏显示“发现”），而非天气预报首页
            - 可以滑动“发现”页面的内容，因此没有大型浮窗或弹出窗口阻挡界面主体区域滑动

            请按照以下格式返回：
            - analysis: 对应用当前状态的分析。
            - is_correct_state: 应用是否处于正确状态（True 或 False）。
            - possible_fix: 如果不正确，可能的解决方法。
            """
            ret = self.analyze_app_state(criteria)
            if not ret.is_correct_state:
                self.logger.debug("OpenAI check failed, use adb input again and check again")
                # Use adb input again
                As('input tap 325 2100', [AsOption.STDERR_TO_STDOUT, AsOption.STDOUT_NO_PRINT], device_serial=self.device_id)
                self.logger.info("Wait 5 seconds after clicking again on 时景")
                time.sleep(5)  # wait for the page to load
                ret = self.analyze_app_state(criteria)
            else:
                self.logger.debug("App is in correct state after OpenAI check")

        elif 'musically' in activity:
            self.tiktok_handler()
            if not skip_check:
                time.sleep(2)
                criteria = """
                我在进行Android应用自动化测试，请分析以下截图，并判断应用程序是否处于正确的状态。

                正确状态的标准：
                - 当前处于抖音（TikTok）主界面。
                - 没有任何浮窗或弹出窗口遮挡页面（注意：toast信息不是弹窗，不会影响我的滑动操作，请勿误判）。
                - 视频内容应可见，界面元素正常显示。

                请按照以下格式返回：
                - analysis: 对应用当前状态的分析。
                - is_correct_state: 应用是否处于正确状态（True 或 False）。
                - possible_fix: 如果不正确，可能的解决方法。
                """
                ret = self.analyze_app_state(criteria)
        elif 'Slack' in activity:
            self.slack_handler()
        elif 'com.facebook.katana' in activity:
            pass
        return ret


if __name__ == '__main__':
    logger = setup_logging()
    logger.info("Setting up appium")
    # appium = AppiumWrapper('px4:45555', logger, os.getcwd(), dummy=True)
    appium = AppiumWrapper('px3:35555', logger, os.getcwd())
    # appium.easybike_handler()
    # appium.slack_handler()
    # fg_act = get_foreground_activities()[0]
    # logger.info(fg_act)

    # criteria = appium.get_criteria(fg_act)
    # ret = appium.analyze_app_state(criteria)

    act = appium.driver.current_package
    logger.info(act)
    ret =appium.pre_execute_app(act)
    print(ret)
    # appium.moji_handler()
    appium.close()
