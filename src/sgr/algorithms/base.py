from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from sgr.models import Signal


class Strategy(ABC):
    name: str

    @abstractmethod
    def generate(self, *args, **kwargs) -> Iterable[Signal]:
        raise NotImplementedError
