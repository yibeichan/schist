import { describe, expect, it } from "@jest/globals";
import { snippetContent, SNIPPET_MAX_CODE_POINTS } from "../../src/protocol/snippet.js";

describe("SNIPPET_MAX_CODE_POINTS", () => {
  it("is 200 (spec default)", () => {
    expect(SNIPPET_MAX_CODE_POINTS).toBe(200);
  });
});

describe("snippetContent", () => {
  it("returns input unchanged if shorter than max", () => {
    expect(snippetContent("short content")).toBe("short content");
  });

  it("returns input unchanged if exactly at max (200 cp)", () => {
    const exactly200 = "a".repeat(200);
    expect(snippetContent(exactly200)).toBe(exactly200);
  });

  it("truncates to 200 code points + ellipsis when input exceeds max", () => {
    const input = "a".repeat(250);
    const out = snippetContent(input);
    expect([...out].length).toBe(201); // 200 + ellipsis
    expect(out).toBe("a".repeat(200) + "…");
  });

  it("counts code points (not UTF-16 units) for surrogate-pair-safe truncation", () => {
    // 250 emoji, each 2 UTF-16 units; str.length == 500, code points == 250
    const input = "\u{1F50D}".repeat(250); // U+1F50D = magnifying glass
    const out = snippetContent(input);
    expect([...out].length).toBe(201);
    // Must NOT split mid-surrogate-pair
    expect(out).toBe("\u{1F50D}".repeat(200) + "…");
  });

  it("handles CJK content correctly (1 cp == 1 BMP char)", () => {
    const input = "中".repeat(250); // U+4E2D (中)
    const out = snippetContent(input);
    expect([...out].length).toBe(201);
    expect(out).toBe("中".repeat(200) + "…");
  });

  it("accepts a custom maxCodePoints", () => {
    expect(snippetContent("abcdefghij", 5)).toBe("abcde…");
    expect(snippetContent("abc", 5)).toBe("abc");
  });

  it("handles empty string", () => {
    expect(snippetContent("")).toBe("");
  });

  it("never appends ellipsis when input fits exactly", () => {
    // Edge: input of exactly N code points must NOT get ellipsis
    const exactly = "x".repeat(SNIPPET_MAX_CODE_POINTS);
    const out = snippetContent(exactly);
    expect(out).toBe(exactly);
    expect(out.endsWith("…")).toBe(false);
  });
});
