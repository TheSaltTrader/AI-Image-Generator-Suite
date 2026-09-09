# The Lodestone kit

Drop this folder into a repository and you have the assessment running in about ten
minutes, reporting honestly that eleven of its twelve sweeps do not exist yet. That is the
right first result: the gaps become countable, and you close them one release at a time.

```
kit/
  README.md              this
  lodestone.py           the runner. Knows nothing about your language.
  lodestone.toml         what your sweeps are and how to run them. EDIT THIS.
  mutate.py              the mutation harness. Any test runner.
  mutations.json         one entry per guard, with 25 worked examples.
  journey_driver.md      how to write the sweep that walks and PHOTOGRAPHS the
                         running application. Read this before writing one.
  clicking_it.md         how to write the sweep that OPENS A WINDOW and clicks it.
                         Read this before writing that one.
  templates/
    censuses.md          how to write the four sweeps that need code of their own.
    drive_app.py.example a real journey driver, exported verbatim from the project
                         that wrote it. Not a sketch -- it runs.
    drive_window.py.example  and a real clicking one, exported the same way.
    screen_controls.py.example  the census: every control the screens declare, so
                         the clicking sweep can count every facet rather than
                         walking the journeys somebody thought of.
    clicking-ledger.json.example  the controls deliberately never clicked, each
                         with its reason. Three states and no fourth.
```

Needs Python 3.11 or later (for `tomllib`). Nothing else.

---

## Ten minutes to a first verdict

**1. Copy `kit/` into your repository.** It runs commands from `root` in
`lodestone.toml`, which defaults to the folder above it.

**2. Open `lodestone.toml` and delete every `command` line.** All twelve sweeps now report
`NOT BUILT`, each with its `why`.

```
python kit/lodestone.py
```

That report is your starting position. Nothing is passing and nothing is pretending to.

**3. Put back the one command you already have** — whatever runs your existing test
suite — under whichever sweep it honestly belongs to. For most projects that is a
partial `Reach` or `Guardrails`, not a full one.

**4. Build `Accounting` next.** It is the cheapest and it locks the ratchet: from that
moment, nothing new can be added to your product without somebody noticing it is
untested. See `templates/censuses.md`.

**5. Then Mutation**, on the ten tests you would most hate to be wrong about. Expect one
or two to keep passing when you break what they guard. Those are the discoveries.

---

## The one rule

> **A test must enumerate its own subjects from the artefact, not from a list somebody
> typed.**

A test naming the screens it checks stops covering the next screen added, and says
nothing. A test that *finds* every screen and requires each to be declared cannot stop
covering anything without failing the build.

That is the difference between a scenario and a census, and it is the whole method.
Everything in `templates/censuses.md` is that rule applied to a different surface.

---

## The four verdicts

```
PASS       the sweep exists and it passed
FAIL       the sweep exists and it found something
NOT RUN    the sweep exists and this machine could not host it
NOT BUILT  the sweep does not exist yet
```

**They are never averaged into a score.** A percentage invites rounding a missing sweep
up to "mostly fine". The report names what it could not check, every single run.

`NOT RUN` and `NOT BUILT` are different problems with different fixes — one needs
somebody to change the machine, the other needs somebody to write the check — and
**neither is a pass**. A probe that skips must always say which capability was missing
and what would provide it; a skip reported in silence reads exactly like a pass.

---

## Making a sweep report NOT RUN

Have the sweep print a phrase, and name it in `lodestone.toml`:

```toml
not_run_when = ["no trusted certificate", "not an administrator"]
```

Anything printing one of those is `NOT RUN`, with that line as the reason. Test runners
generally do not print *why* a test was skipped, so the practical trick is a small test
that always runs and prints the machine's capabilities for the runner to read back.

---

## The rules that make it more than a naming exercise

- **A guard that has never been seen to fail is not evidence.** Break it on purpose and
  require the test named after it to fail. Applied to tests written for this very
  method, it found two that would have passed while the defect they named was live.
- **Coverage ratchets.** Assert the floor with a test, so what has been assessed cannot
  quietly stop being.
- **Set every bar at what is true today**, not at what would be ideal.
- **Expect the censuses to break your own tests first.** They will find that one of your
  tests can silently change another's result. That suite was already lying; the census
  is what made it say so.

---

## What this does not do

- It does not prove the absence of defects. Nothing does.
- It checks *consistency*, not *correctness*. A screen that saves the wrong field —
  consistently, losslessly, and with every guard green — passes every sweep here.
- It does not replace using your product. It replaces having to test **every facet by
  hand every time**, which is a different and much larger job.
- A census is only as good as its enumeration. When you add a new *kind* of entry point,
  the census has to learn about it, and that is a judgement rather than a mechanism.
- A sweep is not a superset of the one below it. Some defects can only be caught by
  running the real thing; some can only be caught by reading the text, because no
  harness can reproduce the condition. Both kinds of guard are needed.

The honest claim is narrower than "bomb proof" and more useful: **after this runs, what
is untested is written down, by name, instead of being found by a person clicking on a
live system.**
