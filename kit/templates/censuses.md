# Writing the four censuses

Concrete patterns for the sweeps that need code of their own. Each is given as the
shape first, then what it looked like in a real .NET application, then the traps — every
one of which cost real time.

The language does not matter much. What matters is the property they all share:
**nothing here names a subject.** Every list is produced by asking the artefact what it
contains. That is what stops a census silently ceasing to cover the next thing added.

---

## The harness that makes three of them affordable

Build this first. Two of the four censuses need the same thing — *something that can
construct any screen for a name it was given* — and building it once turns two large
jobs into two small ones.

```
class Screens:
    ALL = every page/controller/view-model type in the app, found by reflection

    def __init__():
        services = new container
        add_the_applications_own_registrations(services)   # <- the important line

        # then replace ONLY what reaches outside the process
        services.replace(SecretStore,  in memory)
        services.replace(StateStore,   a temporary file)
        services.replace(Database,     a double)
        services.replace(HttpClient,   a double)
        services.replace(FilePicker,   a double)
        services.replace(Scheduler,    a double)

    def build(screen):  return container.create(screen)
    def completed():    walk the app's own state to the end, so guards let you in
```

**Use the application's own registration.** Not a hand-written list of dependencies. A
service added to the product then arrives in the census by itself; a hand-written list
goes stale the first week.

### Traps

- **Give each built screen a request context.** A handler that sets a status code
  touches the response, and a view-model built without one throws a null reference
  there. A census that skipped it would report an unhandled exception in the product
  and be reporting its own harness.
- **Walk the app's state to the end before driving anything.** Screens guard themselves
  and redirect when the steps before them are not done. On a blank state every handler
  returns a redirect without touching anything, and the census cheerfully reports that
  no button anywhere takes anything away. This happened; it is not obvious from the
  results, because they all look green.
- **A double that throws makes every screen behind it look broken.** Answer, do not
  throw. Install and remove buttons have to be drivable or nothing checks them.

---

## 1. Accounting — the ledger

```
LEDGER = {
    "Screen.Handler": Assessed(covered_by = "the test that covers it"),
    "Other.Handler":  Assessed(covered_by = None, note = "NotYetAssessed: what is at risk"),
    ...
}

test every_entry_point_is_in_the_ledger:
    found = reflect over the app for every entry point
    assert every one of `found` appears in LEDGER

test the_ledger_names_nothing_that_no_longer_exists:
    assert every LEDGER key is in `found`      # a stale entry reads as coverage

test the_assessed_share_does_not_go_backwards:
    assert count(covered) >= N                 # N raised by hand, deliberately
```

`NotYetAssessed` is a legitimate entry. It is debt, written down where it can be
counted. What is forbidden is an entry point in **neither** state.

**Set N at what is true today.** A gate that fails on the day it lands gets switched off
the day after. In the real case it started at 6 of 42 and reached 25 of 42 over two
releases; writing the 19 down as a list is what made closing them a job somebody could
pick up.

---

## 2. No-loss — what a press destroys

```
MAY_CHANGE = { "Screen.Handler": ["FieldA", "FieldB"], ... }

test a_press_takes_away_nothing_it_was_not_declared_to_change(handler):
    shown  = build the screen the way a GET builds it
    posted = A FRESH screen, filled with what a form would send back
    before = read every bound field of `shown`

    press(posted, handler)

    for field not in MAY_CHANGE[handler]:
        assert before[field] == read(posted, field)
```

**Model the whole round trip.** Screen shows state → form sends a subset → a *fresh*
object receives it. Pressing a handler on the same object that rendered the page keeps
whatever the render left lying around, and passes while the real screen comes back
empty. That mistake was made building this and was caught only by the mutation sweep.

**Measure the declarations, do not guess them.** Run each handler, read what moved,
judge each one: is this the button doing its job, or is it destroying work? Doing that
across every screen is what produced a second rule nobody had thought of — three screens
carried a password field and only one cleared it — which became:

```
test no_button_hands_a_secret_back:
    for field whose name contains "password" or "secret":
        assert it is empty after every handler
```

That found three of four buttons on one screen keeping a typed password. Not
exploitable, because the input type suppressed it; one markup change from being so.

---

## 3. Guardrails — hostile input, enumerated

```
HOSTILE = [
    "", "   ",
    "'; DROP TABLE x--",              # has cost this project a real table
    "..\\..\\..\\windows\\system32\\config\\SAM",
    "\\\\attacker\\share\\payload",
    "<script>alert(1)</script>",
    "{0}{1} %s %n",
    "before\\0after",
    "one\\r\\ntwo",
    "abc\\u202Egfe.exe",              # right-to-left override
    "A" * 100_000,
    "ok\\ud800end",                   # lone surrogate
]

test no_screen_throws_when_every_field_carries_it(value):
    for screen, handler in every entry point:
        page = build(screen)
        fill EVERY bound field with `value`
        assert press(page, handler) answers      # refusing is fine; throwing is not
```

**The requirement is not success.** Most of these should be refused. What may never
happen is an unhandled exception — untrusted input is checked at the boundary and
answered, not turned into a stack trace.

**It may find nothing on the first run.** That is a real result, not a wasted afternoon:
before it, there was no way to know.

---

## 4. Endurance — what is still held

```
test a_long_run_does_not_go_on_taking_handles:
    warm up 20 times                     # first pass through any path opens things
    before = handle_count()
    run 200 times
    assert handle_count() - before < 100

test a_source_file_is_closed_when_the_read_is_finished:
    read the file 20 times
    DELETE IT                            # a held handle makes this throw
```

**Deleting is the test** for open files. Nothing else about a reader tells you it left a
handle open — while on the machine it turns into "the file is in use by another process"
at two in the morning, when tonight's export tries to overwrite it.

**Thresholds are ratchets, not budgets.** They catch a leak *per iteration*. Real
numbers from 200 runs: +5 handles, +448 KB. A leak of one handle per run would show as
200, which is nowhere near normal noise. A product already using twice the memory it
should will pass every one of them — that is not what this sweep is for.

### The trap that costs an afternoon

Writing this broke seven unrelated tests before it found anything about the product. Two
hundred runs wrote two hundred files into a folder every runner shares; a file named
from the moment collided with what a later test was about to write; that test counted one
file where it wrote two. Seven failures, all about something else.

**A sweep about not leaving things behind must not leave things behind.** Record what was
there before, remove what you added, in one shared implementation. And a suite where one
test can silently change another's result was already lying — the census is what made it
say so.

---

## 5. Handover — the far side, for real

Not a census; a set of probes. One per thing you hand to a system that is not your code.

```
probe:
    hand it over for real                          # register the task, install the
                                                   # service, run the file, build the menu
    read the effect back FROM THAT SYSTEM          # query the task, ask the service
                                                   # manager, read what was printed
    clean up in a finally, however it ended
```

### Rules, each learned the hard way

- **Read the effect from the far side.** Your own exit code is not evidence. Registering
  a task and checking that your command returned 0 proves nothing; querying the task
  back out and asserting its logon type proves the job will run at 2am with nobody
  logged on.
- **Use names the product never uses.** Probes here register into their own folder,
  install their own service name, and store a key under a probe identifier that no real
  installation uses. The single-account form of that key is *the engineer's real
  credential*, and a probe writing there would have destroyed it.
- **Clean up however it ends.** A probe that leaves a service installed is a probe
  nobody runs twice.
- **Never weaken the product to make a probe easier.** The endpoint here must be https;
  the stub serves real TLS rather than the rule being relaxed. Where that costs a probe
  on some machines, the cost is paid in NOT RUN.
- **Drain both output streams at once, and answer stdin.** Reading one to the end and
  then the other deadlocks when the child fills the one you are not reading — measured,
  a process stuck for ten minutes with the whole run waiting behind it. And some console
  tools answer a *closed* stdin differently from an *empty line*: one gave exit 1 and a
  password prompt, the other gave success.

### Two limits worth knowing before you start

- **A probe cannot be mutation-verified against source**, because it runs the *packaged*
  artefact. Breaking the source does not reach it. Add a source-level guard alongside so
  a regression fails before anything is packaged.
- **Some defects cannot be caught by running at all.** A `pause` in a batch file blocks
  only on a real console; every harness redirects stdin, so under a probe it prints and
  carries on. The guard for that one has to read the generated text. **A sweep is not a
  superset of the one below it** — the text guard covers what cannot be run, the run
  covers what cannot be read.
