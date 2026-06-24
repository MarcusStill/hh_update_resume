import configparser
import os
import re
import sys
import time
from datetime import datetime, timedelta

# 1. Корректное определение базовой папки для .exe и .py
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. Изоляция бинарников браузера в папке приложения (решает ошибку Temp)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(BASE_DIR, "pw-browsers")

# Импортируем Playwright строго после установки переменной окружения
from playwright.sync_api import sync_playwright

CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
AUTH_STATE_FILE = os.path.join(BASE_DIR, "auth_state.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")


def log_message(message, newline=True):
    """Синхронная запись логов в консоль и в файл log.txt."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"

    if newline:
        print(full_message)
    else:
        print(full_message, end="", flush=True)

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(full_message + "\n")
    except Exception as e:
        print(f"\nОшибка записи в лог-файл: {e}")


def load_config():
    """Загружает файл конфигурации."""
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config


def check_and_lift_resumes():
    """Обходит все резюме из config.ini.

    Возвращает:
      - (True, None): Если хотя бы одно резюме было успешно поднято.
      - (False, min_wait_seconds): Если все заблокированы (время до ближайшего).
      - (False, None): При критической ошибке или устаревшей сессии.
    """
    config = load_config()

    if "RESUMES" not in config or not config["RESUMES"]:
        log_message("[Ошибка] Список резюме в config.ini пуст или секция [RESUMES] отсутствует.")
        return False, None

    resumes = config["RESUMES"]
    valid_resumes = [(k, v) for k, v in resumes.items() if v and not v.startswith('#')]

    if not valid_resumes:
        log_message("[Ошибка] В секции [RESUMES] не найдено ни одной правильной ссылки.")
        return False, None

    wait_times_pool = []
    any_resume_lifted = False

    with sync_playwright() as p:
        has_auth = os.path.exists(AUTH_STATE_FILE)

        # Режим АВТОРИЗАЦИИ (Если файла сессии еще нет)
        if not has_auth:
            log_message("[Авторизация] Сессия не найдена. Открываю окно браузера для входа...")
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()

            page.goto("https://hh.ru")
            log_message("[Авторизация] Ожидание входа пользователя (таймаут 3 минуты)...")

            try:
                page.wait_for_url(lambda u: "hh.ru" in u and "login" not in u, timeout=180000)
                time.sleep(5)
                context.storage_state(path=AUTH_STATE_FILE)
                log_message("[Авторизация] Успех! Сессия сохранена в auth_state.json. Перезапускаю процесс...")
                browser.close()
                return False, 5
            except Exception as e:
                log_message(f"[Ошибка авторизации] Время ожидания истекло или вход отменен: {e}")
                browser.close()
                return False, None

        # Режим МОНИТОРИНГА (Сессия есть, работаем в фоне)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_STATE_FILE)
        page = context.new_page()

        # Обход всех резюме по очереди
        for name, url in valid_resumes:
            log_message(f"[Проверка] Переход на резюме '{name}'...")
            try:
                page.goto(url)
                page.wait_for_load_state("networkidle")

                if "login" in page.url:
                    log_message(
                        f"[Ошибка] Авторизация устарела для '{name}'. Удалите файл auth_state.json для повторного входа.")
                    browser.close()
                    return False, None

                lift_button = page.locator("button:has-text('Поднять в поиске')").first

                if lift_button.is_visible() and lift_button.is_enabled():
                    log_message(f"Кнопка доступна. Нажимаю для '{name}'...")
                    lift_button.click()
                    time.sleep(3)
                    log_message(f"[УСПЕХ] Резюме '{name}' успешно поднято!")
                    any_resume_lifted = True
                    continue

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

                        target_time = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                        if is_tomorrow:
                            target_time += timedelta(days=1)

                        time_diff = target_time - now
                        seconds_to_wait = int(time_diff.total_seconds())

                        day_str = "завтра" if is_tomorrow else "сегодня"
                        log_message(
                            f"[{name}] Блокировка активна. Можно {day_str} в {match.group(0)} (осталось {seconds_to_wait // 60} мин).")

                        if seconds_to_wait > 0:
                            wait_times_pool.append(seconds_to_wait)
                    else:
                        log_message(f"[{name}] Ограничение активно, но формат времени не распознан: {clean_text}")
                else:
                    log_message(f"[{name}] Кнопка поднятия скрыта, информационный блок времени не найден.")

            except Exception as e:
                log_message(f"[Ошибка выполнения для '{name}']: {e}")

        browser.close()

    if any_resume_lifted:
        return True, None

    if wait_times_pool:
        return False, min(wait_times_pool)

    return False, None


def console_countdown(sleep_time):
    """Таймер обратного отсчета по системным часам ПК."""
    end_time = datetime.now() + timedelta(seconds=sleep_time)

    while True:
        now = datetime.now()
        remaining = end_time - now
        remaining_seconds = int(remaining.total_seconds())

        if remaining_seconds <= 0:
            break

        hours, remainder = divmod(remaining_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        # Выводим тикающую строчку с перезаписью (\r)
        print(f"\r⏳ До следующего обновления осталось: {hours:02d} ч. {minutes:02d} мин. {seconds:02d} сек.", end="",
              flush=True)
        time.sleep(0.5)

    print("\r" + " " * 70 + "\r", end="", flush=True)  # Очищаем строку логов


def main():
    log_message("Сервис автоматизации hh.ru успешно запущен.")

    # Счетчик последовательных ошибок подряд
    consecutive_errors = 0

    # Автоматическая загрузка Chromium при первом старте на любом ПК
    try:
        browsers_dir = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        if not os.path.exists(browsers_dir) or not os.listdir(browsers_dir):
            log_message("[Система] Локальный браузер не найден. Скачивание и подготовка Chromium...")
            from playwright.__main__ import main as playwright_cli
            sys.argv = ["playwright", "install", "chromium"]
            playwright_cli()
            log_message("[Система] Локальный браузер успешно развернут.")
    except Exception as e:
        log_message(f"[Система] Предупреждение при подготовке браузера: {e}")

    while True:
        status, wait_seconds = check_and_lift_resumes()

        if status:
            # Сброс ошибок при успешном поднятии
            consecutive_errors = 0
            try:
                config = load_config()
                sleep_time = int(config["SETTINGS"]["check_interval_seconds"])
                log_message(f"[Успех] Резюме обновлено. Плановый сон из config.ini: {sleep_time} сек.")
            except Exception:
                sleep_time = 14400
                log_message("[Успех] Ошибка конфига. Аварийный сон: 14400 сек.")

        elif wait_seconds is not None and wait_seconds > 0:
            # Сброс ошибок: сайт ответил корректно, просто кнопка заблокирована
            consecutive_errors = 0
            sleep_time = wait_seconds + 60
            minutes = sleep_time // 60
            log_message(f"Сон: рассчитано время ожидания ({minutes} мин).")

        else:
            # Произошла ошибка (функция вернула wait_seconds=None)
            consecutive_errors += 1

            if consecutive_errors < 3:
                sleep_time = 300  # Быстрая перепроверка через 5 минут
                log_message(
                    f"[Сбой #{consecutive_errors}] Ошибка сети или структуры сайта. Быстрая перепроверка через 5 минут ({sleep_time} сек)...")
            else:
                # Если упало 3 раза подряд — уходим в тяжелый сон из конфигурации
                try:
                    config = load_config()
                    sleep_time = int(config["SETTINGS"]["fallback_interval_seconds"])
                    log_message(
                        f"[Критический сбой] Ошибка повторилась 3 раза подряд. Включаем резервное ожидание из config.ini: {sleep_time} сек.")
                except Exception:
                    sleep_time = 14400
                    log_message(f"[Защита] Ошибка чтения конфига. Применяем аварийный сон: {sleep_time} сек.")

                # Сбрасываем счетчик после длинного сна, чтобы начать заново с коротких попыток
                consecutive_errors = 0

        # Запускаем точный отсчет времени в консоли
        console_countdown(sleep_time)


if __name__ == "__main__":
    main()
