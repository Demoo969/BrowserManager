from playwright.async_api import Page, BrowserContext
from parser_logic.utils.logger import log_info, log_error, log_exception, log_warning
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from . import BrowserManager


class ManagerDataMixin:
    def __init__(self, manager: 'BrowserManager'):
        self._manager = manager

    @property
    def profile_name(self) -> str | None:
        return self._manager._profile_name

    @property
    def user_id(self) -> str:
        return self._manager._user_id

    @property
    def browser_context(self) -> BrowserContext | None:
        return self._manager.context