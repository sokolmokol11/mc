"""Список всех ботов. Чтобы добавить нового — создай класс и допиши его сюда."""
from bots.core_bots import PumpShort, ListingShort, RepeatPumpSpot, WeeklyMomentum
from bots.pump_v2 import PumpShortV2
from bots.trend_bots import SuperTrend
from bots.reversion_bots import RsiBollinger
from bots.hunter_bots import SqueezeLong

ALL_BOTS = [
    # основные стратегии
    PumpShortV2, PumpShort, ListingShort, RepeatPumpSpot, WeeklyMomentum,
    # контрольная группа — для сравнения
    SuperTrend, RsiBollinger, SqueezeLong,
]
