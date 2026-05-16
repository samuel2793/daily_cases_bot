from __future__ import annotations

import builtins
import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PromptRequest:
    title: str
    message: str
    default: str = ""
    password: bool = False


class InteractionProvider(Protocol):
    def ask(self, request: PromptRequest) -> str: ...


class ConsoleInteractionProvider:
    def __init__(self) -> None:
        self._input = builtins.input

    def ask(self, request: PromptRequest) -> str:
        return self._input(request.message)


class InteractionManager:
    def __init__(self) -> None:
        self._provider: InteractionProvider = ConsoleInteractionProvider()
        self._lock = threading.RLock()

    def set_provider(self, provider: InteractionProvider) -> None:
        with self._lock:
            self._provider = provider

    def reset_provider(self) -> None:
        with self._lock:
            self._provider = ConsoleInteractionProvider()

    def ask(self, request: PromptRequest) -> str:
        with self._lock:
            provider = self._provider
        return provider.ask(request)


_manager = InteractionManager()


def set_interaction_provider(provider: InteractionProvider) -> None:
    _manager.set_provider(provider)


def reset_interaction_provider() -> None:
    _manager.reset_provider()


def ask_text(
    message: str,
    *,
    title: str = "Intervencion requerida",
    default: str = "",
    password: bool = False,
) -> str:
    return _manager.ask(
        PromptRequest(
            title=title,
            message=message,
            default=default,
            password=password,
        )
    )
