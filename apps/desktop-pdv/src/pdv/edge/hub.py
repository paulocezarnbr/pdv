"""Barramento de eventos do KDS.

O KDS não pode perguntar "mudou alguma coisa?" de segundo em segundo: com seis
telas na cozinha isso vira consulta constante no SQLite que o caixa precisa para
vender. O caminho é o inverso — quem grava avisa quem está olhando.

Por que uma fila por assinante, e não uma lista de callbacks
------------------------------------------------------------

Uma cozinha lenta não pode segurar a venda. Se o hub chamasse o assinante
diretamente, uma tela de KDS travada (Wi-Fi ruim, tablet congelado) bloquearia a
thread que está gravando o pedido — e o caixa pararia por causa de um tablet.
Com fila por assinante, o publicador só enfileira e segue.

O que acontece quando a fila enche
----------------------------------

Descarta o evento **mais antigo**, não o mais novo. Uma tela que ficou para trás
precisa do estado atual da cozinha, não do histórico que ela perdeu; e o KDS se
reconcilia buscando a lista completa ao reconectar. Descartar o mais novo
deixaria a tela permanentemente defasada.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from pdv.domain.models import iso, utc_now

logger = logging.getLogger(__name__)

#: Eventos represados por assinante antes de começar a descartar.
MAX_QUEUE_SIZE = 200


@dataclass(frozen=True, slots=True)
class Event:
    """Um fato já gravado. Nunca um pedido de ação."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: iso(utc_now()))

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, **self.payload}


class Subscription:
    """Ponta de leitura de um assinante."""

    def __init__(self, hub: EventHub, topics: frozenset[str]) -> None:
        self._hub = hub
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.topics = topics
        self.dropped = 0

    def offer(self, event: Event) -> None:
        """Enfileira sem nunca bloquear o publicador."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Abre espaço jogando fora o mais antigo. Se a corrida deixar a fila
            # cheia de novo, o evento é perdido e contabilizado — silenciar aqui
            # esconderia uma tela que parou de acompanhar.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except (queue.Empty, queue.Full):  # pragma: no cover
                pass
            self.dropped += 1

    def get(self, timeout: float | None = None) -> Event | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._hub.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EventHub:
    """Publicação em memória, segura entre threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscription] = []

    def subscribe(self, topics: frozenset[str] | set[str] | None = None) -> Subscription:
        """Assina os tópicos dados. `None` recebe tudo."""
        subscription = Subscription(self, frozenset(topics) if topics else frozenset())
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def publish(self, event: Event) -> int:
        """Entrega a quem interessa. Devolve quantos receberam.

        A cópia sob lock é curta de propósito: a entrega acontece fora dele, para
        que enfileirar num assinante não segure quem quer publicar.
        """
        with self._lock:
            targets = [
                s for s in self._subscribers if not s.topics or event.kind in s.topics
            ]

        for subscription in targets:
            subscription.offer(event)
        return len(targets)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
