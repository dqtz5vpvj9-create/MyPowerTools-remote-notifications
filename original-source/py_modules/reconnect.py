import cv2
import pytesseract
import pyautogui
import numpy as np
import time

from logging_lib import setup_logging
from aip import AipOcr
class ReconnectHandler:
    def __init__(self, logger):
        # Load the template image
        self.logger = logger
        APP_ID = '30443450'
        API_KEY = 'GVhIULYvRWdjvYexGaGrExGf'
        SECRET_KEY = 'YM9Kz6rfcTOdFUBuZAFmRrGVSCGlLnOP'
        self.client = AipOcr(APP_ID, API_KEY, SECRET_KEY)
        self.template = cv2.imread(r"C:\Users\lixinrui\Downloads\reload_button_template.png", cv2.IMREAD_GRAYSCALE)
    def handle_reconnect(self):
        # Get a screenshot of the entire screen
        screenshot = pyautogui.screenshot()
        screenshot.save(r"C:\Users\lixinrui\Downloads\test.png")
        with open(r"C:\Users\lixinrui\Downloads\test.png", "rb") as f:
            image = f.read()
        options = {}
        options["language_type"] = "CHN_ENG"
        options["detect_direction"] = "true"
        options["detect_language"] = "true"
        options["probability"] = "true"

        text = self.client.basicGeneral(image, options)
        # Perform OCR on the screenshot
        for i in text["words_result"]:
            logger.debug(i["words"])

        # Check if the text "Reload Window" is present in the OCR output
        reload_needed = False
        if "测试字符" in text:
            reload_needed = True

            # Find the location of the text "Reload Window"
            text_location = pytesseract.image_to_boxes(screenshot)
            lines = text_location.split("\n")
            for line in lines:
                if "转到" in line:
                    x, y, w, h = int(line.split(" ")[1]), int(line.split(" ")[2]), int(line.split(" ")[3]), int(line.split(" ")[4])
                    break

            # Calculate the center of the text
            ocr_button_x = x + w / 2
            ocr_button_y = y + h / 2

        # Perform template matching to find the "Reload Window" button
        screenshot2 = cv2.cvtColor(np.array(pyautogui.screenshot()), cv2.COLOR_BGR2RGB)
        gray_screenshot = cv2.cvtColor(screenshot2, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_screenshot, self.template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > 0.8:
            # Calculate the position of the "Reload Window" button
            match_button_x = max_loc[0] + self.template.shape[1] / 2
            match_button_y = max_loc[1] + self.template.shape[0] / 2

            # Check if the button location found by OCR and image matching are close enough
            if reload_needed and abs(ocr_button_x - match_button_x) < 10 and abs(ocr_button_y - match_button_y) < 10:
                # Click the "Reload Window" button
                pyautogui.click(match_button_x, match_button_y)


logger = setup_logging()
# Create an instance of the ReconnectHandler
handler = ReconnectHandler(logger)

handler.handle_reconnect()