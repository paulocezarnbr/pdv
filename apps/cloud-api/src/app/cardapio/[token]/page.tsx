/**
 * `/cardapio/<token>` — o cardápio que o cliente abre pelo QR da mesa.
 *
 * Público, sem login, e por isso 404 igual para token inexistente, revogado ou
 * de restaurante suspenso: distinguir os casos contaria a quem tenta adivinhar
 * quais links já existiram. Ver `lib/menu/load.ts` para a ordem das consultas.
 */

import type { Metadata, Viewport } from "next";
import { notFound } from "next/navigation";

import { loadMenu } from "@/lib/menu/load";

import { MenuView } from "./menu-view";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Cardápio",
  // Cardápio de mesa não é página de busca: indexado, o QR de uma loja
  // apareceria no Google e seria aberto de fora do restaurante.
  robots: { index: false, follow: false },
};

// A barra do navegador acompanha o cardápio claro, e não o painel escuro.
export const viewport: Viewport = { themeColor: "#faf8f5" };

export default async function MenuPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const menu = await loadMenu(token);
  if (!menu) notFound();
  return <MenuView menu={menu} />;
}
