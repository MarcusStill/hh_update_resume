import configparser
import os
import re
import sys
import time
import signal
import threading
import atexit
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from pathlib import Path

# ------------------------------------------------------------
# Версия приложения
# ------------------------------------------------------------
APP_VERSION = "2.4.0"

# ------------------------------------------------------------
# Базовые пути
# ------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / "pw-browsers")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ------------------------------------------------------------
# Конфигурация
# ------------------------------------------------------------
CONFIG_FILE = BASE_DIR / "config.ini"
AUTH_STATE_FILE = BASE_DIR / "auth_state.json"
LOG_FILE = BASE_DIR / "log.txt"
PID_FILE = BASE_DIR / "hh_autoupdater.pid"

# Настройка логирования с ротацией
logger = logging.getLogger("hh_updater")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)


# ------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------
def disable_quick_edit():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h_input = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_input, ctypes.byref(mode)):
            new_mode = (mode.value & ~0x0040) | 0x0080
            kernel32.SetConsoleMode(h_input, new_mode)
    except Exception:
        pass


def load_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config


def ensure_browser_installed():
    """Устанавливает браузер через отдельный поток, если отсутствует."""
    browsers_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    # Сортировка и взятие последней версии
    chromium_dirs = sorted(browsers_dir.glob("chromium-*"))
    if chromium_dirs:
        chromium_dir = chromium_dirs[-1]  # берём последнюю (самую свежую)
        if sys.platform == "win32":
            chrome_exe = chromium_dir / "chrome-win" / "chrome.exe"
        else:
            chrome_exe = chromium_dir / "chrome-linux" / "chrome"
        if chrome_exe.exists():
            return False

    logger.info("Проверка браузера.")

    def install():
        from playwright.__main__ import main as playwright_main
        old_argv = sys.argv.copy()
        try:
            sys.argv = ["playwright", "install", "chromium"]
            playwright_main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    thread = threading.Thread(target=install)
    thread.start()
    thread.join()
    if not list(browsers_dir.glob("chromium-*")):
        raise RuntimeError("Не удалось установить браузер.")
    logger.info("Локальный браузер успешно развернут.")
    return True


def is_process_alive(pid):
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    kernel32.CloseHandle(handle)
                    return exit_code.value == 259
                kernel32.CloseHandle(handle)
            return False
        except:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except:
            return False


# Обработка PID-файла
def create_pid_file():
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            old_pid = int(content)
            if is_process_alive(old_pid):
                logger.error(f"Процесс с PID {old_pid} уже запущен. Выход.")
                sys.exit(1)
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            logger.warning("Повреждён PID-файл. Выполняю восстановление.")
            PID_FILE.unlink(missing_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def remove_pid_file():
    PID_FILE.unlink(missing_ok=True)


# Регистрация очистки при завершении
atexit.register(remove_pid_file)

# Глобальные переменные для graceful shutdown
browser_instance = None
should_exit = False


def signal_handler(sig, frame):
    global should_exit
    logger.info("Получен сигнал завершения. Останавливаемся...")
    should_exit = True
    if browser_instance:
        try:
            browser_instance.close()
        except:
            pass


# ------------------------------------------------------------
# Авторизация с повторными попытками
# ------------------------------------------------------------
def perform_auth(p, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Попытка авторизации #{attempt} из {max_attempts}")
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://hh.ru")
        logger.info("Пожалуйста, войдите в аккаунт в открывшемся окне. Таймаут 3 минуты.")

        auth_success = False
        start_time = time.time()
        while time.time() - start_time < 180:
            try:
                current_url = page.url.lower()
                if "login" not in current_url and "hh.ru" in current_url:
                    try:
                        if (page.locator('a[data-qa="mainmenu_resumes"]').count() > 0 or
                                page.locator('button:has-text("Выйти")').count() > 0):
                            auth_success = True
                            break
                    except Exception:
                        pass
            except Exception:
                logger.warning("Окно авторизации было закрыто.")
                break
            time.sleep(2)

        if not auth_success:
            # Попробуем финальную проверку элементов
            try:
                if (page.locator('a[data-qa="mainmenu_resumes"]').count() > 0 or
                        page.locator('button:has-text("Выйти")').count() > 0):
                    auth_success = True
            except:
                pass

        if auth_success:
            logger.info("Авторизация подтверждена. Сохраняем сессию...")
            context.storage_state(path=str(AUTH_STATE_FILE))
            browser.close()
            logger.info("Сессия сохранена в auth_state.json")
            return True
        else:
            logger.warning(
                "Не удалось подтвердить авторизацию. Возможно, вход не выполнен или страница загрузилась некорректно.")
            browser.close()
            if attempt < max_attempts:
                logger.info("Повторная попытка через 10 секунд...")
                time.sleep(10)
            else:
                logger.error("Все попытки авторизации исчерпаны.")
    return False


# ------------------------------------------------------------
# Основная логика поднятия резюме
# ------------------------------------------------------------
def get_resume_status(page, resume_name, url):
    logger.info(f"Проверка резюме '{resume_name}'...")

    for attempt in range(3):
        try:
            # Короткая пауза
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)  # стабилизация DOM
            break
        except Exception as e:
            if attempt == 2:
                logger.error(f"Не удалось загрузить страницу после 3 попыток: {e}")
                return False, None
            wait_time = (attempt + 1) * 5
            logger.warning(f"Ошибка загрузки, повтор через {wait_time} сек...")
            time.sleep(wait_time)
    else:
        return False, None

    # Проверка на редирект на логин – автоматическое удаление сессии
    if any(marker in page.url.lower() for marker in ("/login", "/auth", "account/login", "oauth/authorize")):
        logger.warning("Сессия устарела. Удаляю auth_state.json.")
        try:
            AUTH_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False, None

    button_selector = 'button[data-qa="resume-update-button"]'
    fallback_selectors = [
        'button:has-text("Поднять в поиске")',
        'button:has-text("Поднять")'
    ]

    lift_button = None
    for sel in [button_selector] + fallback_selectors:
        locator = page.locator(sel)
        if locator.count():
            lift_button = locator
            break

    if lift_button:
        is_visible = False
        is_enabled = False
        try:
            lift_button.wait_for(state="visible", timeout=5000)
            is_visible = True
        except PlaywrightTimeoutError:
            pass
        if is_visible:
            try:
                is_enabled = lift_button.is_enabled(timeout=5000)
            except PlaywrightTimeoutError:
                pass

        if is_visible and is_enabled:
            logger.info(f"Кнопка доступна для '{resume_name}'. Нажимаем...")
            # Упрощённая проверка успеха – считаем клик успешным
            try:
                lift_button.click(timeout=10000)
                page.wait_for_timeout(3000)
                logger.info(f"[УСПЕХ] Команда поднятия отправлена для '{resume_name}'.")
                return True, None
            except Exception as e:
                logger.error(f"Ошибка при клике: {e}")
                return False, None
        else:
            logger.info(f"Кнопка для '{resume_name}' неактивна или невидима. Ищем время следующего поднятия...")
    else:
        logger.info(f"Кнопка не найдена для '{resume_name}'. Ищем информацию о времени...")

    # Поиск времени следующего поднятия
    time_pattern = re.compile(r"(сегодня|завтра)\s+в\s+(\d{2}):(\d{2})", re.IGNORECASE)
    status_text = ""
    try:
        possible_divs = page.locator(
            "div:has-text('Можно сегодня в'), div:has-text('Можно завтра в'), div:has-text('Поднятие резюме')")
        if possible_divs.count():
            status_text = possible_divs.last.inner_text()
    except:
        pass

    if not status_text:
        # Безопасное получение текста body
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""
        match = time_pattern.search(body_text)
        if match:
            day_word = match.group(1).lower()
            hour = int(match.group(2))
            minute = int(match.group(3))
            now = datetime.now()
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day_word == "завтра":
                target_time += timedelta(days=1)
            elif day_word == "сегодня" and target_time < now:
                target_time += timedelta(days=1)
            diff_seconds = max(0, int((target_time - now).total_seconds()))
            logger.info(
                f"Найдено время следующего поднятия: {target_time.strftime('%Y-%m-%d %H:%M')} (через {diff_seconds // 60} мин)")
            return False, diff_seconds
        else:
            logger.warning(f"Не удалось найти информацию о времени поднятия для '{resume_name}'.")
            return False, None
    else:
        clean_text = re.sub(r"\s+", " ", status_text).strip()
        match = time_pattern.search(clean_text)
        if match:
            day_word = match.group(1).lower()
            hour = int(match.group(2))
            minute = int(match.group(3))
            now = datetime.now()
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day_word == "завтра":
                target_time += timedelta(days=1)
            elif day_word == "сегодня" and target_time < now:
                target_time += timedelta(days=1)
            diff_seconds = max(0, int((target_time - now).total_seconds()))
            logger.info(
                f"Для '{resume_name}' следующее поднятие: {target_time.strftime('%Y-%m-%d %H:%M')} (через {diff_seconds // 60} мин)")
            return False, diff_seconds
        else:
            logger.warning(f"Формат времени не распознан: {clean_text}")
            return False, None


def check_and_lift_resumes():
    config = load_config()
    if not config.has_section("RESUMES"):
        logger.error("Секция [RESUMES] отсутствует в config.ini")
        return False, None

    resumes = config["RESUMES"]
    valid_resumes = [(k, v) for k, v in resumes.items() if v and not v.startswith("#")]
    if not valid_resumes:
        logger.error("Не найдено ни одной валидной ссылки в [RESUMES]")
        return False, None

    wait_times = []
    any_success = False

    with sync_playwright() as p:
        global browser_instance

        if not AUTH_STATE_FILE.exists():
            if not perform_auth(p):
                return False, None

        browser_instance = p.chromium.launch(headless=True)
        context = browser_instance.new_context(storage_state=str(AUTH_STATE_FILE))
        page = context.new_page()

        for name, url in valid_resumes:
            if should_exit:
                break
            success, wait_sec = get_resume_status(page, name, url)
            if success:
                any_success = True
            elif wait_sec is not None:
                wait_times.append(wait_sec)

        # Явное закрытие контекста и браузера
        try:
            context.close()
        except Exception:
            pass
        try:
            browser_instance.close()
        except Exception:
            pass
        browser_instance = None

    if any_success:
        return True, None
    elif wait_times:
        return False, min(wait_times)
    else:
        return False, None


# ------------------------------------------------------------
# Обратный отсчёт с обновлением раз в 5 секунд
# ------------------------------------------------------------
def console_countdown(sleep_time):
    end_time = time.time() + sleep_time
    while not should_exit:
        remaining = int(end_time - time.time())
        if remaining <= 0:
            break
        hours, rem = divmod(remaining, 3600)
        minutes, _ = divmod(rem, 60)
        print(
            f"\r⏳ До следующей проверки: "
            f"{hours:02d}ч {minutes:02d}мин  ",
            end="",
            flush=True
        )
        # Проверяем завершение каждые 5 секунд
        for _ in range(min(5, remaining)):
            if should_exit:
                break
            time.sleep(1)
    print("\r" + " " * 50 + "\r", end="", flush=True)


# ------------------------------------------------------------
# Основной цикл
# ------------------------------------------------------------
def main():
    disable_quick_edit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, signal_handler)

    try:
        ensure_browser_installed()
    except Exception as e:
        logger.critical(f"Не удалось установить браузер: {e}")
        sys.exit(1)

    create_pid_file()
    logger.info(f"HH Resume Updater {APP_VERSION} запущен. PID={os.getpid()}")

    consecutive_errors = 0

    while not should_exit:
        try:
            status, wait_seconds = check_and_lift_resumes()
        except Exception as e:
            logger.error(f"Необработанное исключение: {e}", exc_info=True)
            status = False
            wait_seconds = None

        if should_exit:
            break

        if status:
            consecutive_errors = 0
            try:
                config = load_config()
                sleep_time = int(config["SETTINGS"].get("check_interval_seconds", "14500"))
                logger.info(f"Резюме обновлены. Плановый сон: {sleep_time} сек.")
            except Exception:
                sleep_time = 14400
                logger.warning("Ошибка чтения check_interval. Использую 14400 сек.")
        elif wait_seconds is not None and wait_seconds > 0:
            consecutive_errors = 0
            sleep_time = wait_seconds + 300
            logger.info(
                f"Рассчитано время до следующего поднятия: {wait_seconds} сек. Сон с запасом: {sleep_time} сек.")
        else:
            consecutive_errors += 1
            if consecutive_errors < 3:
                sleep_time = 300
                logger.warning(f"Сбой #{consecutive_errors}. Быстрая перепроверка через 5 мин.")
            else:
                try:
                    config = load_config()
                    sleep_time = int(config["SETTINGS"].get("fallback_interval_seconds", "14500"))
                    logger.warning(f"Критический сбой (3 раза). Резервный сон: {sleep_time} сек.")
                except Exception:
                    sleep_time = 14400
                    logger.warning("Ошибка чтения fallback_interval. Аварийный сон 14400 сек.")
                consecutive_errors = 0

        console_countdown(sleep_time)

    logger.info("Сервис остановлен.")
    remove_pid_file()


if __name__ == "__main__":
    main()