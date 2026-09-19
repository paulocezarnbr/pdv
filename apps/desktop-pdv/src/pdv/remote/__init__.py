"""Canal de comandos do painel administrativo para o terminal.

O oposto de `pdv.sync`, que só empurra dados para fora. Aqui o terminal
*recebe* ordens — e por isso todo o módulo é escrito em torno de recusar, não
de obedecer. Ver `commands.py` para as travas e o porquê de cada uma.
"""

from pdv.remote.commands import (
    CHANNEL,
    REMOTE_ENABLED_KEY,
    ApplyReport,
    CommandRefused,
    RemoteCommandService,
)
from pdv.remote.inbox import CommandResult, InboxRepository
from pdv.remote.protocol import (
    CommandKind,
    CommandStatus,
    RemoteCommand,
    sign_command,
    verify_signature,
)

__all__ = [
    "CHANNEL",
    "REMOTE_ENABLED_KEY",
    "ApplyReport",
    "CommandKind",
    "CommandRefused",
    "CommandResult",
    "CommandStatus",
    "InboxRepository",
    "RemoteCommand",
    "RemoteCommandService",
    "sign_command",
    "verify_signature",
]
