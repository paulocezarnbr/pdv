import { verify } from "@node-rs/argon2";
import { describe, expect, it } from "vitest";

import { hashPin, validatePin } from "../src/lib/auth/pin.ts";

describe("PIN operacional", () => {
  it("aceita PIN forte e gera Argon2id compatível", async () => {
    expect(validatePin("84627519")).toBe("84627519");
    const encoded = await hashPin("84627519");
    expect(encoded).toMatch(/^\$argon2id\$v=19\$m=65536,t=3,p=4\$/);
    await expect(verify(encoded, "84627519")).resolves.toBe(true);
    await expect(verify(encoded, "84627518")).resolves.toBe(false);
  });

  it.each(["123456", "654321", "111111", "121212", "12345", "1234567890123", "senha1"])(
    "recusa PIN fraco %s",
    (pin) => expect(() => validatePin(pin)).toThrow(),
  );
});
