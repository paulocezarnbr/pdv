"""Configuração persistida do terminal (`device_settings`).

Separada de `config.py` de propósito: `AppConfig` é a configuração **do
processo** (lida do ambiente na inicialização); isto aqui é a configuração **da
máquina**, gravada pelo instalador e pela detecção automática de periféricos, e
que sobrevive a reinstalações.

Nada de segredo entra aqui. O `device_secret` fica no DPAPI
(ver `provisioning/secrets.py`): guardá-lo no mesmo banco que ele protege
esvaziaria a proteção.
"""

from __future__ import annotations

from dataclasses import dataclass

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.domain.models import iso, utc_now


@dataclass(frozen=True, slots=True)
class DeviceSettings:
    """Instantâneo das configurações gravadas na máquina."""

    scale_protocol: str | None = None
    scale_port: str | None = None
    scale_baudrate: int | None = None
    printer_backend: str | None = None
    printer_name: str | None = None
    activated: bool = False
    tenant_id: str | None = None
    store_id: str | None = None
    device_id: str | None = None
    cloud_base_url: str | None = None
    store_name: str | None = None


class SettingsStore:
    """Leitura e escrita chave/valor em `device_settings`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- primitivas ----------------------------------------------------------- #

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.query_one(
            "SELECT value FROM device_settings WHERE key = ?", (key,)
        )
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._db.transaction() as connection:
            self._set_in(connection, key, value)

    def set_many(self, values: dict[str, str]) -> None:
        """Grava várias chaves numa transação só.

        Importa: a detecção de periféricos grava porta, protocolo e impressora
        juntos. Gravar uma a uma deixaria o terminal num estado meio
        configurado se faltasse energia no meio.
        """
        with self._db.transaction() as connection:
            for key, value in values.items():
                self._set_in(connection, key, value)

    def set_many_if_absent(self, values: dict[str, str]) -> dict[str, str]:
        """Grava apenas as chaves ainda não configuradas. Devolve o que gravou.

        É o modo correto numa **atualização**. Redetectar e sobrescrever a cada
        update é destrutivo por dois caminhos, ambos silenciosos:

        * a balança estar desligada no instante do update rebaixaria o terminal
          para `simulated`, e a loja pararia de vender produto por peso sem que
          nada tenha quebrado de fato;
        * o nome de impressora que o técnico corrigiu à mão seria substituído
          pelo palpite da detecção.

        Redetecção que sobrescreve é ato deliberado — o atalho "Reconfigurar
        periféricos" — e passa por `set_many`.
        """
        applied: dict[str, str] = {}
        with self._db.transaction() as connection:
            for key, value in values.items():
                row = connection.execute(
                    "SELECT value FROM device_settings WHERE key = ?", (key,)
                ).fetchone()
                if row is not None and row["value"]:
                    continue
                self._set_in(connection, key, value)
                applied[key] = value
        return applied

    @staticmethod
    def _set_in(connection, key: str, value: str) -> None:  # noqa: ANN001
        connection.execute(
            "INSERT INTO device_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, iso(utc_now())),
        )

    # -- visão tipada --------------------------------------------------------- #

    def load(self) -> DeviceSettings:
        rows = self._db.query_all("SELECT key, value FROM device_settings")
        data = {row["key"]: row["value"] for row in rows}

        return DeviceSettings(
            scale_protocol=data.get("scale.protocol"),
            scale_port=data.get("scale.port"),
            scale_baudrate=int(data["scale.baudrate"])
            if data.get("scale.baudrate")
            else None,
            printer_backend=data.get("printer.backend"),
            printer_name=data.get("printer.name"),
            activated=data.get("device.activated") == "1",
            tenant_id=data.get("device.tenant_id"),
            store_id=data.get("device.store_id"),
            device_id=data.get("device.id"),
            cloud_base_url=data.get("cloud.base_url"),
            store_name=data.get("store.name"),
        )

    def apply_to(self, config: AppConfig) -> AppConfig:
        """Sobrepõe o `AppConfig` com o que foi detectado/ativado na máquina.

        Precedência: o que está gravado na máquina **vence** o padrão do código,
        porque foi posto ali pela detecção real de hardware ou pela ativação do
        terminal. Variáveis de ambiente continuam úteis para depuração, mas em
        produção quem manda é o que o instalador descobriu.
        """
        settings = self.load()

        scale = config.scale
        if settings.scale_protocol and settings.scale_port:
            scale = ScaleConfig(
                protocol=settings.scale_protocol,  # type: ignore[arg-type]
                port=settings.scale_port,
                baudrate=settings.scale_baudrate or config.scale.baudrate,
                stable_readings=config.scale.stable_readings,
                max_weight_grams=config.scale.max_weight_grams,
            )

        printer = config.printer
        if settings.printer_backend:
            printer = PrinterConfig(
                backend=settings.printer_backend,  # type: ignore[arg-type]
                windows_printer_name=settings.printer_name
                or config.printer.windows_printer_name,
                columns=config.printer.columns,
                codepage=config.printer.codepage,
                output_dir=config.printer.output_dir,
            )

        return AppConfig(
            tenant_id=settings.tenant_id or config.tenant_id,
            store_id=settings.store_id or config.store_id,
            device_id=settings.device_id or config.device_id,
            device_secret=config.device_secret,
            store_name=settings.store_name or config.store_name,
            store_document=config.store_document,
            store_address=config.store_address,
            database_path=config.database_path,
            cloud_base_url=settings.cloud_base_url or config.cloud_base_url,
            scale=scale,
            printer=printer,
            stock=config.stock,
        )
