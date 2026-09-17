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
from dataclasses import dataclass
from datetime import timedelta

from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.domain.models import EntityId, iso, new_id, utc_now

logger = logging.getLogger(__name__)

#: Validade do código de pareamento. Curto porque fica visível na tela do caixa,
#: onde qualquer um que passe pelo balcão consegue ler.
PAIRING_TTL = timedelta(minutes=5)

#: Dígitos do código. Seis são digitáveis sem erro e, com validade de 5 minutos
#: e uso único, bastam — o atacante ainda precisaria estar na LAN.
PAIRING_CODE_DIGITS = 6

TOKEN_BYTES = 32


class PairingError(PdvError):
    """Código de pareamento inválido, expirado ou já usado."""


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

    # -- pareamento ----------------------------------------------------------- #

    def create_pairing_code(self) -> str:
        """Gera e persiste um código. Devolve-o em texto **uma única vez**."""
        code = "".join(secrets.choice("0123456789") for _ in range(PAIRING_CODE_DIGITS))
        now = utc_now()

        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO edge_pairing_codes (code_hash, created_at, expires_at) "
                "VALUES (?, ?, ?)",
                (_hash(code), iso(now), iso(now + PAIRING_TTL)),
            )

        logger.info("Código de pareamento gerado (validade %s)", PAIRING_TTL)
        return code

    def pair(self, code: str, *, device_name: str, kind: str = "waiter") -> str:
        """Troca um código válido por um token de aparelho.

        Devolve o token em texto — é a única vez que ele existe fora do celular.

        Raises:
            PairingError: código inválido, expirado ou já usado.
        """
        if kind not in ("waiter", "kds"):
            raise PairingError(f"Tipo de aparelho desconhecido: {kind!r}")

        now = utc_now()
        device_id = new_id()
        token = secrets.token_urlsafe(TOKEN_BYTES)

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

        logger.info("Aparelho pareado: %s (%s)", device_name, kind)
        return token

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
