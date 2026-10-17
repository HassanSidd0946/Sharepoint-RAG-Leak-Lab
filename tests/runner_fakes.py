"""A fake system under test for the runner tests, with injectable misbehaviour.

By default it serves exactly what the oracle's intent says, `lag_s` seconds late (so lag_s=0 is a
perfect system). On top of that a test can inject:
  lag_by_pair   a different lag for some (persona, canary) pairs
  flap          {pair: seconds}: for that long after the (lagged) change the answer alternates
                between old and new on every read, then settles on the new one
  never_revoke  pairs that keep being served with the OLD answer forever (a permanent leak)
  deny_until    {pair: time}: the pair is refused until then, whatever intent says (a baseline
                that has no access yet)
  leak_after    {pair: time}: from then on the pair is served although intent denies it
  error_every   every n-th read returns served=None (a transport error)
  error_pairs   {pair: time}: from then on that pair ALWAYS errors (a persistently failing path)
  raise_every   every n-th read RAISES (a broken adapter); the engine must survive it
  crash_after   after this many reads raise KeyboardInterrupt (simulates a crash of the process)
Time comes from the same FakeClock as the generator, so reads and mutations share one timeline.
"""

from collections.abc import Callable, Mapping, Sequence

from generator_fakes import FakeClock
from leaklab.oracle import IntentTimeline, Mutation
from leaklab.runner.model import Observation, Pair
from leaklab.spec import TenantSpec


class FakeSystem:
    def __init__(
        self,
        spec: TenantSpec,
        clock: FakeClock,
        applied: Callable[[], list[Mutation]],
        *,
        name: str = "fake",
        lag_s: float = 0.0,
        lag_by_pair: Mapping[Pair, float] | None = None,
        flap: Mapping[Pair, float] | None = None,
        never_revoke: Sequence[Pair] = (),
        deny_until: Mapping[Pair, float] | None = None,
        leak_after: Mapping[Pair, float] | None = None,
        error_every: int = 0,
        error_pairs: Mapping[Pair, float] | None = None,
        raise_every: int = 0,
        crash_after: int | None = None,
    ) -> None:
        self.name = name
        self._spec = spec
        self._clock = clock
        self._applied = applied
        self._lag = lag_s
        self._lag_by_pair = dict(lag_by_pair or {})
        self._flap = dict(flap or {})
        self._never_revoke = set(never_revoke)
        self._deny_until = dict(deny_until or {})
        self._leak_after = dict(leak_after or {})
        self._error_every = error_every
        self._error_pairs = dict(error_pairs or {})
        self._raise_every = raise_every
        self._crash_after = crash_after
        self.reads = 0
        self.calls: list[tuple[float, Pair]] = []
        self.prepared: list[tuple[list[str], list[str]]] = []
        self._pair_reads: dict[Pair, int] = {}
        self._timeline: tuple[int, IntentTimeline] | None = None

    def prepare(self, personas: Sequence[str], canaries: Sequence[str]) -> None:
        self.prepared.append((list(personas), list(canaries)))

    def close(self) -> None:
        pass

    def _intent(self) -> IntentTimeline:
        applied = self._applied()
        if self._timeline is None or self._timeline[0] != len(applied):
            self._timeline = (len(applied), IntentTimeline(self._spec, applied))
        return self._timeline[1]

    def _allowed(self, pair: Pair, at: float) -> bool:
        persona, canary = pair
        return canary in self._intent().expected_at(at).get(persona, ())

    def read(self, persona: str, canary: str) -> Observation:
        t = self._clock.time()
        pair = (persona, canary)
        self.reads += 1
        self.calls.append((t, pair))
        if self._crash_after is not None and self.reads > self._crash_after:
            raise KeyboardInterrupt("simulated crash")
        if self._raise_every and self.reads % self._raise_every == 0:
            raise RuntimeError("broken adapter")
        if self._error_every and self.reads % self._error_every == 0:
            return Observation(
                system=self.name, persona=persona, canary=canary, ts=t,
                served=None, error="transport:ConnectError",
            )  # fmt: skip
        if pair in self._error_pairs and t >= self._error_pairs[pair]:
            return Observation(
                system=self.name, persona=persona, canary=canary, ts=t,
                served=None, error="http:503",
            )  # fmt: skip
        served = self._served(pair, t)
        return Observation(
            system=self.name, persona=persona, canary=canary, ts=t, served=served,
            status=200 if served else 404,
        )  # fmt: skip

    def _served(self, pair: Pair, t: float) -> bool:
        timeline = self._intent()
        lag = self._lag_by_pair.get(pair, self._lag)
        n = self._pair_reads.get(pair, 0)
        self._pair_reads[pair] = n + 1
        if pair in self._deny_until and t < self._deny_until[pair]:
            return False
        if pair in self._never_revoke:
            return self._allowed(pair, float("-inf"))
        lagged = t - lag
        answer = self._allowed(pair, lagged)
        if pair in self._flap:
            flips = [
                c for c in timeline.change_times()
                if c <= lagged and self._allowed(pair, c) != self._allowed(pair, c - 1e-6)
            ]  # fmt: skip
            if flips and lagged - flips[-1] < self._flap[pair]:
                return answer if n % 2 else not answer
        if pair in self._leak_after and t >= self._leak_after[pair] and not self._allowed(pair, t):
            return True
        return answer


# -- a generated fake tenant to run scenarios against --------------------------------------------

from pathlib import Path  # noqa: E402

from generator_fakes import FakeTenant  # noqa: E402
from leaklab.generator import Env, GenSettings, MutationLog, generate  # noqa: E402
from leaklab.spec import load_spec  # noqa: E402

SPEC_PATH = Path(__file__).resolve().parents[1] / "tenant_spec.yaml"
SPEC = load_spec(SPEC_PATH)


def make_env(tenant: FakeTenant, clock: FakeClock) -> Env:
    return Env(
        spec=SPEC,
        graph=tenant.graph,
        sp=tenant.sp,
        settings=GenSettings(
            password="pw",
            usage_location="US",
            site_host="t.sharepoint.com",
            site_path="/sites/T",
            membership_settle_s=20.0,
        ),
        log=MutationLog(run_id="test", created_at=0.0),
        sleep=clock.sleep,
        clock=clock.time,
    )


class TickingClock(FakeClock):
    """A fake clock that also moves a millisecond on every reading, like a real one: a read taken
    just before a mutation never shares a timestamp with the mutation's confirmed_at."""

    def time(self) -> float:
        now = self.now
        self.now += 0.001
        return now


def make_world() -> tuple[FakeTenant, Env, FakeClock]:
    """A generated fake tenant, its Env and the shared fake clock."""
    tenant, clock = FakeTenant(), TickingClock()
    env = make_env(tenant, clock)
    generate(env)
    return tenant, env, clock
