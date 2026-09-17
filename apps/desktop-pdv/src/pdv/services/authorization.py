"""Autorização de gerente — validada **offline**.

Cancelamento e desconto são o vetor de furto nº 1 em PDV: o operador registra a
venda, recebe do cliente, cancela o item e fica com o dinheiro. O controle que
fecha isso é exigir a credencial de alguém com poder para autorizar, e registrar
quem autorizou no ledger.

Por que Argon2id e não um PIN comparado direto
----------------------------------------------

O `pdv_local.db` pode ser aberto pelo operador (ver `packaging/README.md`). PIN
em texto no banco seria o mesmo que não ter PIN. Argon2id é caro de calcular por
projeto, então mesmo com o hash em mãos não se recupera um PIN de 4 dígitos em
tempo útil — e a réplica local é o que permite autorizar **sem internet**, que é
o requisito do negócio.

O atraso entre tentativas
-------------------------

Um PIN de 4 dígitos tem 10 000 combinações. Sem freio, alguém com o terminal por
alguns minutos passa por todas. O bloqueio progressivo por operador transforma
isso em horas, e cada tentativa falha vira evento de auditoria: quem insiste
aparece no relatório.

O que este módulo **não** resolve
---------------------------------

Um administrador da máquina com depurador lê o PIN da memória no momento em que
é digitado. Não há solução local para isso — a garantia forte continua sendo a
ancoragem no servidor: o evento de autorização já sincronizado não pode ser
apagado da nuvem.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from pdv.data.database import Database
from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import EntityId

logger = logging.getLogger(__name__)

#: Tentativas antes do bloqueio temporário, por usuário.
MAX_ATTEMPTS = 5

#: Segundos de bloqueio depois de estourar o teto. Dobra a cada novo estouro.
LOCKOUT_BASE_SECONDS = 30

#: Teto do bloqueio. Além disso o gerente não conseguiria autorizar uma venda
#: legítima num sábado cheio — a proteção viraria o problema.
LOCKOUT_MAX_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Authorizer:
    """Quem autorizou. Vai para o ledger junto do evento."""

    id: EntityId
    name: str
    role: str
    max_discount_percent: Decimal

    def may_discount(self, percent: Decimal) -> bool:
        return percent <= self.max_discount_percent


class AuthorizationService:
    """Valida credencial de autorizador contra a réplica local."""

    def __init__(self, database: Database, tenant_id: str) -> None:
        self._db = database
        self._tenant_id = tenant_id
        #: login -> (tentativas, momento em que o bloqueio termina)
        self._failures: dict[str, tuple[int, float]] = {}

    # -- API ------------------------------------------------------------------ #

    def authorize(self, login: str, pin: str) -> Authorizer:
        """Valida a credencial e devolve quem autorizou.

        Raises:
            AuthorizationRequiredError: credencial inválida, usuário sem poder
                de autorizar, ou tentativas esgotadas.
        """
        login = login.strip().lower()
        self._check_lockout(login)

        row = self._db.query_one(
            "SELECT id, name, role, pin_hash, can_authorize, max_discount_percent "
            "  FROM users "
            " WHERE lower(login) = ? AND tenant_id = ? AND is_active = 1",
            (login, self._tenant_id),
        )

        # Mesmo sem o usuário, gastamos o custo do Argon2 contra um hash
        # descartável. Responder na hora para login inexistente diria ao
        # atacante quais logins existem antes mesmo de ele tentar um PIN.
        if row is None:
            _burn_time()
            self._register_failure(login)
            raise AuthorizationRequiredError("Credencial de gerente inválida.")

        if not row["pin_hash"]:
            _burn_time()
            self._register_failure(login)
            raise AuthorizationRequiredError(
                f"{row['name']} não tem PIN de autorização cadastrado."
            )

        if not _verify(str(row["pin_hash"]), pin):
            self._register_failure(login)
            logger.warning("Tentativa de autorização recusada para %r", login)
            raise AuthorizationRequiredError("Credencial de gerente inválida.")

        if not int(row["can_authorize"]):
            # Credencial correta, poder ausente: a mensagem pode ser específica
            # porque quem chegou aqui já provou quem é.
            self._register_failure(login)
            raise AuthorizationRequiredError(
                f"{row['name']} não tem permissão para autorizar esta operação."
            )

        self._failures.pop(login, None)
        return Authorizer(
            id=EntityId(str(row["id"])),
            name=str(row["name"]),
            role=str(row["role"]),
            max_discount_percent=Decimal(str(row["max_discount_percent"] or "0")),
        )

    def authorize_discount(
        self, login: str, pin: str, percent: Decimal
    ) -> Authorizer:
        """Autoriza um desconto, respeitando o teto do perfil.

        O teto é do **perfil de quem autoriza**, não da operação. Um gerente com
        limite de 30% não concede 50% nem com a senha certa — senão o limite
        seria decorativo.
        """
        authorizer = self.authorize(login, pin)
        if not authorizer.may_discount(percent):
            raise AuthorizationRequiredError(
                f"{authorizer.name} pode conceder até "
                f"{authorizer.max_discount_percent}% — o pedido é de {percent}%."
            )
        return authorizer

    def list_authorizers(self) -> list[str]:
        """Logins que podem autorizar, para preencher o diálogo."""
        rows = self._db.query_all(
            "SELECT login FROM users "
            " WHERE tenant_id = ? AND is_active = 1 AND can_authorize = 1 "
            " ORDER BY name",
            (self._tenant_id,),
        )
        return [str(row["login"]) for row in rows]

    # -- freio ---------------------------------------------------------------- #

    def _check_lockout(self, login: str) -> None:
        attempts, until = self._failures.get(login, (0, 0.0))
        remaining = until - time.monotonic()
        if remaining > 0:
            raise AuthorizationRequiredError(
                f"Muitas tentativas. Aguarde {int(remaining) + 1}s antes de tentar "
                "de novo."
            )

    def _register_failure(self, login: str) -> None:
        attempts, _until = self._failures.get(login, (0, 0.0))
        attempts += 1

        until = 0.0
        if attempts >= MAX_ATTEMPTS:
            # Dobra a cada teto atingido: 30s, 60s, 120s… até o limite.
            exponent = attempts - MAX_ATTEMPTS
            delay = min(LOCKOUT_BASE_SECONDS * (2**exponent), LOCKOUT_MAX_SECONDS)
            until = time.monotonic() + delay

        self._failures[login] = (attempts, until)


# --------------------------------------------------------------------------- #
# Argon2id
# --------------------------------------------------------------------------- #

_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _hasher():  # noqa: ANN202
    from argon2 import PasswordHasher

    return PasswordHasher()


def hash_pin(pin: str) -> str:
    """Gera o hash de um PIN. Usado no seed e no cadastro de usuário."""
    return _hasher().hash(pin)


def _verify(stored_hash: str, pin: str) -> bool:
    try:
        from argon2.exceptions import VerificationError, VerifyMismatchError
    except ImportError:  # pragma: no cover
        logger.error("argon2-cffi ausente: autorização indisponível")
        return False

    try:
        return bool(_hasher().verify(stored_hash, pin))
    except (VerifyMismatchError, VerificationError):
        return False
    except Exception:  # noqa: BLE001 - hash corrompido não pode virar "autorizado"
        logger.exception("Falha ao verificar credencial")
        return False


def _burn_time() -> None:
    """Gasta o mesmo tempo de uma verificação real.

    Sem isto, a resposta imediata para um login inexistente entrega quais
    logins existem — e o atacante passa a gastar tentativas só nos que valem.
    """
    try:
        _verify(_DUMMY_HASH, "pin-que-nao-existe")
    except Exception:  # noqa: BLE001 # pragma: no cover
        pass
