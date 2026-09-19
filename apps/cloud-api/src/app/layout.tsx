import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

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
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          background: "#0e1116",
          color: "#e7eaef",
          fontFamily:
            '"Segoe UI Variable Text","Segoe UI",system-ui,"Noto Sans",sans-serif',
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {children}
      </body>
    </html>
  );
}
