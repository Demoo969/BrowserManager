import asyncio
from .base import ManagerBase
from .memory import MemoryManager
from playwright.async_api import Page, BrowserContext
from parser_logic.utils.logger import log_error,log_warning,log_exception,log_info

class BrowserManager:
    def __init__(self, config: dict, 
        user_id: str, 
        profile_name: str|None = None, 
        memory_limit: float | None = None,  
        memory_check_interval: int = 120
    ):
        self._browser_context : BrowserContext | None = None
        self.config: dict = config # для типизации есть идея но пока без реализации возможно конфиг отсюда уберется в будущем чтобы стать чище
        self._user_id: str = user_id
        self._profile_name = profile_name
        # проверка памяти и очистка по надобнасти
        self.memory_limit = memory_limit
        self.memory_check_interval = memory_check_interval
        self._memory_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.main: ManagerBase = ManagerBase(self)
        self.memory: MemoryManager = MemoryManager(self)
        
        
    @property
    def context(self) -> BrowserContext:
        if not self._browser_context:
            raise RuntimeError("Браузер не инициализирован")
        return self._browser_context
    
    @context.setter
    def context(self, context: BrowserContext | None):
        self._browser_context = context
        self.memory.on_context_updated()
        
    async def __aenter__(self):
        await self.main.open_profile()
        if self.memory_limit:
            await self.start_mem_watcher()

        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc:
            log_error(f"Ошибка внутри контекста: {exc}", self._profile_name)
        if self._memory_task:
            try:
                await self.stop_memory_watcher()
            except asyncio.CancelledError: pass

        await self.main.close_browser()
    
    async def start_mem_watcher(self):
        if self._memory_task and not self._memory_task.done():
            await self.stop_memory_watcher() 
        self._stop_event.clear()
        self._memory_task = asyncio.create_task(self._memory_watcher())
        
    async def stop_memory_watcher(self):
        if not self._memory_task: return
        
        self._stop_event.set()
        self._memory_task.cancel()

        try: await self._memory_task
        except asyncio.CancelledError: pass

        self._memory_task = None
    
    
    async def _memory_watcher(self):
        watcher_id = id(asyncio.current_task())
        while not self._stop_event.is_set():
            try:
                if not self.memory.renderer_PIDs:
                    await asyncio.wait_for(
                        self.memory.get_renderer_pids(),
                        timeout=20
                    )

                usage = await asyncio.wait_for(
                    self.memory.check_memory_usage(),
                    timeout=20
                )
                log_warning(
                        f"Проверка лимита памяти: {usage} MB лимит{self.memory_limit} MB",
                        self._profile_name
                    )
                if self.memory_limit and int(usage) > self.memory_limit:
                    log_warning(
                        f"Превышен лимит памяти: {usage} MB > {self.memory_limit} MB",
                        self._profile_name
                    )

                    page = None
                    if self._browser_context and self._browser_context.pages:
                        page = self._browser_context.pages[0]

                    if page:
                        await asyncio.wait_for(
                            self.memory.clear_browser_data(page, soft=True),
                            timeout=20
                        )
                        await page.reload(wait_until='load')

            except asyncio.TimeoutError:
                log_warning("memory_watcher timeout", self._profile_name)
            except asyncio.CancelledError:
                log_info(f"Memory watcher cancelled: {watcher_id}", self._profile_name)
                raise
            except Exception:
                log_exception("memory_watcher", self._profile_name)

            await asyncio.sleep(self.memory_check_interval)