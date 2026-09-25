"""Log que continua funcionando na pasta append-only da instalação.

O `harden.ps1` deixa `ProgramData\\ERPFood\\PDV\\logs` só com "acrescentar"
para o grupo Usuários: o operador não consegue apagar nem reescrever o rastro.
Só que o `open(..., "a")` do Python pede ao Windows escrita **completa**
(`GENERIC_WRITE`, que inclui sobrescrever), e o Windows recusa. O PDV
instalado ficava sem log nenhum — e a primeira falha depois do login não
deixava rastro em lugar nenhum.

Aqui o arquivo é aberto pedindo exatamente o que a ACL concede:
`FILE_APPEND_DATA`. O Windows garante que cada gravação cai no fim do arquivo,
seja qual for a posição do ponteiro — o próprio handle não consegue apagar
nada.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TextIO

_FILE_APPEND_DATA = 0x0004
_FILE_READ_ATTRIBUTES = 0x0080  # o `fstat` do Python precisa dele
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80


def open_append_only(path: Path | str, encoding: str = "utf-8") -> TextIO:
    """Abre `path` para acrescentar, com o mínimo de permissão possível.

    Fora do Windows, ou quando a ACL permite escrita completa, é o `open`
    comum. `PermissionError` só sai daqui se nem acrescentar for permitido.
    """
    try:
        return open(path, "a", encoding=encoding)  # noqa: SIM115 - quem fecha é o chamador
    except PermissionError:
        if os.name != "nt":
            raise
    return _open_append_handle(Path(path), encoding)


def _open_append_handle(path: Path, encoding: str) -> TextIO:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.restype = wintypes.HANDLE
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]

    handle = create_file(
        str(path),
        _FILE_APPEND_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_ALWAYS,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise PermissionError(error, ctypes.FormatError(error), str(path))

    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_WRONLY)
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return open(descriptor, "a", encoding=encoding, closefd=True)  # noqa: SIM115


class AppendOnlyFileHandler(logging.FileHandler):
    """`FileHandler` que abre com `open_append_only`."""

    def __init__(self, filename: Path | str, encoding: str = "utf-8") -> None:
        super().__init__(filename, mode="a", encoding=encoding, delay=True)
        # Abre já, e não na primeira mensagem: quem configura o log precisa
        # saber agora se esta pasta serve, para tentar a próxima.
        self.stream = self._open()

    def _open(self) -> TextIO:
        return open_append_only(self.baseFilename, self.encoding or "utf-8")
