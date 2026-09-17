"""Configuração comum da suíte.

Duas coisas acontecem aqui, ambas antes de qualquer import de teste:

1. `src/` entra no `sys.path`. O projeto usa layout `src/` sem instalação
   editável, então sem isto `import pdv` só funciona com `PYTHONPATH` setado à
   mão — e um `pytest` digitado direto falharia por motivo nenhum.
2. O Qt roda **offscreen**. Os testes de diálogo não podem depender de sessão
   gráfica: numa build de CI não existe uma, e abrir janela de verdade
   transforma teste em processo travado esperando alguém clicar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# `setdefault`: quem quiser ver a janela roda com QT_QPA_PLATFORM=windows.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
