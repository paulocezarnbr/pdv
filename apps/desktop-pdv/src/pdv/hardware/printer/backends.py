"""Backends de entrega do payload ESC/POS à Epson TM-T20X.

Três rotas, mesma entrada (`bytes`):

1. **`Win32RawPrinter` — primária no Windows.** Mantém o driver Epson instalado
   e envia bytes crus pelo spooler com datatype ``RAW``. A impressora continua
   utilizável por outros programas e a fila do Windows faz o enfileiramento.
2. **`EscposUsbPrinter` — libusb.** Exige trocar o driver Epson por WinUSB
   (Zadig), o que **impede** outros programas de usarem a impressora. Reservada
   para Linux ou terminal dedicado.
3. **`FilePrinter` — desenvolvimento.** Grava `.bin` (bytes) e `.txt` (prévia
   legível). Permite revisar o cupom sem hardware.

A impressão roda em thread própria (`PrintService`): uma impressora sem papel
não pode congelar o caixa (invariante 9 do `plan.md`).
"""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Callable

from pdv.config import PrinterConfig
from pdv.domain.errors import PrinterError, PrinterNotFoundError


class PrinterBackend(ABC):
    """Entrega bytes ESC/POS ao dispositivo."""

    @abstractmethod
    def send(self, payload: bytes, job_name: str = "PDV Cupom") -> None:
        """Raises: PrinterError se a entrega falhar."""

    def is_available(self) -> bool:
        return True


class Win32RawPrinter(PrinterBackend):
    """Spooler do Windows em modo RAW — rota recomendada em produção."""

    def __init__(self, printer_name: str) -> None:
        self._printer_name = printer_name

    def is_available(self) -> bool:
        try:
            import win32print
        except ImportError:
            return False
        printers = {
            info[2]
            for info in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )
        }
        return self._printer_name in printers

    def send(self, payload: bytes, job_name: str = "PDV Cupom") -> None:
        try:
            import win32print
        except ImportError as exc:  # pragma: no cover
            raise PrinterError(
                "pywin32 não instalado — execute: pip install pywin32"
            ) from exc

        try:
            handle = win32print.OpenPrinter(self._printer_name)
        except Exception as exc:
            raise PrinterNotFoundError(
                f"Impressora {self._printer_name!r} não encontrada. "
                "Confira o nome exato em Dispositivos e Impressoras."
            ) from exc

        try:
            # ("nome", saída=None, datatype="RAW"): RAW faz o spooler entregar os
            # bytes sem interpretar — indispensável para ESC/POS.
            job = win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, payload)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
            del job
        except Exception as exc:
            raise PrinterError(f"Falha ao imprimir: {exc}") from exc
        finally:
            win32print.ClosePrinter(handle)


class EscposUsbPrinter(PrinterBackend):
    """python-escpos sobre libusb (Linux / terminal dedicado)."""

    def __init__(self, vendor_id: int, product_id: int) -> None:
        self._vendor_id = vendor_id
        self._product_id = product_id

    def is_available(self) -> bool:
        try:
            import usb.core  # type: ignore[import-untyped]
        except ImportError:
            return False
        return usb.core.find(idVendor=self._vendor_id, idProduct=self._product_id) is not None

    def send(self, payload: bytes, job_name: str = "PDV Cupom") -> None:
        try:
            from escpos.printer import Usb  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise PrinterError(
                "python-escpos não instalado — execute: pip install python-escpos"
            ) from exc

        try:
            device = Usb(self._vendor_id, self._product_id)
        except Exception as exc:
            raise PrinterNotFoundError(
                f"Impressora USB {self._vendor_id:04X}:{self._product_id:04X} "
                "não encontrada (driver WinUSB/libusb instalado?)"
            ) from exc

        try:
            device._raw(payload)  # noqa: SLF001 — enviamos nosso próprio ESC/POS
        except Exception as exc:
            raise PrinterError(f"Falha ao imprimir via USB: {exc}") from exc
        finally:
            try:
                device.close()
            except Exception:  # pragma: no cover
                pass


class FilePrinter(PrinterBackend):
    """Grava o cupom em disco. Desenvolvimento, testes e contingência."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def send(self, payload: bytes, job_name: str = "PDV Cupom") -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = self._output_dir / f"cupom_{stamp}"
        base.with_suffix(".bin").write_bytes(payload)
        # Prévia legível: remove os comandos de controle para leitura humana.
        preview = payload.decode("cp850", errors="replace")
        readable = "".join(ch for ch in preview if ch.isprintable() or ch == "\n")
        base.with_suffix(".txt").write_text(readable, encoding="utf-8")


def build_printer(config: PrinterConfig) -> PrinterBackend:
    """Fábrica a partir da configuração do terminal."""
    if config.backend == "win32raw":
        return Win32RawPrinter(config.windows_printer_name)
    if config.backend == "escpos_usb":
        return EscposUsbPrinter(config.usb_vendor_id, config.usb_product_id)
    return FilePrinter(config.output_dir)


class PrintService:
    """Fila de impressão assíncrona com retry.

    Regra do food service: **a venda já foi registrada e o estoque já baixou**
    quando a impressão é enfileirada. Se o papel acabar, o operador troca a
    bobina e reimprime — mas a transação nunca é perdida por causa da
    impressora.
    """

    def __init__(
        self,
        backend: PrinterBackend,
        max_attempts: int = 3,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._max_attempts = max_attempts
        self._on_error = on_error
        self._queue: queue.Queue[tuple[bytes, str] | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="printer-worker", daemon=True
        )
        self._thread.start()

    def submit(self, payload: bytes, job_name: str = "PDV Cupom") -> None:
        """Enfileira sem bloquear o chamador."""
        self._queue.put((payload, job_name))

    def shutdown(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            payload, name = job
            self._deliver(payload, name)

    def _deliver(self, payload: bytes, name: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._backend.send(payload, name)
                return
            except PrinterError as exc:
                last_error = exc
                threading.Event().wait(0.5 * attempt)  # backoff linear
        if last_error is not None and self._on_error is not None:
            self._on_error(f"Impressão falhou após {self._max_attempts} tentativas: {last_error}")
