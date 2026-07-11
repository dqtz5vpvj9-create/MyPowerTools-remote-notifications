# This sample code uses the Appium python client v2
# pip install Appium-Python-Client
# Then you can paste this into a file and simply run with Python

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy

# For W3C actions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

caps = {}
caps["platformName"] = "Android"
caps["appium:platformVersion"] = "11"
caps["appium:deviceName"] = "pixel4a:5555"
caps["appium:appPackage"] = "com.futuremark.pcmark.android.benchmark"
caps["appium:appActivity"] = "com.futuremark.gypsum.activity.SplashPageActivity"
caps["appium:resetKeyboard"] = True
caps["appium:noReset"] = True
caps["appium:ensureWebviewsHavePages"] = True
caps["appium:nativeWebScreenshot"] = True
caps["appium:newCommandTimeout"] = 3600
caps["appium:connectHardwareKeyboard"] = True

driver = webdriver.Remote("http://127.0.0.1:4723/wd/hub", caps)

el1 = driver.find_element(by=AppiumBy.CLASS_NAME, value="(//android.view.View[@content-desc=\"运行\"])[1]")
el1.click()

driver.quit()
