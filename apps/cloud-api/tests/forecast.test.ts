import { describe, expect, it } from "vitest";

import {
  calendarFrom,
  chooseMethod,
  features,
  forecast,
  purchaseSuggestion,
  wape,
  type Series,
} from "../src/lib/forecast/model.ts";

/** `days` dias a partir de `first`, com o valor que `value` der para cada data. */
function series(first: string, days: number, value: (date: Date, index: number) => number | null): Series {
  const start = Date.parse(`${first}T12:00:00Z`);
  return Array.from({ length: days }, (_, index) => value(new Date(start + index * 86_400_000), index));
}

const FRIDAY = 5;
const MONDAY = 1;

describe("sazonal", () => {
  it("sexta que vende o triplo é prevista como sexta", () => {
    const first = "2026-05-04";
    const history = series(first, 84, (d) => (d.getUTCDay() === FRIDAY ? 30 : 10));

    const result = forecast(history, calendarFrom(first));

    const calendar = calendarFrom(first);
    const fridays = result.values.filter((_, i) => calendar.weekday(84 + i) === FRIDAY);
    expect(fridays).toEqual([30]);
    expect(result.values.filter((v) => v === 10)).toHaveLength(6);
  });

  it("o padrão perfeito fica com o sazonal: o boosting não ganha de erro zero", () => {
    const first = "2026-05-04";
    const history = series(first, 120, (d) => (d.getUTCDay() === FRIDAY ? 30 : 10));

    const result = forecast(history, calendarFrom(first));

    expect(result.method).toBe("sazonal");
    expect(result.backtest.sazonal).toBe(0);
  });

  it("segunda de loja fechada é prevista como zero, não como média", () => {
    const first = "2026-05-04";
    const history = series(first, 84, (d) => (d.getUTCDay() === MONDAY ? null : 12));
    const calendar = calendarFrom(first);

    const result = forecast(history, calendar);

    result.values.forEach((value, i) => {
      expect(value).toBe(calendar.weekday(84 + i) === MONDAY ? 0 : 12);
    });
  });

  it("um feriado fechado não derruba a semana seguinte", () => {
    const first = "2026-05-04";
    const history = [...series(first, 84, () => 20)];
    history[80] = null; // feriado

    const result = forecast(history, calendarFrom(first));

    expect(result.values).toEqual(Array(7).fill(20));
  });
});

describe("boosting", () => {
  // Movimento dobra do dia 5 ao 10: o salário. O dia da semana não enxerga isso.
  const payday = (d: Date) => {
    const base = d.getUTCDay() === FRIDAY ? 30 : 12;
    return d.getUTCDate() >= 5 && d.getUTCDate() <= 10 ? base * 2 : base;
  };

  it("entra quando ganha do sazonal no passado da própria loja", () => {
    // Termina no dia 12: as duas semanas de teste cobrem o começo do mês.
    const first = "2026-02-13";
    const history = series(first, 210, payday);

    const result = forecast(history, calendarFrom(first));

    expect(result.method).toBe("boosting");
    expect(result.backtest.boosting!).toBeLessThan(result.backtest.sazonal! * 0.95);
  });

  it("prevê o começo do mês acima do resto", () => {
    const first = "2026-01-01";
    const history = series(first, 213, payday); // termina em 1º de agosto
    const calendar = calendarFrom(first);

    const result = forecast(history, calendar);

    const days = result.values.map((value, i) => ({ value, day: calendar.dayOfMonth(213 + i), wd: calendar.weekday(213 + i) }));
    const inPayday = days.filter((d) => d.day >= 5 && d.day <= 10 && d.wd !== FRIDAY);
    const outside = days.filter((d) => (d.day < 5 || d.day > 10) && d.wd !== FRIDAY);
    expect(inPayday.length).toBeGreaterThan(0);
    expect(outside.length).toBeGreaterThan(0);
    const mean = (xs: { value: number }[]) => xs.reduce((a, b) => a + b.value, 0) / xs.length;
    expect(mean(inPayday)).toBeGreaterThan(mean(outside) * 1.4);
  });

  it("três semanas de história não têm dado para boosting", () => {
    const first = "2026-05-04";
    const history = series(first, 21, (_, i) => 10 + (i % 3));

    const result = forecast(history, calendarFrom(first));

    expect(result.method).toBe("sazonal");
    expect(result.backtest.boosting).toBeNull();
  });

  it("seis semanas ainda não bastam: treinaria com 14 dias", () => {
    const first = "2026-05-04";
    const history = series(first, 42, (d, i) => (d.getUTCDay() === FRIDAY ? 30 : 10) + (i % 4));

    const result = forecast(history, calendarFrom(first));

    expect(result.backtest.boosting).toBeNull();
    expect(result.method).toBe("sazonal");
  });

  it("a escolha exige 5% de ganho; empate e ganho pequeno ficam com o sazonal", () => {
    expect(chooseMethod(0.3, 0.28)).toBe("boosting");
    expect(chooseMethod(0.3, 0.29)).toBe("sazonal");
    expect(chooseMethod(0.3, 0.3)).toBe("sazonal");
    expect(chooseMethod(0.3, 0.4)).toBe("sazonal");
    expect(chooseMethod(0.3, null)).toBe("sazonal");
    expect(chooseMethod(null, 0.1)).toBe("sazonal");
  });

  it("os atributos nunca olham para os 6 dias antes do previsto", () => {
    const first = "2026-05-04";
    const history = [...series(first, 60, () => 10)];
    for (let i = 54; i < 60; i += 1) history[i] = Number.NaN; // "futuro" envenenado

    const row = features(history, 60, calendarFrom(first));

    expect(row).not.toBeNull();
    expect(row!.every(Number.isFinite)).toBe(true);
  });

  it("é determinístico: a mesma loja vê sempre a mesma previsão", () => {
    const first = "2026-02-13";
    const history = series(first, 210, payday);
    const calendar = calendarFrom(first);

    expect(forecast(history, calendar)).toEqual(forecast(history, calendar));
  });
});

it("sem nenhuma venda não inventa previsão", () => {
  const result = forecast(Array(60).fill(null), calendarFrom("2026-05-04"));

  expect(result.method).toBe("sem-historico");
  expect(result.values).toEqual(Array(7).fill(0));
});

it("previsão nunca é negativa", () => {
  const first = "2026-02-13";
  const history = series(first, 150, (_, i) => (i % 11 === 0 ? 40 : 0));

  expect(forecast(history, calendarFrom(first)).values.every((v) => v >= 0)).toBe(true);
});

describe("wape", () => {
  it("é a soma dos erros sobre a soma do vendido", () => {
    expect(wape([10, 10], [8, 13])).toBeCloseTo(0.25);
  });

  it("sem venda no período não há erro para medir", () => {
    expect(wape([0, 0], [1, 2])).toBeNull();
  });
});

describe("sugestão de compra", () => {
  it("compra o que falta para a semana mais a margem do erro", () => {
    const result = purchaseSuggestion({ forecast: Array(7).fill(1000), errorSd: 100, balance: 2000 });

    expect(result.need).toBe(7000);
    expect(result.safety).toBeCloseTo(1.28 * 100 * Math.sqrt(7));
    expect(result.buy).toBeCloseTo(7000 + 1.28 * 100 * Math.sqrt(7) - 2000);
    expect(result.coverageDays).toBeCloseTo(2);
  });

  it("estoque que cobre a semana não pede compra", () => {
    const result = purchaseSuggestion({ forecast: Array(7).fill(100), errorSd: 10, balance: 5000 });

    expect(result.buy).toBe(0);
  });

  it("saldo desconhecido não vira sugestão — compraria o que já está na prateleira", () => {
    const result = purchaseSuggestion({ forecast: Array(7).fill(100), errorSd: 10, balance: null });

    expect(result.buy).toBeNull();
    expect(result.coverageDays).toBeNull();
  });

  it("previsão mais incerta pede mais margem", () => {
    const calm = purchaseSuggestion({ forecast: Array(7).fill(100), errorSd: 5, balance: 0 });
    const wild = purchaseSuggestion({ forecast: Array(7).fill(100), errorSd: 50, balance: 0 });

    expect(wild.buy!).toBeGreaterThan(calm.buy!);
  });
});

describe("contagem digitada", () => {
  it("vírgula e ponto valem igual — 12,5 kg não vira 12 kg", async () => {
    const { parseKg } = await import("../src/lib/forecast/input.ts");

    expect(parseKg("12,5")).toBe(12.5);
    expect(parseKg("12.5")).toBe(12.5);
    expect(parseKg(" 3 ")).toBe(3);
    expect(parseKg("0")).toBe(0);
  });

  it("o que não é número positivo é recusado, não arredondado", async () => {
    const { parseKg } = await import("../src/lib/forecast/input.ts");

    for (const text of ["", "abc", "-1", "1,2,3", "12kg", "1e3", "200000"]) {
      expect({ text, value: parseKg(text) }).toEqual({ text, value: null });
    }
  });
});
