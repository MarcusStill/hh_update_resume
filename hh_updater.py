import atexit
import base64
import configparser
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ------------------------------------------------------------
# Версия приложения
# ------------------------------------------------------------
APP_VERSION = "2.8.8"

# ------------------------------------------------------------
# Базовые пути
# ------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BASE_DIR / "pw-browsers")

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

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
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

# ------------------------------------------------------------
# Вспомогательные функции для диагностики и очистки
# ------------------------------------------------------------
import glob

def cleanup_old_files(pattern="auth_failed_*.html", keep=5):
    """Оставляет только последние N файлов, остальные удаляет."""
    files = sorted(glob.glob(str(pattern)), key=os.path.getmtime, reverse=True)
    for f in files[keep:]:
        try:
            os.remove(f)
            logger.info(f"Удалён старый файл диагностики: {f}")
        except Exception as e:
            logger.warning(f"Не удалось удалить {f}: {e}")

def is_logged_in_url(url: str) -> bool:
    """Проверяет, указывает ли URL на личный кабинет соискателя."""
    url = url.lower()
    return (
        "login" not in url
        and (
            "/applicant" in url
            or "?role=applicant" in url
            or "&role=applicant" in url
            or "role=applicant" in url
        )
    )

# ------------------------------------------------------------
# Обфускация данных
# ------------------------------------------------------------
def _get_obfuscation_key() -> int:
    hostname = socket.gethostname()
    salt = "HH_AUTO_UPDATER_2024"
    combined = (hostname + salt).encode('utf-8')
    hash_bytes = hashlib.sha256(combined).digest()[:4]
    return int.from_bytes(hash_bytes, byteorder='big')

def obfuscate_data(data: dict) -> str:
    key = _get_obfuscation_key() & 0xFF
    json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    obfuscated = ''.join(chr(ord(c) ^ key) for c in json_str)
    return base64.b64encode(obfuscated.encode('utf-8')).decode('ascii')

def deobfuscate_data(obfuscated_str: str) -> dict:
    key = _get_obfuscation_key() & 0xFF
    try:
        decoded = base64.b64decode(obfuscated_str.encode('ascii')).decode('utf-8')
        json_str = ''.join(chr(ord(c) ^ key) for c in decoded)
        return json.loads(json_str)
    except Exception as e:
        raise ValueError(f"Не удалось деобфусцировать данные: {e}")

def save_auth_state(context, path: Path) -> None:
    """Сохраняет сессию с обфускацией (без валидации)."""
    try:
        auth_data = context.storage_state()
        obfuscated = obfuscate_data(auth_data)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(obfuscated)
        logger.info(f"Сессия сохранена в {path}")
    except Exception as e:
        logger.error(f"Ошибка сохранения сессии: {e}")
        raise

def load_auth_state(path: Path) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            obfuscated = f.read().strip()
        if not obfuscated:
            raise ValueError("Файл сессии пуст")
        return deobfuscate_data(obfuscated)
    except Exception as e:
        logger.warning(f"Ошибка загрузки сессии: {e}")
        raise

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

    logger.info("Проверка наличия браузера.")

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
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
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
    """Обработчик сигналов - только устанавливает флаг выхода."""
    global should_exit
    logger.info("Получен сигнал завершения. Останавливаемся...")
    should_exit = True
    # Не закрываем браузер здесь, чтобы избежать конфликтов с основным потоком


# ------------------------------------------------------------
# Авторизация с повторными попытками
# ------------------------------------------------------------
def perform_auth(p, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Попытка авторизации #{attempt} из {max_attempts}")
        browser = None
        context = None
        try:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context()
            page = context.new_page()

            try:
                page.goto(
                    "https://hh.ru",
                    wait_until="domcontentloaded",
                    timeout=45000
                )
                logger.info(f"Главная страница загружена: {page.url}")
            except Exception as e:
                logger.error(f"Не удалось загрузить страницу: {e}")
                raise

            logger.info(f"Стартовый URL: {page.url}")
            logger.info("Пожалуйста, войдите в аккаунт. Таймаут 3 минуты.")

            auth_success = False
            start_time = time.time()
            last_log_time = start_time
            last_text_check = start_time
            last_url = page.url
            auth_url = None  # запомним URL, на котором подтвердилась авторизация

            while time.time() - start_time < 180:
                current_url = page.url

                if time.time() - last_log_time > 15:
                    try:
                        logger.info(f"Текущий URL: {current_url}")
                        logger.info(f"Заголовок: {page.title()}")
                        logger.info(f"Окно закрыто: {page.is_closed()}")
                    except Exception:
                        pass
                    last_log_time = time.time()

                if current_url != last_url:
                    logger.info(f"URL изменился: {current_url}")
                    last_url = current_url

                if is_logged_in_url(current_url):
                    logger.info(f"Обнаружен переход в личный кабинет: {current_url}")
                    auth_success = True
                    auth_url = current_url   # запоминаем URL для валидации
                    break

                if "login" not in current_url:
                    try:
                        for selector in [
                            'a[data-qa="mainmenu_resumes"]',
                            'a[data-qa="applicant-resumes"]',
                        ]:
                            if page.locator(selector).first.is_visible(timeout=500):
                                logger.info(f"Найден селектор: {selector}")
                                auth_success = True
                                auth_url = current_url
                                break
                    except Exception:
                        pass
                    if auth_success:
                        break

                if time.time() - last_text_check > 20:
                    try:
                        body = page.locator("body").inner_text(timeout=2000)
                        if "Мои резюме" in body or "Мои отклики" in body:
                            logger.info("Найдены ключевые фразы в тексте")
                            auth_success = True
                            auth_url = current_url
                            break
                    except Exception:
                        pass
                    last_text_check = time.time()

                page.wait_for_timeout(1000)

            if not auth_success:
                logger.warning("Не удалось подтвердить авторизацию. Собираю детальную диагностику...")
                try:
                    logger.info(f"Финальный URL: {page.url}")
                    logger.info(f"Заголовок: {page.title()}")
                    logger.info(f"Окно закрыто: {page.is_closed()}")
                    cookies = context.cookies()
                    logger.info(f"Куки: {[c['name'] for c in cookies]}")
                    page.screenshot(path="auth_failed.png", full_page=True)
                    logger.info("Сохранён скриншот: auth_failed.png")
                    body_text = page.locator("body").inner_text(timeout=3000)
                    logger.info(f"Текст страницы (первые 500 символов):\n{body_text[:500]}")
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    html_filename = f"auth_failed_{timestamp}.html"
                    html = page.content()
                    with open(html_filename, "w", encoding="utf-8") as f:
                        f.write(html)
                    logger.info(f"Сохранён HTML: {html_filename}")
                    cleanup_old_files("auth_failed_*.html", keep=5)
                    logger.info(f"Открыто страниц: {len(context.pages)}")
                    for i, p_page in enumerate(context.pages):
                        logger.info(f"  Страница {i+1}: {p_page.url}")
                except Exception as e:
                    logger.error(f"Ошибка при сборе диагностики: {e}")

            if auth_success and auth_url:
                logger.info("Авторизация подтверждена. Сохраняем сессию...")
                logger.info(f"Сессия сохраняется для URL: {auth_url}")

                try:
                    save_auth_state(context, AUTH_STATE_FILE)
                except Exception as e:
                    logger.error(f"Не удалось сохранить сессию: {e}")
                    return False

                # === ВАЛИДАЦИЯ СОХРАНЁННОЙ СЕССИИ (используем сохранённый URL) ===
                try:
                    auth_data = load_auth_state(AUTH_STATE_FILE)
                    validation_browser = p.chromium.launch(headless=True)
                    validation_context = validation_browser.new_context(storage_state=auth_data)
                    validation_page = validation_context.new_page()
                    # Открываем тот же URL, где была авторизация (с регионом)
                    validation_page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
                    validation_page.wait_for_timeout(2000)

                    if is_user_authorized(validation_page):
                        logger.info("✅ Валидация сессии пройдена: авторизация восстановлена.")
                        validation_context.close()
                        validation_browser.close()
                        return True
                    else:
                        logger.warning("❌ Валидация не пройдена: сессия не даёт авторизацию. Удаляю auth_state.json.")
                        try:
                            AUTH_STATE_FILE.unlink(missing_ok=True)
                        except Exception:
                            pass
                        validation_context.close()
                        validation_browser.close()
                        return False
                except Exception as e:
                    logger.error(f"Ошибка валидации сессии: {e}")
                    try:
                        AUTH_STATE_FILE.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return False

            else:
                logger.warning("Авторизация не распознана. Возможно, требуется дополнительное действие (капча, SMS) или вход в новом окне.")

        except Exception as e:
            logger.error(f"Ошибка при авторизации: {e}")
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass

        if attempt < max_attempts:
            logger.info("Повторная попытка через 10 секунд...")
            time.sleep(10)
        else:
            logger.error("Все попытки авторизации исчерпаны.")
    return False


# ------------------------------------------------------------
# Проверка авторизации для загруженной сессии
# ------------------------------------------------------------
def is_user_authorized(page) -> bool:
    try:
        url = page.url.lower()
        if is_logged_in_url(url):
            return True

        if "login" not in url:
            for selector in [
                'a[data-qa="mainmenu_resumes"]',
                'a[data-qa="applicant-resumes"]',
            ]:
                if page.locator(selector).first.is_visible(timeout=500):
                    return True

        body = page.locator("body").inner_text(timeout=3000)
        if "Мои резюме" in body or "Мои отклики" in body:
            return True

        return False
    except Exception:
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
    except Exception:
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

# ------------------------------------------------------------
# Проверка резюме
# ------------------------------------------------------------
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
            logger.warning("Сессия отсутствует. Требуется повторная авторизация.")
            if not perform_auth(p):
                return False, None

        browser_instance = p.chromium.launch(headless=True)
        context = None
        try:
            try:
                auth_data = load_auth_state(AUTH_STATE_FILE)
                context = browser_instance.new_context(storage_state=auth_data)
            except (PlaywrightError, Exception) as e:
                logger.warning(f"Повреждена сессия: {e}. Удаляю auth_state.json и запрашиваю новую.")
                try:
                    AUTH_STATE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                if not perform_auth(p):
                    return False, None
                auth_data = load_auth_state(AUTH_STATE_FILE)
                context = browser_instance.new_context(storage_state=auth_data)

            page = context.new_page()

            # Проверяем, что сессия действительна
            try:
                page.goto("https://hh.ru", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                if not is_user_authorized(page):
                    logger.warning("Сессия недействительна. Удаляю auth_state.json и запрашиваю новую.")
                    AUTH_STATE_FILE.unlink(missing_ok=True)
                    if not perform_auth(p):
                        return False, None
                    # Обновляем контекст после новой авторизации
                    auth_data = load_auth_state(AUTH_STATE_FILE)
                    context = browser_instance.new_context(storage_state=auth_data)
                    page = context.new_page()
            except Exception as e:
                logger.error(f"Ошибка при проверке сессии: {e}")
                return False, None

            for name, url in valid_resumes:
                if should_exit:
                    break
                success, wait_sec = get_resume_status(page, name, url)
                if success:
                    any_success = True
                elif wait_sec is not None:
                    wait_times.append(wait_sec)

        finally:
            # Гарантированное закрытие ресурсов
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser_instance:
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
# Обратный отсчёт (из версии 2.7.0)
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