"""Identidade e autorização — validadas **offline**.

Duas perguntas diferentes, deliberadamente separadas
----------------------------------------------------

* `authenticate()` responde **quem é você**. Serve para abrir o PDV e para o
  garçom entrar no app. Um caixa passa aqui.
* `authorize()` responde **você pode liberar isto**. Serve para cancelamento e
  desconto, e exige `can_authorize`.

Juntar as duas foi o erro que este módulo já evitava e que agora fica explícito:
quem pode operar não é automaticamente quem pode liberar. Liberar o próprio
cancelamento é o furto inteiro em um passo.

Por que Argon2id e não um PIN comparado direto
----------------------------------------------

O `pdv_local.db` pode ser aberto pelo operador (ver `packaging/README.md`). PIN
em texto no banco seria o mesmo que não ter PIN. Argon2id com os parâmetros
recomendados pela OWASP (m=64 MiB, t=3, p=4) custa ~37 ms por tentativa nesta
máquina — caro por projeto, e é isso que sustenta o resto.

O freio, e por que ele vive em disco
------------------------------------

37 ms × 10 000 combinações de um PIN de 4 dígitos = **6 minutos**. O freio não é
um detalhe de conforto: é o que transforma um PIN curto em algo inviável de
adivinhar. Por isso duas correções que valem registro:

1. **O contador persiste** (`auth_throttle`). Antes vivia num dicionário de
   instância, e matar o processo zerava tudo: cada reabertura dava mais cinco
   tentativas livres. Um laço de "abre, tenta cinco, mata" recuperava as 10 000
   combinações em minutos.
2. **Existe um teto global**, além do teto por login. Sem ele, bastava espalhar
   as tentativas por vários logins para cada um render sua cota livre.

E o PIN passou a ter política: seis dígitos no mínimo (1 000 000 de
combinações) e recusa de sequências e repetições, que são o que as pessoas
escolhem quando ninguém as impede.

O que este módulo **não** resolve
---------------------------------

Um administrador da máquina com depurador lê o PIN da memória no momento em que
é digitado, e edita este banco à vontade — inclusive o `auth_throttle` e o
relógio. Não há solução local para esse perfil. A garantia forte continua sendo
a ancoragem no servidor: o evento já sincronizado não pode ser apagado da nuvem.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pdv.data.database import Database
from pdv.domain.errors import AuthorizationRequiredError, PdvError
from pdv.domain.models import EntityId, iso, utc_now

logger = logging.getLogger(__name__)

#: Tentativas por login antes do bloqueio temporário.
MAX_ATTEMPTS = 5

#: Tentativas somadas de **todos** os logins antes do freio global. Mais
#: generoso que o individual porque uma loja com cinco pessoas erra o PIN de
#: verdade algumas vezes por turno; apertado o bastante para que espalhar as
#: tentativas por vários logins não saia de graça.
MAX_GLOBAL_ATTEMPTS = 20

#: Segundos de bloqueio depois de estourar o teto. Dobra a cada novo estouro.
LOCKOUT_BASE_SECONDS = 30

#: Teto do bloqueio. Além disso o gerente não conseguiria autorizar uma venda
#: legítima num sábado cheio — a proteção viraria o problema.
LOCKOUT_MAX_SECONDS = 300

#: Expoente máximo do dobro. Sem o teto, um atacante insistente levaria o
#: cálculo a `2**10000` antes do `min()` — a defesa viraria o travamento.
_MAX_EXPONENT = 16

#: Janela após a qual as falhas antigas deixam de contar. Sem ela, cinco erros
#: espalhados por seis meses bloqueariam alguém que nunca foi atacado.
FAILURE_WINDOW = timedelta(hours=1)

#: Chave do freio global na `auth_throttle`.
_GLOBAL = "*"

#: Mínimo de dígitos do PIN. Quatro dígitos são 10 000 combinações; seis são
#: 1 000 000. Com o freio persistente, seis tornam a adivinhação inviável mesmo
#: que alguém contorne o bloqueio.
PIN_MIN_LENGTH = 6
PIN_MAX_LENGTH = 12

#: PINs que uma política que não os recusa acaba encontrando na loja.
_FORBIDDEN_PINS = frozenset(
    {"123456", "654321", "112233", "121212", "123123", "000000", "111111",
     "696969", "666666", "159753", "147258", "102030", "123321", "010203"}
)


class WeakPinError(PdvError):
    """O PIN escolhido é fraco demais para proteger o que protege."""


@dataclass(frozen=True, slots=True)
class Identity:
    """Quem entrou. Responde "quem é você", não "o que você pode liberar"."""

    id: EntityId
    name: str
    login: str
    role: str
    can_authorize: bool
    max_discount_percent: Decimal

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.login


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
    """Valida credenciais contra a réplica local, sem internet."""

    def __init__(self, database: Database, tenant_id: str) -> None:
        self._db = database
        self._tenant_id = tenant_id
        #: Piso monotônico por escopo. O banco guarda o bloqueio entre
        #: execuções; isto impede que atrasar o relógio encurte um bloqueio
        #: **dentro** da sessão em andamento.
        self._floor: dict[str, float] = {}

    # -- quem é você ---------------------------------------------------------- #

    def authenticate(self, login: str, pin: str) -> Identity:
        """Confere a credencial de qualquer usuário ativo.

        Não exige `can_authorize`: o caixa precisa entrar para trabalhar, e o
        garçom precisa entrar para lançar em nome dele. O que ele **pode
        liberar** é outra pergunta, respondida por `authorize()`.

        Raises:
            AuthorizationRequiredError: credencial inválida ou freio ativo.
        """
        return self._check(login, pin, require_authorizer=False)

    # -- o que você pode liberar ---------------------------------------------- #

    def authorize(self, login: str, pin: str) -> Authorizer:
        """Valida a credencial de quem tem poder de autorizar."""
        identity = self._check(login, pin, require_authorizer=True)
        return _to_authorizer(identity)

    def authorize_role(
        self, login: str, pin: str, *, allowed_roles: frozenset[str]
    ) -> Authorizer:
        """Autoriza somente quando a credencial pertence ao papel exigido.

        ``can_authorize`` responde se a pessoa tem algum poder. O papel
        responde *qual* poder. Sem esta segunda verificação, a senha de um
        gerente serviria como senha de proprietário apenas porque ambas têm o
        primeiro bit ligado.
        """
        identity = self._check(login, pin, require_authorizer=True)
        if identity.role not in allowed_roles:
            self._register_failure(identity.login)
            expected = (
                "proprietário"
                if allowed_roles == frozenset({"owner"})
                else "gerente"
            )
            raise AuthorizationRequiredError(
                f"Esta operação exige a credencial de um {expected}."
            )
        return _to_authorizer(identity)

    def authorize_discount(
        self, login: str, pin: str, percent: Decimal,
        *, allowed_roles: frozenset[str] | None = None,
    ) -> Authorizer:
        """Autoriza um desconto, respeitando o teto do perfil.

        O teto é do **perfil de quem autoriza**, não da operação. Um gerente com
        limite de 30% não concede 50% nem com a senha certa — senão o limite
        seria decorativo.
        """
        authorizer = (
            self.authorize_role(login, pin, allowed_roles=allowed_roles)
            if allowed_roles is not None
            else self.authorize(login, pin)
        )
        if not authorizer.may_discount(percent):
            raise AuthorizationRequiredError(
                f"{authorizer.name} pode conceder até "
                f"{authorizer.max_discount_percent}% — o pedido é de {percent}%."
            )
        return authorizer

    # -- consultas ------------------------------------------------------------ #

    def list_authorizers(
        self, allowed_roles: frozenset[str] | None = None
    ) -> list[str]:
        """Logins que podem autorizar, para preencher o diálogo."""
        rows = self._db.query_all(
            "SELECT login,role FROM users "
            " WHERE tenant_id = ? AND is_active = 1 AND can_authorize = 1 "
            " ORDER BY name",
            (self._tenant_id,),
        )
        return [
            str(row["login"]) for row in rows
            if allowed_roles is None or str(row["role"]) in allowed_roles
        ]

    def find(self, user_id: EntityId) -> Identity | None:
        row = self._db.query_one(
            "SELECT id, name, login, role, can_authorize, max_discount_percent "
            "  FROM users WHERE id = ? AND tenant_id = ? AND is_active = 1",
            (user_id, self._tenant_id),
        )
        return _to_identity(row) if row else None

    # -- verificação ---------------------------------------------------------- #

    def _check(self, login: str, pin: str, *, require_authorizer: bool) -> Identity:
        login = login.strip().lower()
        if not login:
            raise AuthorizationRequiredError("Informe o login.")

        self._assert_not_locked(login)

        row = self._db.query_one(
            "SELECT id, name, login, role, pin_hash, can_authorize, "
            "       max_discount_percent "
            "  FROM users "
            " WHERE lower(login) = ? AND tenant_id = ? AND is_active = 1",
            (login, self._tenant_id),
        )

        # Mesmo sem o usuário, gastamos o custo do Argon2 contra um hash
        # descartável. Responder na hora para login inexistente diria ao
        # atacante quais logins existem antes mesmo de ele tentar um PIN.
        if row is None or not row["pin_hash"]:
            _burn_time()
            self._register_failure(login)
            raise AuthorizationRequiredError("Login ou PIN inválido.")

        if not _verify(str(row["pin_hash"]), pin):
            self._register_failure(login)
            logger.warning("Tentativa de autenticação recusada para %r", login)
            raise AuthorizationRequiredError("Login ou PIN inválido.")

        identity = _to_identity(row)

        if require_authorizer and not identity.can_authorize:
            # Credencial correta, poder ausente: a mensagem pode ser específica
            # porque quem chegou aqui já provou quem é. E **conta como falha**:
            # insistir em liberar o que não se pode é sinal, não engano.
            self._register_failure(login)
            raise AuthorizationRequiredError(
                f"{identity.name} não tem permissão para autorizar esta operação."
            )

        self._clear(login)
        return identity

    # -- freio ---------------------------------------------------------------- #

    def lock_status(self, login: str) -> int:
        """Segundos restantes de bloqueio para este login. Zero se liberado."""
        login = login.strip().lower()
        return max(
            self._remaining(f"login:{login}"), self._remaining(_GLOBAL)
        )

    def _assert_not_locked(self, login: str) -> None:
        individual = self._remaining(f"login:{login}")
        if individual > 0:
            raise AuthorizationRequiredError(
                f"Muitas tentativas para este login. Aguarde {individual}s."
            )

        overall = self._remaining(_GLOBAL)
        if overall > 0:
            raise AuthorizationRequiredError(
                f"Muitas tentativas neste terminal. Aguarde {overall}s. "
                "Se não foi você, avise o gerente."
            )

    def _remaining(self, scope: str) -> int:
        """Quantos segundos faltam, pelo maior dos dois relógios.

        O banco sobrevive ao restart; o piso monotônico sobrevive ao relógio
        atrasado. Nenhum dos dois sozinho cobre os dois casos.
        """
        floor = self._floor.get(scope, 0.0) - time.monotonic()

        row = self._db.query_one(
            "SELECT locked_until FROM auth_throttle WHERE scope = ?", (scope,)
        )
        stored = 0.0
        if row is not None and row["locked_until"]:
            try:
                until = datetime.fromisoformat(str(row["locked_until"]))
            except ValueError:  # pragma: no cover - coluna corrompida
                until = utc_now()
            stored = (until - utc_now()).total_seconds()

        remaining = max(floor, stored)
        return int(remaining) + 1 if remaining > 0 else 0

    def _register_failure(self, login: str) -> None:
        for scope, ceiling in (
            (f"login:{login}", MAX_ATTEMPTS),
            (_GLOBAL, MAX_GLOBAL_ATTEMPTS),
        ):
            self._bump(scope, ceiling)

    def _bump(self, scope: str, ceiling: int) -> None:
        now = utc_now()
        row = self._db.query_one(
            "SELECT failures, first_failure_at FROM auth_throttle WHERE scope = ?",
            (scope,),
        )

        failures = 1
        first_at = now
        if row is not None:
            try:
                previous_first = datetime.fromisoformat(str(row["first_failure_at"]))
            except ValueError:  # pragma: no cover
                previous_first = now
            # Falhas antigas deixam de contar. Sem a janela, cinco erros
            # espalhados por seis meses bloqueariam quem nunca foi atacado.
            if now - previous_first <= FAILURE_WINDOW:
                failures = int(row["failures"]) + 1
                first_at = previous_first

        locked_until = None
        if failures >= ceiling:
            exponent = min(failures - ceiling, _MAX_EXPONENT)
            delay = min(LOCKOUT_BASE_SECONDS * (2**exponent), LOCKOUT_MAX_SECONDS)
            locked_until = now + timedelta(seconds=delay)
            self._floor[scope] = time.monotonic() + delay
            logger.warning(
                "Freio de autenticação ativo em %s por %ds (%d falhas)",
                scope, delay, failures,
            )

        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO auth_throttle "
                "   (scope, failures, locked_until, first_failure_at, last_failure_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (scope) DO UPDATE SET "
                "   failures = excluded.failures, "
                "   locked_until = excluded.locked_until, "
                "   first_failure_at = excluded.first_failure_at, "
                "   last_failure_at = excluded.last_failure_at",
                (
                    scope,
                    failures,
                    iso(locked_until) if locked_until else None,
                    iso(first_at),
                    iso(now),
                ),
            )

    def _clear(self, login: str) -> None:
        """Sucesso limpa o contador do login — e **não** o global.

        Limpar o global no primeiro acerto daria ao atacante uma saída barata:
        errar dezenove vezes, acertar o próprio login e recomeçar do zero.
        O global decai sozinho pela janela.
        """
        scope = f"login:{login}"
        self._floor.pop(scope, None)
        with self._db.transaction() as connection:
            connection.execute("DELETE FROM auth_throttle WHERE scope = ?", (scope,))


# --------------------------------------------------------------------------- #
# Política de PIN
# --------------------------------------------------------------------------- #


def validate_pin(pin: str) -> str:
    """Confere se o PIN é forte o bastante. Devolve-o normalizado.

    A política não é burocracia: o PIN é a única coisa entre um operador
    curioso e a autorização de cancelamento. Quatro dígitos sequenciais são o
    que as pessoas escolhem quando ninguém as impede, e são justamente o que um
    atacante tenta primeiro.

    Raises:
        WeakPinError: com a explicação do que precisa mudar.
    """
    pin = str(pin).strip()

    if not pin.isdigit():
        raise WeakPinError("O PIN deve conter apenas dígitos.")
    if len(pin) < PIN_MIN_LENGTH:
        raise WeakPinError(
            f"O PIN precisa de pelo menos {PIN_MIN_LENGTH} dígitos."
        )
    if len(pin) > PIN_MAX_LENGTH:
        raise WeakPinError(f"O PIN pode ter no máximo {PIN_MAX_LENGTH} dígitos.")
    if pin in _FORBIDDEN_PINS:
        raise WeakPinError("Este PIN é um dos mais tentados. Escolha outro.")
    if len(set(pin)) == 1:
        raise WeakPinError("O PIN não pode ser um único dígito repetido.")
    if _is_run(pin, +1) or _is_run(pin, -1):
        raise WeakPinError("O PIN não pode ser uma sequência de dígitos.")
    if len(set(pin)) == 2 and _is_alternating(pin):
        raise WeakPinError("O PIN não pode ser dois dígitos alternados.")

    return pin


def _is_run(pin: str, step: int) -> bool:
    return all(
        (int(pin[i + 1]) - int(pin[i])) % 10 == step % 10 for i in range(len(pin) - 1)
    )


def _is_alternating(pin: str) -> bool:
    return all(pin[i] == pin[i % 2] for i in range(len(pin)))


# --------------------------------------------------------------------------- #
# Argon2id
# --------------------------------------------------------------------------- #

#: Hash descartável para gastar tempo quando o login não existe. Os parâmetros
#: acompanham os do `PasswordHasher` padrão para que o custo bata.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _hasher():  # noqa: ANN202
    from argon2 import PasswordHasher

    # Padrões do argon2-cffi: m=64 MiB, t=3, p=4, Argon2id — a configuração
    # recomendada pela OWASP. Não são reduzidos aqui de propósito: o custo por
    # tentativa é o que sustenta o freio.
    return PasswordHasher()


def hash_pin(pin: str, *, enforce_policy: bool = True) -> str:
    """Gera o hash de um PIN. Usado no seed e no cadastro de usuário.

    Args:
        enforce_policy: desligado apenas para **importar** hashes ou PINs
            legados vindos da retaguarda, onde recusar travaria a loja inteira
            por um cadastro antigo. Nunca desligue para um PIN digitado agora.
    """
    if enforce_policy:
        pin = validate_pin(pin)
    return _hasher().hash(pin)


def _verify(stored_hash: str, pin: str) -> bool:
    try:
        from argon2.exceptions import VerificationError, VerifyMismatchError
    except ImportError:  # pragma: no cover
        logger.error("argon2-cffi ausente: autenticação indisponível")
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


def _to_identity(row) -> Identity:  # noqa: ANN001
    return Identity(
        id=EntityId(str(row["id"])),
        name=str(row["name"]),
        login=str(row["login"]),
        role=str(row["role"]),
        can_authorize=bool(int(row["can_authorize"])),
        max_discount_percent=Decimal(str(row["max_discount_percent"] or "0")),
    )


def _to_authorizer(identity: Identity) -> Authorizer:
    return Authorizer(
        id=identity.id,
        name=identity.name,
        role=identity.role,
        max_discount_percent=identity.max_discount_percent,
    )


__all__ = [
    "FAILURE_WINDOW",
    "MAX_ATTEMPTS",
    "MAX_GLOBAL_ATTEMPTS",
    "PIN_MIN_LENGTH",
    "AuthorizationService",
    "Authorizer",
    "Identity",
    "WeakPinError",
    "hash_pin",
    "validate_pin",
]
