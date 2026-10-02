import { describe, expect, it } from "vitest";

import { applyRemoteState } from "./useRoom.js";

const at = (rev) => ({ rev, players: { Bob: 0 } });

describe("applyRemoteState", () => {
  it("takes the first state it sees", () => {
    expect(applyRemoteState(null, at(3))).toEqual(at(3));
  });

  it("applies a newer state", () => {
    expect(applyRemoteState(at(5), at(6))).toEqual(at(6));
  });

  it("accepts an equal revision (the same action delivered twice)", () => {
    expect(applyRemoteState(at(5), at(5))).toEqual(at(5));
  });

  it("drops a straggler older than the screen", () => {
    expect(applyRemoteState(at(7), at(6))).toEqual(at(7));
  });

  it("applies states that carry no revision (back-compat)", () => {
    expect(applyRemoteState(at(7), { players: {} })).toEqual({ players: {} });
  });
});
