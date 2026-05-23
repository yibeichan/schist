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
        /SCHIST_ALLOWED_AGENTS is defined but parses to an empty list/
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

    it("throws CONFIG_ERROR on defined-but-empty SCHIST_ALLOWED_AGENTS, even if SCHIST_AGENT_ID is set", () => {
      // Operators may intuitively set SCHIST_ALLOWED_AGENTS="" to "disable" the
      // allowlist, expecting it to fall back to single-agent mode. That's a
      // footgun — fail loudly instead so the operator unsets the variable
      // when they want legacy semantics.
      process.env.SCHIST_ALLOWED_AGENTS = "";
      process.env.SCHIST_AGENT_ID = "eleven";
      expectThrow(
        () => validateOwner("eleven"),
        "CONFIG_ERROR",
        /SCHIST_ALLOWED_AGENTS is defined but parses to an empty list/
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

  describe("returns canonical (trimmed) owner — #131 trim symmetry", () => {
    // Pre-#131 the allowlist parser trimmed entries but the includes-check
    // ran against the raw owner. So `SCHIST_ALLOWED_AGENTS="atwood,octopus"`
    // with caller-sent `owner="atwood "` would reject with VALIDATION_ERROR
    // even though the operator intended atwood==atwood. Both halves are
    // now canonicalized.

    it("accepts owner with leading/trailing whitespace in allowlist mode", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      expect(() => validateOwner("atwood ")).not.toThrow();
      expect(() => validateOwner(" atwood")).not.toThrow();
      expect(() => validateOwner("\tatwood\n")).not.toThrow();
    });

    it("accepts owner with leading/trailing whitespace in legacy mode", () => {
      process.env.SCHIST_AGENT_ID = "sansan";
      expect(() => validateOwner(" sansan")).not.toThrow();
      expect(() => validateOwner("sansan\n")).not.toThrow();
    });

    it("returns the canonical trimmed string", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      expect(validateOwner("atwood ")).toBe("atwood");
      expect(validateOwner(" octopus\t")).toBe("octopus");
    });

    it("returns the same string when no whitespace is present", () => {
      process.env.SCHIST_AGENT_ID = "sansan";
      expect(validateOwner("sansan")).toBe("sansan");
    });

    it("still rejects whitespace-only owner (canonical form is empty)", () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      expectThrow(
        () => validateOwner("   "),
        "VALIDATION_ERROR",
        /not in SCHIST_ALLOWED_AGENTS/
      );
    });
  });
});
