"use client";

/**
 * Avisos do painel — SweetAlert2, num lugar só.
 *
 * Três formas, e a escolha entre elas não é estética:
 *
 * * `toast` — deu certo. Some sozinho: parar o dono para confirmar que o
 *   cadastro salvou é gastar a atenção dele num fato que ele já vai ver.
 * * `fail` — deu errado. Fica até alguém fechar: um erro que some sozinho é um
 *   erro que ninguém leu, e aqui ele costuma ser "a nota não vai sair".
 * * `confirm` — não tem volta, ou tem consequência fiscal. Ligar a emissão e
 *   mudar de ambiente pedem um segundo gesto deliberado.
 *
 * O import é dinâmico: o SweetAlert2 só carrega quando um aviso acontece, e o
 * painel abre sem pagar por ele.
 */

const theme = {
  background: "#161616",
  color: "#f4f4f4",
  confirmButtonColor: "#0f62fe",
  cancelButtonColor: "#393939",
};

async function swal() {
  return (await import("sweetalert2")).default;
}

export async function toast(title: string, icon: "success" | "info" = "success"): Promise<void> {
  const Swal = await swal();
  await Swal.fire({
    ...theme,
    toast: true,
    position: "top-end",
    icon,
    title,
    showConfirmButton: false,
    timer: 2600,
    timerProgressBar: true,
  });
}

export async function fail(message: string, title = "Não foi possível concluir"): Promise<void> {
  const Swal = await swal();
  await Swal.fire({ ...theme, icon: "error", title, text: message, confirmButtonText: "Entendi" });
}

export async function confirm(options: {
  title: string;
  text: string;
  ok: string;
  danger?: boolean;
}): Promise<boolean> {
  const Swal = await swal();
  const result = await Swal.fire({
    ...theme,
    icon: options.danger ? "warning" : "question",
    title: options.title,
    text: options.text,
    showCancelButton: true,
    confirmButtonText: options.ok,
    cancelButtonText: "Voltar",
    confirmButtonColor: options.danger ? "#da1e28" : theme.confirmButtonColor,
    reverseButtons: true,
    focusCancel: true,
  });
  return result.isConfirmed;
}
