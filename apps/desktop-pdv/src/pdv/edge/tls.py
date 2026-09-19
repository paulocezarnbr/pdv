"""Certificado TLS do servidor do salão.

Por que HTTPS numa rede local
-----------------------------

O servidor do salão escuta em `0.0.0.0` e a LAN da loja é, quase sempre, a
mesma rede do Wi-Fi que o restaurante oferece ao cliente — com a senha escrita
num cartaz. Em HTTP, três coisas trafegam em claro por essa rede:

* o **token do aparelho**, no cabeçalho `Authorization` de toda requisição;
* o **PIN do garçom e o do gerente**, no corpo do login;
* o **token de sessão**, na resposta desse login.

Qualquer aparelho na mesma rede captura isso sem esforço nenhum. Toda a defesa
construída até aqui — pareamento presencial, Argon2id, freio persistente, poder
de gerente com prazo curto — é contornada por quem simplesmente escuta a rede
e reusa o token. TLS é o que fecha esse caminho.

O certificado é autoassinado, e isso é honesto
----------------------------------------------

Não há autoridade certificadora que emita certificado para `192.168.0.14`, e um
domínio público apontando para o caixa da loja seria pior — exporia a rede
interna e dependeria de internet para renovar, num sistema cuja premissa é
funcionar sem ela.

O que o autoassinado entrega e o que não entrega:

* **Entrega confidencialidade.** O token e o PIN deixam de trafegar legíveis.
  Contra o atacante passivo — o que escuta — a proteção é completa.
* **Não entrega autenticidade por si só.** Quem consegue se pôr no meio da rede
  pode apresentar *outro* certificado autoassinado, e o navegador do celular
  mostra o mesmo aviso nos dois casos.

Por isso a impressão digital (`fingerprint`) aparece na tela do caixa: no
pareamento, quem está no balcão confere os primeiros dígitos com o que o
celular mostra. É a mesma âncora física do código de pareamento, aplicada à
identidade do servidor.

Se a geração falhar, o salão sobe em HTTP
------------------------------------------

Mesma regra que vale para a balança, a impressora e a nuvem neste sistema:
nenhum acessório pode impedir a venda. Sem `cryptography` instalada, ou com o
diretório sem permissão de escrita, o servidor sobe em claro e registra o aviso
— um salão sem criptografia atende; um salão que não sobe, não.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Validade do certificado. 398 dias é o teto que os navegadores aceitam para
#: certificado público; seguir o mesmo número aqui evita descobrir, no dia em
#: que a regra passar a valer também para os autoassinados, que a loja inteira
#: parou.
VALIDITY_DAYS = 398

#: Margem para renovar antes de vencer. Sem ela, o certificado vira no meio de
#: um sábado e o app do garçom para de conectar com a loja cheia.
RENEW_BEFORE_DAYS = 30

CERT_NAME = "edge-cert.pem"
KEY_NAME = "edge-key.pem"


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """O par de arquivos que o uvicorn consome, e a digital para conferência."""

    certificate_path: Path
    key_path: Path
    fingerprint: str
    not_after: dt.datetime
    hosts: tuple[str, ...]

    @property
    def short_fingerprint(self) -> str:
        """Os primeiros blocos da digital, que é o que alguém confere na tela.

        Ninguém compara 32 bytes em hexadecimal olhando para um celular. Quatro
        blocos são o que uma pessoa consegue ler em voz alta e conferir sem
        errar — e adivinhar uma colisão nesses 32 bits exigiria gerar bilhões
        de certificados, o que não é o ataque que acontece numa loja.
        """
        return " ".join(self.fingerprint.split(":")[:4]).upper()


def ensure_certificate(
    directory: Path, *, store_name: str, hosts: tuple[str, ...]
) -> TlsMaterial | None:
    """Devolve o certificado do terminal, gerando-o se preciso.

    Reaproveita o que já existe enquanto ele continuar válido **e** continuar
    cobrindo todos os endereços pedidos. O segundo teste importa mais do que
    parece: o IP da loja vem de DHCP e muda quando o roteador reinicia; um
    certificado emitido para o endereço antigo faria o navegador do celular
    recusar a conexão com um erro que ninguém no balcão sabe interpretar.

    Devolve `None` quando não foi possível — e aí o servidor sobe em HTTP.
    """
    try:
        return _ensure(directory, store_name=store_name, hosts=hosts)
    except ImportError:
        logger.warning(
            "cryptography ausente: o salão sobe em HTTP. "
            "O token do aparelho e o PIN trafegam em claro na rede da loja."
        )
        return None
    except Exception:  # noqa: BLE001 - acessório nunca impede a venda
        logger.exception("Não foi possível preparar o certificado do salão")
        return None


def _ensure(
    directory: Path, *, store_name: str, hosts: tuple[str, ...]
) -> TlsMaterial:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CERT_NAME
    key_path = directory / KEY_NAME

    existing = _load(cert_path, key_path, hosts)
    if existing is not None:
        return existing

    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name(
        [
            x509.NameAttribute(x509.NameOID.COMMON_NAME, store_name[:64] or "PDV"),
            x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "PDV Balcão"),
        ]
    )

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        # Emissor igual ao sujeito: é autoassinado, e fingir outra coisa só
        # confundiria quem for diagnosticar.
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Um minuto para trás: relógio de máquina de loja atrasa, e um
        # certificado que "ainda não vale" é recusado do mesmo jeito que um
        # vencido — com um erro muito menos óbvio.
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
        .add_extension(_san(hosts), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    _write_private(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            # Sem senha: quem lê este arquivo é o próprio processo, na
            # inicialização, sem ninguém para digitar nada. A senha viveria ao
            # lado da chave no mesmo disco, o que não protege de nada e só
            # daria a impressão de proteger.
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )

    logger.info(
        "Certificado do salão gerado para %s (válido até %s)",
        ", ".join(hosts),
        certificate.not_valid_after_utc.date(),
    )
    return _material(certificate, cert_path, key_path, hosts)


def _load(cert_path: Path, key_path: Path, hosts: tuple[str, ...]) -> TlsMaterial | None:
    """O certificado em disco, se ainda servir. `None` manda gerar outro."""
    from cryptography import x509

    if not cert_path.exists() or not key_path.exists():
        return None

    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception:  # noqa: BLE001 - arquivo corrompido: gera outro
        logger.warning("Certificado do salão ilegível; um novo será gerado")
        return None

    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=RENEW_BEFORE_DAYS)
    if certificate.not_valid_after_utc <= horizon:
        logger.info("Certificado do salão perto de vencer; renovando")
        return None

    if not _covers(certificate, hosts):
        logger.info("O endereço da loja mudou; emitindo certificado novo")
        return None

    return _material(certificate, cert_path, key_path, hosts)


def _covers(certificate, hosts: tuple[str, ...]) -> bool:  # noqa: ANN001
    from cryptography import x509

    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:  # pragma: no cover - certificado alheio
        return False

    present = {str(name) for name in san.get_values_for_type(x509.DNSName)}
    present |= {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return all(host in present for host in hosts)


def _san(hosts: tuple[str, ...]):  # noqa: ANN202
    from cryptography import x509

    names = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return x509.SubjectAlternativeName(names)


def _material(
    certificate, cert_path: Path, key_path: Path, hosts: tuple[str, ...]
) -> TlsMaterial:  # noqa: ANN001
    from cryptography.hazmat.primitives import hashes

    digest = certificate.fingerprint(hashes.SHA256())
    return TlsMaterial(
        certificate_path=cert_path,
        key_path=key_path,
        fingerprint=":".join(f"{byte:02x}" for byte in digest),
        not_after=certificate.not_valid_after_utc,
        hosts=hosts,
    )


def _write_private(path: Path, content: bytes) -> None:
    """Grava a chave privada com a permissão mais fechada que der.

    No Windows o `chmod` do Python só mexe no bit de somente-leitura e não
    remove ninguém do ACL — ou seja, isto **não** protege a chave de um
    administrador da máquina, nem pretende. É a mesma fronteira honesta do
    resto do sistema: quem tem a máquina tem o banco, a chave e o processo (ver
    o cabeçalho de `services/authorization.py`). O que a chave protege é o
    tráfego na rede da loja, contra quem não está na máquina.
    """
    path.write_bytes(content)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - sistema de arquivos sem suporte
        logger.debug("Não foi possível restringir a permissão de %s", path.name)


def default_hosts() -> tuple[str, ...]:
    """Os endereços pelos quais o app vai chegar neste terminal."""
    from pdv.edge.discovery import local_ip_address

    address = local_ip_address()
    # `localhost` e `127.0.0.1` para o KDS rodando na própria máquina do caixa;
    # o IP da LAN para os celulares. Ordem estável para o `_covers` comparar.
    hosts = ["localhost", "127.0.0.1"]
    if address not in hosts:
        hosts.append(address)
    return tuple(hosts)


__all__ = [
    "CERT_NAME",
    "KEY_NAME",
    "RENEW_BEFORE_DAYS",
    "VALIDITY_DAYS",
    "TlsMaterial",
    "default_hosts",
    "ensure_certificate",
]
