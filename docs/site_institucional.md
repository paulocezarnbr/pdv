# site_institucional.md — Dolce Affetto Pool Bar

> **Contrato de contexto**, como o [`plan.md`](./plan.md): o que o site será e em
> qual ordem. Mudou o escopo? Atualize aqui antes de escrever código.

---

## 1. Para que o site existe

O sistema (`app.dolceaffettopoolbar.com.br`) é para quem trabalha na casa. O
site (`dolceaffettopoolbar.com.br`) é para quem **ainda não entrou nela**, e
tem três trabalhos, nesta ordem:

1. **Fazer a pessoa vir.** Onde fica, que horas abre, como é o lugar — e um
   botão que leva ao WhatsApp ou ao mapa em um toque.
2. **Mostrar o cardápio de verdade.** O mesmo cardápio do caixa, com o preço
   do caixa. Cardápio em PDF desatualizado é a reclamação clássica ("no site
   estava R$ 18") e é briga no balcão.
3. **Ser encontrado.** Quem busca "pool bar" ou "confeitaria" na cidade
   precisa achar a casa no Google e no mapa, com horário certo.

O que o site **não** é: loja virtual, área de cliente, blog. Cada um desses é
um produto com manutenção própria; entram se e quando houver motivo (seção 7).

---

## 2. Páginas

| Página | Conteúdo | Por quê |
|---|---|---|
| **Início** | Foto forte do lugar, uma frase do que a casa é, horário de hoje ("aberto até 23h"), botões *Como chegar* e *WhatsApp* | 70% das visitas vêm do celular, na rua, decidindo agora. O que decide tem de estar acima da dobra. |
| **Cardápio** | Categorias e itens com foto, descrição e preço, lidos do catálogo do ERP | Uma fonte da verdade: mudou o preço no caixa, mudou no site. |
| **O espaço** | Galeria: piscina, bar, salão, eventos | É o diferencial de um *pool bar*; foto vende mais que texto. |
| **Eventos e reservas** | Aniversários, day use, eventos fechados; formulário curto que vira mensagem de WhatsApp | Reserva de evento é conversa, não checkout. |
| **Contato** | Endereço, mapa, telefone, WhatsApp, Instagram, horário completo | Também é o que o Google lê para o card da empresa. |
| **Privacidade** | Política de privacidade e cookies (LGPD) | Obrigatório assim que houver formulário ou análise de visitas. |

---

## 3. Decisões técnicas

- **Next.js, gerado estático**, no mesmo monorepo (`apps/site`). Mesma
  linguagem e mesmo pipeline do ERP; HTML pronto é rápido em 4G e não tem
  servidor para cair.
- **Cardápio vindo do ERP por uma rota pública e só de leitura**
  (`GET /api/public/menu/<loja>`): nome, descrição, preço, foto e
  disponibilidade — nada de custo, estoque ou ficha técnica. Com cache de
  5 minutos e o site revalidando a cada 10: o cardápio pode demorar minutos
  para refletir o caixa, nunca dias.
  *Cuidado que não se negocia:* a rota é multi-tenant, então a loja é
  identificada por um `slug` público e a consulta passa pelo mesmo
  `withTenant` do resto da API. Um slug errado devolve 404, nunca o cardápio de
  outro cliente.
- **Hospedagem no Coolify**, no mesmo servidor, como aplicação separada. Queda
  do site não derruba o caixa, e deploy do caixa não tira o site do ar.
- **Imagens otimizadas no build** (WebP/AVIF, tamanhos responsivos). Foto de
  celular de 6 MB é o que deixa site de restaurante lento.
- **Sem banco próprio, sem painel próprio.** O conteúdo que muda (cardápio,
  horário) vem do ERP; o que quase não muda (textos, fotos) vive no
  repositório. Um CMS agora seria um terceiro sistema para manter.

---

## 4. Conteúdo que só a casa tem

Nada disto se inventa, e o site não vai ao ar com texto de exemplo:

- [ ] Logo em vetor (SVG/PDF) e as cores da marca.
- [ ] 15–30 fotos em boa resolução: fachada, piscina, bar, pratos e drinks.
- [ ] Endereço exato, telefone, WhatsApp e Instagram.
- [ ] Horário por dia da semana, e os feriados em que muda.
- [ ] Um parágrafo sobre a casa (história, proposta).
- [ ] Regras de eventos/day use que o site pode publicar.
- [ ] Cardápio: **descrição e foto** de cada item no cadastro de produtos do
      ERP (hoje o produto tem nome e preço).

---

## 5. Fases

### Fase S1 — Fundação
- [ ] `apps/site` (Next.js estático) no monorepo, na CI e no Coolify.
- [ ] DNS: `dolceaffettopoolbar.com.br` e `www` (redireciona para o domínio
      raiz) apontando para o servidor, com HTTPS automático.
- [ ] Layout mobile-first, tema da marca, páginas Início, Contato e
      Privacidade com o conteúdo real.
- **Aceite:** o site abre no celular em 4G em menos de 2 s (Lighthouse
  Performance ≥ 90, Acessibilidade ≥ 95) e os botões de WhatsApp e mapa
  funcionam em Android e iPhone.

### Fase S2 — Cardápio vivo
- [ ] Campos de descrição, foto e "aparece no site" no cadastro de produtos
      (ERP) e sincronização deles.
- [ ] Rota pública `GET /api/public/menu/<loja>`, só leitura, com cache e
      teste de isolamento entre tenants.
- [ ] Página de cardápio por categoria, com busca e marcação de indisponível.
- **Aceite:** mudar o preço de um item no ERP muda o site em até 10 minutos, e
  um teste automatizado prova que o slug de uma loja nunca devolve item de
  outra.

### Fase S3 — Ser encontrado
- [ ] Dados estruturados `Restaurant`/`BarOrPub` (endereço, horário, cardápio,
      faixa de preço), `sitemap.xml`, `robots.txt`, Open Graph para o link no
      WhatsApp e Instagram sair com foto.
- [ ] Perfil da Empresa no Google com o mesmo endereço, horário e link
      (feito pela casa; o site só precisa bater com ele).
- [ ] Análise de visitas **sem cookie de rastreamento** (Plausible ou Umami,
      hospedado no mesmo Coolify) — dispensa o banner de consentimento.
- **Aceite:** o Rich Results Test do Google valida a página inicial, e o link
  compartilhado no WhatsApp mostra título, foto e descrição.

### Fase S4 — Eventos e galeria
- [ ] Galeria do espaço e página de eventos com formulário que abre o
      WhatsApp já preenchido (data, pessoas, tipo de evento).
- **Aceite:** um pedido de reserva feito no celular chega ao WhatsApp da casa
  com todos os campos, sem nenhum dado ficar guardado no site.

---

## 6. Riscos

| Risco | Mitigação |
|---|---|
| Preço do site divergir do caixa | Cardápio lido do ERP (Fase S2); nada de cardápio digitado à parte. |
| Rota pública vazar dado de outro tenant | Slug público + `withTenant` + teste de isolamento na CI. |
| Site lento por foto pesada | Otimização no build e orçamento de performance na CI. |
| Conteúdo de exemplo ir ao ar | Checklist da seção 4 bloqueia a Fase S1. |
| LGPD | Sem cookie de rastreamento; formulário não armazena dado — vai direto ao WhatsApp. |

---

## 7. Fora do escopo agora (e quando entraria)

- **Pedido online / delivery:** quando a casa decidir operar delivery próprio
  — é o módulo M11 (cardápio QR) do `plan.md`, não uma página do site.
- **Reserva com calendário e pagamento de sinal:** se a demanda de eventos
  justificar; antes disso, WhatsApp resolve com menos atrito.
- **Blog:** só com alguém responsável por publicar; blog abandonado passa
  impressão de casa fechada.
