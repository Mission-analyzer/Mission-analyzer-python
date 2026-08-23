"""
online_tiles.py — тайлы карты напрямую из интернета, с выбором провайдера
(OpenStreetMap / Google) и диск-кэшем на будущее, разложенным по папкам
под каждый провайдер (чтобы не путать тайлы разных карт между собой и
докачивать из сети только то, чего ещё нет на диске).

Имеет тот же интерфейс get_tile(z, x, y), что и остальные источники
тайлов, поэтому map_view.py работает одинаково с любым провайдером.
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

# Честная оговорка: адрес Google ("mt0-3.google.com/vt/...") — неофициальный,
# но широко используемый в хобби-проектах способ получить тайлы. Это не
# Google Maps API, лицензионные условия Google формально не разрешают так
# тянуть тайлы напрямую. Годится для личного нечастого использования;
# для промышленных объёмов нужен официальный API с ключом.
#
# supports_hl -- чи розуміє провайдер параметр мови підписів (&hl=..).
# OSM Standard -- ні (стиль завжди малює локальною мовою, параметр
# просто ігнорується сервером) -- тому для OSM НЕ додаємо мовний
# суфікс до папки кешу (він там нічого б не розрізняв, лише плодив би
# порожню другу копію тих самих тайлів).
PROVIDERS = {
    "osm": {
        "name": "OpenStreetMap",
        "url_templates": [
            "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
        ],
        "supports_hl": False,
    },
    "google_roadmap": {
        "name": "Google Maps (схема)",
        "url_templates": [
            "https://mt0.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt2.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt3.google.com/vt/lyrs=m&x={x}&y={y}&z={z}&hl={hl}",
        ],
        "supports_hl": True,
    },
    "google_satellite": {
        "name": "Google Satellite",
        "url_templates": [
            "https://mt0.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt2.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt3.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl={hl}",
        ],
        "supports_hl": True,  # Satellite сам по собі без підписів, але параметр не заважає -- лишаємо однаково для всіх Google-шарів
    },
    "google_hybrid": {
        "name": "Google Гибрид (спутник+подписи)",
        "url_templates": [
            "https://mt0.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt2.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl={hl}",
            "https://mt3.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl={hl}",
        ],
        "supports_hl": True,
    },
}

DEFAULT_PROVIDER = "osm"
DEFAULT_LANGUAGE = "en"


class OnlineTileCache:
    """
    Качает тайлы из интернета от выбранного провайдера. Диск-кэш (якщо
    заданий) розкладається по підпапках вида <disk_cache_dir>/<provider>/
    (чи <provider>_<language>/, якщо провайдер розуміє параметр мови --
    див. нижче), щоб тайли різних карт не плутались між собою.

    language -- явний, керований параметр (не хеш усього URL!) -- зараз
    рівно ДВА можливих значення ("en"/"uk", ті самі, що й мова
    інтерфейсу програми), тому й підпапок кешу для Google-провайдерів
    рівно дві, а не довільна кількість. Раніше (перша спроба) кеш
    прив'язувався до хеша ВСЬОГО url_templates -- це занадто -- при
    будь-якій майбутній зміні URL (не лише мови) плодило б нову
    підпапку. Тепер розрізняється РІВНО той параметр, що реально може
    відрізнятись -- мова підписів, і рівно стільки варіантів, скільки
    їх є насправді.
    """

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        disk_cache_dir: str | None = None,
        language: str = DEFAULT_LANGUAGE,
        timeout: float = 6.0,
        polite_delay_s: float = 0.02,
        retries: int = 2,
    ):
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный провайдер карты: {provider}. Доступны: {list(PROVIDERS)}")
        self.provider = provider
        self.language = language
        self.url_templates = PROVIDERS[provider]["url_templates"]
        self._supports_hl = PROVIDERS[provider].get("supports_hl", False)
        self._subdir = f"{provider}_{language}" if self._supports_hl else provider

        self.disk_cache_dir = None
        if disk_cache_dir:
            self.disk_cache_dir = Path(disk_cache_dir) / self._subdir
            self.disk_cache_dir.mkdir(parents=True, exist_ok=True)

        self.timeout = timeout
        self.polite_delay_s = polite_delay_s
        self.retries = retries
        self._counter = 0
        self.network_errors = 0  # для диагностики: сколько запросов реально упало

    def _disk_path(self, z: int, x: int, y: int) -> Path | None:
        if self.disk_cache_dir is None:
            return None
        # без расширения намеренно: реальный формат (png/jpg) определяется по
        # содержимому при декодировании, а не по имени файла
        return self.disk_cache_dir / f"{z}_{x}_{y}.tile"

    def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        # Базовий набір тайлів (Україна + Європейська Росія, zoom=8)
        # НЕ окрема сутність у коді -- користувач сам, вручну, перед
        # збіркою .exe копіює готові файли просто в ЦЮ САМУ теку
        # (disk_cache_dir), замінюючи її поточний вміст (див.
        # download_base_maps.py + інструкцію зі збірки). Програма
        # ніяк не відрізняє "тайл прийшов з базового набору" від
        # "тайл довантажений користувачем" -- для неї це той самий
        # диск-кеш, просто вже частково заповнений із коробки.
        disk_path = self._disk_path(z, x, y)
        if disk_path is not None and disk_path.exists():
            try:
                return disk_path.read_bytes()
            except OSError:
                pass

        data = None
        for attempt in range(self.retries + 1):
            template = self.url_templates[self._counter % len(self.url_templates)]
            url = template.format(z=z, x=x, y=y, hl=self.language)
            self._counter += 1

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MissionAnalyzer/1.0 (personal UAV mission-planning tool)"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                break  # получилось — дальше не пытаемся
            except (URLError, HTTPError, TimeoutError, OSError):
                self.network_errors += 1
                if attempt < self.retries:
                    time.sleep(0.3 * (attempt + 1))  # короткая пауза перед повтором
                    continue
            finally:
                if self.polite_delay_s:
                    time.sleep(self.polite_delay_s)

        if data is None:
            return None

        if disk_path is not None:
            try:
                disk_path.write_bytes(data)
            except OSError:
                pass

        return data
