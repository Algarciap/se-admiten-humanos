"""Durable source-health transitions and low-noise operator alerts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .persistence import RentalStore, SourceHealthRecord
from .telegram import TelegramBotClient


class MonitoringNotifier(Protocol):
    def notify(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MonitoringSettings:
    failure_threshold: int = 3
    stale_after_seconds: int = 1800

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "MonitoringSettings":
        values = os.environ if environment is None else environment
        try:
            settings = cls(
                failure_threshold=int(
                    values.get("SOURCE_FAILURE_ALERT_THRESHOLD", "3").strip()
                ),
                stale_after_seconds=int(
                    values.get("SOURCE_STALE_AFTER_SECONDS", "1800").strip()
                ),
            )
        except ValueError as error:
            raise ValueError("Monitoring settings must contain whole numbers") from error
        if settings.failure_threshold < 2:
            raise ValueError("SOURCE_FAILURE_ALERT_THRESHOLD must be at least 2")
        if settings.stale_after_seconds < 300:
            raise ValueError("SOURCE_STALE_AFTER_SECONDS must be at least 300")
        return settings


class TelegramMonitoringNotifier:
    """Send operational alerts to an operator chat, never to search subscribers."""

    def __init__(self, client: TelegramBotClient, chat_id: str) -> None:
        self.client = client
        self.chat_id = chat_id.strip()

    def notify(self, message: str) -> None:
        self.client.send_message(self.chat_id, message)


def monitoring_notifier_from_environment(
    environment: Mapping[str, str] | None = None,
) -> MonitoringNotifier | None:
    values = os.environ if environment is None else environment
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (
        values.get("MONITORING_TELEGRAM_CHAT_ID", "").strip()
        or values.get("TELEGRAM_CHAT_ID", "").strip()
    )
    if not token or not chat_id:
        return None
    return TelegramMonitoringNotifier(TelegramBotClient(token), chat_id)


_SOURCE_LABELS = {
    "alquiler_seguro": "Alquiler Seguro",
    "tecnocasa": "Tecnocasa",
    "redpiso": "Redpiso",
    "nestar": "Nestar",
    "idealista": "Idealista",
}


class SourceHealthMonitor:
    """Persist every source outcome and emit only state-changing alerts."""

    def __init__(
        self,
        store: RentalStore,
        settings: MonitoringSettings,
        notifier: MonitoringNotifier | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.notifier = notifier

    def success(
        self,
        source_id: str,
        status: str,
        listing_count: int,
    ) -> tuple[str, ...]:
        record, previous_alert_state = self.store.record_source_success(
            source_id,
            status,
            listing_count,
        )
        if previous_alert_state == "healthy":
            return ()
        return self._emit(
            "✅ Fuente recuperada\n"
            f"Fuente: {self._label(record.source_id)}\n"
            f"Inventario visible: {listing_count}\n"
            "Las comprobaciones vuelven a funcionar."
        )

    def failure(
        self,
        source_id: str,
        error: str,
        *,
        unexpected_zero: bool = False,
    ) -> tuple[str, ...]:
        status = "zero_results" if unexpected_zero else "failed"
        record = self.store.record_source_failure(
            source_id,
            status,
            error,
            listing_count=0 if unexpected_zero else None,
        )
        if record.alert_state != "healthy":
            return ()
        if unexpected_zero:
            alert_state = "zero_results"
            heading = "🚨 Una fuente ha devuelto cero viviendas"
        elif record.consecutive_failures >= self.settings.failure_threshold:
            alert_state = "failing"
            heading = "🚨 Una fuente está fallando repetidamente"
        else:
            return ()
        record = self.store.set_source_alert_state(record.source_id, alert_state)
        return self._emit(
            f"{heading}\n"
            f"Fuente: {self._label(record.source_id)}\n"
            f"Fallos consecutivos: {record.consecutive_failures}\n"
            f"Último éxito: {record.last_success_at or 'todavía ninguno'}\n"
            f"Detalle: {record.last_error or 'sin detalle'}"
        )

    def check_stale(self, source_ids: tuple[str, ...]) -> tuple[str, ...]:
        errors: list[str] = []
        now = datetime.now(UTC)
        threshold = timedelta(seconds=self.settings.stale_after_seconds)
        for source_id in source_ids:
            record = self.store.source_health(source_id)
            if (
                record is None
                or record.last_success_at is None
                or record.alert_state != "healthy"
            ):
                continue
            last_success = datetime.fromisoformat(record.last_success_at)
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=UTC)
            if now - last_success < threshold:
                continue
            record = self.store.set_source_alert_state(source_id, "stale")
            errors.extend(
                self._emit(
                    "⚠️ Fuente sin éxitos recientes\n"
                    f"Fuente: {self._label(record.source_id)}\n"
                    f"Último éxito: {record.last_success_at}\n"
                    f"Umbral: {self.settings.stale_after_seconds // 60} minutos"
                )
            )
        return tuple(errors)

    def _emit(self, message: str) -> tuple[str, ...]:
        if self.notifier is None:
            return ()
        try:
            self.notifier.notify(message[:4096])
        except Exception as error:
            return (f"monitoring notification failed: {type(error).__name__}: {error}"[:1000],)
        return ()

    @staticmethod
    def _label(source_id: str) -> str:
        return _SOURCE_LABELS.get(source_id, source_id)


def source_health_age_seconds(
    record: SourceHealthRecord,
    *,
    now: datetime | None = None,
) -> int | None:
    if record.last_success_at is None:
        return None
    observed = datetime.fromisoformat(record.last_success_at)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return max(0, int(((now or datetime.now(UTC)) - observed).total_seconds()))
