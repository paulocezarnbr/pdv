"""Sessão de gerente no app do garçom.

O problema que este módulo resolve
----------------------------------

Até aqui o pareamento era a única autenticação do salão: qualquer aparelho
pareado podia tudo o que um garçom pode. Isso bastava enquanto o app só
lançava item — lançar a mais é erro visível, que o cliente reclama.

Configurar mesa, cancelar comanda e transferir conta não são assim. Cancelar
comanda com item lançado é **o** vetor de furto do salão: a comida sai, a
comanda some, e ninguém reclama porque o cliente pagou em dinheiro ao garçom.
Essas operações precisam de pessoa, não de aparelho.

Por que uma concessão curta, e não "logar como gerente"
-------------------------------------------------------

O celular do garçom fica em cima do balcão, desbloqueado, a noite inteira.
Uma sessão de gerente que durasse o turno seria, na prática, promover o
aparelho — qualquer um que passasse pelo balcão herdaria o poder.

A concessão vale poucos minutos e é **por aparelho**. O gerente digita o PIN,
faz o que veio fazer e o poder evapora sozinho. Ninguém precisa lembrar de
sair.

A credencial é validada pelo mesmo `AuthorizationService` do balcão: Argon2id
contra a réplica local, com o mesmo bloqueio progressivo. Offline inclusive —
que é o ponto do sistema inteiro.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import EntityId, utc_now
from pdv.services.authorization import AuthorizationService, Authorizer

logger = logging.getLogger(__name__)

#: Duração da concessão. Curta o bastante para que o celular esquecido no
#: balcão não vire um gerente permanente; longa o bastante para o gerente
#: cadastrar meia dúzia de mesas sem redigitar o PIN a cada uma.
GRANT_TTL = timedelta(minutes=10)

#: Teto de concessões vivas. Cada gerente na loja tem a sua; o limite existe
#: para que um app defeituoso pedindo concessão em laço não coma a memória do
#: processo que roda o caixa.
MAX_GRANTS = 64


@dataclass(frozen=True, slots=True)
class ManagerGrant:
    """Uma autorização de gerente, viva por alguns minutos num aparelho."""

    token: str
    authorizer: Authorizer
    device_id: EntityId
    expires_at: datetime

    def is_valid(self, *, now: datetime | None = None) -> bool:
        return (now or utc_now()) < self.expires_at

    def to_json(self) -> dict[str, object]:
        return {
            "token": self.token,
            "user_id": self.authorizer.id,
            "name": self.authorizer.name,
            "role": self.authorizer.role,
            "max_discount_percent": str(self.authorizer.max_discount_percent),
            "expires_at": self.expires_at.isoformat(),
            "expires_in_seconds": int(
                (self.expires_at - utc_now()).total_seconds()
            ),
        }


class ManagerSessions:
    """As concessões vivas. Só em memória, de propósito.

    Reiniciar o PDV derruba toda concessão de gerente. Isso é a decisão certa:
    o processo que caiu é o mesmo que guarda a comanda, então alguém já está
    olhando para o caixa — e uma autorização que sobrevivesse a um restart
    sobreviveria também a um restart provocado.
    """

    def __init__(self, authorization: AuthorizationService) -> None:
        self._auth = authorization
        self._grants: dict[str, ManagerGrant] = {}
        # O servidor atende vários celulares em paralelo; o dicionário não é
        # seguro para escrita concorrente sem isto.
        self._lock = threading.Lock()

    # -- emissão -------------------------------------------------------------- #

    def authorize(
        self, *, login: str, pin: str, device_id: EntityId
    ) -> ManagerGrant:
        """Valida a credencial e emite a concessão.

        Raises:
            AuthorizationRequiredError: credencial inválida, perfil sem poder de
                autorizar, ou tentativas esgotadas. A mensagem vem do
                `AuthorizationService` e é deliberadamente genérica — dizer
                *qual* parte errou entregaria os logins que existem.
        """
        authorizer = self._auth.authorize(login, pin)
        grant = ManagerGrant(
            token=secrets.token_urlsafe(32),
            authorizer=authorizer,
            device_id=device_id,
            expires_at=utc_now() + GRANT_TTL,
        )
        with self._lock:
            self._purge()
            if len(self._grants) >= MAX_GRANTS:
                raise AuthorizationRequiredError(
                    "Autorizações demais em aberto neste terminal. "
                    "Aguarde um minuto e tente de novo."
                )
            self._grants[grant.token] = grant

        logger.info(
            "Gerente %s autorizado no aparelho %s", authorizer.name, device_id
        )
        return grant

    # -- verificação ---------------------------------------------------------- #

    def require(self, token: str | None, device_id: EntityId) -> Authorizer:
        """Devolve quem autorizou, ou recusa.

        A concessão é conferida contra o **aparelho** que a pediu. Sem isso, um
        token vazado do celular do garçom valeria em qualquer outro aparelho
        pareado da loja.
        """
        if not token:
            raise AuthorizationRequiredError(
                "Esta operação precisa de autorização de gerente."
            )

        with self._lock:
            grant = self._grants.get(token)
            if grant is None or not grant.is_valid():
                self._grants.pop(token, None)
                raise AuthorizationRequiredError(
                    "Autorização expirada. Chame o gerente novamente."
                )
            if grant.device_id != device_id:
                # Não se apaga a concessão aqui: quem a obteve legitimamente
                # continua com ela. Quem tentou usá-la de outro aparelho é que
                # sai de mãos vazias.
                logger.warning(
                    "Concessão de %s usada no aparelho errado", grant.authorizer.name
                )
                raise AuthorizationRequiredError(
                    "Autorização não vale para este aparelho."
                )
            return grant.authorizer

    def revoke(self, token: str | None) -> bool:
        """Encerra a concessão. O gerente saiu antes do prazo."""
        if not token:
            return False
        with self._lock:
            return self._grants.pop(token, None) is not None

    def active_count(self) -> int:
        with self._lock:
            self._purge()
            return len(self._grants)

    def _purge(self) -> None:
        """Remove as vencidas. Sempre chamado com o lock em mãos."""
        now = utc_now()
        expired = [t for t, g in self._grants.items() if not g.is_valid(now=now)]
        for token in expired:
            del self._grants[token]


__all__ = ["GRANT_TTL", "MAX_GRANTS", "ManagerGrant", "ManagerSessions"]
