import asyncio, time,json
from playwright._impl._errors import TargetClosedError
from adspower.async_api.playwright import Profile
from adspower.async_api.http_client import HTTPClient
from httpx import ConnectError, ReadTimeout
from playwright.async_api import Page, BrowserContext
from parser_logic.utils.logger import log_info, log_error, log_exception, log_warning

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from . import BrowserManager

from typing import Awaitable, Callable, TypeVar
from .mixin import ManagerDataMixin

T = TypeVar("T")

TINY = ["--window-size=1920,4120", "--window-position=0,0", "--force-device-scale-factor=0.3"]
NORMAL = ["--window-size=1920,1030", "--window-position=0,0"]

params = [
    "--enable-precise-memory-info",
    "--js-flags=--expose-gc",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-dev-shm-usage",
]

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


class ManagerBase(ManagerDataMixin):
    """
    Менеджер жизненного цикла браузерного профиля AdsPower.

    Отвечает за:
        • запуск браузерного профиля
        • корректное закрытие профиля
    """
    def __init__(self, manager):
        super().__init__(manager)
        self._profile: Profile | None = None


    async def open_profile(
        self,
        headless: bool = False,
        clear_cache: bool = True,
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
        log_info("Инициализация AdsPower Playwright...", self.profile_name)

        window_args = TINY if self._manager.mode == "tiny" else NORMAL
        launch_args = json.dumps(window_args + params)

        HTTPClient.set_port(self._manager.config["local_port"])
        HTTPClient.set_timeout(30.0)

        async with API_MANAGER:
            profiles = await Profile.query(id_=self._manager._user_id)

        if not profiles:
            raise ValueError(f"Профиль не найден: user_id={self._manager._user_id}")

        self._profile = profiles[0]

        async def launch_browser():
            if not self._profile:
                raise RuntimeError('Переменная профиля пуста или потеряна')
            async with API_MANAGER:
                browser = await self._profile.get_browser(
                    ip_tab=False,
                    new_first_tab=False,
                    launch_args=launch_args,  # type: ignore
                    headless=headless,
                    clear_cache_after_closing=clear_cache,
                )
            if not browser:
                raise RuntimeError("Браузер не инициализирован")
            return browser

        self._manager.context = await self._retry(
            launch_browser,
            "Запуск браузера",
            max_retries,
            retry_delay,
        )

        log_info("Браузер открыт успешно", self.profile_name)
        
    async def close_browser(self):
        """Корректное закрытие браузерного профиля AdsPower."""
        log_info("Закрытие браузера", self.profile_name)
        HTTPClient.set_port(self._manager.config['local_port'])
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
            self._manager.context = None
            self._profile = None
        
    # пока в базе, если будет расширяться функционал вынесется в отдельный файл инструментов
    async def get_main_page(self, start: str) -> Page:
        """
        Получение главной страницы браузера с переходом на стартовый URL.

        Parameters
        ----------
        start : str
            URL для перехода после открытия страницы.
        """
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
            log_error(f"Ошибка в get_main_page: {str(e)}", self.profile_name)
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
                log_info(f"Попытка {attempt}/{max_retries}: {action_name}", self.profile_name)
                return await coro_func()

            except (ConnectError, ReadTimeout):
                log_exception(f"{action_name} — ошибка соединения", self.profile_name)
                
            except TargetClosedError:
                log_info(f"{action_name} — уже закрыто (OK)", self.profile_name)
                return None

            except Exception as e:
                err_str = str(e).lower()
                if (
                    "user_id is not open" in err_str
                    or "profile is not running" in err_str
                    or "has been closed" in err_str
                ):
                    log_info(f"{action_name} — уже закрыто (OK)", self.profile_name)
                    break
                log_exception(f"{action_name} — ошибка в работе", self.profile_name)

            if attempt < max_retries:
                log_warning(
                    f"{action_name} — повтор через {retry_delay} сек",
                    self.profile_name
                )
                await asyncio.sleep(retry_delay)
            else:
                raise RuntimeError(f"{action_name} не удалось после {max_retries} попыток")
