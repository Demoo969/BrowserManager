import asyncio
import psutil
import time
from adspower.async_api.playwright import Profile
from adspower.async_api.http_client import HTTPClient
from httpx import ConnectError, ReadTimeout
from playwright.async_api import Page, BrowserContext
from utils.logger import log_info, log_error, log_exception, log_warning

from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class AdsPowerAPILimiter:
    """
    Асинхронный rate-limiter для API AdsPower.

    AdsPower API имеет ограничение: примерно 1 запрос каждые ~2 секунды.
    При параллельных вызовах (например запуск нескольких профилей одновременно)
    API может возвращать ошибки соединения, таймауты или некорректные ответы.

    Этот примитив реализует:
        • глобальную синхронизацию через asyncio.Lock
        • выдерживание минимального интервала между API вызовами

    Использование:

        async with API_MANAGER:
            await Profile.query(...)

    Это гарантирует, что между вызовами API будет выдержан интервал delay.
    """
    def __init__(self, delay: float = 2.0):
        self.delay = delay
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def __aenter__(self):
        async with self._lock:
            now = time.monotonic()
            wait = self.delay - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


API_MANAGER = AdsPowerAPILimiter(2.2)


class BrowserManager:
    """
    Менеджер жизненного цикла браузерного профиля AdsPower.

    Отвечает за:
        • запуск браузерного профиля
        • корректное закрытие профиля
        • получение renderer процессов
        • мониторинг потребления памяти Chrome
    """
    def __init__(self, config: dict, user_id: str, _profile_name: str | None = None):
        self.config: dict = config
        self.user_id: str = user_id
        self._profile_name = _profile_name
        self.browser_context: BrowserContext | None = None
        self._profile: Profile | None = None
        self.renderer_PIDs: list[int] = []

    async def __aenter__(self):
        await self.open_profile()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_browser()


    async def open_profile(
        self,
        headless: bool = False,
        clear_cache: bool = False,
        max_retries: int = 3,
        retry_delay: int = 5,
    ) -> None:
        """
        Запуск браузерного профиля AdsPower.

        Parameters
        ----------
        headless : bool
            Запуск браузера в headless режиме.
        clear_cache : bool
            Очистить кэш профиля после закрытия.
        max_retries : int
            Максимальное количество попыток запуска.
        retry_delay : int
            Задержка между попытками запуска (секунды).
        """
        log_info("Инициализация AdsPower Playwright...", self._profile_name)

        HTTPClient.set_port(self.config["local_port"])
        HTTPClient.set_timeout(30.0)

        async with API_MANAGER:
            profiles = await Profile.query(id_=self.user_id)

        if not profiles:
            raise ValueError(f"Профиль не найден: user_id={self.user_id}")

        self._profile = profiles[0]

        async def launch_browser():
            if not self._profile:
                raise RuntimeError('Переменная профиля пуста или потеряна')
            async with API_MANAGER:
                browser = await self._profile.get_browser(
                    ip_tab=False,
                    new_first_tab=False,
                    launch_args='["--start-maximized","--enable-precise-memory-info", "--js-flags= --expose-gc", "--disable-background-timer-throttling","--disable-renderer-backgrounding","--disable-dev-shm-usage"]',  # type: ignore
                    headless=headless,
                    clear_cache_after_closing=clear_cache,
                )
            if not browser:
                raise RuntimeError("Браузер не инициализирован")
            return browser

        self.browser_context = await self._retry(
            launch_browser,
            "Запуск браузера",
            max_retries,
            retry_delay,
        )

        log_info("Браузер открыт успешно", self._profile_name)

    async def get_main_page(self, start: str|None = None) -> Page:
        """
        Получение главной страницы браузера с переходом на стартовый URL.

        Parameters
        ----------
        start : str
            URL для перехода после открытия страницы.
        """
        if not start: start='https://x.com/home'
        if not self.browser_context:
            raise RuntimeError("Браузер не инициализирован")
        try:
            # Закрываем лишние вкладки, оставляем одну
            pages = self.browser_context.pages
            if len(pages) > 1:
                for page in pages[1:]:
                    await page.close()
            page = pages[0] if pages else await self.browser_context.new_page()
            await page.goto(start, timeout=30000)
            return page
        except Exception as e:
            log_error(f"Ошибка в get_main_page: {str(e)}", self._profile_name)
            raise

    async def close_browser(self):
        """Корректное закрытие браузерного профиля AdsPower."""
        log_info("Закрытие браузера", self._profile_name)
        HTTPClient.set_port(self.config['local_port'])
        HTTPClient.set_timeout(30.0)

        if not self.browser_context:
            return

        async def quit_profile():
            if not self._profile:
                raise RuntimeError("Переменная профиля пуста или потеряна")
            async with API_MANAGER:
                await self._profile.quit()

        try:
            await self._retry(quit_profile, "Закрытие профиля", 3, 2)
        finally:
            self.browser_context = None
            self._profile = None

    async def get_renderer_pids(self):
        """
        Получение PID процессов renderer браузера Chrome через CDP.
        Эти PID используются для мониторинга потребления памяти.
        """
        if not self.browser_context:
            raise RuntimeError("Браузер не инициализирован")
        log_info(f"{self.user_id} -- получение PIDs", self._profile_name)
        browser_cdp = await self.browser_context.browser.new_browser_cdp_session()  # type: ignore
        info = await browser_cdp.send("SystemInfo.getProcessInfo")
        processes = info.get("processInfo", [])
        browser_pid = None
        for p in processes:
            if p.get("type") == "browser":
                browser_pid = p.get("id")
                break
        if not browser_pid:
            log_error(f"{self.user_id} -- Browser PID не найден", self._profile_name)
            return
        self.renderer_PIDs = [p["id"] for p in processes if p.get("type") == "renderer"]
        await browser_cdp.detach()

    async def check_memory_usage(self, max_proc: int = 2) -> float:
        """
        Проверка использования памяти renderer процессами Chrome.

        Parameters
        ----------
        max_proc : int
            Количество процессов для логирования.

        Returns
        -------
        float
            Использование RAM самым тяжёлым renderer процессом (MB).
        """
        proc_stats = []
        try:
            if not self.renderer_PIDs:
                log_warning(f"{self.user_id} PID'ы не получены", self._profile_name)
                await self.get_renderer_pids()
            for pid in self.renderer_PIDs:
                try:
                    proc = psutil.Process(pid)
                    mem = proc.memory_info().rss
                    cpu = proc.cpu_percent(interval=0.5)
                    proc_stats.append((pid, mem, cpu))
                except psutil.NoSuchProcess:
                    continue
            if not proc_stats:
                log_warning(f"{self.user_id} -- renderer процессы не найдены", self._profile_name)
                return 0
            proc_stats.sort(key=lambda x: x[1], reverse=True)
            for pid, mem, cpu in proc_stats[:max_proc]:
                log_info(f"{self.user_id} -- Renderer PID {pid} | RAM: {round(mem / 1024 / 1024, 2)} MB", self._profile_name)
            return round(proc_stats[0][1] / 1024 / 1024, 2)
        except Exception:
            log_exception("check_memory_usage")
            raise

    async def _retry(
        self,
        coro_func: Callable[[], Awaitable[T]],
        action_name: str,
        max_retries: int = 3,
        retry_delay: int = 5,
    ) -> T | None:
        for attempt in range(1, max_retries + 1):
            try:
                log_info(f"Попытка {attempt}/{max_retries}: {action_name}", self._profile_name)
                return await coro_func()

            except (ConnectError, ReadTimeout):
                log_exception(f"{action_name} — ошибка соединения", self._profile_name)

            except Exception as e:
                err_str = str(e)
                if "User_id is not open" in err_str or "profile is not running" in err_str:
                    log_info("Профиль уже был закрыт. Ошибок нет.")
                    break
                log_exception(f"{action_name} — ошибка в работе", self._profile_name)

            if attempt < max_retries:
                log_warning(
                    f"{action_name} — повтор через {retry_delay} сек",
                    self._profile_name
                )
                await asyncio.sleep(retry_delay)
            else:
                raise RuntimeError(f"{action_name} не удалось после {max_retries} попыток")
