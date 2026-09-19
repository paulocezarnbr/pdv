"""O app do garçom, servido pelo próprio PDV.

Por que web, e não um aplicativo instalado
------------------------------------------

O app nativo continua no roteiro, e este **não** o substitui — resolve o que
ele não resolve:

* **Não depende de loja de aplicativo.** Numa loja sem internet, instalar um
  APK em cinco celulares é o tipo de tarefa que não acontece. Aqui o garçom
  aponta a câmera para o QR do caixa e está dentro.
* **Atualiza junto com o PDV.** O app é servido pelo mesmo processo que tem o
  banco. Não existe versão de celular defasada falando com um terminal novo —
  que é a classe de bug mais cara de diagnosticar por telefone.
* **Funciona no aparelho que a pessoa já tem.** Inclusive no tablet velho da
  cozinha e no celular do dono.

O que ele não é
---------------

Não é offline-first. O celular do garçom fala com o PDV pela LAN da loja; se o
Wi-Fi cair, ele não lança pedido — e é assim mesmo, porque o PDV é a autoridade
da comanda. Guardar pedido no celular e sincronizar depois criaria uma segunda
fonte de verdade sobre o que a mesa consumiu, que é justamente o que o
`client_uuid` ponta a ponta existe para evitar. Queda de Wi-Fi na loja tem
solução física; conta divergente, não.

Os arquivos ficam em disco, não embutidos em string no Python, porque HTML e
CSS dentro de literal Python perdem realce de sintaxe, formatação e diff
legível — e este é o pedaço do sistema que mais vai mudar.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

WEBAPP_DIR = Path(__file__).parent

#: Teto de tamanho do `index.html`. Não é defesa contra ataque — é o alarme de
#: que alguém embutiu um framework inteiro num arquivo que precisa carregar
#: pelo Wi-Fi da loja, no celular mais velho da equipe.
MAX_INDEX_BYTES = 512 * 1024


@lru_cache(maxsize=1)
def index_html() -> str:
    """O app inteiro numa página. Lido uma vez e guardado em memória.

    Ler do disco a cada requisição custaria I/O no mesmo processo que grava a
    venda — e o arquivo só muda quando o PDV é atualizado, o que reinicia o
    processo de qualquer jeito.
    """
    path = WEBAPP_DIR / "index.html"
    content = path.read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > MAX_INDEX_BYTES:  # pragma: no cover
        raise ValueError(
            f"{path.name} passou de {MAX_INDEX_BYTES // 1024} KiB — "
            "o celular do garçom carrega isso pelo Wi-Fi da loja."
        )
    return content


__all__ = ["MAX_INDEX_BYTES", "WEBAPP_DIR", "index_html"]
