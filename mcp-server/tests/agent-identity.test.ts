import { validateOwner } from "../src/agent-identity.js";

// Helper: capture both message and error code in one assertion
function expectThrow(fn: () => void, errorCode: string, msgRegex: RegExp): void {
  try {
    fn();
    fail("Expected validateOwner to throw");
  } catch (e: unknown) {
    expect(e).toBeInstanceOf(Error);
    expect((e as Error).message).toMatch(msgRegex);
    expect((e as Record<string, unknown>).error).toBe(errorCode);
  }
}

describe("validateOwner", () => {
  // Always start each test from a known-clean env
  beforeEach(() => {
    delete process.env.SCHIST_AGENT_ID;
    delete process.env.SCHIST_ALLOWED_AGENTS;
  });

  afterEach(() => {
    delete process.env.SCHIST_AGENT_ID;
    delete process.env.SCHIST_ALLOWED_AGENTS;
  });

  describe("allowlist mode (SCHIST_ALLOWED_AGENTS set)", () => {
    it("accepts owner present in the allowlist", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus,sansan";
      expect(() => validateOwner("octopus")).not.toThrow();
      expect(() => validateOwner("eleven")).not.toThrow();
      expect(() => validateOwner("sansan")).not.toThrow();
    });

    it("rejects owner absent from the allowlist", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "eleven,sansan";
      expectThrow(
        () => validateOwner("octopus"),
        "VALIDATION_ERROR",
        /Owner 'octopus' not in SCHIST_ALLOWED_AGENTS/
      );
    });

    it("trims whitespace around entries", () => {
      process.env.SCHIST_ALLOWED_AGENTS = " eleven , octopus ,  sansan ";
      expect(() => validateOwner("octopus")).not.toThrow();
      expect(() => validateOwner("eleven")).not.toThrow();
    });

    it("filters out empty entries (extra commas)", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "eleven,,octopus,";
      expect(() => validateOwner("octopus")).not.toThrow();
      expectThrow(
        () => validateOwner(""),
        "VALIDATION_ERROR",
        /Owner ''/
      );
    });

    it("throws CONFIG_ERROR when allowlist parses to zero entries", () => {
      process.env.SCHIST_ALLOWED_AGENTS = " , , ";
      expectThrow(
        () => validateOwner("anyone"),
        "CONFIG_ERROR",
        /SCHIST_ALLOWED_AGENTS is set but parses to an empty list/
      );
    });

    it("takes precedence over SCHIST_AGENT_ID when both are set", () => {
      process.env.SCHIST_AGENT_ID = "eleven";
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,sansan";
      // Allowlist wins: 'octopus' allowed even though SCHIST_AGENT_ID=eleven
      expect(() => validateOwner("octopus")).not.toThrow();
      // 'eleven' rejected even though SCHIST_AGENT_ID=eleven — allowlist is authoritative
      expectThrow(
        () => validateOwner("eleven"),
        "VALIDATION_ERROR",
        /not in SCHIST_ALLOWED_AGENTS/
      );
    });

    it("ignores empty-string SCHIST_ALLOWED_AGENTS (falls through to legacy path)", () => {
      // Empty string is falsy — treated as "unset" so legacy SCHIST_AGENT_ID path applies
      process.env.SCHIST_ALLOWED_AGENTS = "";
      process.env.SCHIST_AGENT_ID = "eleven";
      expect(() => validateOwner("eleven")).not.toThrow();
      expectThrow(
        () => validateOwner("octopus"),
        "VALIDATION_ERROR",
        /does not match SCHIST_AGENT_ID/
      );
    });
  });

  describe("legacy mode (only SCHIST_AGENT_ID set)", () => {
    it("accepts owner matching SCHIST_AGENT_ID exactly", () => {
      process.env.SCHIST_AGENT_ID = "sansan";
      expect(() => validateOwner("sansan")).not.toThrow();
    });

    it("rejects owner not matching SCHIST_AGENT_ID", () => {
      process.env.SCHIST_AGENT_ID = "sansan";
      expectThrow(
        () => validateOwner("ninjia"),
        "VALIDATION_ERROR",
        /Owner 'ninjia' does not match SCHIST_AGENT_ID 'sansan'/
      );
    });
  });

  describe("unconfigured (neither env var set)", () => {
    it("throws CONFIG_ERROR — writes require identity to be configured", () => {
      expectThrow(
        () => validateOwner("anyone"),
        "CONFIG_ERROR",
        /SCHIST_AGENT_ID or SCHIST_ALLOWED_AGENTS/
      );
    });
  });
});
