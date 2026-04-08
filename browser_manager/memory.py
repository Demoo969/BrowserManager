import asyncio, psutil
from adspower.async_api.playwright import Profile
from playwright.async_api import Page, BrowserContext
from parser_logic.utils.logger import log_info, log_error, log_exception, log_warning
from .mixin import ManagerDataMixin


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from . import BrowserManager

class MemoryManager(ManagerDataMixin):
    """
    Менеджер памяти браузерного профиля AdsPower.

    Отвечает за:
        • получение renderer процессов
        • мониторинг потребления памяти Chrome
    """
    def __init__(self, manager):
        super().__init__(manager)
        self._renderer_PIDs: list[int] = []
        self.allowed_browsers = ["chrome", "chromium", "sunbrowser", "edge", "opera"]

    @property
    def renderer_PIDs(self) ->list[int]:
        return self._renderer_PIDs
    
    def on_context_updated(self):
        self._renderer_PIDs.clear()
    
    async def get_renderer_pids(self)->None:
        """
        Получение PID процессов renderer браузера Chrome через CDP.
        Эти PID используются для мониторинга потребления памяти.
        """
        log_info(f"{self.user_id} -- получение PIDs", self.profile_name)
        browser_cdp = await self.browser_context.browser.new_browser_cdp_session()  # type: ignore
        try:
            info = await browser_cdp.send("SystemInfo.getProcessInfo")
            processes = info.get("processInfo", [])
            browser_pid = None
            for p in processes:
                if p.get("type") == "browser":
                    browser_pid = p.get("id")
                    break
            if not browser_pid:
                log_error(f"{self.user_id} -- Browser PID не найден", self.profile_name)
                return
            self._renderer_PIDs = [p["id"] for p in processes if p.get("type") == "renderer"]
        finally:
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
            if not self._renderer_PIDs:
                log_warning(f"{self.user_id} PID'ы не получены", self.profile_name)
                await self.get_renderer_pids()
            for pid in self._renderer_PIDs:
                try:
                    proc = psutil.Process(pid)
                    proc_name = proc.name().lower().replace(".exe", "")
                    if proc_name not in self.allowed_browsers:
                        log_warning(f"{self.user_id} PID {pid} не относится к Браузеру, пропускаем", self.profile_name)
                        continue
                    mem = proc.memory_info().rss
                    proc_stats.append((pid, mem))
                except psutil.NoSuchProcess:
                    continue
            if not proc_stats:
                log_warning(f"{self.user_id} -- renderer процессы не найдены", self.profile_name)
                return 0
            proc_stats.sort(key=lambda x: x[1], reverse=True)
            for pid, mem in proc_stats[:max_proc]:
                log_info(f"{self.user_id} -- Renderer PID {pid} | RAM: {round(mem / 1024 / 1024, 2)} MB", self.profile_name)
            return round(proc_stats[0][1] / 1024 / 1024, 2)
        except Exception:
            log_exception("check_memory_usage")
            raise
    
    async def clear_browser_data(self,page:Page, soft:bool=True):
        """Очистка кук и кеша браузера через CDP."""
        try:
            if not page or page.is_closed():
                log_warning("Страница закрыта, очистка данных невозможна", self.profile_name)
                return False

            log_info("Начинаю очистку кук и кеша браузера", self.profile_name)

            # Получаем CDP сессию
            context = page.context
            client = await context.new_cdp_session(page)
            if not soft:
                # Очищаем все куки
                try:
                    await client.send("Network.clearBrowserCookies")
                    log_info("Все куки очищены", self.profile_name)
                except Exception as e:
                    log_error(f"Ошибка при очистке кук: {e}", self.profile_name)
                try:
                    await client.send("Storage.clearDataForOrigin", {
                        "origin": "*",
                        "storageTypes": "all"
                    })
                except Exception as e:
                    log_error(f"Ошибка при очистке кеша: {e}", self.profile_name)
                    pass
            # Очищаем кеш браузера
            try:
                await client.send("Network.clearBrowserCache")
                log_info("Кеш браузера очищен", self.profile_name)
            except Exception as e:
                log_error(f"Ошибка при очистке кеша: {e}", self.profile_name)
            
            await client.detach()
            # Очищаем localStorage и sessionStorage
            try:
                await page.evaluate("""
                    async () => {
                        // sessionStorage можно всегда чистить
                        sessionStorage.clear();

                        // localStorage — только явно мусор
                        const whitelist = ['auth', 'token', 'session', 'user'];

                        Object.keys(localStorage).forEach(key => {
                            const lower = key.toLowerCase();

                            const keep = whitelist.some(w => lower.includes(w));
                            if (!keep) {
                                localStorage.removeItem(key);
                            }
                        });

                        // ❗ IndexedDB НЕ трогаем (там часто авторизация)

                        // чистим только runtime cache (без полного удаления)
                        if (window.caches) {
                            const keys = await caches.keys();
                            for (const key of keys) {
                                if (!key.includes('auth') && !key.includes('user')) {
                                    await caches.delete(key);
                                }
                            }
                        }
                    }
                """)
                log_info("Storage частично очищен", self.profile_name)
            except Exception as e:
                log_warning(f"Ошибка storage: {e}", self.profile_name)

            log_info("Очистка данных завершена успешно", self.profile_name)
            return True

        except Exception as e:
            log_error(f"Ошибка при очистке данных браузера: {e}", self.profile_name)
            return False
