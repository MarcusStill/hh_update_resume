import atexit
import base64
import configparser
import glob
import hashlib
import json
import logging
import os
import re
import signal
import smtplib
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ------------------------------------------------------------
# Версия приложения
# ------------------------------------------------------------
APP_VERSION = "2.8.18"

# ------------------------------------------------------------
# Константы для email
# ------------------------------------------------------------
EMAIL_NOTIFY_INTERVAL = 1800  # минимум 30 минут между уведомлениями
_last_email_sent_time = 0
_email_config = None
_notification_sent_this_run = False

# ------------------------------------------------------------
# Константы
# ------------------------------------------------------------
MIN_AUTH_STATE_SIZE = 500  # минимальный размер auth_state.json (байт)
AUTH_SELECTORS = [
    'a[data-qa="mainmenu_resumes"]',
    'a[data-qa="applicant-resumes"]',
]
TEXT_LOCATORS = [
    "text=Мои резюме",
    "text=Мои отклики",
]
VISIBLE_TIMEOUT = 500  # таймаут для is_visible (мс)
AUTH_CHECK_INTERVAL = 20  # интервал проверки текста (секунд)
_warning_logged = False
_last_warning_time = 0  # время последнего предупреждения (для ограничения частоты)
AUTH_TEXT_SNIPPETS = [
    "Резюме и профиль",
    "Отклики",
    "Ваша активность"
]
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
# Вспомогательные функции
# ------------------------------------------------------------
def cleanup_old_files(pattern="auth_failed_*.html", keep=5):
    """Оставляет только последние N файлов, остальные удаляет."""
    # Используем BASE_DIR для поиска
    search_pattern = str(BASE_DIR / pattern)
    files = sorted(glob.glob(search_pattern), key=os.path.getmtime, reverse=True)
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
            or "role=applicant" in url
        )
    )

# ------------------------------------------------------------
# Функции для работы с email
# ------------------------------------------------------------
def load_email_config():
    """Загружает настройки email из config.ini."""
    logger.info(f"Запуск функции load_email_config")
    config = load_config()
    if not config.has_section("EMAIL"):
        return None
    try:
        cfg = {
            "smtp_server": config.get("EMAIL", "smtp_server"),
            "smtp_port": config.getint("EMAIL", "smtp_port"),
            "smtp_username": config.get("EMAIL", "smtp_username"),
            "smtp_password": config.get("EMAIL", "smtp_password"),
            "to_email": config.get("EMAIL", "to_email"),
            "subject": config.get("EMAIL", "subject", fallback="HH Resume Updater: требуется внимание!"),
        }
        # Проверяем наличие всех обязательных полей
        if not all(cfg.values()):
            logger.warning("Не все параметры email заполнены в config.ini")
            return None
        return cfg
    except Exception as e:
        logger.warning(f"Ошибка чтения настроек email: {e}")
        return None

def read_last_log_lines(n=20):
    """Читает последние n строк из log.txt."""
    logger.info(f"Запуск функции read_last_log_lines")
    try:
        if not LOG_FILE.exists():
            return "Лог-файл не найден."
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return ''.join(lines[-n:])
    except Exception as e:
        return f"Ошибка чтения лога: {e}"


def send_email_notification(subject, body):
    """
    Отправляет email-уведомление, если настроено, не чаще раза в EMAIL_NOTIFY_INTERVAL
    и не более одного раза за запуск программы.
    """
    logger.info("Запуск функции send_email_notification")
    global _last_email_sent_time, _email_config, _notification_sent_this_run

    # Если уведомление уже отправлено в этом запуске — пропускаем
    if _notification_sent_this_run:
        logger.info("Уведомление уже отправлено в текущей сессии, пропускаем.")
        return

    # Загружаем конфигурацию, если ещё не загружена
    if _email_config is None:
        _email_config = load_email_config()

    # Проверка наличия конфигурации
    if _email_config is None:
        logger.warning("Email уведомления не настроены (отсутствует секция EMAIL в config.ini). Пропускаем.")
        return

    # Проверка интервала отправки
    current_time = time.time()
    if current_time - _last_email_sent_time < EMAIL_NOTIFY_INTERVAL:
        remaining = int(EMAIL_NOTIFY_INTERVAL - (current_time - _last_email_sent_time))
        logger.info(f"Пропускаем отправку email (прошло менее {remaining} сек с момента последнего уведомления).")
        return

    try:
        logger.info(
            f"Попытка отправки email на {_email_config['to_email']} через {_email_config['smtp_server']}:{_email_config['smtp_port']}")

        msg = MIMEMultipart()
        msg['From'] = _email_config["smtp_username"]
        msg['To'] = _email_config["to_email"]
        msg['Subject'] = subject

        full_body = f"{body}\n\nВремя: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nPID: {os.getpid()}\n\n--- Последние строки лога ---\n"
        full_body += read_last_log_lines(20)
        msg.attach(MIMEText(full_body, 'plain', 'utf-8'))

        logger.info("Подключаюсь к SMTP-серверу...")
        server = smtplib.SMTP(_email_config["smtp_server"], _email_config["smtp_port"])
        server.set_debuglevel(1)
        server.starttls()
        logger.info("Вход в SMTP-аккаунт...")
        server.login(_email_config["smtp_username"], _email_config["smtp_password"])
        logger.info("Отправка письма...")
        server.send_message(msg)
        server.quit()

        _last_email_sent_time = current_time
        _notification_sent_this_run = True
        logger.info("✅ Email уведомление успешно отправлено.")

    except Exception as e:
        logger.error(f"❌ Ошибка отправки email: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())


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

def save_auth_state(context, path: Path) -> bool:
    """Сохраняет сессию с обфускацией и проверяет размер файла (диагностика)."""
    try:
        auth_data = context.storage_state()
        obfuscated = obfuscate_data(auth_data)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(obfuscated)

        size = os.path.getsize(path)
        logger.info(f"Сессия сохранена в {path} (размер: {size} байт)")

        # Только предупреждение, не ошибка — размер может меняться в зависимости от версии Playwright
        if size < MIN_AUTH_STATE_SIZE:
            logger.warning(
                f"Размер auth_state.json ({size} байт) меньше ожидаемого ({MIN_AUTH_STATE_SIZE}). "
                "Это может быть нормально, но проверьте логи при проблемах с авторизацией."
            )

        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения сессии: {e}")
        return False

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
    chromium_dirs = sorted(browsers_dir.glob("chromium-*"))
    if chromium_dirs:
        chromium_dir = chromium_dirs[-1]
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

atexit.register(remove_pid_file)

browser_instance = None
should_exit = False

def signal_handler(sig, frame):
    global should_exit
    logger.info("Получен сигнал завершения. Останавливаемся...")
    should_exit = True

# ------------------------------------------------------------
# Проверка авторизации (оптимизирована)
# ------------------------------------------------------------
_warning_logged = False  # флаг для предотвращения спама предупреждений

def is_user_authorized(page, check_text=False) -> bool:
    global _last_warning_time
    try:
        url = page.url.lower()

        # 1. Проверка видимых data-qa селекторов (основной признак)
        if "login" not in url:
            for selector in AUTH_SELECTORS:
                if page.locator(selector).first.is_visible(timeout=VISIBLE_TIMEOUT):
                    return True

        # 2. Если URL похож на кабинет, проверяем наличие характерных текстов
        if is_logged_in_url(url):
            for text in AUTH_TEXT_SNIPPETS:
                if page.locator(f"text={text}").first.is_visible(timeout=VISIBLE_TIMEOUT):
                    return True

        # 3. Если явно запрошена проверка текста (для валидации сессии)
        if check_text:
            for text in TEXT_LOCATORS:
                if page.locator(text).first.is_visible(timeout=VISIBLE_TIMEOUT):
                    return True

        return False
    except Exception as e:
        current_time = time.time()
        if current_time - _last_warning_time > 300:
            logger.warning(f"Неожиданная ошибка в is_user_authorized: {e}")
            _last_warning_time = current_time
        return False

# ------------------------------------------------------------
# Авторизация (без валидации через второй браузер)
# ------------------------------------------------------------
def perform_auth(p, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Попытка авторизации #{attempt} из {max_attempts}")
        browser = None
        context = None
        try:
            browser = p.chromium.launch(headless=False)
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

            while time.time() - start_time < 180:
                # Проверка закрытия окна
                try:
                    if page.is_closed():
                        logger.warning("Страница была закрыта пользователем.")
                        break
                except Exception:
                    pass

                current_url = page.url

                # Логирование каждые 15 секунд
                if time.time() - last_log_time > 15:
                    try:
                        logger.info(f"Текущий URL: {current_url}")
                        title = page.title()
                        logger.info(f"Заголовок: {title}")
                    except Exception:
                        pass
                    last_log_time = time.time()

                if current_url != last_url:
                    logger.info(f"URL изменился: {current_url}")
                    last_url = current_url

                # Проверка авторизации (без текста каждую секунду)
                if is_user_authorized(page, check_text=False):
                    logger.info(f"Обнаружена авторизация по URL/селекторам: {current_url}")
                    auth_success = True
                    break

                # Проверка текста – не чаще раза в AUTH_CHECK_INTERVAL секунд
                if time.time() - last_text_check > AUTH_CHECK_INTERVAL:
                    if is_user_authorized(page, check_text=True):
                        logger.info(f"Обнаружена авторизация по тексту: {current_url}")
                        auth_success = True
                        break
                    last_text_check = time.time()

                page.wait_for_timeout(1000)

            # Диагностика при неудаче (без изменений)
            if not auth_success:
                logger.warning("Не удалось подтвердить авторизацию. Собираю детальную диагностику...")
                try:
                    logger.info(f"Финальный URL: {page.url}")
                    try:
                        title = page.title()
                        logger.info(f"Заголовок: {title}")
                    except Exception:
                        pass
                    try:
                        logger.info(f"Окно закрыто: {page.is_closed()}")
                    except Exception:
                        pass
                    cookies = context.cookies()
                    logger.info(f"Куки ({len(cookies)}): {[c['name'] for c in cookies[:10]]}" + ("..." if len(cookies) > 10 else ""))
                    page.screenshot(path="auth_failed.png", full_page=True)
                    logger.info("Сохранён скриншот: auth_failed.png")
                    body_text = page.locator("body").inner_text(timeout=3000)
                    logger.info(f"Текст страницы (первые 500 символов):\n{body_text[:500]}")
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    html_filename = f"auth_failed_{timestamp}.html"
                    html_saved = False
                    try:
                        html = page.content()
                        with open(html_filename, "w", encoding="utf-8") as f:
                            f.write(html)
                        logger.info(f"Сохранён HTML: {html_filename}")
                        html_saved = True
                    except Exception as e:
                        logger.warning(f"Не удалось сохранить HTML: {e}")
                    if html_saved:
                        cleanup_old_files("auth_failed_*.html", keep=5)
                except Exception as e:
                    logger.error(f"Ошибка при сборе диагностики: {e}")

            if auth_success:
                logger.info("Авторизация подтверждена. Сохраняем сессию...")
                logger.info(f"Сессия сохраняется для URL: {page.url}")

                if save_auth_state(context, AUTH_STATE_FILE):
                    logger.info("✅ Сессия успешно сохранена и проверена.")
                    return True
                else:
                    logger.error("❌ Не удалось сохранить сессию (проверка размера не пройдена).")
                    return False
            else:
                logger.warning("Авторизация не распознана. Возможно, требуется дополнительное действие.")

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
            send_email_notification(
                "HH Resume Updater: ошибка авторизации",
                "Не удалось авторизоваться после всех попыток. Требуется ручное вмешательство."
            )
    return False

# ------------------------------------------------------------
# Основная логика поднятия резюме (без изменений)
# ------------------------------------------------------------
def get_resume_status(page, resume_name, url):
    logger.info(f"Проверка резюме '{resume_name}'...")

    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
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

    if any(marker in page.url.lower() for marker in ("/login", "/auth", "account/login", "oauth/authorize")):
        logger.warning("Сессия устарела. Удаляю auth_state.json.")
        AUTH_STATE_FILE.unlink(missing_ok=True)
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
            try:
                lift_button.click(timeout=10000)
                page.wait_for_timeout(3000)
                logger.info(f"[УСПЕХ] Команда поднятия отправлена для '{resume_name}'.")
                return True, None
            except Exception as e:
                logger.error(f"Ошибка при клике: {e}")
                return False, None
        else:
            logger.info(f"Кнопка для '{resume_name}' неактивна или невидима. Ищем время...")
    else:
        logger.info(f"Кнопка не найдена для '{resume_name}'. Ищем информацию о времени...")

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
                AUTH_STATE_FILE.unlink(missing_ok=True)
                if not perform_auth(p):
                    return False, None
                auth_data = load_auth_state(AUTH_STATE_FILE)
                context = browser_instance.new_context(storage_state=auth_data)

            page = context.new_page()

            # Проверка сессии – открываем страницу с параметром role=applicant
            try:
                page.goto("https://hh.ru/?role=applicant", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)  # даём время на отрисовку
                if not is_user_authorized(page, check_text=True):
                    logger.warning("Сессия недействительна. Удаляю auth_state.json и запрашиваю новую.")
                    AUTH_STATE_FILE.unlink(missing_ok=True)
                    if not perform_auth(p):
                        return False, None
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
# Обратный отсчёт
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
            logger.error(f"Необработанное исключение: {e}", exc_info=True)
            send_email_notification(
                "HH Resume Updater: критическая ошибка",
                f"Необработанное исключение: {e}\nПодробности в логе."
            )
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