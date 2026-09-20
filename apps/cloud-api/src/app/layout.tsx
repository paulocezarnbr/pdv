import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "@carbon/styles/css/styles.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "ERP Food Service",
  description: "Retaguarda do ERP de food service.",
  // Nenhum robo indexa a retaguarda de um cliente.
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0e1116",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="pt-BR">
      <body className="cds--g100">{children}</body>
    </html>
  );
}
