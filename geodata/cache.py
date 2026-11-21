"""Кэш клеток 0.25 градуса на диске (JSON): свой файл, совместимые файлы прежних версий, устаревший -- как запасной.

Часть пакета geodata (бывший obstacles.py); старое имя `import obstacles` продолжает работать."""
from __future__ import annotations
import json
import os
import time
from typing import (List, Optional)
from .common import (CACHE_TTL_DAYS, CACHE_VERSION)



class ObstacleCache:
    """Дисковый кэш клеток (JSON, запись атомарная): элементы вида OSM, полученные из Overture."""

    def __init__(self, directory: str, ttl_days: float = CACHE_TTL_DAYS, prefix: str = "obstacles", version: int = CACHE_VERSION,
                 compat=()):
        self.dir, self.ttl_days, self.prefix, self.version = directory, ttl_days, prefix, version
        self.compat = tuple(compat)              # [(префикс, версия)] -- файлы-надмножества, годные вместо своего

    def _path(self, i: int, j: int) -> str:
        return os.path.join(self.dir, f"{self.prefix}_v{self.version}_{i}_{j}.json")

    def load(self, i: int, j: int, allow_stale: bool = False) -> Optional[List[dict]]:
        for prefix, version in ((self.prefix, self.version),) + self.compat:   # свой файл, потом совместимые надмножества
            try:
                with open(os.path.join(self.dir, f"{prefix}_v{version}_{i}_{j}.json"), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("v") != version or not isinstance(data.get("elements"), list):
                    continue
                age_days = (time.time() - float(data.get("fetched", 0.0))) / 86400.0
                if age_days > self.ttl_days and not allow_stale:
                    continue
                return data["elements"]
            except (OSError, ValueError, TypeError):
                continue
        return None

    def load_older(self, i: int, j: int) -> Optional[List[dict]]:
        """Кэш ПРЕЖНЕЙ версии (старше любого срока): когда свежую ячейку скачать не удалось, лучше ЛЭП и препятствия без
        новых видов (например, без железных дорог), чем совсем ничего."""
        for v in range(self.version - 1, 0, -1):
            try:
                with open(os.path.join(self.dir, f"{self.prefix}_v{v}_{i}_{j}.json"), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data.get("elements"), list):
                    return data["elements"]
            except (OSError, ValueError, TypeError):
                continue
        return None

    def save(self, i: int, j: int, elements: List[dict]) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self._path(i, j) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": self.version, "fetched": time.time(), "cell": [i, j], "elements": elements}, f)
            os.replace(tmp, self._path(i, j))
            return True
        except OSError:
            return False
