/**
 * A pagina raiz.
 *
 * Nao e o painel — e o que responde "o servico esta no ar?" para um humano que
 * abriu a URL no navegador. O painel administrativo e um app a parte; esta
 * pagina existe porque uma raiz que devolve 404 faz qualquer um concluir que o
 * deploy falhou, inclusive quem esta diagnosticando.
 */

export const dynamic = "force-dynamic";

export default function Home() {
  return (
    <main style={{ padding: "48px 24px", maxWidth: 640, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, fontWeight: 600, marginBottom: 8 }}>
        ERP Food Service — retaguarda
      </h1>
      <p style={{ color: "#98a1b0", lineHeight: 1.6, fontSize: 15 }}>
        O servico esta no ar. Esta e a API que recebe a sincronizacao dos
        terminais; ela nao tem interface publica.
      </p>
      <p style={{ color: "#69727f", fontSize: 13, marginTop: 24 }}>
        Estado do servico:{" "}
        <a href="/api/health" style={{ color: "#4f8fc0" }}>
          /api/health
        </a>
      </p>
    </main>
  );
}
