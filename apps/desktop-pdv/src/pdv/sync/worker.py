"""Worker de sincronização em segundo plano.

Roda em `QThread` própria: a sincronização **nunca** pode travar a tela do
caixa (invariante 9 do `plan.md`). Um servidor lento não pode virar fila parada
no balcão.

Cadência
--------

O intervalo curto entre ciclos é uma decisão de **segurança**, não de
desempenho. A janela em que uma venda existe apenas no PC do caixa — e portanto
pode ser adulterada por quem tem acesso à máquina — é exatamente o tempo entre
o `COMMIT` local e o ACK do servidor. Sincronizar a cada poucos segundos reduz
essa janela ao mínimo prático.

Quando há fila pendente, o worker acelera. Quando está em dia, desacelera para
não bater no servidor à toa.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from pdv.sync.engine import SyncEngine
from pdv.sync.protocol import SyncReport

logger = logging.getLogger(__name__)

#: Intervalo quando a fila está vazia.
IDLE_INTERVAL_SECONDS = 15

#: Intervalo quando ainda há itens pendentes — fecha a janela de exposição.
BUSY_INTERVAL_SECONDS = 3

#: Pausa após falha de rede. Evita martelar um servidor fora do ar.
ERROR_INTERVAL_SECONDS = 30

#: Ciclos de push entre uma tentativa de pull. O pull de cadastro é bem menos
#: urgente que o push de vendas: preço novo pode esperar, venda não.
PULL_EVERY_N_CYCLES = 20


class SyncWorker(QObject):
    """Laço de sincronização. Vive na thread de sync, não na da UI."""

    cycle_finished = Signal(SyncReport)
    pending_changed = Signal(int)
    connection_changed = Signal(bool)

    def __init__(self, engine: SyncEngine) -> None:
        super().__init__()
        self._engine = engine
        self._running = False
        self._cycle = 0
        self._online = False

    @Slot()
    def start(self) -> None:
        self._running = True

        while self._running:
            interval = self._tick()

            # Dorme em fatias para encerrar rápido quando o app fecha. Um
            # `msleep(30000)` inteiro faria o operador esperar meio minuto para
            # a janela fechar.
            slept = 0
            while self._running and slept < interval * 1000:
                QThread.msleep(200)
                slept += 200

    @Slot()
    def stop(self) -> None:
        self._running = False

    def _tick(self) -> int:
        """Executa um ciclo e devolve quantos segundos dormir depois."""
        self._cycle += 1

        try:
            report = self._engine.drain(max_cycles=10)
        except Exception as exc:  # noqa: BLE001 - o worker não pode morrer
            # Se esta thread morrer, o PDV para de sincronizar em silêncio e
            # ninguém percebe até o relatório do fim do mês. Erro inesperado é
            # registrado e o laço continua.
            logger.exception("Erro inesperado no ciclo de sincronização")
            self._set_online(False)
            self.cycle_finished.emit(SyncReport(error=str(exc)))
            return ERROR_INTERVAL_SECONDS

        self._set_online(report.error is None)
        self.cycle_finished.emit(report)

        pending = self._engine.pending_count()
        self.pending_changed.emit(pending)

        if report.error is not None:
            return ERROR_INTERVAL_SECONDS

        if self._cycle % PULL_EVERY_N_CYCLES == 0:
            try:
                self._engine.pull_once()
            except Exception:  # noqa: BLE001
                logger.exception("Falha no pull de cadastros")

        return BUSY_INTERVAL_SECONDS if pending > 0 else IDLE_INTERVAL_SECONDS

    def _set_online(self, online: bool) -> None:
        if online != self._online:
            self._online = online
            self.connection_changed.emit(online)


class SyncService(QObject):
    """Fachada para a UI: cria a thread, repassa sinais e encerra limpo."""

    cycle_finished = Signal(SyncReport)
    pending_changed = Signal(int)
    connection_changed = Signal(bool)

    def __init__(self, engine: SyncEngine) -> None:
        super().__init__()
        self._thread = QThread()
        self._thread.setObjectName("sync-worker")
        self._worker = SyncWorker(engine)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.start)
        self._worker.cycle_finished.connect(self.cycle_finished)
        self._worker.pending_changed.connect(self.pending_changed)
        self._worker.connection_changed.connect(self.connection_changed)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Encerra o worker.

        Não força o envio do que resta: a fila é durável e sobe no próximo
        início. Segurar o fechamento da janela esperando a rede seria pior —
        o operador mataria o processo no gerenciador de tarefas.
        """
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(timeout_ms)
