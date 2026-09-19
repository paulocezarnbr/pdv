"""Pareamento e autenticação dos aparelhos na LAN.

A premissa que orienta tudo aqui: **a rede da loja não é confiável.** Na prática
é a mesma rede do Wi-Fi que o restaurante oferece ao cliente, com a senha escrita
num cartaz. Estar na rede não pode autorizar nada.

Como o aparelho entra
---------------------

1. Alguém com acesso **físico** ao caixa gera um código de pareamento, exibido
   na tela do PDV e válido por poucos minutos.
2. O garçom digita o código no celular.
3. O terminal devolve um token exclusivo daquele aparelho.

O acesso físico ao caixa é a âncora: quem não chega ao balcão não pareia nada,
mesmo estando na rede e conhecendo o endereço do PDV.

O que é guardado
----------------

Só o **hash** do token e o hash do código. Um dump do `pdv_local.db` — que o
operador consegue abrir, como documentado em `packaging/README.md` — não entrega
credencial de aparelho nenhum.

Revogação
---------

Celular perdido se revoga do próprio caixa, e o efeito é imediato: a verificação
consulta o banco a cada requisição. Cache de token aqui trocaria uma revogação
instantânea por uma janela de minutos, justamente quando ela mais importa.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.domain.models import EntityId, iso, new_id, utc_now

logger = logging.getLogger(__name__)

#: Validade do código de pareamento. Curto porque fica visível na tela do caixa,
#: onde qualquer um que passe pelo balcão consegue ler.
PAIRING_TTL = timedelta(minutes=5)

#: Dígitos do código.
#:
#: Eram seis. Seis dígitos são um milhão de combinações, e o código fica vivo
#: por cinco minutos numa rede onde o atacante pode disparar requisições sem
#: esperar ninguém: a conta que importa não é "quanto tempo para adivinhar
#: tudo", é "quantas tentativas cabem na janela". Oito dígitos multiplicam o
#: espaço por cem e continuam sendo dois grupos de quatro na tela do caixa —
#: exatamente o formato que as pessoas já leem em código de confirmação.
PAIRING_CODE_DIGITS = 8

#: Tentativas de pareamento erradas antes do bloqueio, e quanto ele dura.
#:
#: O freio é do terminal inteiro, não por aparelho: quem tenta adivinhar um
#: código ainda não tem aparelho nenhum, então não há por quem separar. Um
#: garçom que erra de verdade tenta duas, três vezes — dez é folga larga.
MAX_PAIRING_ATTEMPTS = 10
PAIRING_LOCKOUT_SECONDS = 120

#: Escopo do freio de pareamento na `auth_throttle`, que já existe e já
#: sobrevive ao restart. Uma segunda tabela de tentativas teria de repetir a
#: janela, o bloqueio e o piso monotônico — e divergir deles.
_PAIRING_SCOPE = "edge:pair"

TOKEN_BYTES = 32


class PairingError(PdvError):
    """Código de pareamento inválido, expirado ou já usado."""


@dataclass(frozen=True, slots=True)
class PairingCode:
    """Um código vivo na tela do caixa, e quanto falta para ele vencer."""

    expires_at: datetime

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.expires_at - utc_now()).total_seconds()))

    @property
    def is_alive(self) -> bool:
        return self.remaining_seconds > 0


class DeviceAuthError(PdvError):
    """Token de aparelho ausente, desconhecido ou revogado."""


@dataclass(frozen=True, slots=True)
class PairedDevice:
    id: EntityId
    name: str
    kind: str
    operator_id: EntityId | None


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


class EdgeAuth:
    """Ciclo de vida das credenciais dos aparelhos da LAN."""

    def __init__(self, database: Database, tenant_id: str, store_id: str) -> None:
        self._db = database
        self._tenant_id = tenant_id
        self._store_id = store_id
        #: Piso monotônico do freio de pareamento. Ver `pairing_lock_seconds`.
        self._pair_floor = 0.0

    # -- pareamento ----------------------------------------------------------- #

    def create_pairing_code(self) -> tuple[str, PairingCode]:
        """Gera e persiste um código. Devolve-o em texto **uma única vez**.

        Gerar um novo **revoga** os anteriores. Antes não revogava, e o efeito
        era contraintuitivo na direção errada: quem via o código na tela e
        clicava "gerar outro" — porque achou que alguém tinha lido — deixava os
        dois válidos, inclusive o que acabara de ser lido.
        """
        code = "".join(secrets.choice("0123456789") for _ in range(PAIRING_CODE_DIGITS))
        now = utc_now()
        expires_at = now + PAIRING_TTL

        with self._db.transaction() as connection:
            self._expire_all(connection, now)
            connection.execute(
                "INSERT INTO edge_pairing_codes (code_hash, created_at, expires_at) "
                "VALUES (?, ?, ?)",
                (_hash(code), iso(now), iso(expires_at)),
            )

        logger.info("Código de pareamento gerado (validade %s)", PAIRING_TTL)
        return code, PairingCode(expires_at=expires_at)

    def active_pairing_code(self) -> PairingCode | None:
        """O código ainda vivo, se houver. Só o prazo — nunca o código.

        O texto existe uma vez só, na resposta de `create_pairing_code`. Se
        fosse possível relê-lo do banco, o hash não serviria para nada.
        """
        row = self._db.query_one(
            "SELECT expires_at FROM edge_pairing_codes "
            " WHERE used_at IS NULL AND expires_at > ? "
            " ORDER BY expires_at DESC LIMIT 1",
            (iso(utc_now()),),
        )
        if row is None:
            return None
        try:
            return PairingCode(expires_at=datetime.fromisoformat(str(row["expires_at"])))
        except ValueError:  # pragma: no cover - coluna corrompida
            return None

    def revoke_pairing_codes(self) -> int:
        """Mata os códigos vivos antes do prazo. Devolve quantos caíram.

        É o botão para quando alguém estranho passa pelo balcão enquanto o
        código está na tela. Sem ele, a única saída era esperar cinco minutos
        olhando para o próprio código exposto.
        """
        now = utc_now()
        with self._db.transaction() as connection:
            count = self._expire_all(connection, now)
        if count:
            logger.warning("Códigos de pareamento revogados: %d", count)
        return count

    @staticmethod
    def _expire_all(connection, now) -> int:  # noqa: ANN001
        """Vence os códigos abertos, na transação do chamador.

        Vence pelo prazo em vez de marcar `used_at`: um código marcado como
        usado mentiria sobre ter pareado algum aparelho, e é justamente a
        coluna que alguém vai olhar para saber quem entrou.
        """
        cursor = connection.execute(
            "UPDATE edge_pairing_codes SET expires_at = ? "
            " WHERE used_at IS NULL AND expires_at > ?",
            (iso(now), iso(now)),
        )
        return int(cursor.rowcount)

    def pair(self, code: str, *, device_name: str, kind: str = "waiter") -> str:
        """Troca um código válido por um token de aparelho.

        Devolve o token em texto — é a única vez que ele existe fora do celular.

        Raises:
            PairingError: código inválido, expirado ou já usado.
        """
        if kind not in ("waiter", "kds"):
            raise PairingError(f"Tipo de aparelho desconhecido: {kind!r}")

        self._assert_not_throttled()

        now = utc_now()
        device_id = new_id()
        token = secrets.token_urlsafe(TOKEN_BYTES)

        try:
            self._consume(code, device_id, token, device_name, kind, now)
        except PairingError:
            self._register_pairing_failure()
            raise

        self._clear_pairing_failures()
        logger.info("Aparelho pareado: %s (%s)", device_name, kind)
        return token

    def _consume(  # noqa: PLR0913 - é uma transação só, com os dados dela
        self,
        code: str,
        device_id: str,
        token: str,
        device_name: str,
        kind: str,
        now,  # noqa: ANN001
    ) -> None:
        with self._db.transaction() as connection:
            # Consumo atômico: o WHERE carrega todas as condições de validade e
            # o rowcount diz se ESTA transação foi a que consumiu. Ler e depois
            # gravar abriria a janela em que dois celulares usam o mesmo código.
            cursor = connection.execute(
                "UPDATE edge_pairing_codes SET used_at = ?, used_by = ? "
                "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (iso(now), device_id, _hash(code), iso(now)),
            )
            if cursor.rowcount != 1:
                # Mensagem única para inexistente, expirado e usado: distingui-los
                # diria a quem tenta adivinhar que acertou o código e errou só o
                # tempo.
                #
                # A falha é registrada FORA desta transação, no `except` abaixo:
                # levantar aqui faz o rollback desfazer tudo o que esta
                # transação escreveu — inclusive o contador de tentativas, que
                # é justamente o que não pode ser desfeito por quem falhou.
                raise PairingError(
                    "Código inválido, expirado ou já utilizado. "
                    "Gere um novo na tela do caixa."
                )

            connection.execute(
                """
                INSERT INTO edge_devices
                    (id, tenant_id, store_id, name, kind, token_hash,
                     paired_at, created_at, updated_at, client_uuid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    self._tenant_id,
                    self._store_id,
                    device_name.strip()[:64] or "Aparelho sem nome",
                    kind,
                    _hash(token),
                    iso(now),
                    iso(now),
                    iso(now),
                    new_id(),
                ),
            )

    # -- freio do pareamento -------------------------------------------------- #

    def _assert_not_throttled(self) -> None:
        remaining = self.pairing_lock_seconds()
        if remaining > 0:
            raise PairingError(
                f"Tentativas demais de pareamento. Aguarde {remaining}s. "
                "Se não foi você, gere um código novo no caixa."
            )

    def pairing_lock_seconds(self) -> int:
        """Segundos restantes do bloqueio de pareamento. Zero se liberado."""
        floor = self._pair_floor - time.monotonic()

        row = self._db.query_one(
            "SELECT locked_until FROM auth_throttle WHERE scope = ?",
            (_PAIRING_SCOPE,),
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

    def _register_pairing_failure(self) -> None:
        now = utc_now()
        row = self._db.query_one(
            "SELECT failures, first_failure_at FROM auth_throttle WHERE scope = ?",
            (_PAIRING_SCOPE,),
        )

        failures = 1
        first_at = now
        if row is not None:
            try:
                previous = datetime.fromisoformat(str(row["first_failure_at"]))
            except ValueError:  # pragma: no cover
                previous = now
            # Mesma janela do freio de login: erro de hoje não pode somar com
            # erro do mês passado.
            if now - previous <= timedelta(hours=1):
                failures = int(row["failures"]) + 1
                first_at = previous

        locked_until = None
        if failures >= MAX_PAIRING_ATTEMPTS:
            locked_until = now + timedelta(seconds=PAIRING_LOCKOUT_SECONDS)
            # Piso monotônico pelo mesmo motivo do login: o banco sobrevive ao
            # restart, o piso sobrevive ao relógio atrasado dentro da sessão.
            self._pair_floor = time.monotonic() + PAIRING_LOCKOUT_SECONDS
            logger.warning(
                "Freio de pareamento ativo por %ds (%d tentativas)",
                PAIRING_LOCKOUT_SECONDS, failures,
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
                    _PAIRING_SCOPE,
                    failures,
                    iso(locked_until) if locked_until else None,
                    iso(first_at),
                    iso(now),
                ),
            )

    def _clear_pairing_failures(self) -> None:
        """Pareamento bem-sucedido zera o contador.

        Aqui limpar no acerto é seguro, ao contrário do freio global de login:
        acertar exige um código que **o caixa acabou de gerar**, de uso único.
        Não existe a saída barata de errar nove vezes e acertar o próprio para
        recomeçar do zero.
        """
        self._pair_floor = 0.0
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM auth_throttle WHERE scope = ?", (_PAIRING_SCOPE,)
            )

    # -- autenticação --------------------------------------------------------- #

    def authenticate(self, token: str | None) -> PairedDevice:
        """Resolve o token num aparelho pareado e ativo.

        Raises:
            DeviceAuthError: token ausente, desconhecido ou revogado.
        """
        if not token:
            raise DeviceAuthError("Aparelho não autenticado.")

        digest = _hash(token)
        row = self._db.query_one(
            "SELECT id, name, kind, operator_id, token_hash, revoked_at "
            "FROM edge_devices WHERE token_hash = ? AND tenant_id = ?",
            (digest, self._tenant_id),
        )

        if row is None:
            raise DeviceAuthError("Aparelho não reconhecido. Pareie novamente.")

        # compare_digest mesmo já tendo casado no WHERE: a busca é por índice e
        # o retorno precisa passar por comparação de tempo constante para não
        # transformar o banco num oráculo de timing.
        if not hmac.compare_digest(str(row["token_hash"]), digest):  # pragma: no cover
            raise DeviceAuthError("Aparelho não reconhecido.")

        if row["revoked_at"]:
            raise DeviceAuthError(
                "Este aparelho foi revogado. Procure o responsável pelo caixa."
            )

        self._touch(str(row["id"]))
        return PairedDevice(
            id=EntityId(str(row["id"])),
            name=str(row["name"]),
            kind=str(row["kind"]),
            operator_id=EntityId(str(row["operator_id"])) if row["operator_id"] else None,
        )

    def revoke(self, device_id: EntityId) -> bool:
        """Revoga um aparelho. Devolve se algo mudou."""
        now = iso(utc_now())
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE edge_devices SET revoked_at = ?, updated_at = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (now, now, device_id),
            )
            changed = cursor.rowcount == 1

        if changed:
            logger.warning("Aparelho revogado: %s", device_id)
        return changed

    def list_devices(self) -> list[dict[str, object]]:
        rows = self._db.query_all(
            "SELECT id, name, kind, paired_at, last_seen_at, revoked_at "
            "FROM edge_devices WHERE tenant_id = ? ORDER BY paired_at",
            (self._tenant_id,),
        )
        return [dict(row) for row in rows]

    def _touch(self, device_id: str) -> None:
        """Registra o último contato — é como o caixa vê quem está online."""
        with self._db.transaction() as connection:
            connection.execute(
                "UPDATE edge_devices SET last_seen_at = ? WHERE id = ?",
                (iso(utc_now()), device_id),
            )
