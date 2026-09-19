"""Diálogos do caixa: autorização de gerente e recebimento.

Dois diálogos, dois problemas distintos:

* **`ManagerAuthDialog`** é o controle antifurto. Ele não pode ser fachada: um
  diálogo que pede senha e não valida nada é pior do que não pedir, porque
  produz no relatório a *aparência* de uma autorização que nunca houve.
* **`PaymentDialog`** é onde o dinheiro entra. Ele exibe, mas não decide: o
  troco mostrado vem de `CheckoutService._settle_payments`, a mesma função que
  grava. Recalcular aqui criaria duas verdades sobre o mesmo valor.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdv.domain.errors import PdvError
from pdv.domain.models import Cents, Payment, PaymentMethod
from pdv.hardware.printer.escpos import format_cents
from pdv.services.authorization import Authorizer, AuthorizationService
from pdv.ui import theme

#: Rótulos em português para a forma de pagamento. Só os meios que o balcão
#: opera hoje: `prepaid`, `credit_account` e `cashback` dependem de cadastro de
#: cliente, que é módulo de outra fase.
_METHOD_LABELS: tuple[tuple[PaymentMethod, str], ...] = (
    (PaymentMethod.CASH, "Dinheiro"),
    (PaymentMethod.DEBIT, "Cartão de débito"),
    (PaymentMethod.CREDIT, "Cartão de crédito"),
    (PaymentMethod.PIX, "PIX"),
)


class ManagerAuthDialog(QDialog):
    """Pede login e PIN de quem autoriza, e valida antes de fechar.

    O diálogo não fecha com credencial inválida: fechar e deixar o chamador
    descobrir depois espalharia a decisão de "autorizado ou não" por dois
    lugares. Quem sai daqui com `accept()` tem `authorizer` preenchido.
    """

    def __init__(
        self,
        service: AuthorizationService,
        *,
        operation: str,
        percent: Decimal | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._percent = percent
        self.authorizer: Authorizer | None = None

        self.setWindowTitle("Autorização de gerente")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_5, theme.SPACE_4, theme.SPACE_5, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        headline = QLabel(operation)
        headline.setFont(theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_SEMIBOLD))
        headline.setWordWrap(True)
        layout.addWidget(headline)

        form = QFormLayout()
        self._login = QComboBox()
        self._login.setEditable(True)
        self._login.addItems(service.list_authorizers())
        self._login.setMinimumHeight(34)
        form.addRow("Login:", self._login)

        self._pin = QLineEdit()
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin.setMaxLength(12)
        self._pin.setMinimumHeight(34)
        self._pin.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM, mono=True, tracking=4.0)
        )
        form.addRow("PIN:", self._pin)
        layout.addLayout(form)

        self._error = QLabel("")
        self._error.setObjectName("error")
        self._error.setFont(theme.font(theme.SIZE_BODY))
        self._error.setWordWrap(True)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setObjectName("primary")
        ok.setText("Autorizar")
        ok.setMinimumHeight(40)
        # Sem tradutor do Qt carregado, os botões padrão saem em inglês —
        # "Cancel" no meio de um diálogo em português é o tipo de detalhe que
        # faz o operador desconfiar do resto do sistema.
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Cancelar")
        cancel.setMinimumHeight(40)
        buttons.accepted.connect(self._try_authorize)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._pin.returnPressed.connect(self._try_authorize)
        self._pin.setFocus()

    def _try_authorize(self) -> None:
        login = self._login.currentText().strip()
        pin = self._pin.text()
        if not login or not pin:
            self._error.setText("Informe login e PIN.")
            return

        try:
            if self._percent is None:
                self.authorizer = self._service.authorize(login, pin)
            else:
                self.authorizer = self._service.authorize_discount(
                    login, pin, self._percent
                )
        except PdvError as exc:
            # A mensagem do serviço já é deliberadamente vaga para credencial
            # errada e específica para limite estourado. Repassar sem enfeitar.
            self._error.setText(str(exc))
            self._pin.clear()
            self._pin.setFocus()
            return

        self.accept()

    @classmethod
    def ask(
        cls,
        service: AuthorizationService,
        *,
        operation: str,
        percent: Decimal | None = None,
        parent: QWidget | None = None,
    ) -> Authorizer | None:
        """Abre o diálogo e devolve quem autorizou, ou `None` se desistiu."""
        dialog = cls(service, operation=operation, percent=percent, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.authorizer


class PaymentDialog(QDialog):
    """Recebimento com múltiplas formas de pagamento e troco.

    Aceita pagamento dividido porque a mesa que divide a conta é rotina, não
    exceção. As duas travas de `_settle_payments` continuam valendo e são
    espelhadas aqui apenas como *aviso*: falta de valor e troco em cartão.
    """

    def __init__(self, total_cents: Cents, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._total = int(total_cents)
        self._payments: list[Payment] = []

        self.setWindowTitle("Recebimento")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_5, theme.SPACE_4, theme.SPACE_5, theme.SPACE_4
        )
        layout.setSpacing(theme.SPACE_3)

        total_label = QLabel(f"TOTAL: R$ {format_cents(Cents(self._total))}")
        total_label.setFont(
            theme.font(theme.SIZE_TOTAL, theme.WEIGHT_BOLD, display=True, tracking=-1.0)
        )
        total_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(total_label)

        entry = QHBoxLayout()
        entry.setSpacing(theme.SPACE_2)
        self._method = QComboBox()
        for method, label in _METHOD_LABELS:
            self._method.addItem(label, method)
        self._method.setMinimumHeight(38)
        entry.addWidget(self._method, stretch=3)

        self._amount = QDoubleSpinBox()
        self._amount.setPrefix("R$ ")
        self._amount.setDecimals(2)
        self._amount.setMaximum(999_999.99)
        self._amount.setMinimumHeight(38)
        self._amount.setFont(
            theme.font(theme.SIZE_TITLE, theme.WEIGHT_MEDIUM, mono=True)
        )
        self._amount.setValue(self._total / 100)
        entry.addWidget(self._amount, stretch=2)

        add = QPushButton("Adicionar")
        add.setMinimumHeight(38)
        add.setFont(theme.font(theme.SIZE_BODY, theme.WEIGHT_MEDIUM))
        add.clicked.connect(self._add_payment)
        entry.addWidget(add, stretch=1)
        layout.addLayout(entry)

        self._list = QListWidget()
        self._list.setMaximumHeight(140)
        self._list.setFont(theme.font(theme.SIZE_BODY, mono=True))
        layout.addWidget(self._list)

        # Ação terciária: existe para corrigir um engano, não para ser vista.
        # Do tamanho do "Confirmar" ela disputaria o olho com o botão que
        # fecha a venda.
        remove = QPushButton("Remover selecionado")
        remove.setFont(theme.font(theme.SIZE_MICRO))
        remove.setFixedHeight(28)
        remove.clicked.connect(self._remove_selected)
        remove_row = QHBoxLayout()
        remove_row.addStretch()
        remove_row.addWidget(remove)
        layout.addLayout(remove_row)

        self._balance = QLabel("")
        self._balance.setFont(theme.font(theme.SIZE_TITLE, theme.WEIGHT_SEMIBOLD))
        self._balance.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._balance)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        confirm = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setObjectName("primary")
        confirm.setText("Confirmar")
        confirm.setMinimumHeight(44)
        confirm.setFont(theme.font(theme.SIZE_BODY_LG, theme.WEIGHT_SEMIBOLD))
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Cancelar")
        cancel.setMinimumHeight(44)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._amount.setFocus()
        self._amount.selectAll()
        self._refresh()

    # -- estado ---------------------------------------------------------------- #

    @property
    def payments(self) -> tuple[Payment, ...]:
        """As formas informadas. O troco é calculado pelo serviço, não aqui."""
        return tuple(self._payments)

    def _paid(self) -> int:
        return sum(int(p.amount_cents) for p in self._payments)

    def _add_payment(self) -> None:
        cents = int(round(self._amount.value() * 100))
        if cents <= 0:
            return
        method: PaymentMethod = self._method.currentData()
        self._payments.append(Payment(method=method, amount_cents=Cents(cents)))

        label = dict(_METHOD_LABELS)[method]
        self._list.addItem(QListWidgetItem(f"{label} — R$ {format_cents(Cents(cents))}"))

        remaining = max(0, self._total - self._paid())
        self._amount.setValue(remaining / 100)
        self._amount.setFocus()
        self._amount.selectAll()
        self._refresh()

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        self._payments.pop(row)
        self._list.takeItem(row)
        self._refresh()

    def _refresh(self) -> None:
        paid = self._paid()
        difference = paid - self._total
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)

        if not self._payments:
            self._balance.setText("Informe ao menos uma forma de pagamento")
            self._balance.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            ok.setEnabled(False)
            return

        if difference < 0:
            self._balance.setText(f"FALTA: R$ {format_cents(Cents(-difference))}")
            self._balance.setStyleSheet(f"color: {theme.DANGER};")
            ok.setEnabled(False)
            return

        if difference == 0:
            self._balance.setText("Valor exato")
            self._balance.setStyleSheet(f"color: {theme.OK};")
            ok.setEnabled(True)
            return

        # Sobra só vira troco se houver dinheiro em espécie. Em cartão ou PIX
        # significa valor digitado errado na maquininha — e devolver espécie
        # contra pagamento eletrônico é exatamente o golpe do troco.
        if any(p.method.opens_drawer for p in self._payments):
            self._balance.setText(f"TROCO: R$ {format_cents(Cents(difference))}")
            self._balance.setStyleSheet(f"color: {theme.OK};")
            ok.setEnabled(True)
        else:
            self._balance.setText(
                f"Excesso de R$ {format_cents(Cents(difference))} sem espécie — "
                "não há troco para cartão ou PIX"
            )
            self._balance.setStyleSheet(f"color: {theme.DANGER};")
            ok.setEnabled(False)


__all__ = ["ManagerAuthDialog", "PaymentDialog"]
