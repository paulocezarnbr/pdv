"""Contrato da balança e do preço por peso — o que o C# tem de ler e cobrar igual.

O quadro cru da balança é a prova pericial do item pesado: vai para
`order_items.scale_reading_raw` e para o evento `weight_captured`. Se o C# ler
o mesmo quadro com outro peso (ou outro status), cobra o cliente errado; se
guardar o quadro com outra grafia, a prova deixa de bater com o que o Python
gravou para o mesmo equipamento.

Os pontos que divergem em silêncio entre linguagens, e por isso estão aqui:

* o `strip()` do Python também tira os separadores ASCII 0x1C–0x1F;
* o quadro cru é gravado com `backslashreplace`: byte fora do ASCII vira
  ``\\xNN``, não `?` nem U+FFFD;
* a Toledo de 6 dígitos usa os 5 **finais**; a Filizola com campos extras usa
  os 5 **iniciais**;
* em streaming vale o **último** quadro completo do buffer, e o buffer que
  sobra é truncado em 4096 bytes;
* o preço por peso arredonda `ROUND_HALF_UP` uma vez, no fim; o desconto do
  caixa usa o `quantize` padrão do `Decimal`, que é `ROUND_HALF_EVEN`.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_scale_contract.py``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from pdv.domain.errors import InvalidWeightError, ScaleFrameError
from pdv.domain.models import Cents, Grams
from pdv.hardware.scale.protocols import build_protocol
from pdv.hardware.scale.serial_scale import _extract_last_frame
from pdv.services.pricing import net_weight, price_for_weight

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "scale-weighing.json"

STX, ETX = b"\x02", b"\x03"

FRAMES: dict[str, list[bytes]] = {
    "toledo_prix3": [
        b"01234", b"00000", b"30000", b"001234", b"101234", b" 00847\r\n", b"\x1c00847\x1f",
        b"IIIII", b"SSSSS", b"NNNNN", b"0i234", b"00s00", b"0n000",
        b"", b"   ", b"1234", b"12a45", b"\xff00847", b"00847\x80",
    ],
    "filizola": [
        b"00847", b"00000", b"008470010004990042267", b"00847 T00010", b"  01500 ",
        b"I0847", b"0S847", b"-0847", b"0084i",
        b"", b"0084", b"abcde", b"\xfe01000",
    ],
    "urano": [
        b"00847", b"000847", b"100847", b"200847", b"300847", b"400847", b"00000",
        b"0", b"1", b"0084", b"008470", b"0x847", b"", b"\xc300847",
    ],
}

BUFFERS: list[bytes] = [
    STX + b"00847" + ETX,
    STX + b"00100" + ETX + STX + b"00847" + ETX,
    STX + b"00100" + ETX + STX + b"00847" + ETX + STX + b"009",
    b"lixo" + STX + b"00500" + ETX + b"resto",
    STX + b"00123",
    b"sem delimitador",
    b"",
    ETX + STX + b"00200" + ETX,
    STX + STX + b"00300" + ETX,
    STX + ETX,
    b"x" * 5000,
    STX + b"00001" + ETX + b"y" * 4200,
]

WEIGHTS: list[tuple[int, int, int]] = [
    # (preço por kg em centavos, bruto em g, tara em g)
    (4990, 847, 0), (4990, 1000, 153), (8990, 1, 0), (1, 500, 0), (3, 500, 0),
    (5, 100, 0), (15, 100, 0), (1999, 333, 33), (4990, 150, 150), (4990, 100, 101),
    (4990, -1, 0), (4990, 100, -1), (0, 100, 0),
]

DISCOUNTS: list[tuple[int, str]] = [
    # (subtotal em centavos, percentual) — a expressão do CheckoutService.apply_discount
    (1000, "10"), (4227, "10"), (4225, "10"), (4235, "10"), (999, "33.33"), (1, "50"),
    (3, "50"), (12345, "12.5"), (8990, "100"), (8990, "0"), (7777, "7.77"),
]


def _reading(protocol: str, frame: bytes) -> dict:
    try:
        reading = build_protocol(protocol).parse(frame)
    except ScaleFrameError:
        return {"frame_hex": frame.hex(), "error": True}
    return {
        "frame_hex": frame.hex(),
        "status": reading.status.value,
        "weight_grams": int(reading.weight_grams),
        "raw_frame": reading.raw_frame,
    }


def _extract(buffer: bytes) -> dict:
    frame, remaining = _extract_last_frame(bytearray(buffer), STX[0], ETX[0])
    return {
        "buffer_hex": buffer.hex(),
        "frame_hex": None if frame is None else frame.hex(),
        "remaining_hex": bytes(remaining).hex(),
    }


def _weight(price: int, gross: int, tare: int) -> dict:
    case: dict[str, object] = {"price_cents_per_kg": price, "gross_grams": gross, "tare_grams": tare}
    try:
        net = net_weight(Grams(gross), Grams(tare))
        case["net_grams"] = int(net)
        case["total_cents"] = int(price_for_weight(Cents(price), net))
    except InvalidWeightError:
        case["error"] = True
    return case


def _build() -> dict:
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_scale_contract.py. Não edite à mão.",
        "frames": {name: [_reading(name, frame) for frame in frames] for name, frames in FRAMES.items()},
        "buffers": [_extract(buffer) for buffer in BUFFERS],
        "weights": [_weight(*spec) for spec in WEIGHTS],
        "discounts": [
            {"subtotal_cents": subtotal, "percent": percent,
             "discount_cents": int((Decimal(subtotal) * Decimal(percent) / Decimal(100)).quantize(Decimal("1")))}
            for subtotal, percent in DISCOUNTS
        ],
    }


def test_the_scale_contract_matches_the_implementation() -> None:
    built = _build()
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == built, (
        "a leitura da balança ou o preço por peso mudou: regere o contrato e alinhe o C# (apps/pdv-net)"
    )


def test_the_contract_really_exercises_the_traps() -> None:
    built = _build()
    toledo = {case["frame_hex"]: case for case in built["frames"]["toledo_prix3"]}
    assert toledo[b"101234".hex()]["weight_grams"] == 1234  # 5 finais
    assert toledo[b"\x1c00847\x1f".hex()]["weight_grams"] == 847  # strip do Python
    assert toledo[b"\xff00847".hex()]["raw_frame"] == "\\xff00847"  # backslashreplace
    filizola = {case["frame_hex"]: case for case in built["frames"]["filizola"]}
    assert filizola[b"008470010004990042267".hex()]["weight_grams"] == 847  # 5 iniciais
    weights = {(w["price_cents_per_kg"], w["gross_grams"], w["tare_grams"]): w for w in built["weights"]}
    assert weights[(4990, 847, 0)]["total_cents"] == 4227  # 4226,53
    assert weights[(1, 500, 0)]["total_cents"] == 1  # 0,5 sobe (HALF_UP)
    discounts = {(d["subtotal_cents"], d["percent"]): d["discount_cents"] for d in built["discounts"]}
    assert discounts[(4225, "10")] == 422 and discounts[(4235, "10")] == 424  # meio para o par
    assert any(len(b["buffer_hex"]) > 2 * 4096 and len(b["remaining_hex"]) == 2 * 4096 for b in built["buffers"])
