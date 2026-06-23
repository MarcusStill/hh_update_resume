import configparser
import os
import re
import sys
import time
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# Определение путей
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
AUTH_STATE_FILE = os.path.join(BASE_DIR, "auth_state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")


def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_message + "\n")
    except Exception as e:
        print(f"Ошибка записи в лог-файл: {e}")


def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config


def check_and_lift_resumes():
    """Проверяет резюме.

    Возвращает кортеж из двух элементов: (status_bool, wait_seconds_or_none)
    """
    config = load_config()
    resumes = config["RESUMES"]

    if not resumes:
        log_message("[Ошибка] Список резюме в config.ini пуст.")
        return False, None

    name, url = list(resumes.items())[0]

    with sync_playwright() as p:
        if not os.path.exists(AUTH_STATE_FILE):
            log_message(
                "[Внимание] Сессия не найдена. Требуется авторизация через .py!"
            )
            return False, None

        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_STATE_FILE)
        page = context.new_page()

        log_message(f"[Проверка] Переход на резюме '{name}'...")
        try:
            page.goto(url)
            page.wait_for_load_state("networkidle")

            if "login" in page.url:
                log_message(
                    f"[Ошибка] Сессия устарела. Удалите {AUTH_STATE_FILE}."
                )
                browser.close()
                return False, None

            lift_button = page.locator(
                "button:has-text('Поднять в поиске')"
            ).first

            if lift_button.is_visible() and lift_button.is_enabled():
                log_message(f"Кнопка доступна. Нажимаю для '{name}'...")
                lift_button.click()
                time.sleep(3)
                log_message(f"[УСПЕХ] Резюме '{name}' успешно поднято!")
                browser.close()
                return True, None

            # Кнопка недоступна, парсим время
            status_section = page.locator(
                "div:has-text('Можно сегодня в'), div:has-text('Можно завтра в'), div:has-text('Поднятие резюме')"
            ).last

            if status_section.is_visible():
                text = status_section.inner_text()
                clean_text = re.sub(r"\s+", " ", text).strip()
                clean_text = re.sub(r"(?<=\s)\s+", "", clean_text)

                match = re.search(r"(\d{2}):(\d{2})", clean_text)
                if match:
                    target_hour = int(match.group(1))
                    target_minute = int(match.group(2))

                    now = datetime.now()
                    is_tomorrow = "завтра" in clean_text.lower()

                    target_time = now.replace(
                        hour=target_hour,
                        minute=target_minute,
                        second=0,
                        microsecond=0,
                    )
                    if is_tomorrow:
                        target_time += timedelta(days=1)

                    time_diff = target_time - now
                    seconds_to_wait = int(time_diff.total_seconds())

                    day_str = "завтра" if is_tomorrow else "сегодня"
                    log_message(f"[Отказ] Можно {day_str} в {match.group(0)}")

                    browser.close()
                    return False, seconds_to_wait
                else:
                    log_message(
                        f"[Отказ] Время не распознано. Текст: {clean_text}"
                    )
            else:
                log_message("[Отказ] Текст с ограничением времени не найден.")

        except Exception as e:
            log_message(f"[Ошибка при выполнении] {e}")

        browser.close()
    return False, None


def main():
    log_message("Сервис запущен в фоновом режиме с умным расчетом времени.")

    while True:
        status, wait_seconds = check_and_lift_resumes()

        if status:
            sleep_time = 14460
            log_message(
                f"Следующая плановая проверка через 4 часа ({sleep_time} сек)..."
            )
        elif wait_seconds is not None and wait_seconds > 0:
            sleep_time = wait_seconds + 60
            minutes = sleep_time // 60
            log_message(
                f"Умный сон: засыпаю на {minutes} мин. ({sleep_time} сек.) до момента разблокировки."
            )
        else:
            try:
                config = load_config()
                sleep_time = int(
                    config["SETTINGS"]["fallback_interval_seconds"]
                )
                log_message(
                    f"[Защита] Статус не определен (ошибка/смена дизайна). "
                    f"Применяем резервный интервал из конфига: {sleep_time} сек."
                )
            except Exception:
                sleep_time = 14400
                log_message(
                    f"[Защита] Не удалось прочитать конфиг. "
                    f"Применяем аварийный сон: {sleep_time} сек."
                )

        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
