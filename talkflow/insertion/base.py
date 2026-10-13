from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class InsertionOutcome:
    success: bool
    method: str  # "ui_automation" | "clipboard"
    error: str | None = None


class InsertionAdapter(ABC):
    @abstractmethod
    def insert(self, text: str) -> InsertionOutcome:
        """Insert text into whatever control currently has focus."""
