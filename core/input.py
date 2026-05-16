from __future__ import annotations

import builtins
import threading
from dataclasses import dataclass, field
from typing import Protocol


class InputProvider(Protocol):
    def ask(self, prompt: str) -> str: ...


@dataclass(slots=True)
class ConsoleInputProvider:
    original_input: object = field(default=builtins.input, repr=False)

    def ask(self, prompt: str) -> str:
        return self.original_input(prompt)


class PatchedInput:
    _patch_lock = threading.RLock()

    def __init__(self, provider: InputProvider) -> None:
        self.provider = provider
        self.original_input = builtins.input

    def __enter__(self) -> "PatchedInput":
        self._patch_lock.acquire()
        builtins.input = self.provider.ask
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        builtins.input = self.original_input
        self._patch_lock.release()
