"""Testes dos diálogos do caixa.

Estes testes existem porque os dois diálogos carregam regra que o operador
encosta com o dedo, e um botão habilitado na hora errada custa dinheiro:

* o `PaymentDialog` **espelha** as travas de `CheckoutService._settle_payments`.
  O serviço continua sendo a autoridade — se o espelho sair de sincronia, a
  venda falha depois de o cliente já ter pago, na frente da fila.
* o `ManagerAuthDialog` só pode fechar com uma credencial que o
  `AuthorizationService` aceitou.

Rodam com `QT_QPA_PLATFORM=offscreen` (ver `conftest.py`): não abrem janela e
não exigem sessão gráfica, então valem em CI.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_MANAGER_LOGIN,
    DEMO_MANAGER_PIN,
    DEMO_OPERATOR_PIN,
    DEMO_OWNER_LOGIN,
    DEMO_OWNER_PIN,
    seed_demo_data,
)
from pdv.domain.models import Cents, PaymentMethod
from pdv.services.authorization import AuthorizationService

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialogButtonBox  # noqa: E402

from pdv.ui.dialogs import ManagerAuthDialog, PaymentDialog  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def auth(tmp_path: Path) -> AuthorizationService:
    config = AppConfig(
        tenant_id=TENANT,
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return AuthorizationService(database, TENANT)


def _ok(dialog: PaymentDialog):  # noqa: ANN202
    return dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)


# --------------------------------------------------------------------------- #
# Recebimento
# --------------------------------------------------------------------------- #


def test_no_payment_means_no_confirmation(qtbot) -> None:  # noqa: ANN001
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)

    assert not _ok(dialog).isEnabled()


def test_a_partial_payment_cannot_close_the_sale(qtbot) -> None:  # noqa: ANN001
    """Pedido que sai pela porta parcialmente pago só aparece na conciliação —
    quando já não há a quem cobrar."""
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)

    dialog._amount.setValue(10.00)
    dialog._add_payment()

    assert not _ok(dialog).isEnabled()
    assert "FALTA" in dialog._balance.text()


def test_cash_above_the_total_shows_change(qtbot) -> None:  # noqa: ANN001
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)

    dialog._amount.setValue(50.00)
    dialog._add_payment()

    assert _ok(dialog).isEnabled()
    assert "TROCO" in dialog._balance.text()
    assert "25,00" in dialog._balance.text()


def test_a_card_overpayment_is_blocked(qtbot) -> None:  # noqa: ANN001
    """Sobra em cartão não é troco: é valor digitado errado na maquininha.

    Devolver espécie contra um pagamento eletrônico é o golpe do troco, e é
    irreversível — o dinheiro já saiu da gaveta quando a conciliação percebe.
    """
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)

    dialog._method.setCurrentIndex(
        next(
            i
            for i in range(dialog._method.count())
            if dialog._method.itemData(i) is PaymentMethod.DEBIT
        )
    )
    dialog._amount.setValue(30.00)
    dialog._add_payment()

    assert not _ok(dialog).isEnabled()
    assert "não há troco" in dialog._balance.text()


def test_a_split_payment_adds_up(qtbot) -> None:  # noqa: ANN001
    """Mesa que divide a conta é rotina, não exceção."""
    dialog = PaymentDialog(Cents(5000))
    qtbot.addWidget(dialog)

    dialog._method.setCurrentIndex(
        next(
            i
            for i in range(dialog._method.count())
            if dialog._method.itemData(i) is PaymentMethod.PIX
        )
    )
    dialog._amount.setValue(30.00)
    dialog._add_payment()

    # O restante já vem preenchido: o operador não recalcula de cabeça.
    assert dialog._amount.value() == pytest.approx(20.00)
    dialog._method.setCurrentIndex(0)  # dinheiro
    dialog._add_payment()

    assert _ok(dialog).isEnabled()
    assert dialog._balance.text() == "Valor exato"
    assert [int(p.amount_cents) for p in dialog.payments] == [3000, 2000]


def test_removing_a_payment_reopens_the_shortfall(qtbot) -> None:  # noqa: ANN001
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)
    dialog._amount.setValue(25.00)
    dialog._add_payment()

    dialog._list.setCurrentRow(0)
    dialog._remove_selected()

    assert dialog.payments == ()
    assert not _ok(dialog).isEnabled()


def test_the_dialog_does_not_compute_change_itself(qtbot) -> None:  # noqa: ANN001
    """O troco gravado é o de `_settle_payments`.

    O diálogo entrega os valores informados com `change_cents` zerado; quem
    decide onde o troco entra é o serviço. Duas fontes para o mesmo número
    acabam divergindo — e a que diverge é sempre a que ninguém testa.
    """
    dialog = PaymentDialog(Cents(2500))
    qtbot.addWidget(dialog)
    dialog._amount.setValue(50.00)
    dialog._add_payment()

    assert [int(p.change_cents) for p in dialog.payments] == [0]


# --------------------------------------------------------------------------- #
# Autorização
# --------------------------------------------------------------------------- #


def test_the_dialog_only_closes_with_a_valid_credential(  # noqa: ANN001
    qtbot, auth: AuthorizationService
) -> None:
    dialog = ManagerAuthDialog(auth, operation="Cancelar item")
    qtbot.addWidget(dialog)
    dialog._login.setCurrentText(DEMO_MANAGER_LOGIN)
    dialog._pin.setText("0000")

    dialog._try_authorize()

    assert dialog.authorizer is None
    assert dialog.isVisible() is False  # nunca foi mostrado, mas tampouco aceito
    assert dialog.result() != int(ManagerAuthDialog.DialogCode.Accepted)
    assert dialog._error.text()
    assert dialog._pin.text() == "", "o PIN errado não pode ficar no campo"


def test_a_valid_credential_fills_the_authorizer(  # noqa: ANN001
    qtbot, auth: AuthorizationService
) -> None:
    dialog = ManagerAuthDialog(auth, operation="Cancelar item")
    qtbot.addWidget(dialog)
    dialog._login.setCurrentText(DEMO_MANAGER_LOGIN)
    dialog._pin.setText(DEMO_MANAGER_PIN)

    dialog._try_authorize()

    assert dialog.authorizer is not None
    assert dialog.authorizer.id == DEMO_MANAGER_ID


def test_the_cashier_cannot_authorize_from_the_dialog(  # noqa: ANN001
    qtbot, auth: AuthorizationService
) -> None:
    dialog = ManagerAuthDialog(auth, operation="Cancelar item")
    qtbot.addWidget(dialog)
    dialog._login.setCurrentText("ana")
    dialog._pin.setText(DEMO_OPERATOR_PIN)

    dialog._try_authorize()

    assert dialog.authorizer is None
    assert "permissão" in dialog._error.text()


def test_the_discount_ceiling_is_enforced_in_the_dialog(  # noqa: ANN001
    qtbot, auth: AuthorizationService
) -> None:
    dialog = ManagerAuthDialog(
        auth, operation="Desconto de 50%", percent=Decimal("50")
    )
    qtbot.addWidget(dialog)
    dialog._login.setCurrentText(DEMO_MANAGER_LOGIN)
    dialog._pin.setText(DEMO_MANAGER_PIN)

    dialog._try_authorize()

    assert dialog.authorizer is None
    assert "30" in dialog._error.text()


def test_only_authorizers_are_listed(qtbot, auth: AuthorizationService) -> None:  # noqa: ANN001
    dialog = ManagerAuthDialog(auth, operation="Cancelar item")
    qtbot.addWidget(dialog)

    logins = [dialog._login.itemText(i) for i in range(dialog._login.count())]

    assert logins == [DEMO_MANAGER_LOGIN, DEMO_OWNER_LOGIN]


def test_owner_only_dialog_does_not_offer_or_accept_manager(
    qtbot, auth: AuthorizationService
) -> None:  # noqa: ANN001
    dialog = ManagerAuthDialog(
        auth, operation="Usar nível Dono", allowed_roles=frozenset({"owner"})
    )
    qtbot.addWidget(dialog)
    assert [dialog._login.itemText(i) for i in range(dialog._login.count())] == [
        DEMO_OWNER_LOGIN
    ]

    dialog._login.setCurrentText(DEMO_MANAGER_LOGIN)
    dialog._pin.setText(DEMO_MANAGER_PIN)
    dialog._try_authorize()
    assert dialog.authorizer is None
    assert "proprietário" in dialog._error.text()

    dialog._login.setCurrentText(DEMO_OWNER_LOGIN)
    dialog._pin.setText(DEMO_OWNER_PIN)
    dialog._try_authorize()
    assert dialog.authorizer is not None
    assert dialog.authorizer.role == "owner"


def test_the_standard_buttons_are_in_portuguese(  # noqa: ANN001
    qtbot, auth: AuthorizationService
) -> None:
    """Sem tradutor do Qt carregado, o botão padrão sai "Cancel".

    "Cancel" no meio de um diálogo em português é o detalhe pequeno que faz o
    operador desconfiar do resto — e o resto é onde está o dinheiro dele.
    """
    payment = PaymentDialog(Cents(1000))
    qtbot.addWidget(payment)
    manager = ManagerAuthDialog(auth, operation="Cancelar item")
    qtbot.addWidget(manager)

    def texts(dialog):  # noqa: ANN001, ANN202
        box = dialog.findChildren(QDialogButtonBox)[0]
        return sorted(button.text() for button in box.buttons())

    assert texts(payment) == ["Cancelar", "Confirmar"]
    assert texts(manager) == ["Autorizar", "Cancelar"]
