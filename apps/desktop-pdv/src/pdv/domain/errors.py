"""Exceções de domínio.

Toda falha previsível vira uma exceção nomeada: a UI decide o que mostrar a
partir do *tipo*, nunca fazendo parse de string de erro.
"""

from __future__ import annotations


class PdvError(Exception):
    """Raiz de toda falha tratável do PDV."""


# --- Balança ---------------------------------------------------------------- #


class ScaleError(PdvError):
    """Falha de comunicação ou de protocolo com a balança."""


class ScaleNotConnectedError(ScaleError):
    """Porta serial indisponível (cabo solto, COM errada, driver ausente)."""


class ScaleTimeoutError(ScaleError):
    """A balança não respondeu dentro da janela esperada."""


class ScaleFrameError(ScaleError):
    """Quadro recebido não bate com o protocolo configurado."""


class UnstableWeightError(PdvError):
    """Tentativa de registrar venda com peso não estabilizado."""


# --- Impressora ------------------------------------------------------------- #


class PrinterError(PdvError):
    """Falha ao entregar o payload ESC/POS à impressora."""


class PrinterNotFoundError(PrinterError):
    """Impressora não encontrada pelo nome/VID:PID configurado."""


# --- Estoque e venda -------------------------------------------------------- #


class RecipeNotFoundError(PdvError):
    """Produto pesável sem ficha técnica: impossível dar baixa correta."""


class InsufficientStockError(PdvError):
    """Saldo projetado ficaria negativo e a política do tenant bloqueia."""

    def __init__(self, item_name: str, available_mg: int, required_mg: int) -> None:
        super().__init__(
            f"Estoque insuficiente de {item_name}: "
            f"disponível {available_mg / 1000:.3f} g, "
            f"necessário {required_mg / 1000:.3f} g"
        )
        self.item_name = item_name
        self.available_mg = available_mg
        self.required_mg = required_mg


class InsufficientPaymentError(PdvError):
    """Pagamento nao cobre o total da venda (ou troco em meio eletronico)."""

    def __init__(self, total_cents: int, paid_cents: int, detail: str | None = None) -> None:
        super().__init__(
            detail
            or (
                f"Pagamento insuficiente: total R$ {total_cents / 100:.2f}, "
                f"recebido R$ {paid_cents / 100:.2f} "
                f"(faltam R$ {(total_cents - paid_cents) / 100:.2f})"
            )
        )
        self.total_cents = total_cents
        self.paid_cents = paid_cents
        self.missing_cents = max(0, total_cents - paid_cents)


class InvalidWeightError(PdvError):
    """Peso zero, negativo ou acima do limite legal do equipamento."""


# --- Segurança -------------------------------------------------------------- #


class AuthorizationRequiredError(PdvError):
    """Operação exige credencial de gerente (desconto, cancelamento, gaveta)."""


class AuditChainError(PdvError):
    """Cadeia de auditoria quebrada: hash não confere ou há buraco no `seq`."""
