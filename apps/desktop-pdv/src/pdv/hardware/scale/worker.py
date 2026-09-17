"""Worker Qt de leitura contínua da balança.

Invariante 9 do `plan.md`: leitura serial **nunca** roda na UI thread. Uma porta
COM travada congelaria a tela do caixa com a fila esperando — o pior sintoma
possível num PDV.

O worker também resolve o problema de *estabilidade*: a balança oscila enquanto
a mercadoria acomoda no prato. Registrar a venda no primeiro valor lido cobra o
peso errado. Só emitimos `stable_weight` após N leituras idênticas consecutivas.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from pdv.config import ScaleConfig
from pdv.domain.errors import ScaleError
from pdv.domain.models import Grams, ScaleReading, ScaleStatus
from pdv.hardware.scale.base import ScaleDriver


class ScaleWorker(QObject):
    """Roda em `QThread` própria e publica leituras por sinais Qt."""

    #: Toda leitura, estável ou não — alimenta o display ao vivo.
    reading_received = Signal(ScaleReading)
    #: Emitido uma única vez por estabilização; habilita o registro do item.
    stable_weight = Signal(ScaleReading)
    #: Peso saiu da estabilidade (mercadoria retirada / trocada).
    weight_changed = Signal()
    #: Falha de comunicação — a UI mostra o estado, não uma exceção.
    error_occurred = Signal(str)
    connection_changed = Signal(bool)

    def __init__(self, driver: ScaleDriver, config: ScaleConfig) -> None:
        super().__init__()
        self._driver = driver
        self._config = config
        self._running = False
        self._last_grams: int | None = None
        self._repeat_count = 0
        self._stable_emitted = False
        self._error_streak = 0

    # -- ciclo de vida -------------------------------------------------------- #

    @Slot()
    def start(self) -> None:
        """Loop principal. Chamado por `QThread.started`."""
        self._running = True
        try:
            self._driver.open()
            self.connection_changed.emit(True)
        except ScaleError as exc:
            self.error_occurred.emit(str(exc))
            self.connection_changed.emit(False)
            return

        while self._running:
            self._tick()
            QThread.msleep(int(self._config.poll_interval_seconds * 1000))

        self._driver.close()
        self.connection_changed.emit(False)

    @Slot()
    def stop(self) -> None:
        self._running = False

    # -- leitura -------------------------------------------------------------- #

    def _tick(self) -> None:
        try:
            reading = self._driver.read()
        except ScaleError as exc:
            self._error_streak += 1
            # Só reporta erro persistente: um timeout isolado é ruído normal
            # em serial e encheria a tela de alertas inúteis.
            if self._error_streak in (3, 30):
                self.error_occurred.emit(str(exc))
            self._reset_stability()
            return

        self._error_streak = 0
        self.reading_received.emit(reading)
        self._evaluate_stability(reading)

    def _evaluate_stability(self, reading: ScaleReading) -> None:
        if reading.status is not ScaleStatus.STABLE:
            self._reset_stability()
            return

        grams = int(reading.weight_grams)
        if grams == self._last_grams:
            self._repeat_count += 1
        else:
            if self._last_grams is not None and self._stable_emitted:
                self.weight_changed.emit()
            self._last_grams = grams
            self._repeat_count = 1
            self._stable_emitted = False

        if self._repeat_count >= self._config.stable_readings and not self._stable_emitted:
            self._stable_emitted = True
            self.stable_weight.emit(reading)

    def _reset_stability(self) -> None:
        if self._stable_emitted:
            self.weight_changed.emit()
        self._last_grams = None
        self._repeat_count = 0
        self._stable_emitted = False


class ScaleService(QObject):
    """Fachada que a UI usa: cria a thread, conecta sinais e encerra limpo."""

    reading_received = Signal(ScaleReading)
    stable_weight = Signal(ScaleReading)
    weight_changed = Signal()
    error_occurred = Signal(str)
    connection_changed = Signal(bool)

    def __init__(self, driver: ScaleDriver, config: ScaleConfig) -> None:
        super().__init__()
        self._thread = QThread()
        self._thread.setObjectName("scale-reader")
        self._worker = ScaleWorker(driver, config)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.start)
        self._worker.reading_received.connect(self.reading_received)
        self._worker.stable_weight.connect(self.stable_weight)
        self._worker.weight_changed.connect(self.weight_changed)
        self._worker.error_occurred.connect(self.error_occurred)
        self._worker.connection_changed.connect(self.connection_changed)

        self._last_stable: ScaleReading | None = None
        self.stable_weight.connect(self._remember)
        self.weight_changed.connect(self._forget)

    @Slot(ScaleReading)
    def _remember(self, reading: ScaleReading) -> None:
        self._last_stable = reading

    @Slot()
    def _forget(self) -> None:
        self._last_stable = None

    @property
    def last_stable_reading(self) -> ScaleReading | None:
        """Última leitura estável ainda válida, ou ``None`` se o peso mudou."""
        return self._last_stable

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(3000)


__all__ = ["ScaleWorker", "ScaleService", "Grams"]
