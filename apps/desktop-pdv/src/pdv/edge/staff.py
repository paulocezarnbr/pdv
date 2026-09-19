"""A sessão da pessoa no app do garçom.

O que estava errado
-------------------

O app tinha **uma** identidade: o aparelho pareado. Todo pedido saía atribuído
ao celular (`operator_id = S.device`), e daí decorriam duas coisas ruins:

* **Não havia resultado por funcionário.** Três garçons revezando o mesmo
  tablet produziam uma coluna só no relatório. Gorjeta por pessoa era
  impossível de apurar, e a divisão acabava no olho.
* **A trilha de auditoria apontava para um objeto.** "Quem cancelou o item da
  mesa 7" respondia "o celular 3". Num sistema cujo módulo central é
  anti-furto, isso é o mesmo que não responder.

Duas credenciais, duas perguntas
--------------------------------

O pareamento **continua**, e não foi substituído. As duas camadas respondem a
perguntas diferentes e falham em direções diferentes:

* O token do **aparelho** diz *de onde* veio o lançamento. Sai por pareamento
  presencial no caixa e é revogável dali (celular perdido).
* O token da **sessão** diz *quem* lançou. Sai do login e PIN da pessoa,
  validados pelo mesmo `AuthorizationService` do balcão — Argon2id contra a
  réplica local, com o mesmo freio persistente.

Um celular roubado sem o PIN de ninguém não lança nada. Um PIN vazado sem
aparelho pareado também não. Exigir as duas é o que mantém as duas úteis.

Por que esta sessão vive em disco, e a do gerente não
-----------------------------------------------------

`edge/manager.py` guarda a concessão só em memória, deliberadamente: ela é
**poder** — cancelar comanda, configurar mesa — e poder que sobrevive a um
restart sobrevive também a um restart provocado.

Esta sessão é **identidade**. Ela não concede nada que o aparelho pareado já
não pudesse fazer; só nomeia quem está agindo. Se evaporasse a cada reinício do
PDV — que acontece numa queda de energia, no meio do sábado —, a loja inteira
teria de redigitar PIN com o salão cheio, e o caminho de menor resistência
viraria deixar um login só aberto para todos. Seria o problema original de
volta, agora com uma tela de login por cima.

Por isso ela dura o turno e é revogável do caixa, aparelho por aparelho e
pessoa por pessoa.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from pdv.data.database import Database
from pdv.domain.errors import AuthorizationRequiredError
from pdv.domain.models import EntityId, iso, utc_now
from pdv.services.authorization import AuthorizationService, Identity

logger = logging.getLogger(__name__)

#: Duração da sessão. Um turno de food service passa das oito horas num
#: sábado; catorze cobre a abertura até a última mesa sem obrigar ninguém a
#: redigitar PIN no meio do serviço, e ainda garante que o aparelho esquecido
#: na gaveta não amanheça logado.
SESSION_TTL = timedelta(hours=14)

#: Sessões vivas por aparelho. É sempre 1 na prática — entrar derruba a
#: anterior —, mas o teto existe porque um app defeituoso pedindo login em
#: laço não pode encher a tabela do banco que grava a venda.
MAX_SESSIONS_PER_DEVICE = 4

TOKEN_BYTES = 32


class StaffAuthError(AuthorizationRequiredError):
    """Sessão ausente, vencida, revogada ou de outro aparelho."""


@dataclass(frozen=True, slots=True)
class StaffSession:
    """Quem está atendendo, neste aparelho, até quando."""

    token: str
    user_id: EntityId
    name: str
    login: str
    role: str
    device_id: EntityId
    expires_at: datetime

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.login

    def to_json(self, *, with_token: bool = False) -> dict[str, object]:
        data: dict[str, object] = {
            "user_id": self.user_id,
            "name": self.name,
            "first_name": self.first_name,
            "login": self.login,
            "role": self.role,
            "expires_at": self.expires_at.isoformat(),
            "expires_in_seconds": max(
                0, int((self.expires_at - utc_now()).total_seconds())
            ),
        }
        # O token só sai na resposta do login. Repeti-lo em toda consulta o
        # espalharia pelo log de qualquer proxy no caminho.
        if with_token:
            data["token"] = self.token
        return data


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


class StaffSessions:
    """Ciclo de vida das sessões de garçom, persistidas no banco local."""

    def __init__(self, database: Database, tenant_id: str) -> None:
        self._db = database
        self._tenant_id = tenant_id
        self._auth = AuthorizationService(database, tenant_id)

    # -- entrada -------------------------------------------------------------- #

    def login(self, *, login: str, pin: str, device_id: EntityId) -> StaffSession:
        """Valida a credencial e abre a sessão neste aparelho.

        A validação é a **mesma** do balcão: `authenticate()`, não
        `authorize()`. Atender mesa não exige poder de autorizar, e exigi-lo
        obrigaria a dar poder de cancelamento a todo garçom para que ele
        pudesse simplesmente entrar.

        Raises:
            AuthorizationRequiredError: credencial inválida ou freio ativo. A
                mensagem vem do `AuthorizationService` e é genérica de
                propósito — dizer *qual* parte errou entregaria os logins que
                existem.
        """
        identity = self._auth.authenticate(login, pin)

        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = utc_now()
        expires_at = now + SESSION_TTL

        with self._db.transaction() as connection:
            # Entrar derruba quem estava neste aparelho. Sem isto, a troca de
            # turno deixaria a sessão do garçom anterior viva no mesmo celular:
            # bastaria o app guardar o token antigo para os pedidos da noite
            # continuarem saindo no nome de quem já foi para casa.
            connection.execute(
                "UPDATE edge_staff_sessions SET revoked_at = ? "
                " WHERE device_id = ? AND revoked_at IS NULL",
                (iso(now), device_id),
            )
            connection.execute(
                """
                INSERT INTO edge_staff_sessions
                    (token_hash, tenant_id, device_id, user_id, user_name,
                     user_login, role, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash(token),
                    self._tenant_id,
                    device_id,
                    identity.id,
                    identity.name,
                    identity.login,
                    identity.role,
                    iso(now),
                    iso(expires_at),
                    iso(now),
                ),
            )
            self._trim(connection, device_id)

        logger.info(
            "Sessão aberta no salão: %s (%s) no aparelho %s",
            identity.name, identity.role, device_id,
        )
        return StaffSession(
            token=token,
            user_id=identity.id,
            name=identity.name,
            login=identity.login,
            role=identity.role,
            device_id=EntityId(str(device_id)),
            expires_at=expires_at,
        )

    # -- verificação ---------------------------------------------------------- #

    def require(self, token: str | None, device_id: EntityId) -> StaffSession:
        """Resolve o token numa sessão viva **deste** aparelho.

        A conferência contra o aparelho é a mesma da concessão de gerente: sem
        ela, um token lido da tela de um celular valeria em qualquer outro
        aparelho pareado da loja.

        Raises:
            StaffAuthError: sem sessão, vencida, revogada ou de outro aparelho.
        """
        if not token:
            raise StaffAuthError(
                "Entre com o seu login para lançar pedidos neste aparelho."
            )

        digest = _hash(token)
        row = self._db.query_one(
            "SELECT token_hash, device_id, user_id, user_name, user_login, "
            "       role, expires_at, revoked_at "
            "  FROM edge_staff_sessions "
            " WHERE token_hash = ? AND tenant_id = ?",
            (digest, self._tenant_id),
        )
        if row is None:
            raise StaffAuthError("Sessão não reconhecida. Entre de novo.")

        # Igual a `EdgeAuth.authenticate`: a busca casou por índice, e o
        # retorno ainda passa por comparação de tempo constante para não
        # transformar o banco num oráculo de timing.
        if not hmac.compare_digest(str(row["token_hash"]), digest):  # pragma: no cover
            raise StaffAuthError("Sessão não reconhecida.")

        if row["revoked_at"]:
            raise StaffAuthError(
                "Sua sessão foi encerrada no caixa. Entre de novo."
            )

        expires_at = _parse(str(row["expires_at"]))
        if expires_at <= utc_now():
            raise StaffAuthError("Seu turno expirou. Entre de novo para continuar.")

        if str(row["device_id"]) != str(device_id):
            # Não se revoga a sessão aqui: quem a abriu legitimamente continua
            # com ela. Quem tentou usá-la de outro aparelho é que sai de mãos
            # vazias.
            logger.warning(
                "Sessão de %s usada no aparelho errado", row["user_name"]
            )
            raise StaffAuthError("Esta sessão não vale para este aparelho.")

        self._touch(digest)
        return StaffSession(
            token=token,
            user_id=EntityId(str(row["user_id"])),
            name=str(row["user_name"]),
            login=str(row["user_login"]),
            role=str(row["role"]),
            device_id=EntityId(str(row["device_id"])),
            expires_at=expires_at,
        )

    # -- saída ---------------------------------------------------------------- #

    def logout(self, token: str | None) -> bool:
        """Encerra a sessão. Devolve se havia algo a encerrar."""
        if not token:
            return False
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE edge_staff_sessions SET revoked_at = ? "
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (iso(utc_now()), _hash(token)),
            )
            return cursor.rowcount == 1

    def revoke_user(self, user_id: EntityId) -> int:
        """Derruba todas as sessões de uma pessoa, em todos os aparelhos.

        É o botão do caixa para quando alguém vai embora no meio do turno —
        ou para quando o gerente desconfia de como a comanda daquela mesa
        andou. Devolve quantas sessões caíram.
        """
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE edge_staff_sessions SET revoked_at = ? "
                " WHERE user_id = ? AND tenant_id = ? AND revoked_at IS NULL",
                (iso(utc_now()), user_id, self._tenant_id),
            )
        if cursor.rowcount:
            logger.warning("Sessões revogadas do usuário %s: %d", user_id, cursor.rowcount)
        return int(cursor.rowcount)

    # -- consultas ------------------------------------------------------------ #

    def list_active(self) -> list[dict[str, object]]:
        """Quem está em turno agora, para o painel do caixa."""
        rows = self._db.query_all(
            "SELECT user_id, user_name, role, device_id, created_at, "
            "       expires_at, last_seen_at "
            "  FROM edge_staff_sessions "
            " WHERE tenant_id = ? AND revoked_at IS NULL AND expires_at > ? "
            " ORDER BY created_at",
            (self._tenant_id, iso(utc_now())),
        )
        return [dict(row) for row in rows]

    # -- internos ------------------------------------------------------------- #

    def _touch(self, digest: str) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE edge_staff_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (iso(utc_now()), digest),
            )

    def _trim(self, connection, device_id: EntityId) -> None:  # noqa: ANN001
        """Apaga o histórico morto do aparelho, deixando as últimas linhas.

        Sessão revogada não serve a ninguém depois do turno: quem quer saber
        quem lançou o quê lê o pedido, que guarda o `operator_id`. Guardar toda
        sessão de todo turno faria a tabela crescer sem teto num banco que roda
        no caixa.
        """
        connection.execute(
            """
            DELETE FROM edge_staff_sessions
             WHERE device_id = ?
               AND token_hash NOT IN (
                   SELECT token_hash FROM edge_staff_sessions
                    WHERE device_id = ? ORDER BY created_at DESC LIMIT ?
               )
            """,
            (device_id, device_id, MAX_SESSIONS_PER_DEVICE),
        )


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - coluna corrompida
        # Data ilegível vira sessão vencida, nunca sessão eterna: o erro de
        # gravação não pode virar credencial permanente.
        return utc_now() - timedelta(seconds=1)


def identity_of(session: StaffSession) -> Identity:
    """Adapta a sessão para o formato que o resto do sistema já consome."""
    from decimal import Decimal

    return Identity(
        id=session.user_id,
        name=session.name,
        login=session.login,
        role=session.role,
        can_authorize=False,
        max_discount_percent=Decimal("0"),
    )


__all__ = [
    "MAX_SESSIONS_PER_DEVICE",
    "SESSION_TTL",
    "StaffAuthError",
    "StaffSession",
    "StaffSessions",
    "identity_of",
]
