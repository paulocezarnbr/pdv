/**
 * Previsão de demanda por dia: baseline sazonal e gradient boosting.
 *
 * A regra que decide tudo: **o modelo complexo só entra quando ganha do
 * simples no passado da própria loja.** Cada série é testada nas duas últimas
 * semanas que ela já viveu (walk-forward, sem olhar o futuro), e o boosting só
 * é usado se o erro dele for pelo menos 5% menor que o do sazonal. Uma loja
 * com três semanas de história não tem dado para boosting nenhum, e um modelo
 * que decorou três semanas erra mais que "a média das últimas terças".
 *
 * * **Sazonal**: média do mesmo dia da semana nas últimas 4 semanas em que a
 *   loja abriu. É o que o dono já faz de cabeça ("terça vende pouco"), e por
 *   isso é o piso que qualquer coisa mais sofisticada precisa bater.
 * * **Boosting**: árvores de regressão rasas sobre o RESÍDUO do sazonal, com
 *   atributos que só usam dado de 7 ou mais dias antes do dia previsto — é o
 *   que permite prever a semana inteira de uma vez, sem realimentar previsão
 *   como se fosse venda.
 *
 * Dia em que a loja não abriu não é zero de venda: é ausência. Ele sai do
 * treino e das médias (`null` na série), senão um feriado derrubaria a
 * previsão de toda a semana seguinte. E o dia da semana em que a loja
 * costuma ficar fechada é previsto como zero.
 *
 * Determinístico de propósito (sem amostragem aleatória): a mesma loja com os
 * mesmos dados mostra sempre a mesma previsão, e uma sugestão de compra que
 * muda a cada recarga da página não é levada a sério por ninguém.
 */

/** Um valor por dia, em ordem; `null` = a loja não abriu naquele dia. */
export type Series = readonly (number | null)[];

export type Method = "sazonal" | "boosting" | "sem-historico";

export interface ForecastResult {
  method: Method;
  /** Previsão para cada dia do horizonte, na ordem. */
  values: number[];
  /** Erro no passado (WAPE: soma dos erros / soma do vendido), por método. */
  backtest: { sazonal: number | null; boosting: number | null; days: number };
  /** Desvio dos erros do método escolhido, por dia — base do estoque de segurança. */
  errorSd: number;
}

/** O calendário da série: que dia da semana e do mês é cada posição. */
export interface Calendar {
  weekday(index: number): number;
  dayOfMonth(index: number): number;
}

/** Calendário a partir da data (YYYY-MM-DD) da posição 0. */
export function calendarFrom(firstDay: string): Calendar {
  const start = Date.parse(`${firstDay}T12:00:00Z`);
  const at = (index: number) => new Date(start + index * 86_400_000);
  return {
    weekday: (index) => at(index).getUTCDay(),
    dayOfMonth: (index) => at(index).getUTCDate(),
  };
}

export const HORIZON = 7;
const SEASON = 7;
const SEASON_WEEKS = 4;
/** Boosting precisa de pelo menos isso de dias abertos com atributos completos. */
const MIN_TRAIN_ROWS = 35;
/** Ganho mínimo sobre o sazonal para o boosting ser usado. */
const REQUIRED_GAIN = 0.95;

// --------------------------------------------------------------------------- //
// Sazonal
// --------------------------------------------------------------------------- //

/**
 * Média do mesmo dia da semana nas últimas semanas, olhando só até `known`
 * (exclusive). `null` quando não há nenhuma observação daquele dia da semana.
 */
export function seasonalAt(series: Series, target: number, known = target): number | null {
  const values: number[] = [];
  for (let day = target - SEASON; day >= 0 && values.length < SEASON_WEEKS; day -= SEASON) {
    if (day >= known) continue;
    const value = series[day];
    if (value !== null && value !== undefined) values.push(value);
  }
  if (values.length === 0) return null;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

/** A loja costuma ficar fechada neste dia da semana (3 das últimas 4 semanas)? */
export function usuallyClosed(series: Series, target: number, known = target): boolean {
  let closed = 0;
  let seen = 0;
  for (let day = target - SEASON; day >= 0 && seen < SEASON_WEEKS; day -= SEASON) {
    if (day >= known) continue;
    seen += 1;
    if (series[day] === null) closed += 1;
  }
  return seen >= 3 && closed >= 3;
}

function recentMean(series: Series, known: number, window = 14): number | null {
  const values = series.slice(Math.max(0, known - window), known).filter((v): v is number => v !== null);
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

function baselineAt(series: Series, target: number, known: number): number {
  if (usuallyClosed(series, target, known)) return 0;
  return seasonalAt(series, target, known) ?? recentMean(series, known) ?? 0;
}

// --------------------------------------------------------------------------- //
// Boosting
// --------------------------------------------------------------------------- //

/**
 * Atributos do dia `target`, usando só o que era conhecido 7 dias antes.
 *
 * `dayOfMonth` está aqui pelo quinto dia útil: no Brasil o salário cai no
 * começo do mês e o movimento de restaurante acompanha.
 */
export function features(series: Series, target: number, calendar: Calendar): number[] | null {
  const known = target - SEASON + 1; // dados até target-7, inclusive
  const lag7 = series[target - 7];
  const lag14 = series[target - 14];
  if (lag7 === null || lag7 === undefined || lag14 === null || lag14 === undefined) return null;
  const week = series.slice(Math.max(0, target - 13), target - 6).filter((v): v is number => v !== null);
  return [
    baselineAt(series, target, known),
    lag7,
    lag14,
    week.length ? week.reduce((a, b) => a + b, 0) / week.length : lag7,
    calendar.weekday(target),
    calendar.dayOfMonth(target),
    target,
  ];
}

interface Node {
  feature: number;
  threshold: number;
  left: Node | number;
  right: Node | number;
}

/**
 * Árvore de regressão por mínimos quadrados.
 *
 * `orders[f]` é a ordem das linhas pelo atributo `f`, calculada UMA vez para o
 * treino inteiro: as linhas não mudam entre as rodadas do boosting, só o
 * resíduo. Ordenar de novo a cada nó custava 100 ms por série — o painel de
 * um cardápio de 50 itens levaria segundos para abrir.
 */
function fitTree(
  rows: readonly number[][],
  targets: readonly number[],
  orders: readonly Int32Array[],
  members: Uint8Array,
  count: number,
  depth: number,
  minLeaf: number,
): Node | number {
  let total = 0;
  let totalSq = 0;
  for (let i = 0; i < members.length; i += 1) {
    if (!members[i]) continue;
    total += targets[i]!;
    totalSq += targets[i]! ** 2;
  }
  const mean = total / count;
  if (depth === 0 || count < minLeaf * 2) return mean;
  const baseSse = totalSq - total ** 2 / count;

  let best: { gain: number; feature: number; threshold: number } | null = null;
  for (let feature = 0; feature < orders.length; feature += 1) {
    const order = orders[feature]!;
    let leftSum = 0;
    let leftSq = 0;
    let leftCount = 0;
    let previous = -1;
    for (let k = 0; k < order.length; k += 1) {
      const index = order[k]!;
      if (!members[index]) continue;
      if (previous >= 0 && leftCount >= minLeaf && count - leftCount >= minLeaf) {
        const x0 = rows[previous]![feature]!;
        const x1 = rows[index]![feature]!;
        if (x0 !== x1) {
          const rightSum = total - leftSum;
          const sse =
            leftSq - leftSum ** 2 / leftCount +
            (totalSq - leftSq) - rightSum ** 2 / (count - leftCount);
          const gain = baseSse - sse;
          if (gain > 1e-9 && (!best || gain > best.gain)) {
            best = { gain, feature, threshold: (x0 + x1) / 2 };
          }
        }
      }
      leftSum += targets[index]!;
      leftSq += targets[index]! ** 2;
      leftCount += 1;
      previous = index;
    }
  }
  if (!best) return mean;

  const left = new Uint8Array(members.length);
  const right = new Uint8Array(members.length);
  let leftCount = 0;
  for (let i = 0; i < members.length; i += 1) {
    if (!members[i]) continue;
    if (rows[i]![best.feature]! <= best.threshold) {
      left[i] = 1;
      leftCount += 1;
    } else {
      right[i] = 1;
    }
  }
  return {
    feature: best.feature,
    threshold: best.threshold,
    left: fitTree(rows, targets, orders, left, leftCount, depth - 1, minLeaf),
    right: fitTree(rows, targets, orders, right, count - leftCount, depth - 1, minLeaf),
  };
}

function predictTree(node: Node | number, row: readonly number[]): number {
  let current = node;
  while (typeof current !== "number") {
    current = row[current.feature]! <= current.threshold ? current.left : current.right;
  }
  return current;
}

export interface BoostingOptions {
  rounds?: number;
  learningRate?: number;
  depth?: number;
  minLeaf?: number;
}

/** Gradient boosting com perda quadrática, começando do sazonal (atributo 0). */
export function fitBoosting(rows: number[][], targets: number[], options: BoostingOptions = {}) {
  const rounds = options.rounds ?? 60;
  const rate = options.learningRate ?? 0.1;
  const depth = options.depth ?? 2;
  const minLeaf = options.minLeaf ?? 5;
  const width = rows[0]?.length ?? 0;
  const orders = Array.from({ length: width }, (_, feature) =>
    Int32Array.from(rows.map((_, i) => i)).sort((a, b) => rows[a]![feature]! - rows[b]![feature]!),
  );
  const everyone = new Uint8Array(rows.length).fill(1);
  const trees: (Node | number)[] = [];
  const prediction = rows.map((row) => row[0]!);
  for (let round = 0; round < rounds; round += 1) {
    const residuals = targets.map((y, i) => y - prediction[i]!);
    const tree = fitTree(rows, residuals, orders, everyone, rows.length, depth, minLeaf);
    trees.push(tree);
    rows.forEach((row, i) => {
      prediction[i] = prediction[i]! + rate * predictTree(tree, row);
    });
  }
  return (row: number[]) =>
    Math.max(0, trees.reduce<number>((sum, tree) => sum + rate * predictTree(tree, row), row[0]!));
}

// --------------------------------------------------------------------------- //
// Escolha do método e previsão
// --------------------------------------------------------------------------- //

type Model = (row: number[]) => number;

function trainRows(series: Series, until: number, calendar: Calendar) {
  const rows: number[][] = [];
  const targets: number[] = [];
  for (let day = 14; day < until; day += 1) {
    const value = series[day];
    if (value === null || value === undefined) continue;
    const row = features(series, day, calendar);
    if (!row) continue;
    rows.push(row);
    targets.push(value);
  }
  return { rows, targets };
}

/** O boosting treinado só com o que havia antes de `origin`, ou `null` se pouco. */
function boostingBefore(series: Series, origin: number, calendar: Calendar): Model | null {
  const { rows, targets } = trainRows(series, origin, calendar);
  if (rows.length < MIN_TRAIN_ROWS) return null;
  return fitBoosting(rows, targets);
}

/**
 * Prevê `horizon` dias a partir de `origin` (a primeira previsão é para o dia
 * `origin`). Sem `model`, é o sazonal.
 */
function predictFrom(
  series: Series, origin: number, horizon: number, calendar: Calendar, model: Model | null,
): number[] {
  // O horizonte estende a série com "desconhecido": os atributos de um dia
  // previsto nunca olham para dentro do próprio horizonte (lag ≥ 7).
  const extended: (number | null)[] = [...series.slice(0, origin)];
  const out: number[] = [];
  for (let step = 0; step < horizon; step += 1) {
    const day = origin + step;
    extended[day] = null;
    if (usuallyClosed(extended, day, origin)) {
      out.push(0);
      continue;
    }
    const row = model ? features(extended, day, calendar) : null;
    out.push(model && row ? model(row) : baselineAt(extended, day, origin));
  }
  return out;
}

/** Soma dos erros absolutos sobre soma do real. `null` sem venda no período. */
export function wape(actual: readonly number[], predicted: readonly number[]): number | null {
  const sold = actual.reduce((a, b) => a + b, 0);
  if (sold <= 0) return null;
  return actual.reduce((sum, value, i) => sum + Math.abs(value - predicted[i]!), 0) / sold;
}

/**
 * A regra da escolha, isolada para ser testada nas bordas: o boosting precisa
 * errar pelo menos 5% menos que o sazonal. Empate, ou ganho pequeno, fica com o
 * sazonal — ele é o que o dono entende e confere de cabeça.
 */
export function chooseMethod(seasonal: number | null, boosting: number | null): "sazonal" | "boosting" {
  if (seasonal === null || boosting === null) return "sazonal";
  return boosting < seasonal * REQUIRED_GAIN ? "boosting" : "sazonal";
}

export function forecast(series: Series, calendar: Calendar, horizon = HORIZON): ForecastResult {
  if (series.every((v) => !v)) {
    return {
      method: "sem-historico",
      values: Array(horizon).fill(0),
      backtest: { sazonal: null, boosting: null, days: 0 },
      errorSd: 0,
    };
  }

  // Duas semanas de teste, cada uma prevista só com o que havia antes dela.
  const actual: number[] = [];
  const seasonal: number[] = [];
  const boosted: number[] = [];
  let boostingAvailable = true;
  for (const back of [2 * HORIZON, HORIZON]) {
    const origin = series.length - back;
    if (origin < 14) {
      boostingAvailable = false;
      continue;
    }
    const truth = series.slice(origin, origin + HORIZON);
    const plain = predictFrom(series, origin, HORIZON, calendar, null);
    const model = boostingBefore(series, origin, calendar);
    if (!model) boostingAvailable = false;
    const smart = model ? predictFrom(series, origin, HORIZON, calendar, model) : plain;
    truth.forEach((value, i) => {
      if (value === null) return; // dia fechado não conta a favor nem contra
      actual.push(value);
      seasonal.push(plain[i]!);
      boosted.push(smart[i]!);
    });
  }

  const seasonalError = wape(actual, seasonal);
  const boostingError = boostingAvailable ? wape(actual, boosted) : null;
  const useBoosting = chooseMethod(seasonalError, boostingError) === "boosting";

  const chosen = useBoosting ? boosted : seasonal;
  const errors = actual.map((value, i) => value - chosen[i]!);
  const errorSd = errors.length
    ? Math.sqrt(errors.reduce((a, e) => a + e * e, 0) / errors.length)
    : 0;

  const model = useBoosting ? boostingBefore(series, series.length, calendar) : null;
  return {
    method: model ? "boosting" : "sazonal",
    values: predictFrom(series, series.length, horizon, calendar, model),
    backtest: { sazonal: seasonalError, boosting: boostingError, days: actual.length },
    errorSd,
  };
}

/**
 * Quanto comprar para atravessar o horizonte sem faltar.
 *
 * Estoque de segurança = z × desvio do erro diário × √dias: cobre o erro da
 * própria previsão com ~90% de confiança (z = 1,28), sem inventar uma margem
 * fixa que ninguém sabe de onde saiu. Saldo desconhecido (nenhuma contagem ou
 * compra sincronizada) não vira sugestão: sugerir compra contra um saldo que
 * não existe faria comprar o que já está na prateleira.
 */
export function purchaseSuggestion(input: {
  forecast: readonly number[];
  errorSd: number;
  balance: number | null;
  z?: number;
}): { need: number; safety: number; buy: number | null; coverageDays: number | null } {
  const need = input.forecast.reduce((a, b) => a + b, 0);
  const safety = (input.z ?? 1.28) * input.errorSd * Math.sqrt(input.forecast.length);
  if (input.balance === null) return { need, safety, buy: null, coverageDays: null };
  const buy = Math.max(0, need + safety - input.balance);
  const daily = need / input.forecast.length;
  const coverageDays = daily > 0 ? Math.max(0, input.balance) / daily : null;
  return { need, safety, buy, coverageDays };
}
